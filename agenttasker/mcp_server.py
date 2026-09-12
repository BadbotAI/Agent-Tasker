"""MCP server exposing AgentTasker tools over stdio.

Run with `agenttasker mcp` (requires the 'mcp' extra: pip install 'agenttasker[mcp]').
The server resolves its project from its working directory (or $AGENTTASKER_PROJECT),
and every tool accepts an explicit `project` argument to override.
"""

from __future__ import annotations

from typing import Optional

from .core import STATUSES, Store, TaskError, resolve_project
from . import render

_INSTRUCTIONS = f"""\
AgentTasker: a persistent, local, project-namespaced task board shared by agents and humans.

Workflow contract:
1. Capture work as tasks (`add_task`) before doing it. Break big asks into small tasks.
2. Record evidence/analysis in `evidence` BEFORE moving a task to in_progress.
3. Claim a task by setting status to in_progress; work it; set to in_review when done;
   set to done once verified.
4. Declare dependencies (`depends_on`) and external blockers (`blockers`) as soon as
   known, and clear them when resolved.
5. Use list_tasks(ready=True) to pick up work and list_tasks(blocked=True) to surface
   stuck work. Do not let tasks silently stall.

Statuses (board order): {", ".join(STATUSES)}.
Tasks are namespaced per project; reference them as '<id>' or '<project>-<id>'.
The same data is viewable by humans via `agenttasker board` (TUI) or `agenttasker ls` (CLI).
"""


def _project(explicit: Optional[str]) -> str:
    return resolve_project(explicit)


def _list_lines(store: Store, tasks, show_project: bool) -> str:
    if not tasks:
        return "(no tasks matched)"
    # dependency flags need the FULL project task set, not the filtered listing
    maps = {
        proj: {x.id: x for x in store.list_tasks(proj)}
        for proj in {t.project for t in tasks}
    }
    lines = [
        render.task_line(t, maps[t.project], show_project=show_project) for t in tasks
    ]
    lines.append(f"({len(tasks)} task(s))")
    return "\n".join(lines)


def build_server():
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp 2.x
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server

    mcp = Server("agenttasker", instructions=_INSTRUCTIONS)

    @mcp.tool()
    def add_task(
        name: str,
        project: Optional[str] = None,
        description: str = "",
        status: str = "backlog",
        evidence: str = "",
        blockers: Optional[list[str]] = None,
        depends_on: Optional[list[str]] = None,
        affects: Optional[list[str]] = None,
    ) -> str:
        """Create a task in the project board.

        Args:
            name: short task name.
            project: project namespace; defaults to the server's working project.
            description: what needs to happen and why.
            status: one of deferred|backlog|todo|in_progress|in_review|done (default backlog).
            evidence: analysis/findings gathered BEFORE starting the task.
            blockers: free-form external blockers (e.g. "waiting on vendor API key").
            depends_on: task refs this task depends on ('12' or 'Project-12').
            affects: task refs whose outcome this work touches.
        """
        try:
            with Store() as store:
                prj = _project(project)
                task = store.add_task(
                    prj, name,
                    description=description, status=status, evidence=evidence,
                    blockers=blockers or [], depends_on=depends_on or [], affects=affects or [],
                )
                return (
                    f"added {prj}-{task.id} [{task.status}] {task.name}\n"
                    f"hint: `set_status {task.id} in_progress` when you start it"
                )
        except TaskError as exc:
            return f"ERROR: {exc}"

    @mcp.tool()
    def get_task(ref: str, project: Optional[str] = None) -> str:
        """Show full detail for one task: description, evidence, blockers, dependencies."""
        try:
            with Store() as store:
                prj = _project(project)
                task = store.get(prj, ref)
                by_id = {t.id: t for t in store.list_tasks(prj)}
                return render.detail(task, by_id)
        except TaskError as exc:
            return f"ERROR: {exc}"

    @mcp.tool()
    def list_tasks(
        project: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        ready: bool = False,
        blocked: bool = False,
        all_projects: bool = False,
    ) -> str:
        """List tasks in board order.

        Args:
            project: project namespace; defaults to the server's working project.
            status: filter by status (or comma-separated list).
            search: match task name/description/evidence.
            ready: only tasks that can be started now (deps done, no blockers).
            blocked: only blocked tasks (unfinished deps or external blockers).
            all_projects: list across every project in the database.
        """
        try:
            with Store() as store:
                prj = None if all_projects else _project(project)
                statuses = [s for s in (status or "").split(",") if s.strip()] or None
                tasks = store.list_tasks(prj, statuses=statuses, search=search)
                if ready:
                    tasks = [t for t in tasks if store.is_ready(t)]
                if blocked:
                    tasks = [t for t in tasks if store.is_blocked(t)]
                return _list_lines(store, tasks, show_project=prj is None)
        except TaskError as exc:
            return f"ERROR: {exc}"

    @mcp.tool()
    def update_task(
        ref: str,
        project: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        evidence: Optional[str] = None,
        append_evidence: Optional[str] = None,
        blockers: Optional[list[str]] = None,
        depends_on: Optional[list[str]] = None,
        affects: Optional[list[str]] = None,
    ) -> str:
        """Update task fields. List fields REPLACE when provided (pass [] to clear).
        append_evidence adds a timestamped line — use it to log progress/findings.

        Args:
            ref: task id or 'Project-id'.
            project: project namespace; defaults to the server's working project.
            name, description, evidence: new values (replace).
            append_evidence: text appended to evidence with a timestamp.
            blockers: new external blocker list (replaces; [] clears).
            depends_on: new depends-on refs (replaces; [] clears).
            affects: new affects refs (replaces; [] clears).
        """
        try:
            with Store() as store:
                prj = _project(project)
                task = store.get(prj, ref)
                changes: dict = {}
                if name is not None:
                    changes["name"] = name
                if description is not None:
                    changes["description"] = description
                if evidence is not None:
                    changes["evidence"] = evidence
                if append_evidence:
                    from .core import utcnow

                    changes["evidence"] = (
                        f"{task.evidence.rstrip()}\n\n[{utcnow().replace('+00:00', 'Z')}] {append_evidence}".strip("\n")
                    )
                if blockers is not None:
                    changes["blockers"] = blockers
                if depends_on is not None:
                    changes["depends_on"] = depends_on
                if affects is not None:
                    changes["affects"] = affects
                updated = store.update(task, **changes)
                return f"updated {prj}-{updated.id}"
        except TaskError as exc:
            return f"ERROR: {exc}"

    @mcp.tool()
    def set_status(ref: str, status: str, project: Optional[str] = None) -> str:
        """Move a task to another status (claim: in_progress, finish: in_review, verify: done).

        Args:
            ref: task id or 'Project-id'.
            status: one of deferred|backlog|todo|in_progress|in_review|done.
            project: project namespace; defaults to the server's working project.
        """
        try:
            with Store() as store:
                prj = _project(project)
                task = store.get(prj, ref)
                updated = store.set_status(task, status)
                return f"{prj}-{updated.id} {task.status} -> {updated.status}"
        except TaskError as exc:
            return f"ERROR: {exc}"

    @mcp.tool()
    def delete_task(ref: str, project: Optional[str] = None) -> str:
        """Delete a task permanently (references in other tasks are scrubbed).

        Args:
            ref: task id or 'Project-id'.
            project: project namespace; defaults to the server's working project.
        """
        try:
            with Store() as store:
                prj = _project(project)
                task = store.get(prj, ref)
                notes = store.delete(task)
                msg = f"deleted {prj}-{task.id} ({task.name})"
                if notes:
                    msg += "\n" + "\n".join(f"- {n}" for n in notes)
                return msg
        except TaskError as exc:
            return f"ERROR: {exc}"

    @mcp.tool()
    def list_projects() -> str:
        """List all projects in the database with per-status task counts."""
        try:
            with Store() as store:
                counts = store.projects()
                if not counts:
                    return "(no projects yet)"
                lines = []
                for name, bucket in counts.items():
                    parts = " ".join(f"{s}:{bucket[s]}" for s in STATUSES if bucket[s])
                    lines.append(f"{name} — total:{bucket['total']}  {parts}")
                return "\n".join(lines)
        except TaskError as exc:
            return f"ERROR: {exc}"

    return mcp


def serve() -> None:
    mcp = build_server()
    mcp.run()  # stdio transport
