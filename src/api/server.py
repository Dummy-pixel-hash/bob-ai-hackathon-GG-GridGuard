"""GridGuard UI server — stdlib-only HTTP API + static frontend.

No new dependencies: everything here uses the Python standard library plus
the existing GridGuard packages (``data``, ``normalisation``,
``risk_engine``, ``storage``, ``lifecycle``, ``ai_briefing``,
``simulation``, ``maintenance``).

Endpoints
---------
GET  /api/summary            grid counts, averages, exposure totals
GET  /api/assets             enriched list for every asset (live engine scores)
GET  /api/assets/<asset_id>  full detail for one asset
GET  /api/priorities         ranked maintenance plan + crew pre-positioning
GET  /api/briefing_info      active AI provider / model (mock when offline)
POST /api/briefing           {"question": str, "asset_id": str|None,
                              "history": [{"role":"user"|"assistant","content":str}]}
                             -> grounded conversational answer
GET  /api/simulation/status  current simulation phase + plan state
POST /api/simulation/start   {"lat":float?, "lon":float?, "phase":str?}
POST /api/simulation/advance advance to next phase
POST /api/simulation/stop    stop simulation, restore static data
POST /api/simulation/phase   {"phase": str}  — jump to named phase
GET  /api/simulation/plan    current proposed maintenance plan
POST /api/simulation/approve {"approved_by": str?} — operator approves plan
POST /api/simulation/reject  reject pending plan
GET  /api/scenario/list      list of available named scenarios
POST /api/scenario/run       {"scenario_id":str?, "lat":float?, "lon":float?}
POST /api/scenario/pause     pause the running scenario
POST /api/scenario/resume    resume a paused scenario
POST /api/scenario/reset     stop scenario, restore static demo
GET  /api/scenario/tick      advance time + return current interpolated state
GET  /                       the control-room dashboard (static files in ../frontend)

Run with:  python3 run_ui.py   (from the repository root)
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from api.grid_service import GridState, default_db_path  # noqa: E402

FRONTEND_DIR = os.path.join(_SRC, "frontend")

STATE: GridState | None = None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "GridGuardUI/1.0"

    # -- helpers ----------------------------------------------------------

    def _send_json(self, payload, status: int = 200, head_only: bool = False) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_static(self, rel_path: str, head_only: bool = False) -> None:
        fs_path = os.path.normpath(os.path.join(FRONTEND_DIR, rel_path))
        if not fs_path.startswith(FRONTEND_DIR) or not os.path.isfile(fs_path):
            self._send_json({"error": "not found"}, 404, head_only=head_only)
            return
        mime, _ = mimetypes.guess_type(fs_path)
        with open(fs_path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._handle_get(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        # Health checks and link prefetchers issue HEAD; answer with the
        # same status/headers a GET would return, but no response body.
        self._handle_get(head_only=True)

    def _handle_get(self, head_only: bool) -> None:
        assert STATE is not None
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._send_static("index.html", head_only=head_only)
        elif path in ("/styles.css", "/app.js"):
            self._send_static(path.lstrip("/"), head_only=head_only)
        elif path == "/api/summary":
            self._send_json(STATE.summary(), head_only=head_only)
        elif path == "/api/assets":
            self._send_json({"assets": STATE.assets}, head_only=head_only)
        elif path.startswith("/api/assets/"):
            asset_id = path[len("/api/assets/"):]
            asset = STATE.get_asset(asset_id)
            if asset is None:
                self._send_json({"error": f"unknown asset '{asset_id}'"}, 404, head_only=head_only)
            else:
                self._send_json(asset, head_only=head_only)
        elif path == "/api/priorities":
            self._send_json(STATE.priorities(), head_only=head_only)
        elif path == "/api/briefing_info":
            self._send_json(STATE.briefing_info(), head_only=head_only)
        elif path == "/api/simulation/status":
            self._send_json(STATE.simulation_status(), head_only=head_only)
        elif path == "/api/simulation/plan":
            self._send_json(STATE.simulation_plan(), head_only=head_only)
        elif path == "/api/scenario/list":
            self._send_json(STATE.scenario_list(), head_only=head_only)
        elif path == "/api/scenario/tick":
            self._send_json(STATE.scenario_tick(), head_only=head_only)
        else:
            self._send_json({"error": "not found"}, 404, head_only=head_only)

    def do_POST(self) -> None:  # noqa: N802
        assert STATE is not None
        path = urlparse(self.path).path

        # ── Simulation control endpoints (no body required for most) ────────
        if path == "/api/simulation/start":
            body = self._read_json_body()
            lat = body.get("lat")
            lon = body.get("lon")
            phase = body.get("phase") or None
            try:
                flat = float(lat) if lat is not None else None
                flon = float(lon) if lon is not None else None
            except (TypeError, ValueError):
                flat = flon = None
            self._send_json(STATE.simulation_start(lat=flat, lon=flon, phase=phase))
            return
        if path == "/api/simulation/advance":
            self._send_json(STATE.simulation_advance())
            return
        if path == "/api/simulation/stop":
            self._send_json(STATE.simulation_stop())
            return
        if path == "/api/simulation/phase":
            body = self._read_json_body()
            phase = str(body.get("phase", ""))
            if not phase:
                self._send_json({"error": "phase required"}, 400)
                return
            self._send_json(STATE.simulation_set_phase(phase))
            return
        if path == "/api/simulation/approve":
            body = self._read_json_body()
            approved_by = str(body.get("approved_by", "operator") or "operator")
            self._send_json(STATE.simulation_approve_plan(approved_by))
            return
        if path == "/api/simulation/reject":
            self._send_json(STATE.simulation_reject_plan())
            return

        # ── Scenario runner endpoints ─────────────────────────────────────────
        if path == "/api/scenario/run":
            body = self._read_json_body()
            sid = body.get("scenario_id") or None
            lat = body.get("lat")
            lon = body.get("lon")
            try:
                flat = float(lat) if lat is not None else None
                flon = float(lon) if lon is not None else None
            except (TypeError, ValueError):
                flat = flon = None
            self._send_json(STATE.scenario_run(scenario_id=sid, lat=flat, lon=flon))
            return
        if path == "/api/scenario/pause":
            self._send_json(STATE.scenario_pause())
            return
        if path == "/api/scenario/resume":
            self._send_json(STATE.scenario_resume())
            return
        if path == "/api/scenario/reset":
            self._send_json(STATE.scenario_reset())
            return

        if path != "/api/briefing":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "request body must be JSON"}, 400)
            return
        question = str(payload.get("question", "") or "")
        asset_id = payload.get("asset_id") or None
        history = payload.get("history") or []
        if not isinstance(history, list):
            history = []
        if not question.strip():
            self._send_json({"error": "question must be a non-empty string"}, 400)
            return
        self._send_json(STATE.answer_question(question, asset_id, history))

    def _read_json_body(self) -> dict:
        """Read and parse the POST body as JSON; return {} on any error."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}") or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *args) -> None:  # noqa: N802
        sys.stderr.write("gridguard: " + fmt % args + "\n")


def run(port: int, db_path: str) -> None:
    global STATE
    STATE = GridState(db_path=db_path)
    total = len(STATE.assets)
    info = STATE.briefing_info()
    print(f"GridGuard UI: scored {total} assets from the live risk engine.", flush=True)
    print(f"GridGuard UI: database at {db_path}", flush=True)
    live_n = sum(1 for v in STATE.weather_live.values() if v)
    if live_n:
        print(f"GridGuard UI: live Open-Meteo weather for {live_n}/{total} assets", flush=True)
    else:
        print("GridGuard UI: staged static demo weather (set GRIDGUARD_LIVE_WEATHER=1 for live refresh)", flush=True)
    if STATE.env_files:
        print(f"GridGuard UI: loaded env from {', '.join(STATE.env_files)}", flush=True)
    else:
        print("GridGuard UI: no .env file found (src/.env or ./.env); using environment only", flush=True)
    print(f"GridGuard UI: AI provider '{info['provider']}' (model {info['model']})", flush=True)
    if info.get("warning"):
        print(f"GridGuard UI: WARNING — {info['warning']}", flush=True)
    print(f"GridGuard UI: serving on http://localhost:{port}", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGridGuard UI: shutting down.")


if __name__ == "__main__":
    # Render injects PORT; explicit argv > PORT > APP_PORT > 8000.
    _port = int(os.getenv("PORT", "") or os.getenv("APP_PORT", "8000"))
    _db = default_db_path()
    if len(sys.argv) > 1:
        _port = int(sys.argv[1])
    run(_port, _db)
