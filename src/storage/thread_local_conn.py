"""
Thread-safe SQLite connection management for GridGuard.

Problem
-------
``ThreadingHTTPServer`` dispatches each request on a new thread.
SQLite connections created on the main thread (T0) cannot be used on handler
threads (T1, T2, …) without triggering::

    sqlite3.ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread.

Disabling the check via ``check_same_thread=False`` would silence the error but
still allow concurrent writes to race against each other on the same connection.

Solution
--------
``ThreadLocalConnFactory`` provides a ``connection()`` context manager that
returns the correct connection for the **calling thread**:

* **File-backed databases** (``gridguard.db``): each thread opens its own
  connection to the same database file.  SQLite WAL mode (already configured)
  serialises concurrent writers at the OS/SQLite level; multiple readers run
  concurrently.  Connection objects are cached in a ``threading.local`` so they
  are opened at most once per thread.

* **In-memory databases** (``":memory:"``): a single connection is shared
  (opening a second ``":memory:"`` connection creates a completely separate
  empty database, which would not see the seeded data).  A ``threading.Lock``
  serialises all access, making the connection safe to use from any thread
  one at a time.

Usage
-----
::

    factory = ThreadLocalConnFactory("gridguard.db")
    with factory.connection() as conn:
        conn.execute("SELECT 1")

    # or just acquire without context manager:
    conn = factory.acquire()
    conn.execute("SELECT 1")
    # (no release needed for file-backed; lock released automatically for memory)

``GridState`` holds one ``ThreadLocalConnFactory`` and calls
``factory.acquire()`` whenever it needs a connection, replacing the previous
``self.conn`` single-connection pattern.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Apply the standard GridGuard connection settings."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class ThreadLocalConnFactory:
    """
    Manages SQLite connections for a multi-threaded HTTP server.

    Parameters
    ----------
    path:
        File-system path to the SQLite database, or ``":memory:"`` for an
        in-memory database.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._is_memory = (self._path == ":memory:")

        if self._is_memory:
            # Single shared connection + lock for in-memory databases.
            # Must be created here (main thread) so that seeded data persists.
            self._memory_conn: sqlite3.Connection = _configure(
                sqlite3.connect(self._path, check_same_thread=False)
            )
            self._memory_lock = threading.Lock()
        else:
            # Per-thread connections for file-backed databases.
            self._local = threading.local()

    def acquire(self) -> sqlite3.Connection:
        """
        Return the SQLite connection appropriate for the **calling thread**.

        For file-backed databases this opens a new per-thread connection on
        first call and reuses it on subsequent calls from the same thread.

        For in-memory databases this acquires the shared lock and returns the
        single shared connection.  Callers **must** call ``release()`` (or use
        the ``connection()`` context manager) to release the lock.
        """
        if self._is_memory:
            self._memory_lock.acquire()
            return self._memory_conn
        # File-backed: per-thread connection
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = _configure(sqlite3.connect(self._path))
        return self._local.conn

    def release(self) -> None:
        """
        Release the connection lock.

        Only meaningful for in-memory databases (releases the shared lock).
        For file-backed databases this is a no-op; connections remain open
        for the lifetime of the thread.
        """
        if self._is_memory:
            self._memory_lock.release()

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager that yields the thread-appropriate connection and
        releases any lock on exit (even on exception).

        Example::

            with factory.connection() as conn:
                conn.execute("INSERT INTO ...")
        """
        conn = self.acquire()
        try:
            yield conn
        finally:
            self.release()

    def close_all(self) -> None:
        """
        Close the shared in-memory connection (for test teardown).

        For file-backed databases this is a no-op; per-thread connections are
        managed by thread lifetime.
        """
        if self._is_memory:
            with self._memory_lock:
                try:
                    self._memory_conn.close()
                except Exception:
                    pass
