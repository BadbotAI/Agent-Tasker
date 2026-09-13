"""Domain core: statuses, task model, project resolution, task references, Store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .db import connect

# Canonical board order, left to right.
STATUSES: tuple[str, ...] = (
    "deferred",
    "backlog",
    "todo",
    "in_progress",
    "in_review",
    "done",
)
DEFERRED, BACKLOG, TODO, IN_PROGRESS, IN_REVIEW, DONE = STATUSES
STARTABLE_STATUSES = (BACKLOG, TODO)
# list-view order: actionable first, in-flight next, done at the bottom
LIST_STATUS_ORDER: tuple[str, ...] = (TODO, BACKLOG, DEFERRED, IN_PROGRESS, IN_REVIEW, DONE)

# Priority levels: 0 is highest.
PRIORITIES: tuple[int, ...] = (0, 1, 2, 3)
DEFAULT_PRIORITY = 2
PRIORITY_LABELS = ("P0", "P1", "P2", "P3")

# Task types.
TASK_TYPES: tuple[str, ...] = ("task", "feature", "bugfix", "improvement", "chore")
DEFAULT_TYPE = "task"

EXPORT_FORMAT = "agenttasker-export"
EXPORT_VERSION = 1

_STATUS_ALIASES = {
    "inprogress": IN_PROGRESS,
    "in_review": IN_REVIEW,
    "inreview": IN_REVIEW,
    "ip": IN_PROGRESS,
    "ir": IN_REVIEW,
    "wip": IN_PROGRESS,
    "open": TODO,
    "new": BACKLOG,
    "closed": DONE,
    "complete": DONE,
    "completed": DONE,
}


class TaskError(Exception):
    """User-facing error: bad input, missing task, invalid transition."""


def status_label(status: str) -> str:
    return {
        "deferred": "Deferred",
        "backlog": "Backlog",
        "todo": "To Do",
        "in_progress": "In Progress",
        "in_review": "In Review",
        "done": "Done",
    }.get(status, status)


def priority_label(priority: int) -> str:
    return PRIORITY_LABELS[priority] if 0 <= priority < len(PRIORITY_LABELS) else str(priority)


def normalize_type(value) -> str:
    """Accept task/feature/bugfix|bug|fix/improvement|refactor/chore -> canonical type."""
    text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {"bug": "bugfix", "fix": "bugfix", "feat": "feature",
               "refactor": "improvement", "cleanup": "chore", "maintenance": "chore"}
    text = aliases.get(text, text)
    if text not in TASK_TYPES:
        raise TaskError(f"unknown type {value!r}; expected one of: {', '.join(TASK_TYPES)}")
    return text


def normalize_priority(value) -> int:
    """Accept 'P0'/'p1'/'2'/2 -> int priority (0=highest .. 3)."""
    text = str(value).strip().upper().lstrip("P")
    if not text.isdigit() or int(text) not in PRIORITIES:
        raise TaskError(f"unknown priority {value!r}; expected one of: {', '.join(PRIORITY_LABELS)}")
    return int(text)


def parse_tags(values: Iterable[str]) -> list[str]:
    """Flatten repeatable tag flags (each may be comma-separated) preserving order."""
    out: list[str] = []
    for value in values:
        for part in str(value).split(","):
            part = part.strip().lstrip("#")
            if part and part not in out:
                out.append(part)
    return out


def claim_age_hours(task: "Task", now: datetime | None = None) -> float | None:
    """Hours since the claim was taken, or None when unclaimed/unparseable."""
    if not task.claimed_at:
        return None
    try:
        claimed = datetime.fromisoformat(task.claimed_at)
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    if claimed.tzinfo is None:
        claimed = claimed.replace(tzinfo=timezone.utc)
    return (now - claimed).total_seconds() / 3600.0


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_status(value: str) -> str:
    text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    text = _STATUS_ALIASES.get(text, text)
    if text not in STATUSES:
        raise TaskError(f"unknown status {value!r}; expected one of: {', '.join(STATUSES)}")
    return text


def next_status(status: str) -> str:
    i = STATUSES.index(status)
    if i + 1 >= len(STATUSES):
        raise TaskError(f"task is already {DONE}")
    return STATUSES[i + 1]


def prev_status(status: str) -> str:
    i = STATUSES.index(status)
    if i == 0:
        raise TaskError(f"task is already {DEFERRED}")
    return STATUSES[i - 1]


def default_db_path() -> Path:
    env = os.environ.get("AGENTTASKER_DB")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "agenttasker" / "tasks.db"


def _git_toplevel_name() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).name or None


def resolve_project(explicit: str | None = None) -> str:
    """Project namespace: explicit flag > $AGENTTASKER_PROJECT > git repo name > cwd name."""
    if explicit is not None:
        name = explicit.strip()
        if not name:
            raise TaskError("project name must not be empty")
        return name
    env = os.environ.get("AGENTTASKER_PROJECT", "").strip()
    if env:
        return env
    return _git_toplevel_name() or Path.cwd().name or "default"


def parse_ref(ref: str, project: str) -> int:
    """Accept '12', '#12', or 'ProjectName-12' (case-insensitive prefix). Returns task id."""
    text = str(ref).strip().lstrip("#")
    if not text:
        raise TaskError("empty task reference")
    if "-" in text:
        prefix, _, num = text.rpartition("-")
        if not prefix or not num.isdigit():
            raise TaskError(f"invalid task reference {ref!r}")
        if prefix.strip().lower() not in (project.lower(), project.lower().replace(" ", "-")):
            raise TaskError(
                f"reference {ref!r} names project {prefix!r}, but the current project is {project!r}"
            )
        return int(num)
    if not text.isdigit():
        raise TaskError(f"invalid task reference {ref!r} (expected '<id>' or '{project}-<id>')")
    return int(text)


def display_ref(project: str, task_id: int) -> str:
    return f"{project}-{task_id}"


# --- derived state -----------------------------------------------------------
# dep_statuses maps task id -> status for the task's dependencies (same project).

def unfinished_dep_ids(task: "Task", dep_statuses: Mapping[int, str]) -> list[int]:
    return [
        dep_id
        for dep_id in task.depends_on
        if dep_statuses.get(dep_id, "") != DONE  # missing dep counts as blocking
    ]


def is_blocked(task: "Task", dep_statuses: Mapping[int, str]) -> bool:
    if task.status == DONE:
        return False
    return bool(task.blockers) or bool(unfinished_dep_ids(task, dep_statuses))

def is_ready(task: "Task", dep_statuses: Mapping[int, str]) -> bool:
    if task.status not in STARTABLE_STATUSES:
        return False
    if task.blockers:
        return False
    return not unfinished_dep_ids(task, dep_statuses)


# --- task model --------------------------------------------------------------

def _decode_list(raw: str) -> list:
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


@dataclass
class Task:
    id: int
    project: str
    name: str
    status: str = BACKLOG
    description: str = ""
    evidence: str = ""
    blockers: list[str] = field(default_factory=list)   # free-form external blockers
    depends_on: list[int] = field(default_factory=list)  # hard deps: task ids
    affects: list[int] = field(default_factory=list)     # informational: task ids
    owner: str = ""                                      # claim holder ('' = unclaimed)
    claimed_at: str = ""
    priority: int = DEFAULT_PRIORITY
    type: str = DEFAULT_TYPE
    tags: list[str] = field(default_factory=list)        # workstream / labels
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row) -> "Task":
        return cls(
            id=row["id"],
            project=row["project"],
            name=row["name"],
            status=row["status"],
            description=row["description"],
            evidence=row["evidence"],
            blockers=_decode_list(row["blockers"]),
            depends_on=_decode_list(row["depends_on"]),
            affects=_decode_list(row["affects"]),
            owner=row["owner"],
            claimed_at=row["claimed_at"],
            priority=row["priority"] if isinstance(row["priority"], int) else DEFAULT_PRIORITY,
            type=row["type"],
            tags=_decode_list(row["tags"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ref": display_ref(self.project, self.id),
            "project": self.project,
            "name": self.name,
            "status": self.status,
            "priority": self.priority,
            "type": self.type,
            "tags": list(self.tags),
            "owner": self.owner,
            "claimed_at": self.claimed_at,
            "description": self.description,
            "evidence": self.evidence,
            "blockers": list(self.blockers),
            "depends_on": list(self.depends_on),
            "affects": list(self.affects),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# --- store -------------------------------------------------------------------
_UPDATABLE = (
    "name", "status", "description", "evidence", "blockers",
    "depends_on", "affects", "priority", "type", "tags",
)


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path else default_db_path()
        self.db = connect(self.path)

    # lifecycle
    def close(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- CRUD
    def add_task(
        self,
        project: str,
        name: str,
        *,
        description: str = "",
        status: str = BACKLOG,
        evidence: str = "",
        blockers: Sequence[str] = (),
        depends_on: Sequence[str | int] = (),
        affects: Sequence[str | int] = (),
        priority=DEFAULT_PRIORITY,
        type=DEFAULT_TYPE,
        tags: Sequence[str] = (),
    ) -> Task:
        name = name.strip()
        if not name:
            raise TaskError("task name must not be empty")
        status = normalize_status(status)
        priority = normalize_priority(priority)
        type_ = normalize_type(type)
        deps = self._validate_refs(project, depends_on)
        links = self._validate_refs(project, affects)
        now = utcnow()
        cur = None
        for _ in range(8):  # per-project allocation; retry on concurrent writer races
            next_id = self.db.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM tasks WHERE project=?", (project,)
            ).fetchone()[0]
            try:
                cur = self.db.execute(
                    "INSERT INTO tasks (project, id, name, status, description, evidence, blockers,"
                    " depends_on, affects, owner, claimed_at, priority, type, tags,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        project, next_id, name, status, description, evidence,
                        json.dumps([str(b).strip() for b in blockers if str(b).strip()]),
                        json.dumps(deps), json.dumps(links),
                        "", "", priority, type_, json.dumps(parse_tags(tags)), now, now,
                    ),
                )
                break
            except sqlite3.IntegrityError:
                continue  # another writer took this id; loop allocates the next one
        if cur is None:
            raise TaskError(f"could not allocate a task id for project {project!r}")
        self.db.commit()
        task = self.get_by_id(project, next_id)
        assert task is not None
        return task

    def get_by_id(self, project: str, task_id: int) -> Task | None:
        row = self.db.execute(
            "SELECT * FROM tasks WHERE project=? AND id=?", (project, task_id)
        ).fetchone()
        return Task.from_row(row) if row else None

    def get(self, project: str, ref: str) -> Task:
        task_id = parse_ref(ref, project)
        task = self.get_by_id(project, task_id)
        if task is None:
            raise TaskError(f"no task {display_ref(project, task_id)} in project {project!r}")
        return task

    def list_tasks(
        self,
        project: str | None = None,
        *,
        statuses: Sequence[str] | None = None,
        search: str | None = None,
        priorities: Sequence[int] | None = None,
        tags: Sequence[str] | None = None,
        stale_hours: float | None = None,
    ) -> list[Task]:
        query = "SELECT * FROM tasks"
        conds: list[str] = []
        args: list = []
        if project is not None:
            conds.append("project=?")
            args.append(project)
        if statuses:
            wanted = [normalize_status(s) for s in statuses]
            conds.append(f"status IN ({','.join('?' * len(wanted))})")
            args.extend(wanted)
        if priorities:
            wanted_p = [normalize_priority(p) for p in priorities]
            conds.append(f"priority IN ({','.join('?' * len(wanted_p))})")
            args.extend(wanted_p)
        if tags:
            for tag in tags:
                conds.append("tags LIKE ?")
                args.append(f'%"{tag.replace("%", "")}"%')
        if search:
            for term in search.split():
                conds.append("(name LIKE ? OR description LIKE ? OR evidence LIKE ? OR tags LIKE ?)")
                like = f"%{term}%"
                args.extend([like, like, like, like])
        if stale_hours is not None:
            conds.append("owner != ''")
        if conds:
            query += " WHERE " + " AND ".join(conds)
        rows = self.db.execute(query, args).fetchall()
        tasks = [Task.from_row(r) for r in rows]
        # board order, then priority (P0 first), then age
        tasks.sort(key=lambda t: (STATUSES.index(t.status), t.priority, t.id))
        if stale_hours is not None:
            tasks = [
                t for t in tasks
                if (age := claim_age_hours(t)) is not None and age >= stale_hours
            ]
        return tasks

    def update(self, task: Task, **changes) -> Task:
        unknown = set(changes) - set(_UPDATABLE)
        if unknown:
            raise TaskError(f"cannot update field(s): {', '.join(sorted(unknown))}")
        values = dict(changes)
        if "name" in values:
            values["name"] = values["name"].strip()
            if not values["name"]:
                raise TaskError("task name must not be empty")
        if "status" in values:
            values["status"] = normalize_status(values["status"])
        if "priority" in values:
            values["priority"] = normalize_priority(values["priority"])
        if "type" in values:
            values["type"] = normalize_type(values["type"])
        for key in ("depends_on", "affects"):
            if key in values:
                values[key] = self._validate_refs(task.project, values[key], exclude_self=task.id)
        if "blockers" in values:
            values["blockers"] = [str(b).strip() for b in values["blockers"] if str(b).strip()]
        if "tags" in values:
            values["tags"] = parse_tags(values["tags"])
        if not values:
            raise TaskError("nothing to update")
        values["updated_at"] = utcnow()
        row_values = [json.dumps(v) if isinstance(v, list) else v for v in values.values()]
        assignments = ", ".join(f"{k}=?" for k in values)
        self.db.execute(
            f"UPDATE tasks SET {assignments} WHERE project=? AND id=?",
            (*row_values, task.project, task.id),
        )
        self.db.commit()
        updated = self.get_by_id(task.project, task.id)
        assert updated is not None
        return updated

    def append_evidence(self, task: Task, text: str) -> Task:
        """Atomically append a timestamped evidence line (SQL-side concat; safe
        against concurrent writers, unlike read-then-replace)."""
        text = text.strip()
        if not text:
            raise TaskError("evidence text must not be empty")
        stamp = utcnow().replace("+00:00", "Z")
        separator = "" if not task.evidence.strip() else "\n\n"
        self.db.execute(
            "UPDATE tasks SET evidence = evidence || ?, updated_at=? WHERE project=? AND id=?",
            (f"{separator}[{stamp}] {text}", utcnow(), task.project, task.id),
        )
        self.db.commit()
        updated = self.get_by_id(task.project, task.id)
        assert updated is not None
        return updated

    def set_status(self, task: Task, status: str) -> Task:
        return self.update(task, status=normalize_status(status))

    # --- atomic ownership ----------------------------------------------------
    def claim(self, task: Task, owner: str, *, force: bool = False) -> Task:
        """Atomically take ownership (also moves to in_progress). Refuses if
        another owner holds the claim, unless force."""
        owner = owner.strip()
        if not owner:
            raise TaskError("owner name must not be empty")
        now = utcnow()
        cur = self.db.execute(
            "UPDATE tasks SET owner=?, claimed_at=?, status=?, updated_at=?"
            " WHERE project=? AND id=? AND (owner='' OR owner=? OR ?)",
            (owner, now, IN_PROGRESS, now, task.project, task.id, owner, int(force)),
        )
        self.db.commit()
        if cur.rowcount == 0:
            fresh = self.get_by_id(task.project, task.id)
            holder = fresh.owner if fresh else "?"
            raise TaskError(
                f"{display_ref(task.project, task.id)} already claimed by {holder!r}"
                f" (since {(fresh.claimed_at if fresh else '?')}); use --force to take it"
            )
        updated = self.get_by_id(task.project, task.id)
        assert updated is not None
        return updated

    def release(self, task: Task, *, owner: str | None = None, force: bool = False) -> Task:
        """Clear the claim. Succeeds when unclaimed, or when `owner` matches the
        holder, or with force."""
        cur = self.db.execute(
            "UPDATE tasks SET owner='', claimed_at='', updated_at=?"
            " WHERE project=? AND id=? AND (owner='' OR owner=? OR ?)",
            (utcnow(), task.project, task.id, owner or "", int(force)),
        )
        self.db.commit()
        if cur.rowcount == 0:
            fresh = self.get_by_id(task.project, task.id)
            holder = fresh.owner if fresh else "?"
            raise TaskError(
                f"{display_ref(task.project, task.id)} is claimed by {holder!r};"
                " release with the matching --owner or --force"
            )
        updated = self.get_by_id(task.project, task.id)
        assert updated is not None
        return updated

    def handoff(self, task: Task, new_owner: str, *, from_owner: str = "", force: bool = False) -> Task:
        """Atomically transfer the claim to new_owner. Requires from_owner to be
        the current holder (or force)."""
        new_owner = new_owner.strip()
        if not new_owner:
            raise TaskError("owner name must not be empty")
        now = utcnow()
        cur = self.db.execute(
            "UPDATE tasks SET owner=?, claimed_at=?, updated_at=?"
            " WHERE project=? AND id=? AND (owner='' OR owner=? OR ?)",
            (new_owner, now, now, task.project, task.id, from_owner.strip(), int(force)),
        )
        self.db.commit()
        if cur.rowcount == 0:
            fresh = self.get_by_id(task.project, task.id)
            holder = fresh.owner if fresh else "?"
            raise TaskError(
                f"{display_ref(task.project, task.id)} is claimed by {holder!r};"
                " handoff requires the current owner (or --force)"
            )
        updated = self.get_by_id(task.project, task.id)
        assert updated is not None
        return updated

    def delete(self, task: Task) -> list[str]:
        """Delete a task, its attachments, and scrub refs in other tasks."""
        self.delete_task_files(task)
        self.db.execute(
            "DELETE FROM tasks WHERE project=? AND id=?", (task.project, task.id)
        )
        notes: list[str] = []
        for other in self.list_tasks(task.project):
            patch: dict[str, list] = {}
            for key in ("depends_on", "affects"):
                ids = getattr(other, key)
                if task.id in ids:
                    patch[key] = [i for i in ids if i != task.id]
            if patch:
                self.update(other, **patch)
                notes.append(
                    f"removed reference from {display_ref(other.project, other.id)} ({other.name})"
                )
        self.db.commit()
        return notes

    def projects(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        rows = self.db.execute(
            "SELECT project, status, COUNT(*) AS n FROM tasks GROUP BY project, status"
        ).fetchall()
        for row in rows:
            bucket = counts.setdefault(row["project"], {s: 0 for s in STATUSES} | {"total": 0})
            bucket[row["status"]] = row["n"]
            bucket["total"] += row["n"]
        return dict(sorted(counts.items()))

    # --- relations and derived state
    def _validate_refs(
        self, project: str, refs: Iterable[str | int], exclude_self: int | None = None
    ) -> list[int]:
        out: list[int] = []
        for ref in refs:
            task_id = parse_ref(str(ref), project)
            if task_id == exclude_self:
                key = "dependency" if exclude_self is not None else "reference"
                raise TaskError(f"task cannot be its own {key}")
            if task_id not in out:
                out.append(task_id)
            if self.get_by_id(project, task_id) is None:
                raise TaskError(f"task {display_ref(project, task_id)} does not exist")
        return out

    def dep_statuses(self, task: Task) -> dict[int, str]:
        if not task.depends_on:
            return {}
        placeholders = ",".join("?" * len(task.depends_on))
        rows = self.db.execute(
            f"SELECT id, status FROM tasks WHERE project=? AND id IN ({placeholders})",
            (task.project, *task.depends_on),
        ).fetchall()
        return {r["id"]: r["status"] for r in rows}

    def unfinished_deps(self, task: Task) -> list[Task]:
        statuses = self.dep_statuses(task)
        deps: list[Task] = []
        for dep_id in unfinished_dep_ids(task, statuses):
            dep = self.get_by_id(task.project, dep_id)
            if dep is not None:
                deps.append(dep)
        return deps

    def is_blocked(self, task: Task) -> bool:
        return is_blocked(task, self.dep_statuses(task))

    def is_ready(self, task: Task) -> bool:
        return is_ready(task, self.dep_statuses(task))

    def dependents(self, task: Task) -> list[Task]:
        """Tasks (same project) whose depends_on includes this task."""
        return [
            t
            for t in self.list_tasks(task.project)
            if task.id in t.depends_on and t.id != task.id
        ]

    # --- attachments -----------------------------------------------------------
    # Files live on disk under <db_dir>/attachments/<project>/<task_id>/<hash>-<name>,
    # content-addressed by sha256; the table holds the metadata.

    def attachments_dir(self) -> Path:
        return self.path.parent / "attachments"

    def add_attachment(self, task: Task, source: str | Path, *, filename: str | None = None) -> dict:
        source = Path(source).expanduser()
        if not source.is_file():
            raise TaskError(f"attachment file not found: {source}")
        data = source.read_bytes()
        return self.add_attachment_bytes(task, filename or source.name, data)

    def add_attachment_bytes(self, task: Task, filename: str, data: bytes) -> dict:
        filename = str(filename).strip() or "attachment"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "attachment"
        digest = hashlib.sha256(data).hexdigest()
        relpath = f"{task.project}/{task.id}/{digest[:12]}-{safe}"
        target = self.attachments_dir() / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        cur = self.db.execute(
            "INSERT INTO attachments (project, task_id, filename, relpath, size, sha256, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (task.project, task.id, filename, relpath, len(data), digest, utcnow()),
        )
        self.db.commit()
        return {
            "id": cur.lastrowid,
            "filename": filename,
            "path": str(target),
            "size": len(data),
            "sha256": digest,
        }

    def list_attachments(self, task: Task) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM attachments WHERE project=? AND task_id=? ORDER BY id",
            (task.project, task.id),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "filename": r["filename"],
                "path": str(self.attachments_dir() / r["relpath"]),
                "size": r["size"],
                "sha256": r["sha256"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def read_attachment_bytes(self, task: Task, attachment_id: int) -> bytes:
        row = self.db.execute(
            "SELECT * FROM attachments WHERE project=? AND task_id=? AND id=?",
            (task.project, task.id, attachment_id),
        ).fetchone()
        if row is None:
            raise TaskError(f"no attachment #{attachment_id} on {display_ref(task.project, task.id)}")
        return (self.attachments_dir() / row["relpath"]).read_bytes()

    def remove_attachment(self, task: Task, attachment_id: int, *, missing_ok: bool = False) -> str:
        row = self.db.execute(
            "SELECT * FROM attachments WHERE project=? AND task_id=? AND id=?",
            (task.project, task.id, attachment_id),
        ).fetchone()
        if row is None:
            if missing_ok:
                return ""
            raise TaskError(f"no attachment #{attachment_id} on {display_ref(task.project, task.id)}")
        self.db.execute("DELETE FROM attachments WHERE id=?", (row["id"],))
        self.db.commit()
        path = self.attachments_dir() / row["relpath"]
        if path.is_file():
            still_used = self.db.execute(
                "SELECT 1 FROM attachments WHERE relpath=? LIMIT 1", (row["relpath"],)
            ).fetchone()
            if still_used is None:
                path.unlink(missing_ok=True)
        return row["filename"]

    def delete_task_files(self, task: Task) -> None:
        """Drop all attachments for a task (called from delete)."""
        for att in self.list_attachments(task):
            self.remove_attachment(task, att["id"], missing_ok=True)

    # --- export / import --------------------------------------------------------

    def export_tasks(
        self, project: str | None = None, *, include_attachments: bool = True
    ) -> dict:
        """Portable, versioned snapshot. Dependencies are exported as local ids;
        attachments are embedded base64-encoded (sha256-verified on import)."""
        import base64

        tasks_out = []
        for task in self.list_tasks(project):
            data = task.to_dict()
            data["local_id"] = task.id
            if include_attachments:
                data["attachments"] = [
                    {
                        "filename": att["filename"],
                        "size": att["size"],
                        "sha256": att["sha256"],
                        "content_b64": base64.b64encode(
                            self.read_attachment_bytes(task, att["id"])
                        ).decode("ascii"),
                    }
                    for att in self.list_attachments(task)
                ]
            tasks_out.append(data)
        return {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "exported_at": utcnow(),
            "tasks": tasks_out,
        }

    def import_tasks(
        self, payload: dict, *, mode: str = "merge", dry_run: bool = False
    ) -> dict:
        """Import an export. Tasks whose (project, name) already exist are kept
        (their id is reused so dependency links still resolve); everything else is
        created with fresh ids and refs remapped. Returns a report."""
        import base64

        if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
            raise TaskError("not an agenttasker export (missing format marker)")
        version = payload.get("version")
        if version != EXPORT_VERSION:
            raise TaskError(f"unsupported export version {version!r} (expected {EXPORT_VERSION})")
        if mode not in ("merge", "replace"):
            raise TaskError(f"unknown import mode {mode!r} (expected merge or replace)")
        incoming = payload.get("tasks")
        if not isinstance(incoming, list):
            raise TaskError("export has no task list")

        report = {"created": [], "existing": [], "warnings": [], "mode": mode, "dry_run": dry_run}
        try:
            if mode == "replace":
                projects = sorted({t.get("project", "") for t in incoming})
                for existing_task in [
                    t for t in self.list_tasks(None) if t.project in projects
                ]:
                    if not dry_run:
                        self.delete(existing_task)
                    report["warnings"].append(
                        f"replace: deleted {display_ref(existing_task.project, existing_task.id)}"
                        f" ({existing_task.name})"
                    )

            existing_by_name = {
                (t.project, t.name): t for t in self.list_tasks(None)
            }
            id_map: dict[tuple[str, int], int] = {}  # (project, export local id) -> db id
            for item in incoming:
                prj = item.get("project", "")
                old_id = item.get("local_id", item.get("id"))
                name = str(item.get("name", "")).strip()
                if not name:
                    report["warnings"].append("skipped task without a name")
                    continue
                match = existing_by_name.get((prj, name))
                if match is not None and not (mode == "replace" and dry_run):
                    id_map[(prj, old_id)] = match.id
                    report["existing"].append(f"{display_ref(prj, match.id)} {name} (kept)")
                    continue
                if dry_run:
                    report["created"].append(f"{prj}-? {name} (would create)")
                    id_map[(prj, old_id)] = -1
                    continue
                created = self.add_task(
                    prj, name,
                    description=item.get("description", ""),
                    status=item.get("status", BACKLOG),
                    evidence=item.get("evidence", ""),
                    blockers=item.get("blockers", []),
                    priority=item.get("priority", DEFAULT_PRIORITY),
                    type=item.get("type", DEFAULT_TYPE),
                    tags=item.get("tags", []),
                )
                if item.get("owner"):
                    self.db.execute(
                        "UPDATE tasks SET owner=?, claimed_at=? WHERE project=? AND id=?",
                        (item["owner"], item.get("claimed_at", ""), prj, created.id),
                    )
                id_map[(prj, old_id)] = created.id
                report["created"].append(f"{display_ref(prj, created.id)} {name}")

            # second pass: remap relationships onto created tasks
            for item in incoming:
                prj = item.get("project", "")
                old_id = item.get("local_id", item.get("id"))
                new_id = id_map.get((prj, old_id))
                if not new_id or new_id < 0:
                    continue
                task = self.get_by_id(prj, new_id)
                if task is None:
                    continue

                def remap(ids):
                    out = []
                    for ref in ids or []:
                        mapped = id_map.get((prj, ref))
                        if mapped is None or mapped < 0:
                            report["warnings"].append(
                                f"{display_ref(prj, new_id)}: dropped unresolvable ref to old #{ref}"
                            )
                        elif mapped not in out:
                            out.append(mapped)
                    return out

                deps = remap(item.get("depends_on"))
                links = remap(item.get("affects"))
                # compare against the created task's CURRENT refs, not the export's
                # (an old id can coincidentally equal the new one)
                if deps != task.depends_on or links != task.affects:
                    if not dry_run:
                        self.update(task, depends_on=deps, affects=links)

                for att in item.get("attachments", []) or []:
                    if dry_run:
                        continue
                    data = base64.b64decode(att.get("content_b64", ""))
                    digest = hashlib.sha256(data).hexdigest()
                    if att.get("sha256") and digest != att["sha256"]:
                        report["warnings"].append(
                            f"{display_ref(prj, new_id)}: attachment {att.get('filename')!r}"
                            " failed sha256 check; skipped"
                        )
                        continue
                    self.add_attachment_bytes(task, att.get("filename", "attachment"), data)

            self.db.commit()
            return report
        except TaskError:
            self.db.rollback()
            raise
