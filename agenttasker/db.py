"""SQLite storage layer. Single local file, WAL mode, no server."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'backlog',
    description TEXT NOT NULL DEFAULT '',
    evidence    TEXT NOT NULL DEFAULT '',
    blockers    TEXT NOT NULL DEFAULT '[]',   -- JSON array of free-form strings (external blockers)
    depends_on  TEXT NOT NULL DEFAULT '[]',   -- JSON array of task ids (hard dependencies)
    affects     TEXT NOT NULL DEFAULT '[]',   -- JSON array of task ids (informational links)
    owner       TEXT NOT NULL DEFAULT '',     -- current claim holder ('' = unclaimed)
    claimed_at  TEXT NOT NULL DEFAULT '',     -- when the current claim was taken
    priority    INTEGER NOT NULL DEFAULT 2,   -- 0=P0 (highest) .. 3=P3
    type        TEXT NOT NULL DEFAULT 'task', -- task|feature|bugfix|improvement|chore
    tags        TEXT NOT NULL DEFAULT '[]',   -- JSON array of strings (workstream/labels)
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project, status);

CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    task_id     INTEGER NOT NULL,
    filename    TEXT NOT NULL,
    relpath     TEXT NOT NULL,                -- relative to the attachments dir next to the db
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_task ON attachments(project, task_id);
"""

# columns added after initial release; older databases are migrated in place
_MIGRATIONS = (
    ("tasks", "owner", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "claimed_at", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "priority", "INTEGER NOT NULL DEFAULT 2"),
    ("tasks", "tags", "TEXT NOT NULL DEFAULT '[]'"),
    ("tasks", "type", "TEXT NOT NULL DEFAULT 'task'"),
)


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    for table, column, decl in _MIGRATIONS:
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()
    return conn
