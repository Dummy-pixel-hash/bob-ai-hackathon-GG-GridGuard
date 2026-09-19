"""
Deterministic maintenance scheduler.

Produces a prioritised list of inspection and maintenance tasks from the
current risk engine outputs, grid impact factors, crew availability, and
time windows.

DESIGN CONTRACT
---------------
- Entirely deterministic: same inputs → same ranked task list.
- Reads only from engine outputs (``RiskResult``) and static config.
  It does NOT call the LLM, does NOT store state, and does NOT modify
  risk scores.  The LLM may *explain* the plan, but the scheduler is the
  sole authoritative source of task ordering.
- Operator approval is required before any plan change is applied.
  The scheduler produces a *proposed* plan; ``approve_plan`` returns an
  immutable snapshot that the API layer uses to update the serving state.
- Crew windows are simple time-slot structures.  There is no real calendar
  integration — this is a demo representation of the scheduling concept.

RANKING ALGORITHM
-----------------
Each task receives a priority score:

    priority = overall_risk * 0.50
             + grid_impact_score * 0.30
             + urgency_bonus * 0.20

Where ``urgency_bonus`` is derived from:
  - Simulation phase (storm phases add 0–30 points)
  - Maintenance overdue days (each 30-day period adds 5 points, max 25)
  - Repeat fault flag (+15 points)
  - No redundant path (+10 points)

Tasks are then sorted by priority descending, then by grid_impact_score
descending as the tie-break.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from risk_engine.models import RiskResult


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class CrewWindow:
    """A single available maintenance crew time-slot."""
    crew_id: str                      # e.g. "CREW-ALPHA"
    region: str                       # region the crew is staged in
    available_from: datetime          # ISO datetime (UTC)
    available_until: datetime         # ISO datetime (UTC)
    specialisation: str = "general"   # "general" | "oil" | "electrical"


@dataclass
class ScheduledTask:
    """One proposed maintenance/inspection task for an asset."""
    rank: int                         # 1-based priority rank
    asset_id: str
    substation: str
    region: str
    status: str                       # "Critical" | "High" | "Monitoring" | "Healthy"
    overall_risk: float               # engine score 0–100
    grid_impact_score: float          # engine grid-impact component 0–100
    priority_score: float             # derived scheduling score 0–100
    urgency_bonus: float              # bonus added to base priority
    dominant_factor: str
    dominant_factor_label: str
    recommended_action: str           # "Inspect today" | … as per status
    action_detail: str
    urgency: str                      # "immediate" | "priority" | "routine" | "monitor"
    window_hours: int                 # how long the crew window should be
    assigned_crew: Optional[str]      # crew_id if assigned, else None
    scheduled_start: Optional[str]    # ISO datetime string or None
    customers_served: int
    critical_facilities: int
    has_redundant_path: bool
    # Reason this task was ranked here — for LLM context and UI display.
    scheduling_reason: str = ""
    # Simulation phase that triggered this priority (if applicable)
    triggered_by_phase: Optional[str] = None
    # True if this task was reordered vs. the previous plan
    reordered: bool = False


@dataclass
class MaintenancePlan:
    """Complete proposed maintenance plan produced by the scheduler."""
    generated_at: str                       # ISO datetime string (UTC)
    simulation_phase: Optional[str]         # Current sim phase label or None
    tasks: list[ScheduledTask] = field(default_factory=list)
    crew_prepositioning: list[dict] = field(default_factory=list)
    # True when this plan has been approved by an operator
    approved: bool = False
    approved_at: Optional[str] = None
    approved_by: str = "operator"
    # Summary changes vs. previous plan (for display)
    change_summary: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ACTION_BY_STATUS = {
    "Critical": ("Inspect today", "Immediate — dispatch crew within 24h", "immediate", 4),
    "High":     ("Inspect this week", "Priority — schedule within 7 days", "priority", 8),
    "Monitoring": ("Plan inspection", "Routine — schedule within 30 days", "routine", 16),
    "Healthy":  ("Routine monitoring", "No action — next scheduled check", "monitor", 0),
}

_COMPONENT_LABELS = {
    "sensor_health": "Sensor health",
    "weather_risk": "Weather risk",
    "historical_failure": "Historical failure",
    "asset_degradation": "Asset degradation",
    "grid_impact": "Grid impact",
}

# Default crew pool (demo-only — no real calendar)
_DEFAULT_CREWS: list[CrewWindow] = [
    CrewWindow(
        crew_id="CREW-ALPHA",
        region="East",
        available_from=datetime.now(tz=timezone.utc),
        available_until=datetime.now(tz=timezone.utc) + timedelta(hours=12),
        specialisation="oil",
    ),
    CrewWindow(
        crew_id="CREW-BRAVO",
        region="West",
        available_from=datetime.now(tz=timezone.utc),
        available_until=datetime.now(tz=timezone.utc) + timedelta(hours=10),
        specialisation="electrical",
    ),
    CrewWindow(
        crew_id="CREW-CHARLIE",
        region="South",
        available_from=datetime.now(tz=timezone.utc),
        available_until=datetime.now(tz=timezone.utc) + timedelta(hours=8),
        specialisation="general",
    ),
    CrewWindow(
        crew_id="CREW-DELTA",
        region="Central",
        available_from=datetime.now(tz=timezone.utc),
        available_until=datetime.now(tz=timezone.utc) + timedelta(hours=12),
        specialisation="general",
    ),
    CrewWindow(
        crew_id="CREW-ECHO",
        region="North",
        available_from=datetime.now(tz=timezone.utc),
        available_until=datetime.now(tz=timezone.utc) + timedelta(hours=8),
        specialisation="general",
    ),
]


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class MaintenanceScheduler:
    """
    Deterministic maintenance scheduler.

    Parameters
    ----------
    crews:
        Available crew time-windows.  Defaults to the demo crew pool.
    sim_phase_label:
        Current simulation phase label (used in scheduling reason text
        and urgency bonus calculation).
    """

    def __init__(
        self,
        crews: Optional[list[CrewWindow]] = None,
        sim_phase_label: Optional[str] = None,
    ) -> None:
        self._crews: list[CrewWindow] = crews if crews is not None else copy.deepcopy(_DEFAULT_CREWS)
        self._sim_phase = sim_phase_label or ""

    # -- Public API -----------------------------------------------------------

    def build_plan(
        self,
        results: list[RiskResult],
        asset_views: list[dict],
        previous_plan: Optional[MaintenancePlan] = None,
    ) -> MaintenancePlan:
        """
        Build a new proposed maintenance plan.

        Parameters
        ----------
        results:
            Current ``RiskResult`` list from the engine (one per asset).
        asset_views:
            Asset view dicts from ``GridState.assets`` (for topology info).
        previous_plan:
            The previously approved/proposed plan for change-detection.

        Returns
        -------
        ``MaintenancePlan`` (not yet approved — pending operator confirmation).
        """
        results_by_id = {r.asset_id: r for r in results}
        views_by_id = {v["id"]: v for v in asset_views}

        tasks: list[ScheduledTask] = []
        used_crews: set[str] = set()

        for result in results:
            view = views_by_id.get(result.asset_id, {})
            task = self._make_task(result, view)
            tasks.append(task)

        # Sort by priority score desc, then grid impact desc (tie-break)
        tasks.sort(
            key=lambda t: (t.priority_score, t.grid_impact_score),
            reverse=True,
        )

        # Assign ranks and crews
        assigned_starts: dict[str, datetime] = {}
        for rank, task in enumerate(tasks, start=1):
            task.rank = rank
            # Try to find a crew in the same region
            crew = self._find_crew(task, used_crews)
            if crew is not None:
                task.assigned_crew = crew.crew_id
                start = assigned_starts.get(crew.region, crew.available_from)
                task.scheduled_start = start.isoformat(timespec="minutes")
                # Stagger next crew start for this region
                assigned_starts[crew.region] = start + timedelta(hours=task.window_hours)
                used_crews.add(crew.crew_id)

        # Detect reordering vs. previous plan
        change_summary: list[str] = []
        if previous_plan is not None and previous_plan.approved:
            prev_rank = {t.asset_id: t.rank for t in previous_plan.tasks}
            for task in tasks:
                old = prev_rank.get(task.asset_id)
                if old is not None and old != task.rank:
                    delta = old - task.rank
                    if delta > 0:
                        task.reordered = True
                        change_summary.append(
                            f"{task.asset_id} moved up {delta} position{'s' if delta > 1 else ''} "
                            f"(was #{old}, now #{task.rank})"
                        )
                    elif delta < -1:
                        change_summary.append(
                            f"{task.asset_id} moved down {abs(delta)} positions "
                            f"(was #{old}, now #{task.rank})"
                        )

        # Crew pre-positioning (same logic as existing GridState.priorities)
        crew_prep = self._crew_prepositioning(tasks, asset_views)

        return MaintenancePlan(
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            simulation_phase=self._sim_phase or None,
            tasks=tasks,
            crew_prepositioning=crew_prep,
            approved=False,
            change_summary=change_summary,
        )

    def approve_plan(self, plan: MaintenancePlan, approved_by: str = "operator") -> MaintenancePlan:
        """
        Mark a plan as operator-approved.

        This is a thin stamp: it does not re-score anything; it records
        the approval metadata and returns an immutable copy.
        """
        approved = copy.deepcopy(plan)
        approved.approved = True
        approved.approved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        approved.approved_by = approved_by
        return approved

    # -- Internal helpers -----------------------------------------------------

    def _urgency_bonus(self, result: RiskResult, view: dict) -> float:
        """Compute the urgency bonus for a task based on context."""
        bonus = 0.0

        # Simulation phase bonus — match on human-readable phase label keywords.
        # Labels come from PhaseInfo.label e.g. "Peak storm — asset degradation active"
        phase_label = self._sim_phase.lower()
        if "peak storm" in phase_label or "degradation" in phase_label:
            bonus += 30.0
        elif "storm approaching" in phase_label or "maintenance triggered" in phase_label:
            bonus += 20.0
        elif "advisory" in phase_label:
            bonus += 10.0
        elif phase_label and "calm" not in phase_label and "recovery" not in phase_label:
            # Any unmatched active phase label adds a small bonus
            bonus += 5.0

        # Maintenance overdue
        overdue = view.get("degradation", {}).get("maintenance_overdue_days", 0) or 0
        bonus += min(25.0, (overdue // 30) * 5.0)

        # Repeat fault flag
        if view.get("history", {}).get("repeat_mode_flag", False):
            bonus += 15.0

        # No redundant path
        if not view.get("grid_impact", {}).get("has_redundant_path", True):
            bonus += 10.0

        return bonus

    def _priority_score(self, result: RiskResult, view: dict) -> tuple[float, float]:
        """Return (priority_score, urgency_bonus) for a task."""
        ub = self._urgency_bonus(result, view)
        score = (
            result.overall_risk * 0.50
            + result.components.grid_impact * 0.30
            + ub * 0.20
        )
        return round(min(100.0, score), 2), round(ub, 2)

    def _make_task(self, result: RiskResult, view: dict) -> ScheduledTask:
        """Build a ScheduledTask from a RiskResult and its asset view."""
        status_label = {
            "Normal": "Healthy",
            "Watch": "Monitoring",
            "High": "High",
            "Critical": "Critical",
        }.get(result.risk_level.value, result.risk_level.value)

        action, detail, urgency, window = _ACTION_BY_STATUS.get(
            status_label,
            ("Routine monitoring", "No action — next scheduled check", "monitor", 0),
        )

        priority, ub = self._priority_score(result, view)

        gi = view.get("grid_impact", {})
        customers = gi.get("customers_served", 0)
        crit_fac = gi.get("critical_facility_count", 0)
        redundant = gi.get("has_redundant_path", True)

        reason_parts: list[str] = []
        if self._sim_phase:
            reason_parts.append(f"Sim phase: {self._sim_phase}")
        if result.overall_risk >= 85:
            reason_parts.append("CRITICAL — immediate intervention required")
        elif result.overall_risk >= 70:
            reason_parts.append("HIGH — schedule within 7 days")
        if not redundant:
            reason_parts.append("no N-1 redundancy")
        if crit_fac:
            reason_parts.append(f"{crit_fac} critical facilit{'y' if crit_fac == 1 else 'ies'} downstream")

        return ScheduledTask(
            rank=0,  # filled in after sort
            asset_id=result.asset_id,
            substation=view.get("substation", result.asset_id),
            region=view.get("region", ""),
            status=status_label,
            overall_risk=result.overall_risk,
            grid_impact_score=result.components.grid_impact,
            priority_score=priority,
            urgency_bonus=ub,
            dominant_factor=result.dominant_factor,
            dominant_factor_label=_COMPONENT_LABELS.get(
                result.dominant_factor, result.dominant_factor
            ),
            recommended_action=action,
            action_detail=detail,
            urgency=urgency,
            window_hours=window,
            assigned_crew=None,
            scheduled_start=None,
            customers_served=customers,
            critical_facilities=crit_fac,
            has_redundant_path=redundant,
            scheduling_reason="; ".join(reason_parts) if reason_parts else "Standard periodic inspection",
            triggered_by_phase=self._sim_phase or None,
        )

    def _find_crew(
        self, task: ScheduledTask, used: set[str]
    ) -> Optional[CrewWindow]:
        """
        Find the best available crew for a task.

        Preference order:
        1. Same region + matching specialisation (for Critical assets).
        2. Same region + any specialisation.
        3. Any region with specialisation match.
        4. Any available crew.
        """
        if task.urgency == "monitor":
            return None

        available = [c for c in self._crews if c.crew_id not in used]
        if not available:
            return None

        # Determine preferred specialisation
        dominant = task.dominant_factor
        pref_spec = "oil" if dominant in ("sensor_health", "asset_degradation") else \
                    "electrical" if dominant == "historical_failure" else "general"

        # Priority 1: same region + preferred spec
        for c in available:
            if c.region == task.region and c.specialisation == pref_spec:
                return c

        # Priority 2: same region
        for c in available:
            if c.region == task.region:
                return c

        # Priority 3: spec match
        for c in available:
            if c.specialisation == pref_spec:
                return c

        # Priority 4: any
        return available[0]

    def _crew_prepositioning(
        self,
        tasks: list[ScheduledTask],
        asset_views: list[dict],
    ) -> list[dict]:
        """Build crew pre-positioning recommendations from high-consequence tasks."""
        views_by_id = {v["id"]: v for v in asset_views}
        crew: dict[str, dict] = {}
        for task in tasks:
            view = views_by_id.get(task.asset_id, {})
            exposed = (
                view.get("components", {}).get("weather_risk", 0) >= 60.0
                or view.get("weather_raw", {}).get("storm_warning_level", 0) >= 2
            )
            consequential = (
                task.overall_risk >= 70.0
                or task.critical_facilities > 0
            )
            if exposed and consequential:
                cell = crew.setdefault(
                    task.region,
                    {"region": task.region, "assets": [], "reason": ""},
                )
                if task.asset_id not in cell["assets"]:
                    cell["assets"].append(task.asset_id)

        crew_list: list[dict] = []
        for region, cell in crew.items():
            worst_task = max(
                (t for t in tasks if t.asset_id in cell["assets"]),
                key=lambda t: t.overall_risk,
            )
            view = views_by_id.get(worst_task.asset_id, {})
            weather_score = view.get("components", {}).get("weather_risk", 0)
            storm_level = view.get("weather_raw", {}).get("storm_warning_level", 0)
            phase_note = f" [{self._sim_phase}]" if self._sim_phase else ""
            cell["reason"] = (
                f"Storm exposure (weather {weather_score}/100, "
                f"warning level {storm_level}) on "
                f"{worst_task.asset_id} ({worst_task.status}, "
                f"{worst_task.overall_risk}/100) — stage crews in "
                f"{region} before the front arrives.{phase_note}"
            )
            crew_list.append(cell)

        crew_list.sort(
            key=lambda c: max(t.overall_risk for t in tasks if t.asset_id in c["assets"]),
            reverse=True,
        )
        return crew_list
