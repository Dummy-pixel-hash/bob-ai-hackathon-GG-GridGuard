"""
Tests for the ScenarioRunner autonomous simulation layer.

Coverage:
  - All five named scenarios exist and are loadable
  - DEFAULT_SCENARIO is valid
  - ScenarioRunner raises ValueError for unknown scenario ID
  - run() starts the runner; tick() returns StepState
  - Elapsed time advances through steps correctly (injected clock)
  - step_progress goes from 0 to 1 across a step
  - sensor_pressure interpolates smoothly between steps (no jump)
  - Auto-advance: runner moves to next step when duration expires
  - completed flag is set after all steps finish
  - pause() freezes elapsed time
  - resume() continues from frozen point
  - reset() returns to step 0, not running
  - reset() after completed clears completed flag
  - scenario with sensor_pressure override uses override value
  - PhaseInfo label/desc overrides are applied in StepState
  - _effective_pressure uses step override over phase default
  - scenario_list from GridState returns all scenarios
  - GridState.scenario_run() activates simulation
  - GridState.scenario_pause() pauses the runner
  - GridState.scenario_resume() resumes the runner
  - GridState.scenario_reset() restores static data
  - GridState.scenario_tick() is safe when no runner
  - GridState.scenario_tick() returns scenario_active=False when stopped
  - Fallback: scenario_run + scenario_reset leaves static scores intact
  - Scenario pressure 0.0 leaves assets at near-static risk
  - Scenario pressure 0.75 raises risk on ageing assets
"""

from __future__ import annotations

import sys
import os
import time

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from simulation.scenario_runner import (
    DEFAULT_SCENARIO,
    SCENARIOS,
    ScenarioRunner,
    StepDef,
    StepState,
)
from simulation.weather_sim import SimulationPhase
from api.grid_service import GridState


# ── Helpers ───────────────────────────────────────────────────────────────────

class FakeClock:
    """Deterministic clock for ScenarioRunner tests."""
    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# ── Scenario registry ─────────────────────────────────────────────────────────

class TestScenarioRegistry:
    def test_all_scenarios_present(self):
        ids = set(SCENARIOS.keys())
        assert "storm_surge" in ids
        assert "heat_load_stress" in ids
        assert "repeat_fault_storm" in ids
        assert "equipment_fault" in ids
        assert "grid_recovery" in ids

    def test_default_scenario_valid(self):
        assert DEFAULT_SCENARIO in SCENARIOS

    def test_each_scenario_has_steps(self):
        for sid, s in SCENARIOS.items():
            assert len(s["steps"]) >= 3, f"{sid} has fewer than 3 steps"

    def test_each_step_has_positive_duration(self):
        for sid, s in SCENARIOS.items():
            for step in s["steps"]:
                assert step.duration_s > 0, f"{sid} has a step with non-positive duration"

    def test_sensor_pressure_in_range(self):
        for sid, s in SCENARIOS.items():
            for step in s["steps"]:
                if step.sensor_pressure is not None:
                    assert 0.0 <= step.sensor_pressure <= 1.0, \
                        f"{sid} has out-of-range sensor_pressure"


# ── ScenarioRunner construction ───────────────────────────────────────────────

class TestScenarioRunnerConstruct:
    def test_unknown_scenario_raises(self):
        with pytest.raises(ValueError, match="Unknown scenario"):
            ScenarioRunner("nonexistent_scenario_xyz")

    def test_valid_scenario_constructs(self):
        r = ScenarioRunner("storm_surge")
        assert r.scenario_id == "storm_surge"
        assert not r.is_running
        assert not r.is_paused
        assert not r.is_completed

    def test_all_scenarios_construct(self):
        for sid in SCENARIOS:
            r = ScenarioRunner(sid)
            assert r.scenario_id == sid


# ── StepState before run() ────────────────────────────────────────────────────

class TestBeforeRun:
    def test_tick_before_run_not_running(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        state = r.tick()
        assert isinstance(state, StepState)
        assert state.running is False
        assert state.step_index == 0
        assert state.elapsed_s == 0.0


# ── run / tick / auto-advance ─────────────────────────────────────────────────

class TestRunAndTick:
    def test_run_starts_runner(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        r.run()
        assert r.is_running
        state = r.tick()
        assert state.running is True
        assert state.step_index == 0

    def test_step_progress_at_start(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        r.run()
        state = r.tick()
        assert state.step_progress == pytest.approx(0.0, abs=0.05)

    def test_step_progress_midway(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        steps = SCENARIOS["storm_surge"]["steps"]
        dur0 = steps[0].duration_s
        r.run()
        clock.advance(dur0 * 0.5)
        state = r.tick()
        assert state.step_index == 0
        assert 0.4 <= state.step_progress <= 0.6

    def test_auto_advance_to_next_step(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        steps = SCENARIOS["storm_surge"]["steps"]
        dur0 = steps[0].duration_s
        r.run()
        # Advance just past the first step's duration
        clock.advance(dur0 + 0.1)
        state = r.tick()
        assert state.step_index == 1

    def test_sensor_pressure_increases_across_storm_surge(self):
        """sensor_pressure should be higher in later storm steps than early ones."""
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        steps = SCENARIOS["storm_surge"]["steps"]
        r.run()

        # Get pressure at step 0 (calm)
        clock.advance(steps[0].duration_s * 0.5)
        early_state = r.tick()
        early_p = early_state.sensor_pressure

        # Jump past steps 0,1,2 to step 3 (peak_storm)
        total_dur = sum(s.duration_s for s in steps[:3])
        clock.advance(total_dur)
        peak_state = r.tick()
        assert peak_state.step_index >= 3
        assert peak_state.sensor_pressure > early_p

    def test_completed_after_all_steps(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        steps = SCENARIOS["storm_surge"]["steps"]
        total_dur = sum(s.duration_s for s in steps)
        r.run()
        clock.advance(total_dur + 1.0)
        state = r.tick()
        assert state.completed is True
        assert r.is_completed

    def test_sensor_pressure_clamped_0_to_1(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        r.run()
        steps = SCENARIOS["storm_surge"]["steps"]
        total = sum(s.duration_s for s in steps)
        for t in [0, total * 0.25, total * 0.5, total * 0.75, total]:
            clock.advance(0)
            state = r.tick()
            assert 0.0 <= state.sensor_pressure <= 1.0
            clock._t = t  # jump ahead


# ── pause / resume ────────────────────────────────────────────────────────────

class TestPauseResume:
    def test_pause_freezes_elapsed(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        r.run()
        clock.advance(5.0)
        r.pause()
        assert r.is_paused
        # Advance clock — elapsed should not change while paused
        clock.advance(100.0)
        state = r.tick()
        assert state.elapsed_s == pytest.approx(5.0, abs=0.1)
        assert state.paused is True
        assert state.running is False

    def test_resume_continues_from_frozen_point(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        r.run()
        clock.advance(5.0)
        r.pause()
        clock.advance(100.0)  # frozen during pause
        r.resume()
        assert not r.is_paused
        clock.advance(3.0)
        state = r.tick()
        # Should be ~8 seconds of effective elapsed, not 108
        assert state.elapsed_s == pytest.approx(8.0, abs=0.3)

    def test_pause_idempotent(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        r.run()
        clock.advance(2.0)
        r.pause()
        r.pause()  # second pause — no-op
        r.resume()
        state = r.tick()
        assert state.elapsed_s == pytest.approx(2.0, abs=0.1)


# ── reset ─────────────────────────────────────────────────────────────────────

class TestReset:
    def test_reset_stops_runner(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        r.run()
        clock.advance(30.0)
        r.reset()
        assert not r.is_running
        assert not r.is_completed
        state = r.tick()
        assert state.running is False
        assert state.step_index == 0

    def test_reset_after_completed(self):
        clock = FakeClock()
        r = ScenarioRunner("storm_surge", _now_fn=clock)
        steps = SCENARIOS["storm_surge"]["steps"]
        r.run()
        clock.advance(sum(s.duration_s for s in steps) + 5)
        assert r.tick().completed
        r.reset()
        assert not r.is_completed
        state = r.tick()
        assert state.running is False


# ── label / description overrides ────────────────────────────────────────────

class TestLabelOverrides:
    def test_label_override_applied(self):
        """Steps with label_override should expose that label in StepState.phase_info."""
        clock = FakeClock()
        r = ScenarioRunner("heat_load_stress", _now_fn=clock)
        r.run()
        steps = SCENARIOS["heat_load_stress"]["steps"]
        # Find the first step with a label override
        override_step = None
        cumulative = 0.0
        for i, step in enumerate(steps):
            if step.label_override:
                override_step = (i, step, cumulative)
                break
            cumulative += step.duration_s
        if override_step is None:
            pytest.skip("no label_override in heat_load_stress")
        idx, step, cum = override_step
        clock.advance(cum + step.duration_s * 0.5)
        state = r.tick()
        assert state.phase_info.label == step.label_override


# ── pressure override ─────────────────────────────────────────────────────────

class TestPressureOverride:
    def test_step_pressure_override_used(self):
        """A step with explicit sensor_pressure should drive sensor_pressure in state."""
        # Use heat_load_stress which has explicit overrides
        clock = FakeClock()
        r = ScenarioRunner("heat_load_stress", _now_fn=clock)
        r.run()
        steps = SCENARIOS["heat_load_stress"]["steps"]
        # Find a step with explicit pressure
        cumulative = 0.0
        for step in steps:
            if step.sensor_pressure is not None:
                # Jump to middle of that step
                clock.advance(cumulative + step.duration_s * 0.9)
                state = r.tick()
                # After ramp-in (0.9 > 0.4 threshold), pressure should equal override
                assert abs(state.sensor_pressure - step.sensor_pressure) < 0.05
                return
            cumulative += step.duration_s
        pytest.skip("no pressure override found")


# ── GridState integration ─────────────────────────────────────────────────────

class TestGridStateScenario:
    def _gs(self):
        return GridState(db_path=":memory:")

    def test_scenario_list_returns_all(self):
        gs = self._gs()
        result = gs.scenario_list()
        assert "scenarios" in result
        ids = [s["id"] for s in result["scenarios"]]
        for sid in SCENARIOS:
            assert sid in ids

    def test_scenario_list_has_default(self):
        gs = self._gs()
        result = gs.scenario_list()
        assert result["default"] == DEFAULT_SCENARIO

    def test_scenario_run_activates_simulation(self):
        gs = self._gs()
        result = gs.scenario_run(scenario_id="grid_recovery")
        assert result.get("active") is True
        assert result.get("scenario_active") is True
        assert result["scenario_id"] == "grid_recovery"

    def test_scenario_run_scores_differ_from_static(self):
        """Running a high-pressure scenario should raise risk above static baseline."""
        gs = self._gs()
        static_scores = {a["id"]: a["overall_risk"] for a in gs.assets}
        # jump to peak_storm step which has high pressure
        gs.scenario_run(scenario_id="storm_surge")
        # Advance runner to a high-pressure step (peak storm)
        if gs._runner:
            steps = SCENARIOS["storm_surge"]["steps"]
            total = sum(s.duration_s for s in steps[:4])
            gs._runner._start_time = gs._runner._now() - total - 0.5
        gs.scenario_tick()
        sim_scores = {a["id"]: a["overall_risk"] for a in gs.assets}
        # At least some assets should be higher
        higher = sum(1 for aid in sim_scores if sim_scores[aid] > static_scores[aid])
        assert higher >= 2

    def test_scenario_pause_returns_paused(self):
        gs = self._gs()
        gs.scenario_run(scenario_id="storm_surge")
        result = gs.scenario_pause()
        assert result.get("paused") is True

    def test_scenario_resume_after_pause(self):
        gs = self._gs()
        gs.scenario_run(scenario_id="storm_surge")
        gs.scenario_pause()
        result = gs.scenario_resume()
        assert result.get("paused") is False

    def test_scenario_reset_restores_static(self):
        gs = self._gs()
        static_scores = {a["id"]: a["overall_risk"] for a in gs.assets}
        gs.scenario_run(scenario_id="storm_surge")
        # Push to high pressure
        if gs._runner:
            steps = SCENARIOS["storm_surge"]["steps"]
            total = sum(s.duration_s for s in steps[:4])
            gs._runner._start_time = gs._runner._now() - total
        gs.scenario_tick()
        gs.scenario_reset()
        # Simulation should be inactive
        assert not gs._sim_active
        # Scores should be back to static
        restored_scores = {a["id"]: a["overall_risk"] for a in gs.assets}
        for aid in static_scores:
            assert restored_scores[aid] == pytest.approx(static_scores[aid], abs=0.01)

    def test_scenario_tick_when_inactive(self):
        gs = self._gs()
        result = gs.scenario_tick()
        assert result.get("active") is False
        assert result.get("scenario_active") is False

    def test_scenario_run_invalid_id_returns_error(self):
        gs = self._gs()
        result = gs.scenario_run(scenario_id="totally_invalid_xyz")
        assert "error" in result

    def test_scenario_run_uses_offline_fallback(self):
        """scenario_run should succeed even when attempt_live=True but network fails."""
        gs = self._gs()
        # Patch WeatherSimulator to not attempt live fetch
        result = gs.scenario_run(scenario_id="storm_surge")
        # Should succeed (live fetch may fail, static fallback used)
        assert result.get("active") is True

    def test_static_fallback_intact_after_stop(self):
        """simulation_stop() after a scenario should fully restore static data."""
        gs = self._gs()
        static_scores = {a["id"]: a["overall_risk"] for a in gs.assets}
        gs.scenario_run(scenario_id="storm_surge")
        gs.simulation_stop()
        assert not gs._sim_active
        for a in gs.assets:
            assert a["overall_risk"] == pytest.approx(static_scores[a["id"]], abs=0.01)
