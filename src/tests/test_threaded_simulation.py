"""
Regression test: simulation endpoints through the real ThreadingHTTPServer.

This test directly reproduces the original crash:

    sqlite3.ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread.

ThreadingHTTPServer dispatches each request on a *new* thread.  The original
code created one ``sqlite3.Connection`` on the main thread and stored it in
``GridState.conn``.  Any POST to a simulation endpoint on a handler thread
then triggered the crash.

These tests start a real ``ThreadingHTTPServer`` bound to localhost on a
random free port, send HTTP requests to it, and verify:

  1. POST /api/simulation/start   — returns 200 with active=true
  2. POST /api/simulation/advance — returns 200 with next phase
  3. GET  /api/simulation/plan    — returns 200 with a plan
  4. POST /api/simulation/approve — returns 200 with approved=true
  5. POST /api/simulation/reject  — 400/error because plan was just approved
                                    (no pending plan left)
  6. POST /api/simulation/stop    — returns 200 with active=false

All run sequentially to prove the full workflow works across threads.
A second round of start→stop confirms the in-memory DB is still consistent
after the first cycle (data seeded on startup is still visible).

Implementation notes
--------------------
* The server is started on a background thread and torn down in fixture
  teardown.  We bind to port 0 and read the actual port from the server
  socket so there are no port collisions.
* ``GRIDGUARD_LLM_PROVIDER=mock`` is set to avoid any network calls.
* The test deliberately avoids mocking the connection layer — the whole
  point is to exercise real cross-thread SQLite access.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

# Put src/ on path and pin offline LLM provider before any imports.
os.environ["GRIDGUARD_LLM_PROVIDER"] = "mock"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.server as _server_module
from api.grid_service import GridState
from api.server import Handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _start_server() -> tuple[ThreadingHTTPServer, int, threading.Thread]:
    """
    Start a ThreadingHTTPServer on a free port.

    Returns (server, port, thread).  The caller is responsible for calling
    ``server.shutdown()`` to stop it.
    """
    state = GridState(db_path=":memory:")
    _server_module.STATE = state

    # Bind to port 0 — OS assigns a free port.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port, t


def _request(
    port: int,
    method: str,
    path: str,
    body: dict | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict]:
    """Send an HTTP request to the test server and return (status_code, json_body)."""
    url = f"http://127.0.0.1:{port}{path}"
    data: bytes | None = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def server():
    """Start the ThreadingHTTPServer once for all tests in this module."""
    srv, port, thread = _start_server()
    yield port
    srv.shutdown()
    thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Regression tests: full simulation workflow through real threaded HTTP
# ---------------------------------------------------------------------------

class TestSimulationThreadedHTTP:
    """
    Each test method is an isolated HTTP call to the ThreadingHTTPServer.
    Together they walk the full start→advance→plan→approve→stop lifecycle,
    proving no SQLite cross-thread errors occur.
    """

    def test_01_start_does_not_crash(self, server):
        """
        POST /api/simulation/start must succeed without raising
        sqlite3.ProgrammingError on the handler thread.
        """
        status, body = _request(server, "POST", "/api/simulation/start", {})
        assert status == 200, f"Expected 200, got {status}: {body}"
        assert body.get("active") is True, f"active should be True: {body}"

    def test_02_status_reflects_active_simulation(self, server):
        """GET /api/simulation/status should show the simulation is running."""
        status, body = _request(server, "GET", "/api/simulation/status")
        assert status == 200
        assert body.get("active") is True
        assert body.get("phase") == "calm"

    def test_03_advance_moves_to_next_phase(self, server):
        """
        POST /api/simulation/advance must succeed across threads.
        This specifically triggers _apply_simulation() which writes to the
        risk_results table — the original crash site.
        """
        status, body = _request(server, "POST", "/api/simulation/advance")
        assert status == 200, f"Expected 200, got {status}: {body}"
        assert body.get("active") is True
        assert body.get("phase") == "advisory", (
            f"Expected advisory after advancing from calm, got: {body.get('phase')}"
        )

    def test_04_advance_again_to_storm_approaching(self, server):
        """Second advance → storm_approaching, still no crash."""
        status, body = _request(server, "POST", "/api/simulation/advance")
        assert status == 200
        assert body.get("phase") == "storm_approaching"

    def test_05_set_phase_to_peak_storm(self, server):
        """POST /api/simulation/phase lets the operator jump to peak_storm."""
        status, body = _request(
            server, "POST", "/api/simulation/phase", {"phase": "peak_storm"}
        )
        assert status == 200
        assert body.get("phase") == "peak_storm"

    def test_06_assets_endpoint_returns_updated_scores(self, server):
        """
        GET /api/assets must return 8 assets with valid scores after simulation.
        This exercises _build_asset_view() on handler threads, which reads
        from the DB via the thread-local connection.
        """
        status, body = _request(server, "GET", "/api/assets")
        assert status == 200
        assets = body.get("assets", [])
        assert len(assets) == 8
        for a in assets:
            assert 0.0 <= a["overall_risk"] <= 100.0, (
                f"{a['id']} overall_risk={a['overall_risk']} out of range"
            )

    def test_07_plan_endpoint_returns_pending_plan(self, server):
        """GET /api/simulation/plan should return a simulation-driven plan."""
        status, body = _request(server, "GET", "/api/simulation/plan")
        assert status == 200
        assert "maintenance_plan" in body
        assert len(body["maintenance_plan"]) == 8
        # Peak storm phase → plan should have a simulation phase label
        assert body.get("simulation_phase") is not None

    def test_08_approve_plan_succeeds(self, server):
        """
        POST /api/simulation/approve marks the plan as operator-approved.
        This also exercises a write operation (updating plan state) on a
        handler thread.
        """
        status, body = _request(
            server, "POST", "/api/simulation/approve", {"approved_by": "regression_test"}
        )
        assert status == 200, f"Expected 200, got {status}: {body}"
        assert body.get("approved") is True
        assert body.get("approved_by") == "regression_test"
        assert "tasks" in body

    def test_09_reject_after_approve_returns_error(self, server):
        """
        POST /api/simulation/reject with no pending plan must return an error,
        not a server crash.  (Plan was consumed by approve in previous test.)
        """
        status, body = _request(server, "POST", "/api/simulation/reject")
        # No pending plan → error response
        assert "error" in body

    def test_10_advance_after_approval_creates_new_pending_plan(self, server):
        """Advancing from peak_storm creates a new pending plan (reorder detection)."""
        status, body = _request(server, "POST", "/api/simulation/advance")
        assert status == 200
        assert body.get("phase") == "degradation_peak"
        assert body.get("plan_pending_approval") is True

    def test_11_stop_restores_static_data(self, server):
        """
        POST /api/simulation/stop must succeed on a handler thread and
        restore the static demo scores.
        """
        status, body = _request(server, "POST", "/api/simulation/stop")
        assert status == 200, f"Expected 200, got {status}: {body}"
        assert body.get("active") is False

    def test_12_assets_match_static_after_stop(self, server):
        """After stop, /api/assets must return the original static scores."""
        status, body = _request(server, "GET", "/api/assets")
        assert status == 200
        assets = body.get("assets", [])
        assert len(assets) == 8
        # TX-007 should still be Critical with a high risk score
        tx007 = next((a for a in assets if a["id"] == "TX-007"), None)
        assert tx007 is not None
        assert tx007["status"] == "Critical"
        assert tx007["overall_risk"] >= 85.0

    def test_13_second_simulation_cycle_works(self, server):
        """
        A second start→advance→stop cycle confirms the in-memory DB remains
        consistent (data seeded on startup is still readable after a full cycle).
        """
        # Start
        status, body = _request(server, "POST", "/api/simulation/start", {})
        assert status == 200
        assert body.get("active") is True

        # Advance through several phases
        for expected_phase in ["advisory", "storm_approaching", "peak_storm"]:
            s2, b2 = _request(server, "POST", "/api/simulation/advance")
            assert s2 == 200
            assert b2.get("phase") == expected_phase

        # Stop
        s3, b3 = _request(server, "POST", "/api/simulation/stop")
        assert s3 == 200
        assert b3.get("active") is False

        # Scores still valid
        s4, b4 = _request(server, "GET", "/api/assets")
        assert s4 == 200
        assert len(b4.get("assets", [])) == 8

    def test_14_priorities_endpoint_still_works(self, server):
        """GET /api/priorities must work after a simulation cycle (regression)."""
        status, body = _request(server, "GET", "/api/priorities")
        assert status == 200
        plan = body.get("maintenance_plan", [])
        assert len(plan) == 8
        assert plan[0]["asset_id"] == "TX-007"  # TX-007 still highest risk

    def test_15_concurrent_reads_do_not_crash(self, server):
        """
        Fire several GET /api/assets requests concurrently.  Each lands on a
        different handler thread.  All must return valid JSON with no crashes.
        """
        results: list[tuple[int, Any]] = []
        lock = threading.Lock()

        def do_request():
            s, b = _request(server, "GET", "/api/assets")
            with lock:
                results.append((s, b))

        threads = [threading.Thread(target=do_request) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert len(results) == 10
        for status, body in results:
            assert status == 200
            assert len(body.get("assets", [])) == 8


# ---------------------------------------------------------------------------
# Unit-level regression: ThreadLocalConnFactory contract
# ---------------------------------------------------------------------------

class TestThreadLocalConnFactory:
    """Direct unit tests for the connection factory."""

    def test_memory_db_same_connection_on_same_thread(self):
        """The same connection object must be returned on every acquire() call
        from the same thread for an in-memory database."""
        from storage.thread_local_conn import ThreadLocalConnFactory

        factory = ThreadLocalConnFactory(":memory:")
        c1 = factory.acquire()
        factory.release()
        c2 = factory.acquire()
        factory.release()
        assert c1 is c2, "In-memory DB must reuse the single shared connection"

    def test_memory_db_lock_serialises_threads(self):
        """
        Two threads acquiring the in-memory connection must be serialised:
        the second must wait until the first releases, not run concurrently.
        """
        from storage.thread_local_conn import ThreadLocalConnFactory

        factory = ThreadLocalConnFactory(":memory:")
        order: list[str] = []
        barrier = threading.Event()

        def thread_a():
            conn = factory.acquire()
            order.append("A-acquired")
            barrier.wait(timeout=2.0)  # hold the lock
            order.append("A-releasing")
            factory.release()

        def thread_b():
            # Wait until A has acquired, then try to acquire ourselves
            while "A-acquired" not in order:
                pass
            # This will block until A releases
            conn = factory.acquire()
            order.append("B-acquired")
            factory.release()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        # Let A hold the lock for a moment, then signal it to release
        import time
        time.sleep(0.05)
        barrier.set()
        ta.join(timeout=2.0)
        tb.join(timeout=2.0)

        # B must have acquired only AFTER A released
        assert order.index("B-acquired") > order.index("A-releasing"), (
            f"Lock did not serialise access: {order}"
        )

    def test_file_db_per_thread_connection(self, tmp_path):
        """Each thread gets its own connection for a file-backed database."""
        from storage.thread_local_conn import ThreadLocalConnFactory
        from storage.schema import create_all_tables

        db = str(tmp_path / "test.db")
        factory = ThreadLocalConnFactory(db)

        conns: list[object] = []
        lock = threading.Lock()

        def grab_conn():
            conn = factory.acquire()
            # No release for file-backed — connections persist per thread
            with lock:
                conns.append(id(conn))

        threads = [threading.Thread(target=grab_conn) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        # Each thread should have gotten a distinct connection object
        assert len(set(conns)) == 3, (
            f"Expected 3 distinct connections, got {len(set(conns))}: {conns}"
        )

    def test_memory_db_data_visible_across_threads(self):
        """
        Data written by the main thread must be readable from a worker thread
        via the shared in-memory connection.
        """
        from storage.thread_local_conn import ThreadLocalConnFactory

        factory = ThreadLocalConnFactory(":memory:")

        # Write on this thread
        with factory.connection() as conn:
            conn.execute("CREATE TABLE t (v INTEGER)")
            conn.execute("INSERT INTO t VALUES (42)")
            conn.commit()

        # Read from a worker thread
        read_values: list[int] = []

        def reader():
            with factory.connection() as conn:
                row = conn.execute("SELECT v FROM t").fetchone()
                if row:
                    read_values.append(row[0])

        t = threading.Thread(target=reader)
        t.start()
        t.join(timeout=2.0)

        assert read_values == [42], (
            f"Worker thread could not read data written by main thread: {read_values}"
        )

    def test_context_manager_releases_lock_on_exception(self):
        """The lock must be released even when the body raises an exception."""
        from storage.thread_local_conn import ThreadLocalConnFactory

        factory = ThreadLocalConnFactory(":memory:")
        try:
            with factory.connection() as conn:
                raise RuntimeError("intentional error")
        except RuntimeError:
            pass

        # Lock should be released — a second acquire must not deadlock.
        acquired = threading.Event()

        def try_acquire():
            with factory.connection():
                acquired.set()

        t = threading.Thread(target=try_acquire)
        t.start()
        t.join(timeout=1.0)
        assert acquired.is_set(), "Lock was not released after exception in context manager"
