"""
Autonomous scenario runner for the live simulation.

Each *scenario* is a named sequence of timed steps.  The runner tracks wall-
clock elapsed time and auto-advances through steps without any manual clicking.
Between step boundaries it linearly interpolates the ``sensor_pressure`` and
weather multipliers so the transition to the next risk level looks smooth
rather than jumping.

Five demo scenarios are provided:

  1. storm_surge          — Storm approaches, peaks, assets degrade, maintenance
                            triggers, recovery
  2. heat_load_stress     — Sustained high-temperature / high-load event on an
                            already-stressed fleet
  3. repeat_fault_storm   — A repeat-fault asset (TX-006) gets hit by a second
                            storm; rapid escalation
  4. equipment_fault      — Sensor fault cascades under weather pressure
  5. grid_recovery        — Post-storm recovery showing risk normalisation

DESIGN CONTRACT
---------------
- The runner is purely time-tracking and interpolation.  It never calls the
  risk engine, the LLM, or the scheduler — those stay in ``GridState``.
- ``ScenarioRunner.tick()`` returns the current interpolated ``StepState``
  which ``GridState`` feeds into ``_apply_simulation()``.
- The runner is deterministic when constructed with ``_now_fn`` injected
  (used in tests); in production it uses ``time.monotonic``.
- Pausing freezes elapsed time; resuming continues from where it left off.
- The runner does NOT auto-stop at the end of a scenario; it stays on the
  final step and sets ``completed=True`` so the UI can prompt the operator.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from simulation.weather_sim import DEMO_PHASES, PhaseInfo, SimulationPhase


# ---------------------------------------------------------------------------
# Step definition — one node in a scenario's timeline
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepDef:
    """
    A single step in a scenario script.

    Parameters
    ----------
    phase:
        Which ``SimulationPhase`` this step maps to (drives label + description
        in the UI banner and maintenance plan annotation).
    sensor_pressure:
        Override the phase's default ``sensor_pressure`` for fine-grained
        scenario control.  ``None`` means use the phase's default value.
    duration_s:
        How many real-world seconds this step lasts before auto-advancing.
        In production the demo uses ~20-second steps so a full run takes
        ~2.5 minutes.  Tests inject a tiny duration (0.01 s).
    label_override:
        Optional replacement for the phase label shown in the UI banner.
    desc_override:
        Optional replacement for the phase description shown in the UI banner.
    """
    phase: SimulationPhase
    duration_s: float = 20.0
    sensor_pressure: Optional[float] = None   # None → use phase default
    label_override: Optional[str] = None
    desc_override: Optional[str] = None


# Fast lookup
_PHASE_MAP = {p.phase: p for p in DEMO_PHASES}


# ---------------------------------------------------------------------------
# Live step state returned by tick()
# ---------------------------------------------------------------------------

@dataclass
class StepState:
    """
    The interpolated simulation state at a particular instant in time.

    All values are ready to be passed directly into
    ``GridState._apply_simulation_with_pressure()``.
    """
    scenario_id: str
    scenario_label: str
    step_index: int              # 0-based index into scenario's step list
    total_steps: int
    phase: SimulationPhase
    phase_info: PhaseInfo        # PhaseInfo with overridden label/desc if set
    sensor_pressure: float       # interpolated 0–1
    elapsed_s: float             # total elapsed seconds since run() was called
    step_elapsed_s: float        # seconds into the current step
    step_duration_s: float       # total duration of the current step
    step_progress: float         # 0.0–1.0 fraction through current step
    running: bool
    paused: bool
    completed: bool              # True when all steps have finished


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

def _phase(p: SimulationPhase) -> PhaseInfo:
    return _PHASE_MAP[p]


# Five named demo scenarios.  Each is a list[StepDef] plus a label/description.

_SCENARIO_STORM_SURGE = [
    StepDef(SimulationPhase.CALM,                duration_s=18),
    StepDef(SimulationPhase.ADVISORY,            duration_s=20,
            label_override="Weather advisory — storm building",
            desc_override="Wind speed and precipitation rising. Monitor ageing assets."),
    StepDef(SimulationPhase.STORM_APPROACHING,   duration_s=22,
            label_override="Storm approaching — sensors elevating",
            desc_override="TX-007 and TX-008 sensor readings climbing. Vulnerability model active."),
    StepDef(SimulationPhase.PEAK_STORM,          duration_s=20,
            label_override="Peak storm — asset degradation active",
            desc_override="Multiple assets crossed risk thresholds. Scheduler re-ranking now."),
    StepDef(SimulationPhase.DEGRADATION_PEAK,    duration_s=18,
            label_override="Peak degradation — maintenance triggered",
            desc_override="Scheduler has reordered inspection plan. Awaiting operator approval."),
    StepDef(SimulationPhase.MAINTENANCE_TRIGGERED, duration_s=20,
            label_override="Crews dispatched — risk stabilising",
            desc_override="Approved maintenance plan active. Field crews en route to high-risk assets."),
    StepDef(SimulationPhase.RECOVERY,            duration_s=22,
            label_override="Recovery — risk normalising",
            desc_override="Storm passing. Preventive actions have reduced critical asset count."),
]

_SCENARIO_HEAT_LOAD = [
    StepDef(SimulationPhase.CALM,                duration_s=15,
            label_override="Normal load — baseline conditions",
            desc_override="Fleet operating normally. Heat-load stress scenario starting."),
    StepDef(SimulationPhase.ADVISORY,            duration_s=20,
            sensor_pressure=0.15,
            label_override="Heat advisory — load factor rising",
            desc_override="Ambient temperature climbing. Aged assets beginning to show load stress."),
    StepDef(SimulationPhase.STORM_APPROACHING,   duration_s=22,
            sensor_pressure=0.40,
            label_override="Sustained heat — cooling stress on TX-005, TX-006",
            desc_override="Winding hot-spot temperatures above normal. Oil dielectric declining."),
    StepDef(SimulationPhase.PEAK_STORM,          duration_s=20,
            sensor_pressure=0.65,
            label_override="Peak heat load — critical thermal threshold",
            desc_override="TX-007 hot-spot approaching alarm ceiling. Risk scores elevated fleet-wide."),
    StepDef(SimulationPhase.DEGRADATION_PEAK,    duration_s=18,
            sensor_pressure=0.80,
            label_override="Thermal degradation — maintenance urgent",
            desc_override="Prolonged heat stress accelerating insulation degradation. Scheduler activated."),
    StepDef(SimulationPhase.MAINTENANCE_TRIGGERED, duration_s=20,
            sensor_pressure=0.55,
            label_override="Cooling intervention — load shed underway",
            desc_override="Emergency cooling dispatched. Load shedding applied to relieve TX-007 and TX-008."),
    StepDef(SimulationPhase.RECOVERY,            duration_s=20,
            sensor_pressure=0.20,
            label_override="Temperature normalising — risk subsiding",
            desc_override="Ambient temperature dropping. Fleet risk scores trending down."),
]

_SCENARIO_REPEAT_FAULT = [
    StepDef(SimulationPhase.CALM,                duration_s=12,
            label_override="Repeat-fault watch — TX-006 flagged",
            desc_override="TX-006 has an unresolved repeat-fault flag. Elevated vulnerability."),
    StepDef(SimulationPhase.ADVISORY,            duration_s=18,
            label_override="Storm building — repeat-fault assets at risk",
            desc_override="Advisory issued. TX-006 repeat-fault multiplier increases effective pressure."),
    StepDef(SimulationPhase.STORM_APPROACHING,   duration_s=20,
            sensor_pressure=0.35,
            label_override="TX-006 degrading rapidly under storm",
            desc_override="Repeat-fault vulnerability amplifies sensor deterioration on TX-006 and TX-008."),
    StepDef(SimulationPhase.PEAK_STORM,          duration_s=18,
            sensor_pressure=0.70,
            label_override="Peak storm — TX-006 at critical threshold",
            desc_override="TX-006 partial discharge alarm imminent. Grid impact: Hillcrest Fire Station."),
    StepDef(SimulationPhase.DEGRADATION_PEAK,    duration_s=18,
            sensor_pressure=0.85,
            label_override="TX-006 critical — second fault event",
            desc_override="Risk engine elevated TX-006 to Critical. Scheduler has re-ranked TX-006 to rank 1."),
    StepDef(SimulationPhase.MAINTENANCE_TRIGGERED, duration_s=20,
            sensor_pressure=0.60,
            label_override="Emergency crew to Hillcrest — TX-006 isolated",
            desc_override="Crew-Alpha dispatched to Hillcrest. TX-006 isolated from network pending inspection."),
    StepDef(SimulationPhase.RECOVERY,            duration_s=22,
            sensor_pressure=0.18,
            label_override="TX-006 remediated — fleet recovering",
            desc_override="Repeat-fault flag cleared after inspection. Risk trending down across fleet."),
]

_SCENARIO_EQUIPMENT_FAULT = [
    StepDef(SimulationPhase.CALM,                duration_s=15,
            label_override="Equipment fault detected — TX-008",
            desc_override="Sensor anomaly detected on TX-008. Partial discharge spike at 1850 pC."),
    StepDef(SimulationPhase.ADVISORY,            duration_s=18,
            sensor_pressure=0.12,
            label_override="Fault escalating — weather front approaching",
            desc_override="TX-008 partial discharge rising. Weather advisory compounds existing fault."),
    StepDef(SimulationPhase.STORM_APPROACHING,   duration_s=20,
            sensor_pressure=0.38,
            label_override="Combined fault + weather stress on TX-008",
            desc_override="Oil dielectric on TX-008 declining under combined thermal and weather pressure."),
    StepDef(SimulationPhase.PEAK_STORM,          duration_s=20,
            sensor_pressure=0.62,
            label_override="Peak combined stress — TX-008 Critical",
            desc_override="TX-008 now Critical. Airport Control Tower and Fuel Pumping Station at risk."),
    StepDef(SimulationPhase.DEGRADATION_PEAK,    duration_s=15,
            sensor_pressure=0.75,
            label_override="Fleet-wide degradation — scheduler triggered",
            desc_override="5 assets above High threshold. Scheduler reordered full maintenance plan."),
    StepDef(SimulationPhase.MAINTENANCE_TRIGGERED, duration_s=20,
            sensor_pressure=0.50,
            label_override="Emergency isolations in progress",
            desc_override="TX-008 isolated. Crews dispatched to Airport Grid Node. Storm abating."),
    StepDef(SimulationPhase.RECOVERY,            duration_s=22,
            sensor_pressure=0.12,
            label_override="Fault repaired — risk profile normalising",
            desc_override="TX-008 fault remediated. Risk scores returning to pre-event baseline."),
]

_SCENARIO_RECOVERY = [
    StepDef(SimulationPhase.PEAK_STORM,          duration_s=15,
            sensor_pressure=0.55,
            label_override="Post-storm assessment — high asset count",
            desc_override="Starting at peak-storm state. Assessing damage across fleet."),
    StepDef(SimulationPhase.DEGRADATION_PEAK,    duration_s=18,
            sensor_pressure=0.70,
            label_override="Damage assessment complete — prioritising",
            desc_override="Scheduler has produced prioritised remediation plan awaiting approval."),
    StepDef(SimulationPhase.MAINTENANCE_TRIGGERED, duration_s=22,
            sensor_pressure=0.45,
            label_override="Remediation crews active — risk falling",
            desc_override="Multiple crews deployed. Risk scores dropping as interventions take effect."),
    StepDef(SimulationPhase.RECOVERY,            duration_s=22,
            sensor_pressure=0.20,
            label_override="Recovery in progress — fleet stabilising",
            desc_override="Storm passed. Preventive maintenance reducing exposure on all zones."),
    StepDef(SimulationPhase.CALM,                duration_s=18,
            label_override="Fleet restored — normal operations",
            desc_override="All interventions complete. Fleet returned to baseline risk profile."),
]


# Registry — ordered for the selector dropdown
SCENARIOS: dict[str, dict] = {
    "storm_surge": {
        "id": "storm_surge",
        "label": "Storm surge — full narrative arc",
        "description": "Storm approaches, asset degradation peaks, maintenance triggers, recovery.",
        "steps": _SCENARIO_STORM_SURGE,
    },
    "heat_load_stress": {
        "id": "heat_load_stress",
        "label": "Heat & load stress event",
        "description": "Sustained high-temperature high-load stress on ageing fleet.",
        "steps": _SCENARIO_HEAT_LOAD,
    },
    "repeat_fault_storm": {
        "id": "repeat_fault_storm",
        "label": "Repeat-fault asset under storm",
        "description": "TX-006 (repeat-fault flag) hit by second storm — rapid escalation.",
        "steps": _SCENARIO_REPEAT_FAULT,
    },
    "equipment_fault": {
        "id": "equipment_fault",
        "label": "Equipment fault + weather compound",
        "description": "TX-008 sensor fault cascades under incoming weather pressure.",
        "steps": _SCENARIO_EQUIPMENT_FAULT,
    },
    "grid_recovery": {
        "id": "grid_recovery",
        "label": "Post-storm recovery",
        "description": "Start at peak-storm state and show risk normalisation.",
        "steps": _SCENARIO_RECOVERY,
    },
}

DEFAULT_SCENARIO = "storm_surge"


# ---------------------------------------------------------------------------
# ScenarioRunner
# ---------------------------------------------------------------------------

class ScenarioRunner:
    """
    Tracks wall-clock time and interpolates simulation state for a named scenario.

    Usage
    -----
    ::

        runner = ScenarioRunner("storm_surge")
        runner.run()

        while True:
            state = runner.tick()
            grid_state._apply_simulation_step(state)
            if state.completed:
                break
            time.sleep(2)

    Parameters
    ----------
    scenario_id:
        Key in ``SCENARIOS``.  Raises ``ValueError`` for unknown IDs.
    _now_fn:
        Callable returning a monotonic float second count.  Defaults to
        ``time.monotonic``.  Inject a deterministic clock in tests.
    """

    def __init__(
        self,
        scenario_id: str = DEFAULT_SCENARIO,
        *,
        _now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if scenario_id not in SCENARIOS:
            raise ValueError(f"Unknown scenario '{scenario_id}'. "
                             f"Valid: {list(SCENARIOS)}")
        self._scenario = SCENARIOS[scenario_id]
        self._steps: list[StepDef] = self._scenario["steps"]
        self._now = _now_fn

        self._running: bool = False
        self._paused: bool = False
        self._completed: bool = False

        self._start_time: float = 0.0    # monotonic time when run() was called
        self._pause_time: float = 0.0    # monotonic time when pause() was called
        self._pause_offset: float = 0.0  # accumulated pause duration (excluded from elapsed)
        self._step_index: int = 0        # current step

    # ── public interface ──────────────────────────────────────────────────────

    @property
    def scenario_id(self) -> str:
        return self._scenario["id"]

    @property
    def scenario_label(self) -> str:
        return self._scenario["label"]

    @property
    def scenario_description(self) -> str:
        return self._scenario["description"]

    @property
    def is_running(self) -> bool:
        return self._running and not self._paused

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_completed(self) -> bool:
        return self._completed

    def run(self) -> None:
        """Start or restart the scenario from step 0."""
        self._running = True
        self._paused = False
        self._completed = False
        self._step_index = 0
        self._pause_offset = 0.0
        self._start_time = self._now()

    def pause(self) -> None:
        """Freeze elapsed time.  Does nothing if not running."""
        if self._running and not self._paused:
            self._paused = True
            self._pause_time = self._now()

    def resume(self) -> None:
        """Continue from where we paused.  Does nothing if not paused."""
        if self._paused:
            self._pause_offset += self._now() - self._pause_time
            self._paused = False

    def reset(self) -> None:
        """Stop and reset to the beginning without starting."""
        self._running = False
        self._paused = False
        self._completed = False
        self._step_index = 0
        self._pause_offset = 0.0
        self._start_time = 0.0

    def tick(self) -> StepState:
        """
        Return the current interpolated simulation state.

        If not yet started, returns a StepState with running=False.
        Auto-advances through steps based on elapsed real time.
        """
        if not self._running:
            return self._make_state(step_index=0, elapsed_s=0.0)

        # Compute elapsed, excluding any paused time
        now = self._now()
        if self._paused:
            elapsed_s = self._pause_time - self._start_time - self._pause_offset
        else:
            elapsed_s = now - self._start_time - self._pause_offset

        # Advance step index until we find the step that owns this elapsed time
        cumulative = 0.0
        for i, step in enumerate(self._steps):
            if elapsed_s < cumulative + step.duration_s:
                # We're inside this step
                self._step_index = i
                return self._make_state(step_index=i, elapsed_s=elapsed_s,
                                        step_offset=elapsed_s - cumulative)
            cumulative += step.duration_s

        # Past the last step → completed
        self._completed = True
        last = len(self._steps) - 1
        return self._make_state(step_index=last, elapsed_s=elapsed_s,
                                step_offset=self._steps[last].duration_s)

    # ── internals ─────────────────────────────────────────────────────────────

    def _make_state(
        self,
        step_index: int,
        elapsed_s: float,
        step_offset: float = 0.0,
    ) -> StepState:
        step = self._steps[step_index]
        duration = step.duration_s
        step_progress = min(1.0, step_offset / duration) if duration > 0 else 0.0

        # Effective sensor pressure: interpolate between previous step's
        # pressure and this step's pressure so the transition looks smooth.
        base_pressure = self._effective_pressure(step)
        if step_index > 0 and step_progress < 1.0:
            prev_step = self._steps[step_index - 1]
            prev_pressure = self._effective_pressure(prev_step)
            # Ramp from prev to current over the first 40% of the step,
            # then hold at the target value.
            ramp_t = min(1.0, step_progress / 0.40) if step_progress < 0.40 else 1.0
            sensor_pressure = prev_pressure + (base_pressure - prev_pressure) * ramp_t
        else:
            sensor_pressure = base_pressure

        # Build a PhaseInfo with any overrides applied
        base_phase_info = _PHASE_MAP[step.phase]
        if step.label_override or step.desc_override:
            phase_info = PhaseInfo(
                phase=base_phase_info.phase,
                label=step.label_override or base_phase_info.label,
                description=step.desc_override or base_phase_info.description,
                temp_mult=base_phase_info.temp_mult,
                precip_mult=base_phase_info.precip_mult,
                wind_mult=base_phase_info.wind_mult,
                storm_level_override=base_phase_info.storm_level_override,
                sensor_pressure=sensor_pressure,
            )
        else:
            # Rebuild with the interpolated sensor_pressure
            phase_info = PhaseInfo(
                phase=base_phase_info.phase,
                label=base_phase_info.label,
                description=base_phase_info.description,
                temp_mult=base_phase_info.temp_mult,
                precip_mult=base_phase_info.precip_mult,
                wind_mult=base_phase_info.wind_mult,
                storm_level_override=base_phase_info.storm_level_override,
                sensor_pressure=sensor_pressure,
            )

        return StepState(
            scenario_id=self._scenario["id"],
            scenario_label=self._scenario["label"],
            step_index=step_index,
            total_steps=len(self._steps),
            phase=step.phase,
            phase_info=phase_info,
            sensor_pressure=round(max(0.0, min(1.0, sensor_pressure)), 4),
            elapsed_s=round(elapsed_s, 2),
            step_elapsed_s=round(step_offset, 2),
            step_duration_s=duration,
            step_progress=round(step_progress, 4),
            running=self._running and not self._paused,
            paused=self._paused,
            completed=self._completed,
        )

    @staticmethod
    def _effective_pressure(step: StepDef) -> float:
        """Resolve a step's sensor pressure (explicit override or phase default)."""
        if step.sensor_pressure is not None:
            return step.sensor_pressure
        return _PHASE_MAP[step.phase].sensor_pressure
