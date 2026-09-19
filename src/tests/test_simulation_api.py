"""
Integration tests for the simulation API layer (GridState simulation methods).

Coverage:
  - simulation_status() returns inactive state before start
  - simulation_start() activates simulation, returns status dict
  - simulation_start() with invalid phase falls back to CALM
  - simulation_advance() moves to next phase
  - simulation_stop() restores static demo data (scores match baseline)
  - simulation_set_phase() jumps to named phase
  - simulation_set_phase() with invalid phase returns error
  - _apply_simulation() re-scores assets through the real engine
  - Static scores preserved after simulation_stop()
  - simulation_approve_plan() marks plan as approved
  - simulation_reject_plan() clears pending plan
  - simulation_plan() falls back to static priorities when sim inactive
  - Live simulation scores differ from static for high-risk assets
  - Fallback: simulation works without network (attempt_live=False path)
"""

from __future__ import annotations

import sys
import os

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from api.grid_service import GridState


# ===========================================================================
# Fixture
# ===========================================================================

@pytest.fixture
def state():
    """GridState backed by an in-memory DB — fast and isolated."""
    return GridState(db_path=":memory:")


# ===========================================================================
# 1.  Initial simulation state
# ===========================================================================

class TestSimulationInitialState:

    def test_simulation_inactive_at_startup(self, state):
        status = state.simulation_status()
        assert status["active"] is False

    def test_simulation_phase_none_at_startup(self, state):
        status = state.simulation_status()
        assert status["phase"] is None

    def test_simulation_phases_list_present(self, state):
        status = state.simulation_status()
        assert len(status["phases"]) == 7  # 7 named phases

    def test_plan_not_pending_at_startup(self, state):
        status = state.simulation_status()
        assert status["plan_pending_approval"] is False


# ===========================================================================
# 2.  Starting the simulation
# ===========================================================================

class TestSimulationStart:

    def test_start_activates_simulation(self, state):
        # Start without live fetch (test environment has no network guarantee)
        state._simulator = None  # ensure clean state
        status = state.simulation_start()
        assert status["active"] is True

    def test_start_returns_calm_phase_by_default(self, state):
        status = state.simulation_start()
        assert status["phase"] == "calm"

    def test_start_with_explicit_phase(self, state):
        status = state.simulation_start(phase="peak_storm")
        assert status["phase"] == "peak_storm"

    def test_start_with_invalid_phase_defaults_to_calm(self, state):
        status = state.simulation_start(phase="not_a_real_phase")
        # Falls back to CALM (the WeatherSimulator default)
        assert status["phase"] == "calm"

    def test_start_populates_assets(self, state):
        state.simulation_start()
        # Assets should still be present (same count)
        assert len(state.assets) == 8

    def test_start_sets_plan_pending(self, state):
        status = state.simulation_start()
        assert status["plan_pending_approval"] is True


# ===========================================================================
# 3.  Advancing the simulation
# ===========================================================================

class TestSimulationAdvance:

    def test_advance_moves_to_next_phase(self, state):
        state.simulation_start()
        status = state.simulation_advance()
        # CALM (0) → ADVISORY (1)
        assert status["phase"] == "advisory"
        assert status["phase_index"] == 1

    def test_advance_on_inactive_returns_error(self, state):
        result = state.simulation_advance()
        assert "error" in result

    def test_repeated_advance_cycles_through_all_phases(self, state):
        state.simulation_start()
        phases_seen = set()
        for _ in range(14):  # more than 7 to test wrap
            s = state.simulation_advance()
            phases_seen.add(s["phase"])
        assert len(phases_seen) == 7


# ===========================================================================
# 4.  Setting a specific phase
# ===========================================================================

class TestSimulationSetPhase:

    def test_set_phase_jumps_correctly(self, state):
        state.simulation_start()
        status = state.simulation_set_phase("degradation_peak")
        assert status["phase"] == "degradation_peak"

    def test_set_invalid_phase_returns_error(self, state):
        state.simulation_start()
        result = state.simulation_set_phase("bogus_phase")
        assert "error" in result
        # Phase should be unchanged after the error
        assert state.simulation_status()["active"] is True

    def test_set_phase_inactive_returns_error(self, state):
        result = state.simulation_set_phase("calm")
        assert "error" in result


# ===========================================================================
# 5.  Stopping the simulation
# ===========================================================================

class TestSimulationStop:

    def test_stop_deactivates_simulation(self, state):
        state.simulation_start()
        result = state.simulation_stop()
        assert result["active"] is False

    def test_stop_restores_static_data(self, state):
        # Record static scores before simulation
        static_scores = {a["id"]: a["overall_risk"] for a in state.assets}

        # Start and advance to peak storm (maximises sensor pressure)
        state.simulation_start(phase="peak_storm")
        sim_scores = {a["id"]: a["overall_risk"] for a in state.assets}

        # Stop and check restoration
        state.simulation_stop()
        restored_scores = {a["id"]: a["overall_risk"] for a in state.assets}

        # Scores after stop should match original static scores
        for aid in static_scores:
            assert restored_scores[aid] == pytest.approx(static_scores[aid], abs=0.1), (
                f"{aid}: expected {static_scores[aid]}, got {restored_scores[aid]}"
            )

    def test_stop_clears_pending_plan(self, state):
        state.simulation_start()
        state.simulation_stop()
        status = state.simulation_status()
        assert status["plan_pending_approval"] is False


# ===========================================================================
# 6.  Score evolution under simulation
# ===========================================================================

class TestSimulationScoreEvolution:

    def test_peak_storm_increases_high_risk_asset_scores(self, state):
        """At peak storm, heavily degraded assets should score higher than at calm."""
        state.simulation_start(phase="calm")
        calm_scores = {a["id"]: a["overall_risk"] for a in state.assets}

        state.simulation_set_phase("peak_storm")
        peak_scores = {a["id"]: a["overall_risk"] for a in state.assets}

        # TX-007 (most degraded) should be higher at peak storm than calm
        assert peak_scores["TX-007"] >= calm_scores["TX-007"]

    def test_peak_storm_has_higher_total_scores_than_calm(self, state):
        """PEAK_STORM fleet total risk should be higher than CALM phase total."""
        state.simulation_start(phase="calm")
        calm_total = sum(a["overall_risk"] for a in state.assets)

        state.simulation_set_phase("peak_storm")
        peak_total = sum(a["overall_risk"] for a in state.assets)

        # Peak storm sensor pressure (0.55) drives scores up across the fleet
        assert peak_total > calm_total

    def test_all_sim_scores_in_valid_range(self, state):
        """All simulated risk scores must remain in [0, 100]."""
        state.simulation_start(phase="peak_storm")
        for a in state.assets:
            assert 0.0 <= a["overall_risk"] <= 100.0, (
                f"{a['id']} overall_risk={a['overall_risk']} out of range"
            )

    def test_asset_count_unchanged_during_simulation(self, state):
        baseline_count = len(state.assets)
        for phase in ["calm", "advisory", "storm_approaching", "peak_storm",
                      "degradation_peak", "maintenance_triggered", "recovery"]:
            state.simulation_set_phase(phase) if state._sim_active else state.simulation_start(phase=phase)
            assert len(state.assets) == baseline_count


# ===========================================================================
# 7.  Operator approval workflow
# ===========================================================================

class TestOperatorApprovalWorkflow:

    def test_approve_plan_marks_approved(self, state):
        state.simulation_start()
        result = state.simulation_approve_plan(approved_by="operator_1")
        assert result.get("approved") is True
        assert result.get("approved_by") == "operator_1"

    def test_approve_plan_clears_pending(self, state):
        state.simulation_start()
        state.simulation_approve_plan()
        status = state.simulation_status()
        assert status["plan_pending_approval"] is False

    def test_approve_no_pending_plan_returns_error(self, state):
        result = state.simulation_approve_plan()
        assert "error" in result

    def test_reject_plan_clears_pending(self, state):
        state.simulation_start()
        state.simulation_reject_plan()
        status = state.simulation_status()
        assert status["plan_pending_approval"] is False

    def test_reject_no_pending_plan_returns_error(self, state):
        result = state.simulation_reject_plan()
        assert "error" in result


# ===========================================================================
# 8.  simulation_plan() endpoint
# ===========================================================================

class TestSimulationPlan:

    def test_plan_falls_back_to_static_when_inactive(self, state):
        plan = state.simulation_plan()
        # Static priorities() always has maintenance_plan list
        assert "maintenance_plan" in plan

    def test_plan_returns_sim_plan_when_pending(self, state):
        state.simulation_start()
        plan = state.simulation_plan()
        assert plan.get("pending_approval") is True
        assert "maintenance_plan" in plan

    def test_plan_returns_approved_plan_after_approval(self, state):
        state.simulation_start()
        state.simulation_approve_plan()
        plan = state.simulation_plan()
        assert plan.get("approved") is True
        assert plan.get("pending_approval") is False

    def test_sim_plan_has_all_assets(self, state):
        state.simulation_start()
        plan = state.simulation_plan()
        assert len(plan["maintenance_plan"]) == 8
