from __future__ import annotations

import argparse
import json as jsonlib
import os
import sys
from collections import Counter
from typing import Sequence

from . import __version__, render
from .core import (
    DONE,
    STATUSES,
    Store,
    Task,
    TaskError,
    display_ref,
    is_blocked,
    is_ready,
    next_status,
    normalize_status,
    prev_status,
    resolve_project,
    status_label,
    unfinished_dep_ids,
    utcnow,
)

_STATUS_HELP = ", ".join(STATUSES)

_EPILOG = f"""\
statuses (board order):
  deferred      parked; revisit later, not part of the active set
  backlog       accepted, but not yet ready or prioritized
  todo          ready to work once dependencies/blockers clear
  in_progress   actively being worked (claim it before starting)
  in_review     work done, awaiting verification/review
  done          complete and verified

tasks are namespaced per project (default: git repo name, else cwd name).
override with --project or $AGENTTASKER_PROJECT.
examples:
  agenttasker add "Fix login redirect" -d "Session cookie lost on 302" --dep 3
  agenttasker ls --ready
  agenttasker move 12 --next && agenttasker show 12
"""


def _comma_list(values: Sequence[str] | None) -> list[str]:
    """Flatten repeatable flags that may carry comma-separated values. Empty items dropped."""
    out: list[str] = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def build_parser() -> argparse.ArgumentParser:
    invoked = os.path.basename(sys.argv[0])
    prog = "python -m agenttasker" if invoked == "__main__.py" else invoked
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Local, project-namespaced task tracker for agents and humans.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--db", metavar="PATH",
        help="task database file (default: $AGENTTASKER_DB or ~/.local/share/agenttasker/tasks.db)",
    )
    parser.add_argument(
        "-p", "--project", metavar="NAME",
        help="project namespace (default: $AGENTTASKER_PROJECT, git repo name, or cwd name)",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", metavar="PATH", default=argparse.SUPPRESS,
                        help="task database file (default: $AGENTTASKER_DB or ~/.local/share/agenttasker/tasks.db)")
    common.add_argument("-p", "--project", metavar="NAME", default=argparse.SUPPRESS,
                        help="project namespace (default: $AGENTTASKER_PROJECT, git repo name, or cwd name)")
    sub = parser.add_subparsers(dest="cmd", metavar="<command>")

    p = sub.add_parser("add", parents=[common], help="create a task")
    p.add_argument("name", help="short task name")
    p.add_argument("-d", "--description", default="", help="what needs to happen and why")
    p.add_argument(
        "-s", "--status", default="backlog", metavar="STATUS",
        help=f"initial status (default: backlog; one of: {_STATUS_HELP})",
    )
    p.add_argument(
        "-e", "--evidence", default="", metavar="TEXT",
        help="evidence / analysis gathered before starting the task",
    )
    p.add_argument(
        "--blocker", action="append", metavar="TEXT",
        help="external blocker (free text, not a task ref); repeatable",
    )
    p.add_argument(
        "--dep", action="append", metavar="REF",
        help="task this depends on (id or PROJECT-id); repeatable, comma-separated ok",
    )
    p.add_argument(
        "--affects", action="append", metavar="REF",
        help="task whose outcome this work affects; repeatable, comma-separated ok",
    )
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("ls", parents=[common], help="list tasks (board order)")
    p.add_argument("-s", "--status", action="append", metavar="STATUS",
                   help=f"filter by status; repeatable, comma-separated ({_STATUS_HELP})")
    p.add_argument("--search", metavar="TEXT", help="match name/description/evidence (AND terms)")
    p.add_argument("--ready", action="store_true", help="only tasks ready to start now")
    p.add_argument("--blocked", action="store_true", help="only tasks blocked (deps or external)")
    p.add_argument("-a", "--all-projects", action="store_true", help="list tasks across all projects")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("show", parents=[common], help="show full task detail")
    p.add_argument("ref", help="task id or PROJECT-id (e.g. '12' or 'demo-12')")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("update", parents=[common], help="edit task fields")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.add_argument("--name", help="new task name")
    p.add_argument("-d", "--description", help="new description (replaces)")
    p.add_argument("-e", "--evidence", help="new evidence text (replaces)")
    p.add_argument("--append-evidence", metavar="TEXT", help="append a line to evidence")
    p.add_argument("--blocker", action="append", metavar="TEXT",
                   help="external blockers (replaces list; repeatable; '' clears)")
    p.add_argument("--dep", action="append", metavar="REF",
                   help="depends-on refs (replaces list; repeatable; '' clears)")
    p.add_argument("--affects", action="append", metavar="REF",
                   help="affects refs (replaces list; repeatable; '' clears)")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("move", parents=[common], help="change task status")
    p.add_argument("ref", help="task id or PROJECT-id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--to", metavar="STATUS", help=f"target status ({_STATUS_HELP})")
    g.add_argument("--next", action="store_true", help="advance one step toward done")
    g.add_argument("--prev", action="store_true", help="step one back toward deferred")
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("done", parents=[common], help="mark a task done")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("rm", parents=[common], help="delete a task (scrubs refs from other tasks)")
    p.add_argument("ref", help="task id or PROJECT-id")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("projects", parents=[common], help="list projects with per-status counts")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("board", parents=[common], help="open the interactive TUI kanban board")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("mcp", parents=[common], help="run the MCP server (stdio; requires the 'mcp' extra)")
    p.set_defaults(func=cmd_mcp)

    return parser


# --- command handlers --------------------------------------------------------

def _project_map(store: Store, project: str) -> dict[int, Task]:
    return {t.id: t for t in store.list_tasks(project)}


def _task_json(store: Store, task: Task) -> dict:
    data = task.to_dict()
    dep_map = store.dep_statuses(task)
    data["unfinished_deps"] = unfinished_dep_ids(task, dep_map)
    data["blocked"] = is_blocked(task, dep_map)
    data["ready"] = is_ready(task, dep_map)
    return data


def cmd_add(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.add_task(
        project,
        args.name,
        description=args.description,
        status=args.status,
        evidence=args.evidence,
        blockers=[b for b in (args.blocker or []) if b.strip()],
        depends_on=_comma_list(args.dep),
        affects=_comma_list(args.affects),
    )
    print(f"Added {display_ref(project, task.id)} [{task.status}] {task.name}")
    return 0


def cmd_ls(args, store: Store) -> int:
    project = None if args.all_projects else resolve_project(args.project)
    statuses = _comma_list(args.status) or None
    tasks = store.list_tasks(project, statuses=statuses, search=args.search)
    if args.ready:
        tasks = [t for t in tasks if store.is_ready(t)]
    if args.blocked:
        tasks = [t for t in tasks if store.is_blocked(t)]

    if args.json:
        print(jsonlib.dumps([_task_json(store, t) for t in tasks], indent=2))
        return 0

    scope = "all projects" if project is None else f"project {project!r}"
    suffix = ""
    if args.ready:
        suffix += " [ready]"
    if args.blocked:
        suffix += " [blocked]"
    if args.search:
        suffix += f" [search: {args.search}]"
    print(f"{scope} — {len(tasks)} task(s){suffix}")
    # dependency flags need the FULL project task set, not the filtered listing
    maps: dict[str, dict[int, Task]] = {
        proj: {x.id: x for x in store.list_tasks(proj)}
        for proj in {t.project for t in tasks}
    }
    for t in tasks:
        print(render.task_line(t, maps[t.project], show_project=project is None))
    counts = Counter(t.status for t in tasks)
    summary = "  ".join(f"{s}:{counts[s]}" for s in STATUSES if counts[s])
    if summary:
        print(summary)
    return 0


def cmd_show(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    if args.json:
        print(jsonlib.dumps(_task_json(store, task), indent=2))
        return 0
    print(render.detail(task, _project_map(store, project)))
    return 0


def cmd_update(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    changes: dict = {}

    if args.name is not None:
        changes["name"] = args.name
    if args.description is not None:
        changes["description"] = args.description
    if args.evidence is not None:
        changes["evidence"] = args.evidence
    if args.append_evidence:
        stamp = utcnow().replace("+00:00", "Z")
        new = f"{task.evidence.rstrip()}\n\n[{stamp}] {args.append_evidence}".strip("\n")
        changes["evidence"] = new
    if args.blocker is not None:
        changes["blockers"] = [b for b in args.blocker if b.strip()]
    if args.dep is not None:
        changes["depends_on"] = _comma_list(args.dep)
    if args.affects is not None:
        changes["affects"] = _comma_list(args.affects)

    updated = store.update(task, **changes)
    print(f"Updated {display_ref(project, updated.id)}")
    print()
    print(render.detail(updated, _project_map(store, project)))
    return 0


def cmd_move(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    if args.to is not None:
        target = normalize_status(args.to)
    elif args.next:
        target = next_status(task.status)
    else:
        target = prev_status(task.status)
    updated = store.set_status(task, target)
    print(f"{display_ref(project, updated.id)}  {task.status} -> {updated.status}")
    return 0


def cmd_done(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    updated = store.set_status(task, DONE)
    print(f"{display_ref(project, updated.id)}  {task.status} -> {updated.status}")
    return 0


def cmd_rm(args, store: Store) -> int:
    project = resolve_project(args.project)
    task = store.get(project, args.ref)
    notes = store.delete(task)
    print(f"Deleted {display_ref(project, task.id)} ({task.name})")
    for note in notes:
        print(f"  - {note}")
    return 0


def cmd_projects(args, store: Store) -> int:
    counts = store.projects()
    if not counts:
        print("no projects yet — create tasks with `agenttasker add`")
        return 0
    name_w = max([len("PROJECT")] + [len(name) for name in counts])
    header = f"{'PROJECT':<{name_w}}  " + "  ".join(f"{s:<11}" for s in STATUSES) + "  TOTAL"
    print(header)
    for name, bucket in counts.items():
        row = f"{name:<{name_w}}  " + "  ".join(
            f"{bucket[s]:<11}" for s in STATUSES
        )
        print(f"{row}  {bucket['total']}")
    return 0


def cmd_board(args, store: Store) -> int:
    from .tui import run_board

    project = resolve_project(args.project)
    run_board(store, project)
    return 0


def cmd_mcp(args=None, store: Store | None = None) -> int:
    try:
        from .mcp_server import serve
    except ImportError:
        print(
            "error: MCP extras not installed. Install with: pip install 'agenttasker[mcp]'",
            file=sys.stderr,
        )
        return 2
    serve()
    return 0


# --- entry point -------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    if args.cmd == "mcp":
        return cmd_mcp()
    try:
        with Store(args.db) as store:
            return args.func(args, store)
    except TaskError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
