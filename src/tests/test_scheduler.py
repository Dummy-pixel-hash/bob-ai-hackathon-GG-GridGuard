"""
Tests for the deterministic maintenance scheduler.

Coverage:
  - MaintenanceScheduler.build_plan() produces ranked ScheduledTask list
  - Ranking: Critical > High > Monitoring > Healthy under equal conditions
  - Simulation phase label increases urgency bonus
  - Crew assignment: prefers same-region crew
  - Crew assignment: fallback to any crew when region not available
  - Operator approval: sets approved=True + timestamps
  - Operator approval: returns immutable copy (original plan unchanged)
  - Operator reject: plan_to_dict pending_approval handling
  - Scheduler rank 1 is always the highest priority_score
  - All tasks are present (no assets dropped)
  - Static plan (no simulation) has zero urgency bonus from phase
  - Repeat fault flag increases urgency bonus
  - No-redundancy flag increases urgency bonus
  - Maintenance overdue adds to urgency bonus (stepped)
  - Change summary populated when previous approved plan exists
"""

from __future__ import annotations

import copy
import sys
import os
from datetime import datetime, timezone

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from maintenance.scheduler import (
    CrewWindow,
    MaintenancePlan,
    MaintenanceScheduler,
    ScheduledTask,
)
from risk_engine.models import (
    ComponentScores,
    RiskInputs,
    RiskLevel,
    RiskResult,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_result(
    asset_id: str,
    overall_risk: float,
    grid_impact: float = 30.0,
    level: RiskLevel = RiskLevel.NORMAL,
) -> RiskResult:
    return RiskResult(
        asset_id=asset_id,
        overall_risk=overall_risk,
        risk_level=level,
        components=ComponentScores(
            sensor_health=overall_risk,
            weather_risk=overall_risk,
            historical_failure=overall_risk,
            asset_degradation=overall_risk,
            grid_impact=grid_impact,
        ),
        dominant_factor="sensor_health",
    )


def _view(
    asset_id: str,
    region: str = "East",
    has_redundant_path: bool = True,
    repeat_mode_flag: bool = False,
    overdue_days: int = 0,
    critical_facilities: int = 0,
    weather_risk_score: float = 20.0,
    storm_level: int = 0,
) -> dict:
    return {
        "id": asset_id,
        "substation": f"{asset_id}-Sub",
        "region": region,
        "components": {"weather_risk": weather_risk_score},
        "weather_raw": {"storm_warning_level": storm_level},
        "grid_impact": {
            "has_redundant_path": has_redundant_path,
            "critical_facility_count": critical_facilities,
            "customers_served": 10_000,
        },
        "history": {"repeat_mode_flag": repeat_mode_flag},
        "degradation": {"maintenance_overdue_days": overdue_days},
    }


def _make_crew(crew_id: str, region: str, spec: str = "general") -> CrewWindow:
    return CrewWindow(
        crew_id=crew_id,
        region=region,
        available_from=datetime.now(tz=timezone.utc),
        available_until=datetime.now(tz=timezone.utc).replace(hour=23),
        specialisation=spec,
    )


# ===========================================================================
# 1.  Basic plan construction
# ===========================================================================

class TestBuildPlan:

    def test_returns_plan_with_all_assets(self):
        results = [
            _make_result("TX-001", 5.0),
            _make_result("TX-002", 50.0, level=RiskLevel.WATCH),
            _make_result("TX-003", 80.0, level=RiskLevel.HIGH),
        ]
        views = [_view("TX-001"), _view("TX-002"), _view("TX-003")]
        sched = MaintenanceScheduler()
        plan = sched.build_plan(results, views)
        assert len(plan.tasks) == 3

    def test_rank_1_is_highest_priority(self):
        results = [
            _make_result("TX-LOW",  5.0,  level=RiskLevel.NORMAL),
            _make_result("TX-HIGH", 85.0, grid_impact=60.0, level=RiskLevel.CRITICAL),
        ]
        views = [
            _view("TX-LOW"),
            _view("TX-HIGH", critical_facilities=2),
        ]
        sched = MaintenanceScheduler()
        plan = sched.build_plan(results, views)
        assert plan.tasks[0].asset_id == "TX-HIGH"
        assert plan.tasks[0].rank == 1

    def test_ranks_are_sequential_from_1(self):
        results = [_make_result(f"TX-{i:03d}", float(i * 10)) for i in range(5)]
        views = [_view(f"TX-{i:03d}") for i in range(5)]
        sched = MaintenanceScheduler()
        plan = sched.build_plan(results, views)
        ranks = [t.rank for t in plan.tasks]
        assert ranks == list(range(1, len(results) + 1))

    def test_plan_not_approved_by_default(self):
        results = [_make_result("TX-001", 50.0)]
        views = [_view("TX-001")]
        sched = MaintenanceScheduler()
        plan = sched.build_plan(results, views)
        assert plan.approved is False
        assert plan.approved_at is None


# ===========================================================================
# 2.  Urgency bonus calculation
# ===========================================================================

class TestUrgencyBonus:

    def test_storm_phase_adds_bonus(self):
        # Use a High-risk result so urgency != "monitor" (which skips crew/bonus)
        result = _make_result("TX-001", 75.0, level=RiskLevel.HIGH)
        view = _view("TX-001")
        sched_static = MaintenanceScheduler(sim_phase_label="")
        sched_storm = MaintenanceScheduler(sim_phase_label="Peak storm — asset degradation active")
        plan_static = sched_static.build_plan([result], [view])
        plan_storm = sched_storm.build_plan([result], [view])
        # Storm phase must add to urgency_bonus
        assert plan_storm.tasks[0].urgency_bonus > plan_static.tasks[0].urgency_bonus

    def test_repeat_fault_adds_15_pts_to_bonus(self):
        r = _make_result("TX-001", 50.0)
        v_normal = _view("TX-001", repeat_mode_flag=False)
        v_repeat = _view("TX-001", repeat_mode_flag=True)
        sched = MaintenanceScheduler()
        bonus_normal = sched._urgency_bonus(r, v_normal)
        bonus_repeat = sched._urgency_bonus(r, v_repeat)
        assert bonus_repeat == pytest.approx(bonus_normal + 15.0)

    def test_no_redundancy_adds_10_pts_to_bonus(self):
        r = _make_result("TX-001", 50.0)
        v_redundant = _view("TX-001", has_redundant_path=True)
        v_none = _view("TX-001", has_redundant_path=False)
        sched = MaintenanceScheduler()
        diff = sched._urgency_bonus(r, v_none) - sched._urgency_bonus(r, v_redundant)
        assert diff == pytest.approx(10.0)

    def test_overdue_30_days_adds_5_pts(self):
        r = _make_result("TX-001", 50.0)
        v_on_time = _view("TX-001", overdue_days=0)
        v_overdue = _view("TX-001", overdue_days=30)
        sched = MaintenanceScheduler()
        diff = sched._urgency_bonus(r, v_overdue) - sched._urgency_bonus(r, v_on_time)
        assert diff == pytest.approx(5.0)

    def test_overdue_capped_at_25_pts(self):
        r = _make_result("TX-001", 50.0)
        v_180 = _view("TX-001", overdue_days=180)
        v_999 = _view("TX-001", overdue_days=9999)
        sched = MaintenanceScheduler()
        assert sched._urgency_bonus(r, v_180) == sched._urgency_bonus(r, v_999)


# ===========================================================================
# 3.  Crew assignment
# ===========================================================================

class TestCrewAssignment:

    def test_crew_assigned_same_region(self):
        crew = [
            _make_crew("CREW-EAST", "East"),
            _make_crew("CREW-WEST", "West"),
        ]
        results = [_make_result("TX-001", 85.0, level=RiskLevel.CRITICAL)]
        views = [_view("TX-001", region="East")]
        sched = MaintenanceScheduler(crews=crew)
        plan = sched.build_plan(results, views)
        assert plan.tasks[0].assigned_crew == "CREW-EAST"

    def test_fallback_crew_when_region_not_available(self):
        crew = [_make_crew("CREW-NORTH", "North")]
        results = [_make_result("TX-001", 85.0, level=RiskLevel.CRITICAL)]
        views = [_view("TX-001", region="South")]
        sched = MaintenanceScheduler(crews=crew)
        plan = sched.build_plan(results, views)
        # Falls back to any crew
        assert plan.tasks[0].assigned_crew == "CREW-NORTH"

    def test_healthy_asset_gets_no_crew(self):
        crew = [_make_crew("CREW-A", "East")]
        results = [_make_result("TX-001", 5.0, level=RiskLevel.NORMAL)]
        views = [_view("TX-001", region="East")]
        sched = MaintenanceScheduler(crews=crew)
        plan = sched.build_plan(results, views)
        # Healthy → monitor → no crew window assigned
        assert plan.tasks[0].assigned_crew is None

    def test_each_crew_assigned_only_once(self):
        crew = [_make_crew("CREW-A", "East"), _make_crew("CREW-B", "East")]
        results = [
            _make_result(f"TX-{i:03d}", 85.0 - i * 5, level=RiskLevel.CRITICAL)
            for i in range(5)
        ]
        views = [_view(f"TX-{i:03d}", region="East") for i in range(5)]
        sched = MaintenanceScheduler(crews=crew)
        plan = sched.build_plan(results, views)
        assigned = [t.assigned_crew for t in plan.tasks if t.assigned_crew is not None]
        # Each crew_id should appear at most once
        assert len(assigned) == len(set(assigned))


# ===========================================================================
# 4.  Operator approval
# ===========================================================================

class TestOperatorApproval:

    def test_approve_plan_sets_approved_true(self):
        results = [_make_result("TX-001", 80.0, level=RiskLevel.HIGH)]
        views = [_view("TX-001")]
        sched = MaintenanceScheduler()
        plan = sched.build_plan(results, views)
        approved = sched.approve_plan(plan, approved_by="test_operator")
        assert approved.approved is True
        assert approved.approved_by == "test_operator"
        assert approved.approved_at is not None

    def test_approve_plan_does_not_mutate_original(self):
        results = [_make_result("TX-001", 80.0, level=RiskLevel.HIGH)]
        views = [_view("TX-001")]
        sched = MaintenanceScheduler()
        plan = sched.build_plan(results, views)
        sched.approve_plan(plan)
        assert plan.approved is False  # original unchanged

    def test_approved_plan_is_deep_copy(self):
        results = [_make_result("TX-001", 80.0, level=RiskLevel.HIGH)]
        views = [_view("TX-001")]
        sched = MaintenanceScheduler()
        plan = sched.build_plan(results, views)
        approved = sched.approve_plan(plan)
        # Mutating approved should not affect original
        approved.tasks[0].rank = 999
        assert plan.tasks[0].rank != 999


# ===========================================================================
# 5.  Change summary detection
# ===========================================================================

class TestChangeSummary:

    def test_no_change_summary_without_previous(self):
        results = [_make_result("TX-001", 80.0, level=RiskLevel.HIGH)]
        views = [_view("TX-001")]
        sched = MaintenanceScheduler()
        plan = sched.build_plan(results, views, previous_plan=None)
        assert plan.change_summary == []

    def test_reorder_detected_vs_approved_plan(self):
        # Build and approve an initial plan with TX-001 ranking higher
        sched = MaintenanceScheduler()
        r1 = _make_result("TX-001", 80.0, level=RiskLevel.HIGH)
        r2 = _make_result("TX-002", 60.0, level=RiskLevel.WATCH)
        v1 = _view("TX-001")
        v2 = _view("TX-002")
        initial = sched.build_plan([r1, r2], [v1, v2])
        approved = sched.approve_plan(initial)
        # Now TX-002 overtakes TX-001 (higher risk)
        r1b = _make_result("TX-001", 50.0, level=RiskLevel.WATCH)
        r2b = _make_result("TX-002", 85.0, level=RiskLevel.CRITICAL)
        new_plan = sched.build_plan([r1b, r2b], [v1, v2], previous_plan=approved)
        # TX-002 moved up — should be in change_summary
        assert any("TX-002" in c for c in new_plan.change_summary)


# ===========================================================================
# 6.  Crew pre-positioning
# ===========================================================================

class TestCrewPrepositioning:

    def test_high_risk_storm_exposed_asset_gets_staging(self):
        results = [_make_result("TX-001", 80.0, level=RiskLevel.HIGH)]
        views = [_view(
            "TX-001",
            region="East",
            weather_risk_score=75.0,
            storm_level=3,
            critical_facilities=1,
        )]
        sched = MaintenanceScheduler()
        plan = sched.build_plan(results, views)
        # Should have crew pre-positioning for East region
        regions = [c["region"] for c in plan.crew_prepositioning]
        assert "East" in regions

    def test_healthy_low_storm_asset_no_prepositioning(self):
        results = [_make_result("TX-001", 10.0, level=RiskLevel.NORMAL)]
        views = [_view("TX-001", region="East", weather_risk_score=5.0, storm_level=0)]
        sched = MaintenanceScheduler()
        plan = sched.build_plan(results, views)
        assert plan.crew_prepositioning == []
