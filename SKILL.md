---
name: agenttasker
description: >-
  Local, project-namespaced task tracker for AI agents and humans (CLI + TUI + MCP).
  Use when planning, tracking, or executing multi-step work; when the user asks to
  create/track/manage tasks, todos, or a board; or when coordinating with other agents
  on the same project. Surfaces ready/blocked work so nothing slips through.
---

# AgentTasker

A shared task board agents and humans both read and write. One SQLite file per machine,
tasks namespaced by project (git repo name by default). Agents use the CLI or MCP tools;
humans can use the same CLI or the TUI (`agenttasker board`).

**Why you use it:** conversations forget; the board doesn't. Anything that spans more
than one step, or that you might hand off later, belongs on the board — not in your head.

## Model

- **Statuses** (board order): `deferred → backlog → todo → in_progress → in_review → done`
- **Task fields**: name, description, `evidence` (analysis recorded *before* starting),
  `blockers` (free-text external impediments, e.g. "waiting on vendor key"),
  `depends_on` (task ids that must be `done` first), `affects` (task ids this work touches).
- **Refs**: `12` or `ProjectName-12` within the current project.
- **Derived state**:
  - *ready* = status `backlog`/`todo` AND no blockers AND all `depends_on` are `done`.
  - *blocked* = not `done` AND (unfinished dependency OR open external blocker).
- **Storage**: `$AGENTTASKER_DB` or `~/.local/share/agenttasker/tasks.db` (SQLite, WAL — safe for concurrent agents).

## Non-negotiable rules

1. **Capture before you build.** When you accept multi-step work, `add_task` each meaningful
   step before starting. If a step will take real thought, it is a task.
2. **Evidence before execution.** Record pre-task analysis (files read, approach chosen,
   constraints found) in `evidence` BEFORE moving anything to `in_progress`.
3. **Claim it.** `set_status <ref> in_progress` when you start; exactly one agent per task.
4. **Declare relationships immediately.** Set `depends_on`/`affects` at creation time;
   add discovered ones the moment you discover them.
5. **Blockers are public.** The second work stalls (missing access, failing dep, unanswered
   question), record it with `blockers`, and clear it the moment it resolves.
   A blocked task you're silent about is a slipped deadline.
6. **`in_review` means done-but-unverified; `done` means verified.** Only mark `done` when
   you've run the code/tests/commands that prove it. Log what you ran via `append_evidence`.
7. **Never delete someone else's task.** Park it: `set_status <ref> deferred`.
8. **Leave no orphans.** Before ending a session: no `in_progress` task you're not actively
   finishing (move it back + add a blocker note if parked), and every `ready` task has been
   acknowledged.

## Session ritual

Start of session:

```
agenttasker ls                    # whole board
agenttasker ls --ready            # pick work from here (oldest first)
agenttasker ls --blocked          # anything stalled? chase it
```

During work: append findings as you go (`update <ref> --append-evidence "..."`).

End of session: `agenttasker ls` and confirm rule 8. Check `deferred` tasks occasionally —
decide deliberately whether they stay parked.

## CLI reference

Both `agenttasker` and the short alias `atx` invoke the same tool; examples use
`agenttasker` for clarity.
```
agenttasker add "Name" [-d DESC] [-s STATUS] [-e EVIDENCE] [--blocker TEXT]... [--dep REF]... [--affects REF]...
agenttasker ls [-s STATUS]... [--search TEXT] [--ready] [--blocked] [-a] [--json]
agenttasker show REF [--json]
agenttasker update REF [--name N] [-d D] [-e E] [--append-evidence TEXT]
                       [--blocker T]... [--dep R]... [--affects R]...
agenttasker move REF (--to STATUS | --next | --prev)
agenttasker done REF
agenttasker rm REF                # permanent; refs in other tasks are scrubbed
agenttasker projects
agenttasker board                 # TUI (humans): arrows navigate, Enter details, m moves
agenttasker mcp                   # stdio MCP server
```

Global flags (before or after the subcommand): `-p/--project NAME`, `--db PATH`.
List-valued flags (`--dep`, `--affects`, `--blocker`) REPLACE the list when given;
pass `--dep ''` / `--blocker ''` to clear.

## MCP tools

`add_task`, `get_task`, `list_tasks` (`ready`/`blocked` filters, `all_projects`),
`update_task`, `set_status`, `delete_task`, `list_projects`.

Tool errors come back as `ERROR: ...` text — read it, fix the input (usually a bad ref
or unknown status), retry. List fields passed to `update_task` replace; `[]` clears.

Client config:

```json
{
  "mcpServers": {
    "agenttasker": {
      "command": "/abs/path/to/agenttasker",
      "args": ["mcp"],
      "env": {"AGENTTASKER_DB": "/home/USER/.local/share/agenttasker/tasks.db"}
    }
  }
}
```

The server resolves its project from its working directory (git repo name); every tool
also takes an explicit `project` argument to operate cross-project.

## Recipes

Discovered a dependency mid-task:
```
agenttasker update 14 --dep 7          # 14 now waits on 7
agenttasker ls --blocked               # confirm visibility
```

Blocked externally, parking honestly:
```
agenttasker update 14 --blocker "need write access to staging bucket"
agenttasker move 14 --prev             # back to todo (or backlog)
```

Task finished, awaiting human review:
```
agenttasker move 14 --next             # -> in_review
agenttasker update 14 --append-evidence "ran full test suite; 34 passed"
```

Chaining handoff work:
```
agenttasker add "Wire config loader" --dep 14 --affects 20
```

## Failure modes to avoid

- Starting work on a task still in `backlog` without claiming it (`in_progress`).
- Marking `done` without evidence of verification.
- Letting `in_progress` tasks pile up across sessions without resolution.
- Free-text task ids in `depends_on` — use numeric refs; external blockers are for prose.
