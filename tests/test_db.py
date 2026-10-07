"""Write-free initialization and read-only connections."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from uribo_mcp import db
from uribo_mcp.db import SCHEMA_VERSION, connect, database, initialize


@pytest.mark.parametrize("state", ["current", "old", "missing_meta", "missing_version", "invalid", "empty", "missing_file", "memory"])
def test_all_readonly_paths_enforce_reader_contract(tmp_path, state):
    path = tmp_path / "reader.db"
    if state in {"current", "old", "missing_meta", "missing_version", "invalid"}:
        with database(path) as conn:
            if state == "missing_meta":
                conn.execute("DROP TABLE schema_meta")
            elif state == "missing_version":
                conn.execute("DELETE FROM schema_meta")
            elif state in {"old", "invalid"}:
                conn.execute("UPDATE schema_meta SET value=?", ("0" if state == "old" else "invalid",))
            conn.commit()
    elif state == "empty":
        sqlite3.connect(path).close()
    elif state == "memory":
        path = ":memory:"
    with database(path, readonly=True) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE items SET title='x'")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE accidental_write(x)")


def test_exclusive_lock_propagates_without_writer_fallback(tmp_path, monkeypatch):
    path = tmp_path / "locked.db"
    with database(path):
        pass
    blocker = sqlite3.connect(path)
    blocker.execute("PRAGMA journal_mode=DELETE")
    blocker.execute("BEGIN EXCLUSIVE")
    calls = []
    monkeypatch.setattr(db, "connect", lambda path: calls.append(path))
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with database(path, readonly=True):
                pytest.fail("must not yield under EXCLUSIVE lock")
        assert time.monotonic() - started >= 4.8
        assert calls == []
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.parametrize("readonly,pragma_index", [(False, 1), (False, 2), (False, 3), (False, 4), (True, 1), (True, 2)])
def test_connect_pragma_failure_closes_connection(tmp_path, monkeypatch, readonly, pragma_index):
    opened = []
    original = sqlite3.connect

    class Tracking(sqlite3.Connection):
        closed = False
        calls = 0

        def execute(self, sql, *args):
            self.calls += 1
            if self.calls == pragma_index:
                raise sqlite3.OperationalError("injected PRAGMA failure")
            return super().execute(sql, *args)

        def close(self):
            self.closed = True
            super().close()

    def tracked(*args, **kwargs):
        conn = original(*args, **kwargs, factory=Tracking)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db.sqlite3, "connect", tracked)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            (db.connect_readonly if readonly else db.connect)(tmp_path / "pragma.db")
        assert opened and all(conn.closed for conn in opened)
    finally:
        for conn in opened:
            conn.close()


@pytest.mark.parametrize("readonly", [False, True])
def test_schema_failure_closes_connection(tmp_path, monkeypatch, readonly):
    path = tmp_path / "schema.db"
    with database(path):
        pass
    opened = []
    original = sqlite3.connect

    class Tracking(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def tracked(*args, **kwargs):
        conn = original(*args, **kwargs, factory=Tracking)
        opened.append(conn)
        return conn

    def fail(conn):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db.sqlite3, "connect", tracked)
    monkeypatch.setattr(db, "schema_version", fail)
    try:
        with pytest.raises(sqlite3.OperationalError, match="I/O"):
            with database(path, readonly=readonly):
                pytest.fail("must not yield")
        assert opened and all(conn.closed for conn in opened)
    finally:
        for conn in opened:
            conn.close()



@pytest.mark.parametrize("message", ["database is locked", "database table is locked", "disk I/O error"])
def test_schema_inspection_operational_errors_propagate(message):
    class Broken(sqlite3.Connection):
        def execute(self, *args):
            raise sqlite3.OperationalError(message)

    conn = sqlite3.connect(":memory:", factory=Broken)
    try:
        with pytest.raises(sqlite3.OperationalError, match=message):
            db.schema_version(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("state", ["missing_file", "old", "memory"])
@pytest.mark.parametrize("failure", ["initialize", "query_only", "busy_timeout"])
def test_migration_and_reader_setup_failures_close_all(tmp_path, monkeypatch, state, failure):
    path = tmp_path / "fallback.db"
    if state == "old":
        with database(path) as conn:
            conn.execute("UPDATE schema_meta SET value='0'")
            conn.commit()
    elif state == "memory":
        path = ":memory:"
    original = sqlite3.connect
    opened = []
    initialized = False

    class Tracking(sqlite3.Connection):
        closed = False

        def execute(self, sql, *args):
            if initialized and failure in sql:
                raise sqlite3.OperationalError("injected reader setup")
            return super().execute(sql, *args)

        def close(self):
            self.closed = True
            super().close()

    def tracked(*args, **kwargs):
        conn = original(*args, **kwargs, factory=Tracking)
        opened.append(conn)
        return conn

    def initializing(conn):
        nonlocal initialized
        if failure == "initialize":
            raise sqlite3.OperationalError("injected migration")
        initialize(conn)
        initialized = True

    monkeypatch.setattr(db.sqlite3, "connect", tracked)
    monkeypatch.setattr(db, "initialize", initializing)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            with database(path, readonly=True):
                pytest.fail("must not yield")
        assert opened and all(conn.closed for conn in opened)
    finally:
        for conn in opened:
            conn.close()


def test_corrupt_schema_inspection_closes_reader(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a SQLite database" * 100)
    opened = []
    original = sqlite3.connect

    class Tracking(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def tracked(*args, **kwargs):
        conn = original(*args, **kwargs, factory=Tracking)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db.sqlite3, "connect", tracked)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not a database"):
            with database(path, readonly=True):
                pytest.fail("must not yield")
        assert len(opened) == 1 and opened[0].closed
    finally:
        for conn in opened:
            conn.close()


def _seed_items(conn: sqlite3.Connection, count: int = 20) -> None:
    conn.executemany(
        "INSERT INTO items(source, source_id, category, title) VALUES ('beef', ?, 'material', ?)",
        [(f"m/{index}", f"資料{index}") for index in range(count)],
    )
    conn.commit()


def _wal_size(path: Path) -> int:
    wal = path.with_name(path.name + "-wal")
    return wal.stat().st_size if wal.exists() else 0


@contextmanager
def _writer_lock(path: Path, hold_seconds: float) -> Iterator[None]:
    """Hold BEGIN IMMEDIATE on ``path`` from another thread for ``hold_seconds``."""
    locked = threading.Event()
    release = threading.Event()

    def run() -> None:
        writer = sqlite3.connect(str(path), timeout=1, check_same_thread=False)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO schema_meta(key, value) VALUES('lock_probe', 'x')")
        locked.set()
        release.wait(hold_seconds)
        writer.rollback()
        writer.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert locked.wait(5)
    try:
        yield
    finally:
        release.set()
        thread.join(5)


def test_initialize_second_run_makes_no_writes(tmp_path: Path) -> None:
    path = tmp_path / "data.db"
    with database(path) as conn:
        _seed_items(conn)
    conn = connect(path)
    try:
        initialize(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        wal_before = _wal_size(path)
        changes_before = conn.total_changes
        initialize(conn)
        assert conn.total_changes == changes_before
        assert _wal_size(path) == wal_before
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 20
    finally:
        conn.close()


def test_initialize_does_not_wait_for_writer_lock(tmp_path: Path) -> None:
    path = tmp_path / "data.db"
    with database(path) as conn:
        _seed_items(conn)
    with _writer_lock(path, hold_seconds=1.5):
        conn = sqlite3.connect(str(path), timeout=0.3)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 300")
        try:
            started = time.perf_counter()
            initialize(conn)
            elapsed = time.perf_counter() - started
        finally:
            conn.close()
    assert elapsed < 0.2, f"initialize() waited {elapsed:.3f}s for the writer"


def test_initialize_backfills_null_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "data.db"
    with database(path) as conn:
        _seed_items(conn, count=3)
        # Rows imported from KOBEDAI_dash get the timestamp columns via ALTER TABLE
        # (nullable, no default) and arrive with NULLs; emulate that shape.
        conn.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE items_legacy AS SELECT * FROM items;
            DROP TABLE items;
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                url TEXT,
                course_name TEXT,
                course_id INTEGER,
                published_at TEXT,
                deadline TEXT,
                importance TEXT,
                is_read INTEGER NOT NULL DEFAULT 0,
                is_done INTEGER NOT NULL DEFAULT 0,
                cluster_id INTEGER,
                content_hash TEXT,
                remote_updated_at TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                available INTEGER NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT,
                UNIQUE(source, source_id)
            );
            INSERT INTO items(id, source, source_id, category, title, created_at)
                SELECT id, source, source_id, category, title, '2024-04-01 00:00:00'
                FROM items_legacy;
            DROP TABLE items_legacy;
            """
        )
        conn.commit()
        assert conn.execute(
            "SELECT count(*) FROM items WHERE first_seen_at IS NULL"
        ).fetchone()[0] == 3
    with database(path) as conn:
        rows = conn.execute(
            "SELECT first_seen_at, last_seen_at, updated_at FROM items ORDER BY id"
        ).fetchall()
    assert len(rows) == 3
    for row in rows:
        assert row["first_seen_at"] == "2024-04-01 00:00:00"
        assert row["last_seen_at"] == "2024-04-01 00:00:00"
        assert row["updated_at"] == "2024-04-01 00:00:00"
    # Once backfilled the next initialize() is write-free again.
    conn = connect(path)
    try:
        before = conn.total_changes
        initialize(conn)
        assert conn.total_changes == before
    finally:
        conn.close()


def test_readonly_database_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "data.db"
    with database(path) as conn:
        _seed_items(conn, count=2)
    with database(path, readonly=True) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE items SET title='x'")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE scratch(x)")
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 2


def test_readonly_database_initializes_when_schema_missing(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    assert not path.exists()
    with database(path, readonly=True) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"items", "courses", "document_chunks", "schema_meta"} <= tables
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
    # A file without schema_meta (empty database) also falls through to initialization.
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()
    with database(bare, readonly=True) as conn:
        assert conn.execute("SELECT count(*) FROM schema_meta").fetchone()[0] >= 1


def test_readonly_database_skips_initialize_when_schema_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data.db"
    with database(path) as conn:
        _seed_items(conn, count=1)

    def boom(conn: sqlite3.Connection) -> None:  # pragma: no cover - must not run
        raise AssertionError("initialize() must not run for a current read-only DB")

    monkeypatch.setattr("uribo_mcp.db.initialize", boom)
    with database(path, readonly=True) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_readonly_database_reads_under_writer_lock(tmp_path: Path) -> None:
    path = tmp_path / "data.db"
    with database(path) as conn:
        _seed_items(conn)
    with _writer_lock(path, hold_seconds=1.5):
        started = time.perf_counter()
        with database(path, readonly=True) as conn:
            count = conn.execute("SELECT count(*) FROM items").fetchone()[0]
        elapsed = time.perf_counter() - started
    assert count == 20
    assert elapsed < 0.1, f"read-only open+SELECT took {elapsed:.3f}s under writer lock"
