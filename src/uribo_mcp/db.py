"""SQLite schema and connection helpers.

The schema deliberately keeps the legacy ``items`` contract while adding normalized
course/document/change tables. This lets the original MCP tools and the new course
tools share one independent database.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    url TEXT,
    course_name TEXT,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    published_at TEXT,
    deadline TEXT,
    importance TEXT,
    is_read INTEGER NOT NULL DEFAULT 0,
    is_done INTEGER NOT NULL DEFAULT 0,
    cluster_id INTEGER,
    content_hash TEXT,
    remote_updated_at TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    available INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_items_course ON items(course_id, category, available);
CREATE INDEX IF NOT EXISTS idx_items_deadline ON items(deadline);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_items_change ON items(updated_at, last_seen_at);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    idnumber TEXT NOT NULL,
    course_code TEXT,
    course_name TEXT NOT NULL,
    academic_year INTEGER,
    term TEXT,
    instructors_json TEXT NOT NULL DEFAULT '[]',
    schedule_text TEXT,
    summary TEXT,
    source_url TEXT,
    content_hash TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    available INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source, idnumber)
);

CREATE INDEX IF NOT EXISTS idx_courses_name ON courses(course_name);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    stable_key TEXT NOT NULL,
    content_id TEXT,
    resource_id TEXT,
    display_name TEXT,
    file_name TEXT NOT NULL,
    mime_type TEXT,
    source_url TEXT,
    object_name TEXT,
    scan_status TEXT,
    open_end_at TEXT,
    sha256 TEXT,
    size_bytes INTEGER,
    local_path TEXT,
    extract_status TEXT NOT NULL DEFAULT 'pending',
    extract_method TEXT,
    extracted_at TEXT,
    error_message TEXT,
    available INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(course_id, stable_key)
);

CREATE INDEX IF NOT EXISTS idx_attachments_item ON attachments(item_id);
CREATE INDEX IF NOT EXISTS idx_attachments_hash ON attachments(sha256);

CREATE TABLE IF NOT EXISTS document_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    text TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(attachment_id, page_number)
);

CREATE TABLE IF NOT EXISTS document_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    text TEXT NOT NULL,
    search_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(attachment_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_attachment ON document_chunks(attachment_id, chunk_index);

CREATE TABLE IF NOT EXISTS item_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    changed_fields_json TEXT NOT NULL DEFAULT '[]',
    before_json TEXT,
    after_json TEXT,
    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_revisions_detected ON item_revisions(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_revisions_item ON item_revisions(item_id, detected_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER REFERENCES item_revisions(id) ON DELETE SET NULL,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT,
    importance TEXT NOT NULL DEFAULT 'info',
    is_acknowledged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notifications_open
    ON notifications(is_acknowledged, created_at DESC);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    parameters_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    current_step TEXT,
    pid INTEGER,
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    error_message TEXT,
    result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_recent ON sync_jobs(requested_at DESC);

CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    destination TEXT NOT NULL,
    sha256 TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS timetable (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_of_week INTEGER NOT NULL,
    period INTEGER NOT NULL,
    course_name TEXT NOT NULL,
    room TEXT,
    quarter TEXT,
    idnumber TEXT
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    new_items INTEGER NOT NULL DEFAULT 0,
    updated_items INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    run_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_scrape_log_source ON scrape_log(source, run_at DESC);

CREATE TABLE IF NOT EXISTS task_snapshot (
    source TEXT NOT NULL DEFAULT 'beef',
    source_id TEXT NOT NULL,
    PRIMARY KEY(source, source_id)
);

CREATE TABLE IF NOT EXISTS course_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_name TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS user_preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SCHEMA_VERSION = 1

# Readers never wait on the WAL writer, so a short timeout only bounds abnormal cases.
READ_BUSY_TIMEOUT_S = 5


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a concurrency-friendly connection for desktop clients."""
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open a read-only connection that can never take the write lock.

    ``query_only`` turns any accidental write into ``sqlite3.OperationalError``.
    No ``journal_mode``/``synchronous`` pragmas are issued: those are writer
    concerns and the former needs a lock when the mode would change.
    """
    conn = sqlite3.connect(str(path), timeout=READ_BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    conn.execute(f"PRAGMA busy_timeout = {READ_BUSY_TIMEOUT_S * 1000}")
    return conn


def schema_version(conn: sqlite3.Connection) -> int | None:
    """Return the stored schema version, or ``None`` when the DB is uninitialized."""
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, declaration: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")


def initialize(conn: sqlite3.Connection) -> None:
    """Create the schema and migrate databases imported from KOBEDAI_dash."""
    # courses must exist before the items FK is parsed on a fresh database.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            idnumber TEXT NOT NULL,
            course_code TEXT,
            course_name TEXT NOT NULL,
            academic_year INTEGER,
            term TEXT,
            instructors_json TEXT NOT NULL DEFAULT '[]',
            schedule_text TEXT,
            summary TEXT,
            source_url TEXT,
            content_hash TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            available INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(source, idnumber)
        )"""
    )
    conn.executescript(SCHEMA)

    # Idempotent compatibility migration from the legacy schema.
    legacy_item_columns = {
        "course_id": "course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL",
        "content_hash": "content_hash TEXT",
        "remote_updated_at": "remote_updated_at TEXT",
        "first_seen_at": "first_seen_at TEXT",
        "last_seen_at": "last_seen_at TEXT",
        "available": "available INTEGER NOT NULL DEFAULT 1",
        "metadata_json": "metadata_json TEXT NOT NULL DEFAULT '{}'",
        "updated_at": "updated_at TEXT",
    }
    for name, declaration in legacy_item_columns.items():
        _ensure_column(conn, "items", name, declaration)
    _ensure_column(conn, "timetable", "idnumber", "idnumber TEXT")
    _ensure_column(conn, "scrape_log", "updated_items", "updated_items INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "task_snapshot", "source", "source TEXT NOT NULL DEFAULT 'beef'")
    _ensure_column(conn, "sync_jobs", "result_json", "result_json TEXT")

    # Writes below are guarded by SELECTs: even a zero-row UPDATE or a same-value
    # upsert takes the RESERVED lock and would wait on a running sync job.
    needs_backfill = conn.execute(
        "SELECT 1 FROM items WHERE first_seen_at IS NULL OR last_seen_at IS NULL "
        "OR updated_at IS NULL LIMIT 1"
    ).fetchone()
    if needs_backfill is not None:
        conn.execute(
            "UPDATE items SET first_seen_at = COALESCE(first_seen_at, created_at, datetime('now'))"
        )
        conn.execute(
            "UPDATE items SET last_seen_at = COALESCE(last_seen_at, created_at, datetime('now'))"
        )
        conn.execute(
            "UPDATE items SET updated_at = COALESCE(updated_at, created_at, datetime('now'))"
        )
    if schema_version(conn) != SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
    conn.commit()


@contextmanager
def database(path: str | Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Yield a connection to ``path``.

    With ``readonly=True`` and an existing file whose stored schema version is
    current, a ``query_only`` connection is yielded without running
    :func:`initialize`, so readers never touch the write lock. Otherwise (file
    missing, schema missing or outdated) the normal write path initializes it.
    """
    if readonly and str(path) != ":memory:" and Path(path).is_file():
        conn = connect_readonly(path)
        version = schema_version(conn)
        if version is not None and version >= SCHEMA_VERSION:
            try:
                yield conn
            finally:
                conn.close()
            return
        conn.close()
    conn = connect(path)
    try:
        initialize(conn)
        yield conn
    finally:
        conn.close()
