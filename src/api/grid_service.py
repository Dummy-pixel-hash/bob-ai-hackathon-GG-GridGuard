"""GridGuard API package — thin read/serve layer over the existing backend.

This package does NOT reimplement any backend logic.  It only:

1. Loads the synthetic demo records from ``data.demo_assets``.
2. Uses the staged static demo weather by default so the demo is
   deterministic (opt-in live Open-Meteo refresh via
   ``GRIDGUARD_LIVE_WEATHER=1``; best-effort per asset, static kept on
   network failure).
3. Normalises them with ``normalisation.normaliser.normalise``.
4. Scores them with ``risk_engine.calculator.score_asset``.
5. Seeds / reads the existing SQLite storage layer (assets, topology,
   lifecycle, risk snapshots).
6. Answers operator questions through the existing
   ``ai_briefing.BriefingService`` provider abstraction.

All numbers served to the frontend therefore come straight from the real
risk engine — nothing is hardcoded for display purposes.
"""

from __future__ import annotations

import copy
import os
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

from ai_briefing import (
    BriefingConfig,
    BriefingService,
    BriefingType,
    ConversationTurn,
)
from ai_briefing.context_builder import AssetRegistryInfo, BriefingContextBuilder
from data.demo_assets import ALL_ASSETS
from data.raw_types import RawAssetRecord
from maintenance.scheduler import MaintenancePlan, MaintenanceScheduler, ScheduledTask
from normalisation.normaliser import normalise
from risk_engine.calculator import score_asset
from risk_engine.models import RiskInputs, RiskResult
from simulation.asset_sim import apply_weather_to_asset
from simulation.scenario_runner import (
    DEFAULT_SCENARIO,
    SCENARIOS,
    ScenarioRunner,
    StepState,
)
from simulation.weather_sim import DEMO_PHASES, SimulationPhase, WeatherSimulator
from storage.repositories.assets import AssetRepository
from storage.repositories.grid_topology import GridTopologyRepository
from storage.repositories.lifecycle import LifecycleRepository
from storage.repositories.retired import RetiredAssetRepository as RetiredRepository
from storage.repositories.risk_results import RiskResultRepository
from storage.schema import create_all_tables
from storage.seeder import seed_demo_assets
from storage.thread_local_conn import ThreadLocalConnFactory
from weather.open_meteo import fetch_weather_with_fallback


# ---------------------------------------------------------------------------
# Minimal .env loader (stdlib only — no python-dotenv dependency)
# ---------------------------------------------------------------------------

def _parse_dotenv(path: str) -> dict[str, str]:
    """Parse a KEY=VALUE dotenv file; ignore blanks, comments, and `export `."""
    values: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key and key.replace("_", "").isalnum():
                values[key] = val
    return values


def load_dotenv() -> list[str]:
    """Load GridGuard env vars from .env files into os.environ.

    Candidate files (all that exist are applied, later ones win):
      1. ``src/.env`` next to this package's source tree
      2. ``.env`` at the repository root
      3. ``$GRIDGUARD_ENV`` if set (explicit override, wins over both)

    Real environment variables always win — files only fill in what is
    unset.  Returns the list of files that were actually loaded.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.dirname(here)
    repo_root = os.path.dirname(src_dir)
    candidates = [
        os.path.join(src_dir, ".env"),
        os.path.join(repo_root, ".env"),
    ]
    explicit = os.getenv("GRIDGUARD_ENV", "")
    if explicit:
        candidates.append(explicit)
    loaded: list[str] = []
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            for key, val in _parse_dotenv(path).items():
                if key not in os.environ:
                    os.environ[key] = val
            loaded.append(path)
        except OSError:
            continue
    return loaded


# ---------------------------------------------------------------------------
# Status mapping: engine RiskLevel -> operator-facing status
# ---------------------------------------------------------------------------

LEVEL_TO_STATUS = {
    "Normal": "Healthy",
    "Watch": "Monitoring",
    "High": "High",
    "Critical": "Critical",
}

STATUS_ORDER = ["Critical", "High", "Monitoring", "Healthy"]

COMPONENT_LABELS = {
    "sensor_health": "Sensor health",
    "weather_risk": "Weather risk",
    "historical_failure": "Historical failure",
    "asset_degradation": "Asset degradation",
    "grid_impact": "Grid impact",
}

COMPONENT_WEIGHTS = {
    "sensor_health": 0.30,
    "weather_risk": 0.20,
    "historical_failure": 0.15,
    "asset_degradation": 0.15,
    "grid_impact": 0.20,
}

_ACTION_BY_STATUS = {
    "Critical": ("Inspect today", "Immediate — dispatch crew within 24h"),
    "High": ("Inspect this week", "Priority — schedule within 7 days"),
    "Monitoring": ("Plan inspection", "Routine — schedule within 30 days"),
    "Healthy": ("Routine monitoring", "No action — next scheduled check"),
}


# ---------------------------------------------------------------------------
# Simulation change-event helpers (presentation of backend truth)
# ---------------------------------------------------------------------------
# The frontend polls /api/scenario/tick every few seconds while a scenario
# runs.  To let the operator *see* what changed without reading logs,
# GridState diffs consecutive scoring snapshots and exposes the per-asset
# transitions as a consumed-on-read ``changes`` list.  This is presentation
# only — it never invents coordinates, scores, or risk values; every field
# is derived from engine RiskResult outputs.
# ---------------------------------------------------------------------------

#: Minimum |overall_risk delta| that counts as a visible change event.
_CHANGE_RISK_DELTA_THRESHOLD = 0.5

#: Cap on unconsumed pending events (oldest are dropped first).
_MAX_PENDING_CHANGE_EVENTS = 200

#: Statuses that mark an asset as an elevated operational priority.
_ALERT_STATUSES = ("High", "Critical")

#: Urgency implied by status for static (non-scheduler) plan rows, mirroring
#: the scheduler's _ACTION_BY_STATUS mapping so diffs can span both shapes.
_STATIC_URGENCY = {
    "Critical": "immediate",
    "High": "priority",
    "Monitoring": "routine",
    "Healthy": "monitor",
}


def _scheduler_plan_view(tasks: list) -> dict[str, dict]:
    """Normalise scheduler ``ScheduledTask`` list to a per-asset view."""
    return {
        t.asset_id: {
            "rank": t.rank,
            "status": t.status,
            "urgency": t.urgency,
            "assigned_crew": t.assigned_crew,
            "overall_risk": t.overall_risk,
        }
        for t in tasks
    }


def _static_plan_view(priorities_payload: dict) -> dict[str, dict]:
    """Normalise static ``priorities()`` rows to the same per-asset view."""
    view: dict[str, dict] = {}
    for row in priorities_payload.get("maintenance_plan", []):
        view[row["asset_id"]] = {
            "rank": row["rank"],
            "status": row["status"],
            "urgency": _STATIC_URGENCY.get(row["status"], "monitor"),
            "assigned_crew": None,
            "overall_risk": row["overall_risk"],
        }
    return view


def _snapshot_asset_views(assets: list[dict]) -> dict[str, dict]:
    """Compact per-asset snapshot used for consecutive-state diffing."""
    snap: dict[str, dict] = {}
    for a in assets:
        snap[a["id"]] = {
            "status": a["status"],
            "risk_level": a.get("risk_level"),
            "overall_risk": a["overall_risk"],
            "dominant_factor": a.get("dominant_factor"),
            "dominant_factor_label": a.get("dominant_factor_label"),
        }
    return snap


def _diff_asset_changes(
    prev: dict[str, dict], new_assets: list[dict]
) -> list[dict]:
    """Diff two consecutive scoring snapshots into operator-facing events.

    An event is emitted when an asset's risk band/status changed, or its
    overall risk moved by at least ``_CHANGE_RISK_DELTA_THRESHOLD``.  Newly
    elevated priorities (Monitoring/Healthy → High/Critical) and cleared
    alerts are flagged explicitly so the UI can highlight them.
    """
    events: list[dict] = []
    for a in new_assets:
        p = prev.get(a["id"])
        if p is None:
            continue
        new_risk = a["overall_risk"]
        old_risk = p["overall_risk"]
        delta = round(new_risk - old_risk, 1)
        band_change = a.get("risk_level") != p.get("risk_level")
        status_change = a["status"] != p["status"]
        if not (band_change or status_change or abs(delta) >= _CHANGE_RISK_DELTA_THRESHOLD):
            continue
        elevated = a["status"] in _ALERT_STATUSES
        was_elevated = p["status"] in _ALERT_STATUSES
        events.append(
            {
                "asset_id": a["id"],
                "previous_status": p["status"],
                "new_status": a["status"],
                "previous_risk": old_risk,
                "new_risk": new_risk,
                "risk_delta": delta,
                "direction": "rising" if delta > 0 else ("falling" if delta < 0 else "flat"),
                "band_change": band_change,
                "status_change": status_change,
                "newly_high_critical": bool(elevated and not was_elevated),
                "cleared_alert": bool(was_elevated and not elevated),
                "dominant_factor": a.get("dominant_factor"),
                "dominant_factor_label": a.get("dominant_factor_label"),
            }
        )
    # Deterministic order: escalations first, then the biggest movers.
    events.sort(
        key=lambda e: (
            0 if e["newly_high_critical"] else (1 if e["band_change"] else 2),
            -abs(e["risk_delta"]),
            e["asset_id"],
        )
    )
    return events


# ---------------------------------------------------------------------------
# Live weather refresh helper (opt-in — demo is static by default)
# ---------------------------------------------------------------------------

def _live_weather_enabled() -> bool:
    """True only when the operator explicitly opts into live weather.

    The staged static demo weather is the default so that scores, bands,
    and crew recommendations are reproducible for every run and recording.
    Set ``GRIDGUARD_LIVE_WEATHER=1`` to attempt a best-effort live
    Open-Meteo refresh per asset instead.
    """
    return os.getenv("GRIDGUARD_LIVE_WEATHER", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _refresh_asset_weather(
    assets: list[RawAssetRecord],
) -> tuple[list[RawAssetRecord], dict[str, bool]]:
    """
    Attempt to replace each asset's static demo weather with a live Open-Meteo
    72-hour forecast for its geographic location.

    Best-effort: if the fetch fails for any individual asset (network error,
    API timeout, parse failure), that asset keeps its existing static weather.
    Never raises — startup must not fail due to weather unavailability.

    Returns ``(records, live_flags)`` where ``live_flags`` maps asset_id to
    True when that asset's weather came from the API.  Originals are not
    mutated.
    """
    refreshed: list[RawAssetRecord] = []
    live: dict[str, bool] = {}
    for raw in assets:
        loc = raw.metadata.location
        obs, is_live = fetch_weather_with_fallback(
            lat=loc.latitude,
            lon=loc.longitude,
            fallback=raw.weather,
            forecast_hours=72,
            timeout_s=8.0,
        )
        if is_live:
            # Replace weather on a shallow copy of the record (other fields unchanged)
            updated = copy.copy(raw)
            updated.weather = obs
            refreshed.append(updated)
        else:
            refreshed.append(raw)
        live[raw.metadata.asset_id] = is_live
    return refreshed, live


# ---------------------------------------------------------------------------
# Grid state
# ---------------------------------------------------------------------------

class GridState:
    """All UI-facing data, computed once at startup from the real backend."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        # Pick up LLM keys / model from .env files (env vars always win).
        self.env_files: list[str] = load_dotenv()

        # ── Thread-safe database access ──────────────────────────────────────
        # ThreadLocalConnFactory provides the correct sqlite3.Connection for
        # the calling thread.  For file-backed databases each thread gets its
        # own connection (SQLite WAL handles concurrent access).  For
        # ":memory:" a single shared connection is used with a threading.Lock.
        # This eliminates the "SQLite objects created in a thread can only be
        # used in that same thread" crash in ThreadingHTTPServer.
        self._db = ThreadLocalConnFactory(db_path)

        # Schema + seed run on the main thread via acquire/release so the data
        # is visible to subsequent reads from the same connection (memory) or
        # file (file-backed).
        with self._db.connection() as conn:
            create_all_tables(conn)
            seed_demo_assets(conn)

        # Weather: staged static demo data by default (deterministic demo).
        # Live Open-Meteo refresh only when explicitly opted in via
        # GRIDGUARD_LIVE_WEATHER=1; per-asset fallback keeps static data
        # whenever the network is unavailable.
        if _live_weather_enabled():
            live_assets, self.weather_live = _refresh_asset_weather(list(ALL_ASSETS))
        else:
            live_assets = list(ALL_ASSETS)
            self.weather_live = {
                r.metadata.asset_id: False for r in ALL_ASSETS
            }

        # Keep raw records by id for telemetry/weather detail.
        # (self.weather_live was populated above: True per asset iff that
        # asset's weather came from the live API.)
        self._raw: dict[str, RawAssetRecord] = {
            r.metadata.asset_id: r for r in live_assets
        }
        # Keep the original (unmodified) static records for simulation fallback.
        self._raw_static: dict[str, RawAssetRecord] = dict(self._raw)

        # Normalised inputs + engine results by id.
        self._inputs: dict[str, RiskInputs] = {}
        self._results: dict[str, RiskResult] = {}

        with self._db.connection() as conn:
            risk_repo = RiskResultRepository(conn)
            for raw in live_assets:
                inputs = normalise(raw)
                result = score_asset(inputs)
                self._inputs[raw.metadata.asset_id] = inputs
                self._results[raw.metadata.asset_id] = result
                # Persist a scoring snapshot (storage layer stays the audit trail).
                try:
                    risk_repo.insert(result)
                except Exception:
                    pass

        self._assets: list[dict] = [
            self._build_asset_view(aid) for aid in sorted(self._raw)
        ]
        # ── Change-event tracking (operator-facing event feed) ───────────────
        # _prev_asset_snapshots holds the last *scored* state; every
        # _apply_simulation*() call diffs the new scores against it and
        # appends events to _pending_changes, which are consumed on read by
        # simulation_status() (→ scenario_tick → frontend poll).
        self._prev_asset_snapshots: dict[str, dict] = _snapshot_asset_views(
            self._assets
        )
        self._pending_changes: list[dict] = []
        # Comparison baseline for maintenance-plan diffs: a normalised view of
        # the last plan the operator saw (approved, sim, or static).  Advanced
        # only on plan transitions (build/approve/reject/stop) — never on
        # read — so consecutive polls show a stable diff until the next
        # scheduler run produces a new plan.
        self._plan_baseline: dict[str, dict] | None = None
        self._briefing_service: BriefingService | None = None
        self._provider_warning: str = ""

        # ── Simulation layer (off by default — static demo is the fallback) ──
        # The simulator advances through named phases.  When active=True the
        # assets/priorities endpoints serve simulation-adjusted data.  When
        # active=False the original static demo data is served unchanged.
        self._sim_active: bool = False
        self._simulator: WeatherSimulator | None = None
        self._sim_plan: MaintenancePlan | None = None
        self._approved_plan: MaintenancePlan | None = None
        # Effective_pressure per asset_id from the last simulation step
        self._sim_pressure: dict[str, float] = {}

        # ── Scenario runner (autonomous timed simulation) ─────────────────────
        # When a scenario is running the runner tracks wall-clock elapsed time
        # and the tick() method returns interpolated StepState values that
        # drive _apply_simulation_step().
        self._runner: ScenarioRunner | None = None
        self._last_step: StepState | None = None

    # -- public accessors -------------------------------------------------

    @property
    def assets(self) -> list[dict]:
        return self._assets

    def get_asset(self, asset_id: str) -> dict | None:
        for a in self._assets:
            if a["id"] == asset_id:
                return a
        return None

    def summary(self) -> dict:
        counts = {"Healthy": 0, "Monitoring": 0, "High": 0, "Critical": 0}
        for a in self._assets:
            counts[a["status"]] += 1
        risks = [a["overall_risk"] for a in self._assets]
        customers_at_risk = sum(
            a["grid_impact"]["customers_served"]
            for a in self._assets
            if a["status"] in ("High", "Critical")
        )
        critical_facilities = sum(
            a["grid_impact"]["critical_facility_count"]
            for a in self._assets
            if a["status"] in ("High", "Critical")
        )
        regions: dict[str, dict] = {}
        for a in self._assets:
            r = regions.setdefault(
                a["region"], {"assets": 0, "worst_risk": 0.0, "worst_status": "Healthy"}
            )
            r["assets"] += 1
            if a["overall_risk"] > r["worst_risk"]:
                r["worst_risk"] = a["overall_risk"]
                r["worst_status"] = a["status"]
        return {
            "total": len(self._assets),
            "counts": counts,
            "average_risk": round(sum(risks) / len(risks), 1) if risks else 0.0,
            "highest_risk": max(risks) if risks else 0.0,
            "customers_at_risk": customers_at_risk,
            "critical_facilities_exposed": critical_facilities,
            "regions": regions,
        }

    def priorities(self) -> dict:
        """Ranked maintenance plan + crew pre-positioning (derived, not stored).

        Ranking is deterministic: overall engine risk first, grid-impact
        component as the tie-break so that a failure which hurts the grid
        most is worked first.  No scoring logic lives here — this only
        presents ``RiskResult`` outputs as an operator work plan.
        """
        ranked = sorted(
            self._assets,
            key=lambda a: (a["overall_risk"], a["components"]["grid_impact"]),
            reverse=True,
        )
        plan = []
        for i, a in enumerate(ranked, start=1):
            action, detail = _ACTION_BY_STATUS[a["status"]]
            plan.append(
                {
                    "rank": i,
                    "asset_id": a["id"],
                    "substation": a["substation"],
                    "region": a["region"],
                    "status": a["status"],
                    "overall_risk": a["overall_risk"],
                    "dominant_factor": a["dominant_factor"],
                    "dominant_factor_label": a["dominant_factor_label"],
                    "customers_served": a["grid_impact"]["customers_served"],
                    "critical_facilities": a["grid_impact"][
                        "critical_facility_count"
                    ],
                    "has_redundant_path": a["grid_impact"]["has_redundant_path"],
                    "recommended_action": action,
                    "action_detail": detail,
                }
            )
        # Crew pre-positioning: weather-exposed, high-consequence assets.
        crew: dict[str, dict] = {}
        for a in self._assets:
            exposed = (
                a["components"]["weather_risk"] >= 60.0
                or a["weather_raw"]["storm_warning_level"] >= 2
            )
            consequential = (
                a["overall_risk"] >= 70.0
                or a["grid_impact"]["critical_facility_count"] > 0
            )
            if exposed and consequential:
                cell = crew.setdefault(
                    a["region"], {"region": a["region"], "assets": [], "reason": ""}
                )
                cell["assets"].append(a["id"])
        crew_list = []
        for region, cell in crew.items():
            worst = max(
                (x for x in self._assets if x["id"] in cell["assets"]),
                key=lambda x: x["overall_risk"],
            )
            cell["reason"] = (
                f"Storm exposure (weather {worst['components']['weather_risk']}/100, "
                f"warning level {worst['weather_raw']['storm_warning_level']}) on "
                f"{worst['id']} ({worst['status']}, {worst['overall_risk']}/100) — "
                f"stage crews in {region} before the front arrives."
            )
            crew_list.append(cell)
        crew_list.sort(
            key=lambda c: max(
                x["overall_risk"] for x in self._assets if x["id"] in c["assets"]
            ),
            reverse=True,
        )
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return {
            "generated_at": generated_at,
            "maintenance_plan": plan,
            "crew_prepositioning": crew_list,
        }

    # -- Simulation change-event + plan-delta internals ---------------------

    def _record_asset_changes(self, assets: list[dict]) -> None:
        """Diff ``assets`` against the previous snapshot and queue events.

        Called after every simulation re-score.  Events accumulate in
        ``_pending_changes`` until consumed by ``simulation_status()``.
        """
        events = _diff_asset_changes(self._prev_asset_snapshots, assets)
        if events:
            self._pending_changes.extend(events)
            overflow = len(self._pending_changes) - _MAX_PENDING_CHANGE_EVENTS
            if overflow > 0:
                del self._pending_changes[:overflow]
        self._prev_asset_snapshots = _snapshot_asset_views(assets)

    def _pop_changes(self) -> list[dict]:
        """Return pending change events and clear the queue (event semantics).

        Each poll of the frontend consumes the events once — a second read
        returns an empty list until the next simulation re-score produces
        new transitions.
        """
        events = list(self._pending_changes)
        self._pending_changes = []
        return events

    def _plan_task_changes(self, plan: MaintenancePlan) -> list[dict]:
        """Diff ``plan`` against the last-seen plan baseline.

        Returns per-task transitions (rank/status/urgency/crew/risk deltas)
        so the maintenance view can reorder *and* visibly highlight newly
        promoted items.  This is presentation of two scheduler outputs — no
        scoring logic lives here.

        The baseline (``_plan_baseline``) is a normalised view of the
        previously served plan; it falls back to the static priorities view
        when no sim/approved plan preceded this one.
        """
        prev = self._plan_baseline or _static_plan_view(self.priorities())
        prev = {k: v for k, v in prev.items()}
        out: list[dict] = []
        for t in plan.tasks:
            p = prev.get(t.asset_id)
            if p is None:
                continue
            risk_delta = round(t.overall_risk - p["overall_risk"], 1)
            moved = t.rank != p["rank"]
            status_changed = t.status != p["status"]
            urgency_changed = t.urgency != p["urgency"]
            crew_changed = (t.assigned_crew or None) != (p["assigned_crew"] or None)
            risk_moved = abs(risk_delta) >= _CHANGE_RISK_DELTA_THRESHOLD
            if not (moved or status_changed or urgency_changed or crew_changed or risk_moved):
                continue
            out.append(
                {
                    "asset_id": t.asset_id,
                    "rank_from": p["rank"],
                    "rank_to": t.rank,
                    "rank_delta": p["rank"] - t.rank,  # positive = moved up
                    "moved_up": t.rank < p["rank"],
                    "moved_down": t.rank > p["rank"],
                    "status_from": p["status"],
                    "status_to": t.status,
                    "urgency_from": p["urgency"],
                    "urgency_to": t.urgency,
                    "crew_from": p["assigned_crew"],
                    "crew_to": t.assigned_crew,
                    "risk_from": p["overall_risk"],
                    "risk_to": t.overall_risk,
                    "risk_delta": risk_delta,
                    "newly_high_critical": (
                        t.status in _ALERT_STATUSES and p["status"] not in _ALERT_STATUSES
                    ),
                    "scheduling_reason": t.scheduling_reason,
                }
            )
        # Deterministic order: biggest rank climbs first.
        out.sort(key=lambda c: (-c["rank_delta"], c["asset_id"]))
        return out

    def _current_plan_view(self) -> dict[str, dict]:
        """Normalised view of the currently served maintenance plan."""
        if self._approved_plan is not None:
            return _scheduler_plan_view(self._approved_plan.tasks)
        if self._sim_plan is not None:
            return _scheduler_plan_view(self._sim_plan.tasks)
        return _static_plan_view(self.priorities())

    # -- Simulation API methods -------------------------------------------

    def simulation_status(self) -> dict:
        """Return the current simulation state for the UI.

        The ``changes`` list carries per-asset transitions since the last
        poll and is consumed on read — the frontend uses it to flash the
        affected map markers and feed the event strip.
        """
        if not self._sim_active or self._simulator is None:
            return {
                "active": False,
                "phase": None,
                "phase_index": None,
                "phase_label": None,
                "phase_description": None,
                "total_phases": len(DEMO_PHASES),
                "phases": [
                    {
                        "index": i,
                        "phase": p.phase.value,
                        "label": p.label,
                        "description": p.description,
                    }
                    for i, p in enumerate(DEMO_PHASES)
                ],
                "live_weather_base": False,
                "plan_pending_approval": False,
                "plan_approved": False,
                "changes": [],
            }

        info = self._simulator.phase_info
        return {
            "active": True,
            "phase": self._simulator.current_phase.value,
            "phase_index": self._simulator.phase_index,
            "phase_label": info.label,
            "phase_description": info.description,
            "total_phases": len(DEMO_PHASES),
            "phases": [
                {
                    "index": i,
                    "phase": p.phase.value,
                    "label": p.label,
                    "description": p.description,
                }
                for i, p in enumerate(DEMO_PHASES)
            ],
            "live_weather_base": self._simulator.live_base,
            "sensor_pressure_per_asset": dict(self._sim_pressure),
            "plan_pending_approval": self._sim_plan is not None and not (
                self._sim_plan.approved
            ),
            "plan_approved": self._approved_plan is not None and self._approved_plan.approved,
            "changes": self._pop_changes(),
        }

    def simulation_start(
        self,
        lat: float | None = None,
        lon: float | None = None,
        phase: str | None = None,
    ) -> dict:
        """
        Start the live simulation.

        Optionally seed a specific lat/lon (defaults to TX-007 Waterfront).
        The simulator attempts a live Open-Meteo fetch; on failure it falls
        back to the static TX-007 base weather — the demo never breaks.
        """
        # TX-007 default location
        use_lat = lat if lat is not None else 51.507
        use_lon = lon if lon is not None else -0.060

        self._simulator = WeatherSimulator(
            base_lat=use_lat,
            base_lon=use_lon,
            attempt_live=True,
        )
        if phase:
            try:
                self._simulator.set_phase(SimulationPhase(phase))
            except ValueError:
                pass  # ignore unknown phase; keep default CALM

        self._sim_active = True
        self._sim_plan = None
        self._approved_plan = None
        self._apply_simulation()
        return self.simulation_status()

    def simulation_advance(self) -> dict:
        """Advance to the next simulation phase and recompute scores."""
        if not self._sim_active or self._simulator is None:
            return {"error": "simulation not active"}
        self._simulator.advance()
        self._apply_simulation()
        return self.simulation_status()

    def simulation_set_phase(self, phase: str) -> dict:
        """Jump directly to a named simulation phase."""
        if not self._sim_active or self._simulator is None:
            return {"error": "simulation not active"}
        try:
            self._simulator.set_phase(SimulationPhase(phase))
        except ValueError:
            return {"error": f"unknown phase '{phase}'"}
        self._apply_simulation()
        return self.simulation_status()

    def simulation_stop(self) -> dict:
        """Stop the simulation and restore static demo data."""
        self._sim_active = False
        self._simulator = None
        self._sim_plan = None
        self._approved_plan = None
        self._sim_pressure = {}
        self._pending_changes = []
        self._plan_baseline = None
        # Restore original static records and re-score
        self._raw = dict(self._raw_static)
        for raw in self._raw.values():
            inputs = normalise(raw)
            result = score_asset(inputs)
            self._inputs[raw.metadata.asset_id] = inputs
            self._results[raw.metadata.asset_id] = result
        self._assets = [
            self._build_asset_view(aid) for aid in sorted(self._raw)
        ]
        self._prev_asset_snapshots = _snapshot_asset_views(self._assets)
        return {"active": False, "message": "Simulation stopped — static demo data restored."}

    def simulation_plan(self) -> dict:
        """Return the current proposed maintenance plan (simulation or static).

        Simulation plans carry a ``task_changes`` list of per-task
        transitions vs. the previously served plan (rank/status/urgency/crew
        deltas) so the maintenance view can visibly react to the simulation.
        """
        if self._approved_plan is not None:
            # The approved plan is what the operator confirmed — no pending
            # deltas remain against it.
            plan_d = self._plan_to_dict(self._approved_plan, pending=False)
            plan_d["task_changes"] = []
            return plan_d
        if self._sim_plan is not None:
            plan_d = self._plan_to_dict(self._sim_plan, pending=True)
            plan_d["task_changes"] = self._plan_task_changes(self._sim_plan)
            return plan_d
        # Fall back to the existing static priorities()
        return self.priorities()

    def simulation_approve_plan(self, approved_by: str = "operator") -> dict:
        """Operator approves the pending simulation maintenance plan."""
        if self._sim_plan is None:
            return {"error": "no pending plan to approve"}
        scheduler = MaintenanceScheduler(
            sim_phase_label=self._simulator.phase_info.label if self._simulator else "",
        )
        self._approved_plan = scheduler.approve_plan(self._sim_plan, approved_by)
        self._sim_plan = None  # consumed
        # The approved plan is now the operator's current view — no pending
        # deltas remain until the next scheduler run.
        self._plan_baseline = None
        return {
            "approved": True,
            "approved_at": self._approved_plan.approved_at,
            "approved_by": self._approved_plan.approved_by,
            "tasks": len(self._approved_plan.tasks),
        }

    def simulation_reject_plan(self) -> dict:
        """Operator rejects the pending plan — it is discarded."""
        if self._sim_plan is None:
            return {"error": "no pending plan to reject"}
        self._sim_plan = None
        self._plan_baseline = None
        return {"rejected": True, "message": "Proposed plan discarded — static plan remains active."}

    # -- Scenario runner API methods ----------------------------------------

    def scenario_list(self) -> dict:
        """Return the list of available scenarios for the selector dropdown."""
        return {
            "scenarios": [
                {
                    "id": s["id"],
                    "label": s["label"],
                    "description": s["description"],
                    "step_count": len(s["steps"]),
                    "total_duration_s": sum(st.duration_s for st in s["steps"]),
                }
                for s in SCENARIOS.values()
            ],
            "default": DEFAULT_SCENARIO,
        }

    def scenario_run(
        self,
        scenario_id: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
    ) -> dict:
        """
        Start (or restart) an autonomous scenario.

        Initialises the ``WeatherSimulator`` (live Open-Meteo if network is
        available, static TX-007 baseline otherwise), then starts the
        ``ScenarioRunner``.  The simulation layer is activated so
        ``/api/assets`` and ``/api/priorities`` return simulation-adjusted data.
        """
        sid = scenario_id or DEFAULT_SCENARIO
        try:
            runner = ScenarioRunner(sid)
        except ValueError as exc:
            return {"error": str(exc)}

        use_lat = lat if lat is not None else 51.507
        use_lon = lon if lon is not None else -0.060

        self._simulator = WeatherSimulator(
            base_lat=use_lat,
            base_lon=use_lon,
            attempt_live=True,
        )
        self._sim_active = True
        self._sim_plan = None
        self._approved_plan = None
        self._runner = runner
        self._runner.run()

        # Apply the initial step immediately
        state = self._runner.tick()
        self._last_step = state
        self._apply_simulation_step(state)
        return self._scenario_status()

    def scenario_pause(self) -> dict:
        """Pause the running scenario (elapsed time is frozen)."""
        if self._runner is None:
            return {"error": "no scenario running"}
        self._runner.pause()
        # Refresh _last_step so _scenario_status() reflects the new paused state
        self._last_step = self._runner.tick()
        return self._scenario_status()

    def scenario_resume(self) -> dict:
        """Resume a paused scenario."""
        if self._runner is None:
            return {"error": "no scenario running"}
        self._runner.resume()
        # Refresh _last_step so _scenario_status() reflects resumed state
        self._last_step = self._runner.tick()
        return self._scenario_status()

    def scenario_reset(self) -> dict:
        """
        Stop the scenario and restore the static demo data.

        Leaves the runner and simulator objects as None so the UI returns to
        the unmodified static demo.
        """
        if self._runner is not None:
            self._runner.reset()
        self._runner = None
        self._last_step = None
        # Delegate the full teardown to simulation_stop()
        return self.simulation_stop()

    def scenario_tick(self) -> dict:
        """
        Advance time and return the current scenario state.

        Should be polled by the frontend every 2–3 seconds while the scenario
        is running.  Re-applies simulation to assets only when the step index
        or sensor pressure has changed enough to matter (> 0.01 change).

        Returns the scenario status dict (a superset of simulation_status).
        """
        if self._runner is None or not self._sim_active:
            return {"active": False, "scenario_active": False}

        state = self._runner.tick()
        self._last_step = state

        # Re-apply if running (not paused) and sensor pressure changed > 1%
        prev_pressure = self._sim_pressure.get("__last_pressure__", -999.0)
        pressure_delta = abs(state.sensor_pressure - prev_pressure)
        if state.running and pressure_delta > 0.01:
            self._sim_pressure["__last_pressure__"] = state.sensor_pressure
            # Sync the WeatherSimulator phase so _apply_simulation_step
            # picks up the right weather multipliers
            if self._simulator:
                self._simulator.set_phase(state.phase)
            self._apply_simulation_step(state)

        return self._scenario_status()

    def _scenario_status(self) -> dict:
        """Build the combined scenario + simulation status dict."""
        base = self.simulation_status()
        if self._runner is None or self._last_step is None:
            return {
                **base,
                "scenario_active": False,
                "scenario_id": None,
                "scenario_label": None,
                "scenario_description": None,
                "step_index": None,
                "total_steps": None,
                "step_progress": None,
                "step_elapsed_s": None,
                "step_duration_s": None,
                "elapsed_s": None,
                "paused": False,
                "completed": False,
            }
        st = self._last_step
        return {
            **base,
            "active": self._sim_active,
            "scenario_active": True,
            "scenario_id": st.scenario_id,
            "scenario_label": st.scenario_label,
            "step_index": st.step_index,
            "total_steps": st.total_steps,
            "phase": st.phase.value,
            "phase_label": st.phase_info.label,
            "phase_description": st.phase_info.description,
            "sensor_pressure": st.sensor_pressure,
            "step_progress": st.step_progress,
            "step_elapsed_s": st.step_elapsed_s,
            "step_duration_s": st.step_duration_s,
            "elapsed_s": st.elapsed_s,
            "running": st.running,
            "paused": st.paused,
            "completed": st.completed,
        }

    def _apply_simulation_step(self, state: StepState) -> None:
        """
        Re-score all assets using the given interpolated StepState.

        This is the scenario-aware equivalent of ``_apply_simulation()``.
        Instead of using ``self._simulator.phase_info.sensor_pressure``
        verbatim it uses the interpolated ``state.sensor_pressure`` so
        transitions between steps are smooth.
        """
        assert self._simulator is not None
        sim_weather = self._simulator.current_weather()
        phase_info = state.phase_info

        self._sim_pressure = {}
        new_inputs: dict = {}
        new_results: dict = {}

        for aid, static_raw in self._raw_static.items():
            cond = apply_weather_to_asset(
                static_raw,
                sim_weather,
                state.sensor_pressure,          # interpolated pressure
            )
            self._sim_pressure[aid] = cond.effective_pressure

            sim_raw = copy.copy(static_raw)
            sim_raw.telemetry = cond.telemetry
            sim_raw.weather = cond.weather
            self._raw[aid] = sim_raw

            inputs = normalise(sim_raw)
            result = score_asset(inputs)
            new_inputs[aid] = inputs
            new_results[aid] = result

        # Seed the tick's pressure marker so the next scenario_tick() does
        # not redundantly re-apply (and spuriously resurrect a pending plan
        # right after an operator approval).
        self._sim_pressure["__last_pressure__"] = state.sensor_pressure

        self._inputs = new_inputs
        self._results = new_results

        with self._db.connection() as conn:
            risk_repo = RiskResultRepository(conn)
            for result in new_results.values():
                try:
                    risk_repo.insert(result)
                except Exception:
                    pass

        self._assets = [
            self._build_asset_view(aid) for aid in sorted(self._raw)
        ]

        # Track per-asset transitions since the previous snapshot so the
        # UI can flash changed markers and feed the simulation event strip.
        self._record_asset_changes(self._assets)

        results_list = list(self._results.values())
        scheduler = MaintenanceScheduler(
            sim_phase_label=phase_info.label,
        )
        previous = self._approved_plan
        # Capture the previously served plan view BEFORE it is replaced, so
        # the new plan can expose per-task deltas against it.
        self._plan_baseline = self._current_plan_view()
        self._sim_plan = scheduler.build_plan(
            results=results_list,
            asset_views=self._assets,
            previous_plan=previous,
        )

    # -- Simulation internals -----------------------------------------------

    def _apply_simulation(self) -> None:
        """
        Re-score all assets under the current simulation phase.

        1. Derives phase-scaled weather from the simulator.
        2. Applies per-asset sensor pressure proportional to degradation.
        3. Builds modified ``RawAssetRecord`` with simulated telemetry/weather.
        4. Re-normalises and re-scores through the real engine.
        5. Rebuilds the asset view list.
        6. Proposes a new maintenance plan via the deterministic scheduler.

        Storage writes use the thread-local connection so this is safe to
        call from any ThreadingHTTPServer handler thread.
        """
        assert self._simulator is not None
        phase_info = self._simulator.phase_info
        sim_weather = self._simulator.current_weather()

        self._sim_pressure = {}
        new_inputs: dict = {}
        new_results: dict = {}

        for aid, static_raw in self._raw_static.items():
            cond = apply_weather_to_asset(
                static_raw,
                sim_weather,
                phase_info.sensor_pressure,
            )
            self._sim_pressure[aid] = cond.effective_pressure

            # Build a modified record with simulated telemetry + weather
            sim_raw = copy.copy(static_raw)
            sim_raw.telemetry = cond.telemetry
            sim_raw.weather = cond.weather
            self._raw[aid] = sim_raw

            inputs = normalise(sim_raw)
            result = score_asset(inputs)
            new_inputs[aid] = inputs
            new_results[aid] = result

        # Seed the tick's pressure marker (same rationale as the
        # scenario-aware path above).
        self._sim_pressure["__last_pressure__"] = phase_info.sensor_pressure

        self._inputs = new_inputs
        self._results = new_results

        # Persist simulation scoring snapshots using the calling thread's connection.
        with self._db.connection() as conn:
            risk_repo = RiskResultRepository(conn)
            for result in new_results.values():
                try:
                    risk_repo.insert(result)
                except Exception:
                    pass

        self._assets = [
            self._build_asset_view(aid) for aid in sorted(self._raw)
        ]

        # Track per-asset transitions since the previous snapshot so the
        # UI can flash changed markers and feed the simulation event strip.
        self._record_asset_changes(self._assets)

        # Build a new proposed maintenance plan (pending operator approval)
        results_list = list(self._results.values())
        scheduler = MaintenanceScheduler(
            sim_phase_label=phase_info.label,
        )
        previous = self._approved_plan  # detect reordering vs. last approved plan
        # Capture the previously served plan view BEFORE it is replaced, so
        # the new plan can expose per-task deltas against it.
        self._plan_baseline = self._current_plan_view()
        self._sim_plan = scheduler.build_plan(
            results=results_list,
            asset_views=self._assets,
            previous_plan=previous,
        )

    def _plan_to_dict(self, plan: MaintenancePlan, pending: bool) -> dict:
        """Serialise a MaintenancePlan to the API response shape."""
        tasks = []
        for t in plan.tasks:
            tasks.append(
                {
                    "rank": t.rank,
                    "asset_id": t.asset_id,
                    "substation": t.substation,
                    "region": t.region,
                    "status": t.status,
                    "overall_risk": t.overall_risk,
                    "grid_impact_score": t.grid_impact_score,
                    "priority_score": t.priority_score,
                    "urgency_bonus": t.urgency_bonus,
                    "dominant_factor": t.dominant_factor,
                    "dominant_factor_label": t.dominant_factor_label,
                    "customers_served": t.customers_served,
                    "critical_facilities": t.critical_facilities,
                    "has_redundant_path": t.has_redundant_path,
                    "recommended_action": t.recommended_action,
                    "action_detail": t.action_detail,
                    "urgency": t.urgency,
                    "window_hours": t.window_hours,
                    "assigned_crew": t.assigned_crew,
                    "scheduled_start": t.scheduled_start,
                    "scheduling_reason": t.scheduling_reason,
                    "triggered_by_phase": t.triggered_by_phase,
                    "reordered": t.reordered,
                }
            )
        return {
            "generated_at": plan.generated_at,
            "simulation_phase": plan.simulation_phase,
            "maintenance_plan": tasks,
            "crew_prepositioning": plan.crew_prepositioning,
            "approved": plan.approved,
            "approved_at": plan.approved_at,
            "approved_by": plan.approved_by,
            "change_summary": plan.change_summary,
            "pending_approval": pending,
        }

    def briefing_info(self) -> dict:
        # Initialise the provider now so the active backend (or a fallback
        # warning) is known up front instead of on the first question.
        self._service()
        config = BriefingConfig.from_env()
        active = self._briefing_service_provider_name() or config.provider_name
        return {
            "provider": active,
            "model": config.model_id,
            "offline": active == "mock",
            "env_files": self.env_files,
            "warning": self._provider_warning,
        }

    # -- AI briefing router (backend only; frontend never implements AI) ---

    def answer_question(
        self,
        question: str,
        asset_id: str | None = None,
        history: list[dict] | None = None,
    ) -> dict:
        """Route an operator message through the conversational BriefingService.

        All turns go through ``BriefingService.chat()`` which uses a single
        adaptive prompt.  The model sees the conversation history and the
        grounded asset context and decides how much to say — short follow-ups
        get short answers, casual reactions get natural replies, full questions
        get full briefings.

        If the provider call fails, a deterministic grounded fallback is
        composed from the risk-engine outputs so the demo never invents numbers.

        Parameters
        ----------
        question:
            The operator's current message.
        asset_id:
            The asset currently selected in the UI (takes priority over any
            asset ID mentioned in the question text).
        history:
            List of previous turns as ``{"role": "user"|"assistant",
            "content": str}`` dicts.  Serialised form used by the HTTP API.
        """
        target = self._resolve_target(question, asset_id)
        service = self._service()

        # Convert the serialised history dicts into ConversationTurn objects.
        # Guard against non-list values (e.g. a stale client sending a string).
        raw_history = history if isinstance(history, list) else []
        turns: list[ConversationTurn] = []
        for h in raw_history:
            role = str(h.get("role", "user"))
            content = str(h.get("content", ""))
            if content:
                turns.append(ConversationTurn(role=role, content=content))

        briefing_type = BriefingType.CONVERSATIONAL
        text = ""
        asset_ids: list[str] = []
        risks: dict[str, float] = {}
        levels: dict[str, str] = {}

        try:
            if target is not None:
                result = self._results[target]
                inputs = self._inputs[target]
                reg = self._registry(target)
                ctx = BriefingContextBuilder.build(result, inputs, registry=reg)
                res = service.chat(question, history=turns, ctx=ctx)
                briefing_type, text = res.briefing_type, res.text
                asset_ids = [target]
                risks = {target: result.overall_risk}
                levels = {target: result.risk_level.value}
            else:
                # No specific asset — pass a fleet summary as grounded context.
                top = sorted(
                    self._assets,
                    key=lambda a: (a["overall_risk"], a["components"]["grid_impact"]),
                    reverse=True,
                )[:4]
                fleet_summary = _build_fleet_summary(top)
                res = service.chat(question, history=turns, fleet_summary=fleet_summary)
                briefing_type, text = res.briefing_type, res.text
                asset_ids = [a["id"] for a in top]
                risks = {a["id"]: a["overall_risk"] for a in top}
                levels = {a["id"]: a["risk_level"] for a in top}
                # Always append the exact ranked list for fleet questions so the
                # demo answer contains real numbers even with the offline mock.
                text = _with_ranked_list(text, top)
        except Exception:
            briefing_type = BriefingType.CONVERSATIONAL
            text = self._grounded_fallback(question, target)
            if target is not None:
                a = self.get_asset(target)
                assert a is not None
                asset_ids = [target]
                risks = {target: a["overall_risk"]}
                levels = {target: a["risk_level"]}

        info = self.briefing_info()
        return {
            "text": text,
            "briefing_type": str(briefing_type.value)
            if isinstance(briefing_type, BriefingType)
            else str(briefing_type),
            "asset_ids": asset_ids,
            "overall_risks": risks,
            "risk_levels": levels,
            "provider": info["provider"],
            "model": info["model"],
            "grounded": True,
            "notice": self._provider_warning,
        }

    # -- internals ----------------------------------------------------------

    def _service(self) -> BriefingService:
        if self._briefing_service is None:
            config = BriefingConfig.from_env()
            # Force offline-safe mock unless the operator configured a real
            # endpoint — the demo must run with zero credentials.
            if config.provider_name not in ("openai", "watsonx", "mock"):
                config.provider_name = "mock"
            if config.provider_name in ("openai", "watsonx") and not _provider_ready(
                config
            ):
                config.provider_name = "mock"
            try:
                self._briefing_service = BriefingService.from_config(config)
            except Exception as exc:  # missing SDK, bad URL, no network at init…
                self._provider_warning = (
                    f"Configured provider '{config.provider_name}' unavailable "
                    f"({exc}); fell back to offline mock."
                )
                config.provider_name = "mock"
                self._briefing_service = BriefingService.from_config(config)
        return self._briefing_service

    def _briefing_service_provider_name(self) -> str | None:
        provider = getattr(self._briefing_service, "_provider", None)
        if provider is None:
            return None
        return {
            "OpenAIProvider": "openai",
            "WatsonxProvider": "watsonx",
            "MockProvider": "mock",
        }.get(type(provider).__name__, "mock")

    def _registry(self, asset_id: str) -> AssetRegistryInfo:
        a = self.get_asset(asset_id)
        if a is None:
            return AssetRegistryInfo()
        return AssetRegistryInfo(asset_type=a["asset_type"], notes=a["notes"])

    def _resolve_target(
        self, question: str, asset_id: str | None
    ) -> str | None:
        if asset_id and asset_id in self._raw:
            return asset_id
        match = re.search(r"\b(TX-?\d{3})\b", (question or "").upper())
        if match:
            candidate = match.group(1).replace("TX", "TX-") if "-" not in match.group(1) else match.group(1)
            candidate = candidate.upper()
            if candidate in self._raw:
                return candidate
        return None

    def _grounded_fallback(self, question: str, asset_id: str | None) -> str:
        """Deterministic engine-grounded text used only if the LLM call fails."""
        if asset_id is None:
            top = sorted(
                self._assets,
                key=lambda a: (a["overall_risk"], a["components"]["grid_impact"]),
                reverse=True,
            )[:3]
            lines = [
                f"{i}. {a['id']} — {a['status']} ({a['overall_risk']}/100), "
                f"driven by {a['dominant_factor_label']}."
                for i, a in enumerate(top, start=1)
            ]
            return (
                "Inspection priority for today (from live risk-engine scores):\n"
                + "\n".join(lines)
            )
        a = self.get_asset(asset_id)
        assert a is not None
        # Rank by weighted contribution (same logic as dominant_factor) so the
        # fallback text is consistent with the engine's dominant_factor field.
        ranked = sorted(
            a["components"].items(),
            key=lambda kv: kv[1] * COMPONENT_WEIGHTS.get(kv[0], 0.0),
            reverse=True,
        )
        top3 = ", ".join(
            f"{COMPONENT_LABELS[k]} {v}/100" for k, v in ranked[:3]
        )
        return (
            f"{a['id']} ({a['substation']}) is rated {a['status']} with an overall "
            f"risk of {a['overall_risk']}/100. Top contributors: {top3}. "
            f"Primary driver: {a['dominant_factor_label']}. "
            f"{_ACTION_BY_STATUS[a['status']][1]}."
        )

    def _build_asset_view(self, asset_id: str) -> dict:
        raw = self._raw[asset_id]
        inputs = self._inputs[asset_id]
        result = self._results[asset_id]

        # All repository reads use a thread-local connection so this method
        # is safe to call from any handler thread.
        with self._db.connection() as conn:
            meta = AssetRepository(conn).get(asset_id)
            topo = GridTopologyRepository(conn).get(asset_id)
            lc = LifecycleRepository(conn).get(asset_id)

            status = LEVEL_TO_STATUS[result.risk_level.value]
            action, action_detail = _ACTION_BY_STATUS[status]

            # Lifecycle view: prefer the stored record (single source of truth),
            # fall back to the raw snapshot fields if storage is unavailable.
            if lc is not None:
                fault_history = [asdict(e) for e in lc.fault_history]
                maint_history = [asdict(e) for e in lc.maintenance_history]
                lifecycle = {
                    "state": lc.state.value,
                    "age_years": lc.age_years,
                    "rated_lifespan_years": lc.rated_lifespan_years,
                    "past_rated_lifespan": lc.is_past_rated_lifespan,
                    "remaining_life_years": round(lc.remaining_life_years, 1),
                    "insulation_health_pct": lc.insulation_health_pct,
                    "cumulative_fault_events": lc.cumulative_fault_events,
                    "maintenance_overdue_days": lc.maintenance_overdue_days,
                    "average_load_factor": lc.average_load_factor,
                    "failure_count_last_5yr": lc.failure_count_last_5yr,
                    "failures_caused_by_weather": lc.failures_caused_by_weather,
                    "last_failure_days_ago": lc.last_failure_days_ago,
                    "repeat_fault_active": lc.repeat_fault_active,
                    "fault_history": fault_history,
                    "maintenance_history": maint_history,
                    "predecessor_asset_id": lc.predecessor_asset_id,
                }
            else:
                d = raw.degradation
                lifecycle = {
                    "state": "active",
                    "age_years": d.age_years,
                    "rated_lifespan_years": meta.rated_lifespan_years if meta else 40.0,
                    "past_rated_lifespan": d.age_years
                    > (meta.rated_lifespan_years if meta else 40.0),
                    "remaining_life_years": round(
                        (meta.rated_lifespan_years if meta else 40.0) - d.age_years, 1
                    ),
                    "insulation_health_pct": d.insulation_health_pct,
                    "cumulative_fault_events": d.cumulative_fault_events,
                    "maintenance_overdue_days": d.maintenance_overdue_days,
                    "average_load_factor": d.average_load_factor,
                    "failure_count_last_5yr": raw.incidents.failure_count_last_5yr,
                    "failures_caused_by_weather": raw.incidents.failures_caused_by_weather,
                    "last_failure_days_ago": raw.incidents.last_failure_days_ago,
                    "repeat_fault_active": raw.incidents.repeat_mode_flag,
                    "fault_history": [],
                    "maintenance_history": [],
                    "predecessor_asset_id": None,
                }

            # Lineage: retired predecessors / successors known to storage.
            lineage: dict = {"predecessor": None, "successor_of_retired": None}
            try:
                retired_repo = RetiredRepository(conn)
                if lifecycle["predecessor_asset_id"]:
                    pred = retired_repo.get(lifecycle["predecessor_asset_id"])
                    if pred is not None:
                        lineage["predecessor"] = {
                            "asset_id": pred.asset_id,
                            "age_at_retirement_years": pred.age_at_retirement_years,
                            "retirement_reason": pred.retirement_reason,
                            "successor_asset_id": pred.successor_asset_id,
                        }
                by_succ = retired_repo.get_by_successor(asset_id)
                if by_succ is not None:
                    lineage["successor_of_retired"] = {
                        "asset_id": by_succ.asset_id,
                        "retirement_reason": by_succ.retirement_reason,
                    }
            except Exception:
                pass

        t = raw.telemetry
        w = raw.weather
        return {
            "id": asset_id,
            "asset_type": meta.asset_type if meta else raw.metadata.asset_type,
            "substation": meta.location.substation_name
            if meta
            else raw.metadata.location.substation_name,
            "region": meta.location.region if meta else raw.metadata.location.region,
            "latitude": meta.location.latitude if meta else raw.metadata.location.latitude,
            "longitude": meta.location.longitude
            if meta
            else raw.metadata.location.longitude,
            "rated_kva": meta.rated_kva if meta else raw.metadata.rated_kva,
            "rated_voltage_kv": meta.rated_voltage_kv
            if meta
            else raw.metadata.rated_voltage_kv,
            "commissioned_year": meta.commissioned_year
            if meta
            else raw.metadata.commissioned_year,
            "notes": meta.notes if meta else raw.metadata.notes,
            # Live engine outputs:
            "status": status,
            "risk_level": result.risk_level.value,
            "overall_risk": result.overall_risk,
            "components": {
                "sensor_health": result.components.sensor_health,
                "weather_risk": result.components.weather_risk,
                "historical_failure": result.components.historical_failure,
                "asset_degradation": result.components.asset_degradation,
                "grid_impact": result.components.grid_impact,
            },
            "component_weights": dict(COMPONENT_WEIGHTS),
            "component_labels": dict(COMPONENT_LABELS),
            "dominant_factor": result.dominant_factor,
            "dominant_factor_label": COMPONENT_LABELS.get(
                result.dominant_factor, result.dominant_factor
            ),
            "recommended_action": action,
            "action_detail": action_detail,
            # Raw evidence behind the scores:
            "sensors_raw": {
                "top_oil_temp_c": t.top_oil_temp_c,
                "winding_hot_spot_c": t.winding_hot_spot_c,
                "vibration_mm_s": t.vibration_mm_s,
                "oil_dielectric_kv": t.oil_dielectric_kv,
                "partial_discharge_pc": t.partial_discharge_pc,
                "load_factor_current": t.load_factor_current,
            },
            "sensors_norm": {
                "temperature_score": inputs.sensors.temperature_score,
                "vibration_score": inputs.sensors.vibration_score,
                "oil_quality_score": inputs.sensors.oil_quality_score,
                "partial_discharge_score": inputs.sensors.partial_discharge_score,
                "load_score": inputs.sensors.load_score,
                "missing_sensor_ratio": inputs.sensors.missing_sensor_ratio,
            },
            "weather_raw": {
                "max_temp_c": w.max_temp_c,
                "min_temp_c": w.min_temp_c,
                "precipitation_mm": w.precipitation_mm,
                "wind_speed_max_kmh": w.wind_speed_max_kmh,
                "storm_warning_level": w.storm_warning_level,
                "forecast_hours": w.forecast_hours,
            },
            "weather_norm": {
                "temperature_stress_score": inputs.weather.temperature_stress_score,
                "precipitation_score": inputs.weather.precipitation_score,
                "wind_storm_score": inputs.weather.wind_storm_score,
                "forecast_hours": inputs.weather.forecast_hours,
            },
            "history": {
                "failure_count_last_5yr": raw.incidents.failure_count_last_5yr,
                "failures_caused_by_weather": raw.incidents.failures_caused_by_weather,
                "last_failure_days_ago": raw.incidents.last_failure_days_ago,
                "repeat_mode_flag": raw.incidents.repeat_mode_flag,
                "mean_time_between_failures_days": raw.incidents.mean_time_between_failures_days,
            },
            "degradation": {
                "age_years": raw.degradation.age_years,
                "cumulative_fault_events": raw.degradation.cumulative_fault_events,
                "maintenance_overdue_days": raw.degradation.maintenance_overdue_days,
                "insulation_health_pct": raw.degradation.insulation_health_pct,
                "average_load_factor": raw.degradation.average_load_factor,
            },
            "grid_impact": {
                "customers_served": topo.customers_served if topo else 0,
                "critical_facility_count": topo.critical_facility_count if topo else 0,
                "critical_facility_names": list(topo.critical_facility_names)
                if topo
                else [],
                "peak_load_mw": topo.peak_load_mw if topo else 0.0,
                "downstream_asset_count": topo.downstream_asset_count if topo else 0,
                "has_redundant_path": bool(topo.has_n1_redundancy) if topo else True,
            },
            "lifecycle": lifecycle,
            "lineage": lineage,
        }


def _build_fleet_summary(top: list[dict]) -> str:
    """Build a grounded plaintext fleet summary for conversational prompts.

    This is injected as ``fleet_summary`` when no specific asset is selected
    so the model has real numbers to reason over even for fleet-wide questions.
    """
    lines = [
        f"{i}. {a['id']} ({a['substation']}) — {a['status']}, "
        f"risk {a['overall_risk']}/100, "
        f"driven by {a['dominant_factor_label']}, "
        f"{a['grid_impact']['customers_served']:,} customers, "
        f"{'N-1 redundant' if a['grid_impact']['has_redundant_path'] else 'no redundancy'}."
        for i, a in enumerate(top, start=1)
    ]
    return "Top assets by risk (live engine scores):\n" + "\n".join(lines)


def _with_ranked_list(text: str, top: list[dict]) -> str:
    """Append the exact engine-ranked inspection list to a prioritisation answer.

    This is presentation of existing ``RiskResult`` outputs (same ordering as
    the maintenance plan), not a second AI implementation — it guarantees the
    demo answer always contains the real ranked numbers even when the
    offline mock provider returns its generic acknowledgement.
    """
    lines = [
        f"{i}. {a['id']} — {a['status']} ({a['overall_risk']}/100), "
        f"driven by {a['dominant_factor_label']}; "
        f"{a['grid_impact']['customers_served']:,} customers, "
        f"{'N-1 redundant' if a['grid_impact']['has_redundant_path'] else 'no redundancy'}."
        for i, a in enumerate(top, start=1)
    ]
    return text.rstrip() + "\n\nLive ranked list (risk engine):\n" + "\n".join(lines)


def _provider_ready(config: BriefingConfig) -> bool:
    """True when a non-mock provider has the credentials/URL it needs."""
    if config.provider_name == "openai":
        return bool(config.llm_base_url or config.llm_api_key)
    if config.provider_name == "watsonx":
        return bool(config.watsonx_api_key and config.watsonx_project_id)
    return True


def default_db_path() -> str:
    """On-disk demo database next to the repository root (overridable)."""
    env = os.getenv("GRIDGUARD_DB", "")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    return os.path.join(repo_root, "gridguard.db")
