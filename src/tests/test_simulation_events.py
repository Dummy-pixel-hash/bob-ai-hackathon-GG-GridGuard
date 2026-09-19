"""
Tests for simulation change events and maintenance-plan deltas.

The frontend visualises simulation transitions (map marker flashes,
escalation highlights, maintenance reorder badges, event feed) from two
backend state payloads:

  1. ``changes`` — per-asset transitions since the previous scoring snapshot,
     exposed (consumed-on-read) by ``simulation_status()`` and therefore by
     ``/api/scenario/tick`` and the manual simulation endpoints.
  2. ``task_changes`` — per-task rank/status/urgency/crew/risk deltas between
     the current simulation plan and the previously served plan, exposed by
     ``simulation_plan()``.

Coverage:
  - Event payload shape and internal consistency (delta arithmetic, direction)
  - Band transitions flagged (band_change / status_change)
  - newly_high_critical matches an actual crossing into High/Critical
  - Consume-on-read: a second status read returns no events
  - Events cleared on simulation_stop()
  - Inactive status carries an empty changes list
  - scenario_tick exposes changes after the runner advances in time
  - First sim plan diffs against the static baseline (crew assignments flagged)
  - Plan deltas are stable across repeated reads
  - Plan task rank_to matches the task's current rank in the served plan
  - Approve clears pending deltas; reject falls back to the static plan
  - Static priorities() shape is unchanged (no task_changes key)
"""

from __future__ import annotations

import sys
import os

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from api.grid_service import GridState
from simulation.scenario_runner import SCENARIOS


# ===========================================================================
# Fixture
# ===========================================================================

@pytest.fixture
def state():
    """GridState backed by an in-memory DB — fast and isolated."""
    return GridState(db_path=":memory:")


# ===========================================================================
# Helpers
# ===========================================================================

_EVENT_KEYS = {
    "asset_id",
    "previous_status", "new_status",
    "previous_risk", "new_risk", "risk_delta",
    "direction", "band_change", "status_change",
    "newly_high_critical", "cleared_alert",
    "dominant_factor", "dominant_factor_label",
}

_TASK_CHANGE_KEYS = {
    "asset_id",
    "rank_from", "rank_to", "rank_delta",
    "moved_up", "moved_down",
    "status_from", "status_to",
    "urgency_from", "urgency_to",
    "crew_from", "crew_to",
    "risk_from", "risk_to", "risk_delta",
    "newly_high_critical", "scheduling_reason",
}


def _known_ids(state):
    return {a["id"] for a in state.assets}


# ===========================================================================
# 1.  Change-event payload
# ===========================================================================

class TestChangeEventPayload:

    def test_start_calm_emits_changes(self, state):
        """Starting the sim re-scores the fleet under calm weather — scores
        move relative to the static stormy baseline, so events exist."""
        status = state.simulation_start(phase="calm")
        assert len(status["changes"]) > 0

    def test_event_shape_and_consistency(self, state):
        state.simulation_start(phase="calm")
        status = state.simulation_set_phase("peak_storm")
        assert len(status["changes"]) > 0
        for ev in status["changes"]:
            assert _EVENT_KEYS <= set(ev.keys()), f"missing keys: {ev}"
            assert ev["asset_id"] in _known_ids(state)
            # Delta arithmetic must be exact (previous + delta == new).
            assert ev["new_risk"] == pytest.approx(
                ev["previous_risk"] + ev["risk_delta"], abs=0.11
            )
            # Direction must agree with the sign of the delta.
            if ev["risk_delta"] > 0:
                assert ev["direction"] == "rising"
            elif ev["risk_delta"] < 0:
                assert ev["direction"] == "falling"
            else:
                assert ev["direction"] == "flat"
            # Band/status flags must agree with the values they describe.
            assert ev["status_change"] == (ev["previous_status"] != ev["new_status"])

    def test_band_change_matches_status_transition(self, state):
        state.simulation_start(phase="calm")
        calm = {a["id"]: a["status"] for a in state.assets}
        status = state.simulation_set_phase("peak_storm")
        new = {a["id"]: a["status"] for a in state.assets}
        by_id = {e["asset_id"]: e for e in status["changes"]}
        for aid, old in calm.items():
            if old != new[aid]:
                assert aid in by_id, f"{aid} changed status but has no event"
                ev = by_id[aid]
                assert ev["previous_status"] == old
                assert ev["new_status"] == new[aid]
                assert ev["band_change"] is True or ev["status_change"] is True

    def test_newly_high_critical_flag(self, state):
        """Advisory→peak ramp must newly elevate at least one asset into
        High/Critical, and the flag must exactly match the crossing."""
        state.simulation_start(phase="calm")
        status = state.simulation_set_phase("peak_storm")
        assert any(e["newly_high_critical"] for e in status["changes"]), (
            "expected at least one newly elevated asset at peak storm"
        )
        for ev in status["changes"]:
            crossed_up = (
                ev["previous_status"] not in ("High", "Critical")
                and ev["new_status"] in ("High", "Critical")
            )
            assert ev["newly_high_critical"] == crossed_up

    def test_dominant_factor_is_engine_grounded(self, state):
        state.simulation_start(phase="calm")
        status = state.simulation_set_phase("peak_storm")
        by_id = {a["id"]: a for a in state.assets}
        for ev in status["changes"]:
            view = by_id[ev["asset_id"]]
            assert ev["dominant_factor"] == view["dominant_factor"]
            assert ev["dominant_factor_label"] == view["dominant_factor_label"]

    def test_quiet_step_emits_no_events(self, state):
        """Re-applying the same phase scores identically — no events."""
        state.simulation_start(phase="calm")
        state.simulation_status()  # consume the start events
        again = state.simulation_set_phase("calm")
        assert again["changes"] == []


# ===========================================================================
# 2.  Consume-on-read semantics
# ===========================================================================

class TestConsumeOnRead:

    def test_second_read_returns_empty(self, state):
        # The start response itself carries (and consumes) the start events.
        started = state.simulation_start(phase="calm")
        assert len(started["changes"]) > 0
        assert state.simulation_status()["changes"] == []

    def test_advance_accumulates_until_read(self, state):
        started = state.simulation_start(phase="calm")
        assert len(started["changes"]) > 0
        # The advance response itself carries that step's events…
        advanced = state.simulation_advance()  # calm -> advisory
        assert len(advanced["changes"]) > 0
        # …and the following explicit read is empty until the next step.
        assert state.simulation_status()["changes"] == []

    def test_stop_clears_pending_events(self, state):
        state.simulation_start(phase="calm")
        state.simulation_stop()
        assert state.simulation_status()["changes"] == []

    def test_inactive_status_has_empty_changes(self, state):
        status = state.simulation_status()
        assert status["active"] is False
        assert status["changes"] == []


# ===========================================================================
# 3.  Scenario-tick change events
# ===========================================================================

class TestScenarioTickChanges:

    def _jump_runner_forward(self, gs, seconds: float) -> None:
        """Force the runner's clock forward so tick() sees a later state."""
        assert gs._runner is not None
        gs._runner._start_time = gs._runner._now() - seconds

    def test_scenario_run_response_carries_changes_key(self, state):
        result = state.scenario_run(scenario_id="storm_surge")
        assert result.get("active") is True
        assert "changes" in result
        assert isinstance(result["changes"], list)

    def test_tick_after_time_advance_exposes_changes(self, state):
        state.scenario_run(scenario_id="storm_surge")
        # Consume the initial-step events…
        state.simulation_status()
        # …then jump into the peak-storm steps and tick.
        steps = SCENARIOS["storm_surge"]["steps"]
        total = sum(s.duration_s for s in steps[:4])
        self._jump_runner_forward(state, total + 0.5)
        tick = state.scenario_tick()
        changes = tick.get("changes", [])
        assert len(changes) > 0
        for ev in changes:
            assert _EVENT_KEYS <= set(ev.keys())
        # The fleet is under rising pressure mid-scenario — some asset must
        # be climbing relative to the step-0 snapshot.
        assert any(e["direction"] == "rising" for e in changes)

    def test_tick_when_inactive_has_no_changes(self, state):
        tick = state.scenario_tick()
        assert tick.get("scenario_active") is False


# ===========================================================================
# 4.  Maintenance-plan task deltas
# ===========================================================================

class TestPlanTaskChanges:

    def test_first_sim_plan_diffs_against_static(self, state):
        """On start, crews get assigned by the scheduler (static rows have no
        crew) — those assignments must appear as crew transitions."""
        state.simulation_start(phase="calm")
        plan = state.simulation_plan()
        assert plan.get("pending_approval") is True
        changes = plan["task_changes"]
        assert len(changes) > 0
        # At least one top task gained a crew assignment vs. the static plan.
        assert any(
            c["crew_from"] is None and c["crew_to"] is not None
            for c in changes
        )

    def test_task_change_shape(self, state):
        state.simulation_start(phase="calm")
        plan = state.simulation_plan()
        for c in plan["task_changes"]:
            assert _TASK_CHANGE_KEYS <= set(c.keys()), f"missing keys: {c}"
            assert c["asset_id"] in _known_ids(state)

    def test_rank_to_matches_served_plan(self, state):
        state.simulation_start(phase="calm")
        plan = state.simulation_plan()
        rank_by_id = {t["asset_id"]: t["rank"] for t in plan["maintenance_plan"]}
        for c in plan["task_changes"]:
            assert c["rank_to"] == rank_by_id[c["asset_id"]]

    def test_peak_storm_promotes_tx003(self, state):
        """Calm → peak storm: TX-003 climbs from Healthy #6 to Monitoring #5
        and gains a crew assignment.  Deterministic end-to-end check that the
        scheduler reorder is surfaced to the UI."""
        state.simulation_start(phase="calm")
        # Establishes the calm sim plan as the comparison baseline for the
        # next scheduler run (baseline advances on build, not on read).
        state.simulation_plan()
        state.simulation_set_phase("peak_storm")
        plan = state.simulation_plan()
        by_id = {c["asset_id"]: c for c in plan["task_changes"]}
        assert "TX-003" in by_id
        tx003 = by_id["TX-003"]
        assert tx003["moved_up"] is True
        assert tx003["rank_from"] == 6 and tx003["rank_to"] == 5
        assert tx003["status_from"] == "Healthy"
        assert tx003["status_to"] == "Monitoring"
        assert tx003["crew_from"] is None
        assert tx003["crew_to"] is not None

    def test_deltas_stable_across_reads(self, state):
        state.simulation_start(phase="calm")
        state.simulation_set_phase("peak_storm")
        first = state.simulation_plan()["task_changes"]
        second = state.simulation_plan()["task_changes"]
        assert first == second

    def test_approve_clears_deltas(self, state):
        state.simulation_start(phase="calm")
        state.simulation_set_phase("peak_storm")
        assert len(state.simulation_plan()["task_changes"]) > 0
        state.simulation_approve_plan()
        plan = state.simulation_plan()
        assert plan.get("approved") is True
        assert plan["task_changes"] == []

    def test_reject_falls_back_to_static(self, state):
        state.simulation_start(phase="calm")
        state.simulation_reject_plan()
        plan = state.simulation_plan()
        # No pending sim plan and nothing approved → static priorities shape.
        assert "task_changes" not in plan
        assert "maintenance_plan" in plan

    def test_static_priorities_shape_unchanged(self, state):
        plan = state.priorities()
        assert "task_changes" not in plan
        assert len(plan["maintenance_plan"]) == 8
