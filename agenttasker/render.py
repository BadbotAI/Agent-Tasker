"""Text rendering shared by the CLI and the MCP server."""

from __future__ import annotations

from .core import (
    DONE,
    Task,
    display_ref,
    is_blocked,
    is_ready,
    status_label,
    unfinished_dep_ids,
)


def _indent(text: str, pad: str = "  ") -> str:
    return "\n".join(pad + line for line in text.rstrip().splitlines() or [pad])


def _dep_label(dep_id: int, by_id: dict[int, Task]) -> str:
    dep = by_id.get(dep_id)
    if dep is None:
        return f"#{dep_id} (missing)"
    return f"#{dep.id} ({dep.status}) {dep.name}"


def task_line(task: Task, by_id: dict[int, Task], show_project: bool = False) -> str:
    """One-line board summary: '#12 in_progress  Fix login  [blocked deps:#10 ext:1]'."""
    prefix = f"{task.project}/" if show_project else ""
    flags: list[str] = []
    if task.status != DONE:
        dep_map = {t.id: t.status for t in by_id.values()}
        pending = unfinished_dep_ids(task, dep_map)
        if pending:
            flags.append("deps:" + ",".join(f"#{i}" for i in pending))
        if task.blockers:
            flags.append(f"ext:{len(task.blockers)}")
        if not flags and is_ready(task, dep_map):
            flags.append("ready")
    flag_text = f"  [{' '.join(flags)}]" if flags else ""
    return f"{prefix}#{task.id:<4} {task.status:<11} {task.name}{flag_text}"


def detail(task: Task, by_id: dict[int, Task]) -> str:
    """Full task detail with computed dependency state."""
    lines: list[str] = [
        f"{display_ref(task.project, task.id)}  {task.name}",
        f"Status:   {status_label(task.status)}",
        f"Project:  {task.project}",
    ]
    dep_map = {t.id: t.status for t in by_id.values()}

    if task.description:
        lines.append("Description:")
        lines.append(_indent(task.description))

    if task.evidence:
        lines.append("Evidence / pre-task analysis:")
        lines.append(_indent(task.evidence))

    lines.append("Blockers (external):")
    if task.blockers:
        lines.extend(f"  - {b}" for b in task.blockers)
    else:
        lines.append("  (none)")

    lines.append("Depends on:")
    if task.depends_on:
        lines.extend(f"  - {_dep_label(i, by_id)}" for i in task.depends_on)
    else:
        lines.append("  (none)")

    lines.append("Affects:")
    if task.affects:
        lines.extend(f"  - {_dep_label(i, by_id)}" for i in task.affects)
    else:
        lines.append("  (none)")

    dependents = [t for t in by_id.values() if task.id in t.depends_on and t.id != task.id]
    lines.append("Blocks (tasks depending on this):")
    if dependents:
        lines.extend(f"  - {_dep_label(t.id, by_id)}" for t in dependents)
    else:
        lines.append("  (none)")

    state = "ready" if is_ready(task, dep_map) else ("BLOCKED" if is_blocked(task, dep_map) else "ok")
    lines.append(f"State:    {state}")
    lines.append(f"Created:  {task.created_at}   Updated: {task.updated_at}")
    return "\n".join(lines)
