"""SQLite storage layer. Single local file, WAL mode, no server.

Task ids are PER-PROJECT (dense 1..N): the primary key is (project, id).
Databases from the older global-id layout are migrated in place on first
open — tasks are renumbered per project and every depends_on/affects
reference and attachment row is remapped.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    project     TEXT NOT NULL,
    id          INTEGER NOT NULL,
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
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (project, id)
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


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in sorted((r for r in rows if r["pk"]), key=lambda r: r["pk"])]


def _migrate_global_ids(conn: sqlite3.Connection) -> None:
    """One-time renumbering from the legacy global-id layout to per-project ids.

    Runs inside a single transaction; every depends_on/affects reference and
    attachments.task_id is remapped to the new ids.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute("SELECT * FROM tasks ORDER BY project, id").fetchall()
        remap: dict[str, dict[int, int]] = {}
        new_rows = []
        counter: dict[str, int] = {}
        for row in rows:
            project = row["project"]
            counter[project] = counter.get(project, 0) + 1
            remap.setdefault(project, {})[row["id"]] = counter[project]
            new_rows.append((row, counter[project]))

        conn.execute("ALTER TABLE tasks RENAME TO tasks_legacy")
        conn.executescript(SCHEMA)
        for row, new_id in new_rows:
            project = row["project"]
            deps = [remap[project].get(i, i) for i in json.loads(row["depends_on"] or "[]")]
            links = [remap[project].get(i, i) for i in json.loads(row["affects"] or "[]")]
            conn.execute(
                "INSERT INTO tasks (project, id, name, status, description, evidence, blockers,"
                " depends_on, affects, owner, claimed_at, priority, type, tags,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    project, new_id, row["name"], row["status"], row["description"],
                    row["evidence"], row["blockers"],
                    json.dumps(deps), json.dumps(links),
                    row["owner"], row["claimed_at"], row["priority"], row["type"], row["tags"],
                    row["created_at"], row["updated_at"],
                ),
            )
        for project, mapping in remap.items():
            for old_id, new_id in mapping.items():
                if old_id != new_id:
                    conn.execute(
                        "UPDATE attachments SET task_id=? WHERE project=? AND task_id=?",
                        (new_id, project, old_id),
                    )
        conn.execute("DROP TABLE tasks_legacy")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='tasks'")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    fresh = len(conn.execute("PRAGMA table_info(tasks)").fetchall()) == 0
    conn.executescript(SCHEMA)
    if not fresh and _pk_columns(conn, "tasks") == ["id"]:
        _migrate_global_ids(conn)  # legacy layout: global autoincrement ids
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    for table, column, decl in _MIGRATIONS:
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()
    return conn
