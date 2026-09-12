# AgentTasker

A local-first, project-namespaced task tracker built for AI agents and humans working
on the same projects. GitHub-Projects-style board (CLI + curses TUI), one SQLite file,
no server, no accounts.

- **CLI** — `agenttasker add|ls|show|update|move|done|rm|projects|board|mcp`
- **TUI** — `agenttasker board`: interactive 6-column kanban for humans
- **MCP** — `agenttasker mcp`: stdio MCP server exposing the same board to agents

## Statuses

Board order:

| Status | Meaning |
|---|---|
| `deferred` | parked; revisit later, not in the active set |
| `backlog` | accepted, not yet ready/prioritized |
| `todo` | ready to work once deps/blockers clear |
| `in_progress` | actively being worked (claim before starting) |
| `in_review` | done, awaiting verification |
| `done` | complete and verified |

Tasks carry a **name**, **description**, **evidence** (analysis gathered pre-task),
free-form external **blockers**, **depends_on** (hard task deps) and **affects**
(informational links). A task is *ready* when it sits in `backlog`/`todo`, has no
external blockers, and all dependencies are `done`. A task is *blocked* while any
dependency is unfinished or any external blocker is open.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[mcp]'   # [mcp] only needed for the MCP server
```

Installs two equivalent commands: `agenttasker` and the short alias `atx`.
Or standalone: `pipx install 'agenttasker[mcp]'` (once published / from a checkout:
`pipx install '/path/to/AgentTasker[mcp]'`).

## Use as a Codex skill

```bash
scripts/install-codex-skill.sh     # copies SKILL.md into ~/.codex/skills/agenttasker/
```

Codex loads skills at session start (restart to pick up changes) and needs a real
file, not a symlink. Invoke explicitly with `$agenttasker`, or let Codex select it
from the description.

## Quick start

```bash
agenttasker add "Fix login redirect" -d "Session cookie lost on 302" -e "traced to SameSite=None" --status todo
agenttasker add "Write regression test" --dep 1
agenttasker ls                      # board order, with ready/blocked flags
agenttasker ls --ready              # work that can start now
agenttasker ls --blocked            # stuck work (unfinished deps or external blockers)
agenttasker move 1 --next           # todo -> in_progress
agenttasker done 1
agenttasker board                   # TUI for humans
```

Tasks are namespaced per **project**: the git repo name (or cwd name outside a repo),
overridable with `--project`/`-p` or `$AGENTTASKER_PROJECT`. References accept `12`
or `ProjectName-12`.

## Storage

One SQLite file, WAL mode:

- default: `~/.local/share/agenttasker/tasks.db`
- override: `AGENTTASKER_DB=/path/to/tasks.db`

## TUI keys

`agenttasker board [-p PROJECT]`

| Key | Action |
|---|---|
| `←`/`→` or `h`/`l` | select column |
| `↑`/`↓` or `k`/`j` | select task |
| `Enter` | full task detail (scroll with arrows) |
| `m` | move task (status menu) |
| `<`/`,` and `>`/`.` | shift task one column left/right |
| `r` | manual reload (the board also auto-reloads when the db changes) |
| `?` / `q` | help / quit |

Cards are 4 lines (name, description/evidence excerpt, deps/blockers/ready state),
colored by status. `!` = blocked (red), `*` = ready, dimmed = done. The board watches
the database file and its WAL side files, so changes from the CLI, MCP agents, or
other TUI sessions appear within ~0.5s.

## MCP

Run the stdio MCP server:

```bash
agenttasker mcp
```

Client config (Claude Desktop / any MCP client):

```json
{
  "mcpServers": {
    "agenttasker": {
      "command": "/path/to/.venv/bin/agenttasker",
      "args": ["mcp"],
      "env": { "AGENTTASKER_DB": "/home/you/.local/share/agenttasker/tasks.db" }
    }
  }
}
```

The server resolves its project from its working directory (git repo name), or
`$AGENTTASKER_PROJECT`; every tool also takes an explicit `project` argument.

Tools: `add_task`, `get_task`, `list_tasks` (with `ready`/`blocked` filters),
`update_task`, `set_status`, `delete_task`, `list_projects`.

Agents: see [SKILL.md](SKILL.md) for the full working discipline.

## Development

```bash
python3 -m unittest discover -s tests
```
