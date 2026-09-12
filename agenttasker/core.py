"""Domain core: statuses, task model, project resolution, task references, Store."""

from __future__ import annotations

import json
import os
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
            "description": self.description,
            "evidence": self.evidence,
            "blockers": list(self.blockers),
            "depends_on": list(self.depends_on),
            "affects": list(self.affects),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# --- store -------------------------------------------------------------------

_UPDATABLE = ("name", "status", "description", "evidence", "blockers", "depends_on", "affects")


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
    ) -> Task:
        name = name.strip()
        if not name:
            raise TaskError("task name must not be empty")
        status = normalize_status(status)
        deps = self._validate_refs(project, depends_on)
        links = self._validate_refs(project, affects)
        now = utcnow()
        cur = self.db.execute(
            "INSERT INTO tasks (project, name, status, description, evidence, blockers,"
            " depends_on, affects, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                project, name, status, description, evidence,
                json.dumps([str(b).strip() for b in blockers if str(b).strip()]),
                json.dumps(deps), json.dumps(links), now, now,
            ),
        )
        self.db.commit()
        task = self.get_by_id(project, cur.lastrowid)
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
        if search:
            for term in search.split():
                conds.append("(name LIKE ? OR description LIKE ? OR evidence LIKE ?)")
                like = f"%{term}%"
                args.extend([like, like, like])
        if conds:
            query += " WHERE " + " AND ".join(conds)
        rows = self.db.execute(query, args).fetchall()
        tasks = [Task.from_row(r) for r in rows]
        tasks.sort(key=lambda t: (STATUSES.index(t.status), t.id))
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
        for key in ("depends_on", "affects"):
            if key in values:
                values[key] = self._validate_refs(task.project, values[key], exclude_self=task.id)
        if "blockers" in values:
            values["blockers"] = [str(b).strip() for b in values["blockers"] if str(b).strip()]
        if not values:
            raise TaskError("nothing to update")
        values["updated_at"] = utcnow()
        row_values = [json.dumps(v) if isinstance(v, list) else v for v in values.values()]
        assignments = ", ".join(f"{k}=?" for k in values)
        self.db.execute(
            f"UPDATE tasks SET {assignments} WHERE id=?", (*row_values, task.id)
        )
        self.db.commit()
        updated = self.get_by_id(task.project, task.id)
        assert updated is not None
        return updated

    def set_status(self, task: Task, status: str) -> Task:
        return self.update(task, status=normalize_status(status))

    def delete(self, task: Task) -> list[str]:
        """Delete a task and scrub references to it from other tasks' dep/affect lists."""
        self.db.execute("DELETE FROM tasks WHERE id=?", (task.id,))
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
