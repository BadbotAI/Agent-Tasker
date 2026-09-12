"""Interactive curses kanban board (TUI) for humans.

Keys:
    h/Left  l/Right   select column        j/Down  k/Up  select task
    Enter             task details         m            move task (status menu)
    < ,    >  .       move card left/right r            manual reload
    ?                 help                 q            quit

The board auto-reloads when the task database changes (other agents/humans
writing via the CLI/MCP appear within ~0.5s). Cards span 4 lines: name,
two-line description/evidence excerpt, and a deps/blockers/ready meta line.
"""

from __future__ import annotations

import curses
import os
import textwrap

from .core import (
    DONE,
    STARTABLE_STATUSES,
    STATUSES,
    Store,
    Task,
    TaskError,
    is_blocked,
    is_ready,
    next_status,
    prev_status,
    status_label,
    unfinished_dep_ids,
)

CARD_HEIGHT = 11  # bordered card: border, 2-line title head, rule, 4-line body, meta, border (+gap)
CARD_INNER_MIN = 8
MIN_COL_WIDTH = 16
WATCH_MS = 400  # db poll interval while idle

_HELP = """\
Board keys
  h / Left, l / Right    select column
  j / Down, k / Up       select task
  Enter                  task details (scroll with arrows, q/Enter closes)
  m                      move task to another status (menu)
  < or ,  /  > or .      move task one column left / right
  r                      manual reload
  ?                      this help
  q                      quit

The board watches the database and auto-reloads when other agents or
humans change tasks (CLI, MCP, or another TUI session).

Legend
  !    blocked: unfinished dependencies or external blockers (red)
  *    ready to start now
  card line 1  task name (status color)
  card lines 2-3  description / evidence excerpt
  card line 4  deps / external blockers / ready state
"""


def _safe(scr, y: int, x: int, text: str, attr: int = 0) -> None:
    """addstr that ignores out-of-bounds (curses raises at the last cell)."""
    try:
        scr.addstr(y, x, text, attr)
    except curses.error:
        pass


class Board:
    def __init__(self, store: Store, project: str):
        self.store = store
        self.project = project
        self.tasks: list[Task] = []
        self.by_id: dict[int, Task] = {}
        self.cols: dict[str, list[Task]] = {s: [] for s in STATUSES}
        self.col = 0
        self.row = 0
        self.offsets: dict[str, int] = {}
        self.done = False
        self.message = ""

    # --- data
    def reload(self) -> None:
        """Reload from db, keeping the selection on the same task when possible."""
        selected = self.selected()
        keep_id = selected.id if selected is not None else None
        self.tasks = self.store.list_tasks(self.project)
        self.by_id = {t.id: t for t in self.tasks}
        self.cols = {s: [t for t in self.tasks if t.status == s] for s in STATUSES}
        if keep_id is not None:
            for ci, status in enumerate(STATUSES):
                for ri, task in enumerate(self.cols[status]):
                    if task.id == keep_id:
                        self.col, self.row = ci, ri
                        break
        self.clamp_selection()

    def clamp_selection(self) -> None:
        self.col = max(0, min(self.col, len(STATUSES) - 1))
        col_tasks = self.cols[STATUSES[self.col]]
        self.row = max(0, min(self.row, max(0, len(col_tasks) - 1)))

    def selected(self) -> Task | None:
        col_tasks = self.cols[STATUSES[self.col]]
        if 0 <= self.row < len(col_tasks):
            return col_tasks[self.row]
        return None

    def dep_status_map(self) -> dict[int, str]:
        return {t.id: t.status for t in self.tasks}

    # --- db watching
    def db_stamp(self):
        """Change detector across the db and its WAL side files (WAL writes
        update -wal/-shm mtimes, not the main file's)."""
        stamp = []
        for suffix in ("", "-wal", "-shm"):
            path = str(self.store.path) + suffix
            try:
                info = os.stat(path)
                stamp.append((info.st_mtime_ns, info.st_size))
            except OSError:
                stamp.append(None)
        return tuple(stamp)

    # --- colors
    def init_colors(self) -> None:
        self.pairs: dict[str, int] = {}
        try:
            curses.start_color()
            curses.use_default_colors()
            palette = {
                "deferred": 8,     # gray
                "backlog": 6,      # cyan
                "todo": 7,         # white
                "in_progress": 3,  # yellow
                "in_review": 5,    # magenta
                "done": 2,         # green
                "alert": 1,        # red
            }
            for i, (name, color) in enumerate(palette.items(), start=1):
                curses.init_pair(i, color, -1)
                self.pairs[name] = curses.color_pair(i)
        except curses.error:
            self.pairs = {name: 0 for name in
                          ("deferred", "backlog", "todo", "in_progress",
                           "in_review", "done", "alert")}

    # --- main loop
    def run(self, stdscr) -> int:
        curses.curs_set(0)
        stdscr.keypad(True)
        self.init_colors()
        self.reload()
        stdscr.timeout(WATCH_MS)
        stamp = self.db_stamp()
        while not self.done:
            self.draw(stdscr)
            ch = stdscr.getch()
            if ch == curses.ERR:  # idle tick: poll the db for outside changes
                current = self.db_stamp()
                if current != stamp:
                    stamp = current
                    self.reload()
                    self.message = "· db changed — reloaded"
                continue
            self.handle(stdscr, ch)
            stamp = self.db_stamp()  # our own writes must not read as foreign
        return 0

    # --- drawing
    def draw(self, stdscr) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        _safe(stdscr, 0, 0, f" {self.project} — agenttasker board", curses.A_BOLD)
        _safe(
            stdscr, 1, 0,
            " ←→ column  ↑↓ task  Enter: details  m: move  </>: shift  ?: help  q: quit"
            "  · auto-reload on db change",
            curses.A_DIM,
        )

        n = len(STATUSES)
        gap = 1
        col_w = (width - gap * (n - 1)) // n
        if col_w < MIN_COL_WIDTH:
            _safe(
                stdscr, 3, 0,
                f" terminal too narrow ({width} cols); need >= {MIN_COL_WIDTH * n + gap * (n - 1)}",
                curses.A_BOLD,
            )
            stdscr.noutrefresh()
            curses.doupdate()
            return

        top = 2
        body_h = height - top - 1
        max_cards = body_h // CARD_HEIGHT

        for ci, status in enumerate(STATUSES):
            x = ci * (col_w + gap)
            w = col_w if ci < n - 1 else max(col_w, width - x)
            col_tasks = self.cols[status]

            # keep the selected card visible
            if ci == self.col:
                off = self.offsets.get(status, 0)
                if self.row < off:
                    off = self.row
                elif self.row >= off + max_cards:
                    off = self.row - max_cards + 1
                self.offsets[status] = max(0, off)
            off = self.offsets.get(status, 0)

            header = f" {status_label(status)} ({len(col_tasks)})"
            attr = self.pairs.get(status, 0) | curses.A_BOLD
            if ci == self.col:
                attr |= curses.A_UNDERLINE
            _safe(stdscr, top, x, header[:w], attr)

            y = top + 1
            for i in range(off, min(len(col_tasks), off + max_cards)):
                task = col_tasks[i]
                self.draw_card(
                    stdscr, task, y, x, w,
                    selected=(ci == self.col and i == self.row),
                )
                y += CARD_HEIGHT

        if max_cards == 0:
            _safe(stdscr, top + 1, 0, " terminal too short to show cards", curses.A_DIM)

        footer = self.message if self.message else self.footer_text()
        _safe(stdscr, height - 1, 0, footer[: width - 1], curses.A_DIM)
        stdscr.noutrefresh()
        curses.doupdate()

    def draw_card(self, stdscr, task: Task, y: int, x: int, w: int, selected: bool) -> None:
        """Bordered mini-card: inverted 2-line title head, rule, then body."""
        dep_map = self.dep_status_map()
        blocked = is_blocked(task, dep_map)
        ready = is_ready(task, dep_map)
        inner = max(CARD_INNER_MIN, w - 4)

        border_attr = self.pairs.get(task.status, 0)
        if task.status == DONE:
            border_attr |= curses.A_DIM
        if selected:
            border_attr |= curses.A_BOLD

        # head: inverted bar in the status color, title forced to wrap over 2 lines
        mark = "!" if blocked else ("*" if ready else " ")
        title_attr = self.pairs.get(task.status, 0) | curses.A_BOLD | curses.A_REVERSE
        if task.status == DONE:
            title_attr |= curses.A_DIM
        title_lines = textwrap.wrap(
            f"#{task.id} {mark} {task.name}", width=inner, max_lines=2, placeholder=" …"
        ) or [""]
        while len(title_lines) < 2:
            title_lines.append("")

        # body: description/evidence excerpt (4 lines) + deps/blockers/ready meta
        body_attr = curses.A_REVERSE if selected else 0  # full-contrast body text
        body = task.description.strip() or task.evidence.strip()
        excerpt = (
            textwrap.wrap(body, width=inner, max_lines=4, placeholder=" …") if body else []
        )
        while len(excerpt) < 4:
            excerpt.append("")
        parts: list[str] = []
        if task.depends_on:
            shown = ",".join(f"#{i}" for i in task.depends_on[:3])
            parts.append("deps:" + shown + ("…" if len(task.depends_on) > 3 else ""))
        if task.blockers:
            parts.append(f"ext:{len(task.blockers)}")
        if ready:
            parts.append("ready")
        meta_attr = self.pairs["alert"] if blocked else 0
        if selected:
            meta_attr |= curses.A_REVERSE

        _safe(stdscr, y, x, ("┌" + "─" * max(0, w - 2) + "┐")[:w], border_attr)
        rule = ("├" + "─" * max(0, w - 2) + "┤")[:w]
        rows = [
            (title_lines[0], title_attr),
            (title_lines[1], title_attr),
            (rule, border_attr),
            (excerpt[0], body_attr),
            (excerpt[1], body_attr),
            (excerpt[2], body_attr),
            (excerpt[3], body_attr),
            (" · ".join(parts), meta_attr),
        ]
        for i, (line, attr) in enumerate(rows):
            if line == rule:  # head/body separator spans the full card width
                _safe(stdscr, y + 1 + i, x, rule, attr)
                continue
            padded = line[:inner].ljust(inner)
            _safe(stdscr, y + 1 + i, x, f"│ {padded} │"[:w], attr)
        _safe(stdscr, y + 9, x, ("└" + "─" * max(0, w - 2) + "┘")[:w], border_attr)

    def footer_text(self) -> str:
        task = self.selected()
        if task is None:
            col = status_label(STATUSES[self.col])
            return f" {col}: no tasks. add one with `agenttasker add \"...\" -p {self.project}`"
        dep_map = self.dep_status_map()
        pending = unfinished_dep_ids(task, dep_map)
        bits = [f" #{task.id} {task.name} [{status_label(task.status)}]"]
        if pending:
            bits.append("blocked by: " + ",".join(f"#{i}" for i in pending))
        elif task.blockers:
            bits.append(f"external blockers: {len(task.blockers)}")
        return " ".join(bits)

    # --- input
    def handle(self, stdscr, ch: int) -> None:
        self.message = ""
        if ch in (ord("q"), ord("Q")):
            self.done = True
        elif ch in (curses.KEY_LEFT, ord("h"), ord("H")):
            self.col = max(0, self.col - 1)
            self.clamp_selection()
        elif ch in (curses.KEY_RIGHT, ord("l"), ord("L")):
            self.col = min(len(STATUSES) - 1, self.col + 1)
            self.clamp_selection()
        elif ch in (curses.KEY_UP, ord("k"), ord("K")):
            self.row = max(0, self.row - 1)
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            col_tasks = self.cols[STATUSES[self.col]]
            self.row = min(max(0, len(col_tasks) - 1), self.row + 1)
        elif ch in (curses.KEY_ENTER, 10, 13):
            task = self.selected()
            if task is not None:
                self.view_popup(stdscr, task)
        elif ch in (ord("m"), ord("M")):
            self.move_menu(stdscr)
        elif ch in (ord("<"), ord(",")):
            self.shift(-1)
        elif ch in (ord(">"), ord(".")):
            self.shift(1)
        elif ch in (ord("r"), ord("R")):
            self.reload()
            self.flash("reloaded")
        elif ch == ord("?"):
            self.help_popup(stdscr)
        elif ch == curses.KEY_RESIZE:
            self.offsets.clear()

    def shift(self, direction: int) -> None:
        task = self.selected()
        if task is None:
            return
        try:
            target = next_status(task.status) if direction > 0 else prev_status(task.status)
        except TaskError as exc:
            self.flash(str(exc))
            return
        self.apply_move(task, target)

    def apply_move(self, task: Task, target: str) -> None:
        old = task.status
        if target == old:
            return
        self.store.set_status(task, target)
        self.reload()
        self.col = STATUSES.index(target)
        col_tasks = self.cols[target]
        for i, t in enumerate(col_tasks):
            if t.id == task.id:
                self.row = i
                break
        self.flash(f"#{task.id} {old} -> {target}")

    def flash(self, message: str) -> None:
        self.message = message  # shown in footer on next draw

    # --- popups (drawn on the shared stdscr; the main loop redraws the board after)
    def _popup_geometry(self, lines, title: str, scr_h: int, scr_w: int):
        longest = max(
            [len(title)] + [len(l[0] if isinstance(l, tuple) else l) for l in lines] + [30]
        )
        w = max(30, min(longest + 4, scr_w - 2))
        h = min(len(lines) + 4, scr_h - 2)
        y = max(0, (scr_h - h) // 2)
        x = max(0, (scr_w - w) // 2)
        return h, w, y, x

    def _draw_box(self, h: int, w: int, y: int, x: int, title: str):
        win = curses.newwin(h, w, y, x)
        win.box()
        _safe(win, 0, 2, f" {title} "[: w - 2], curses.A_BOLD)
        return win

    def view_popup(self, stdscr, task: Task) -> None:
        """Scrollable, sectioned full-detail view."""
        self._scroll_popup(stdscr, f"#{task.id} {task.name}", self._detail_lines(task))

    def help_popup(self, stdscr) -> None:
        self._scroll_popup(stdscr, "Help", _HELP.splitlines())

    # detail view content: list of (text, attr) or plain str lines.
    # A line that is exactly "─" is drawn as a full-width separator.
    def _detail_lines(self, task: Task) -> list:
        dep_map = self.dep_status_map()
        dim = curses.A_DIM
        bold = curses.A_BOLD
        status_attr = self.pairs.get(task.status, 0) | bold
        alert = self.pairs["alert"]
        good = self.pairs["done"]

        def dep_line(dep_id: int, emphasis: bool):
            dep = self.by_id.get(dep_id)
            if dep is None:
                return (f"  #{dep_id} (missing)", alert)
            attr = dim if dep.status == DONE else (alert if emphasis else bold)
            return (f"  #{dep.id} ({dep.status}) {dep.name}", attr)

        lines: list = [
            (f"{task.name}", bold),
            (f"STATUS   {status_label(task.status)}", status_attr),
            (f"PROJECT  {task.project}", dim),
            "─",
        ]

        def section(title: str) -> None:
            lines.append("")
            lines.append((title, bold))

        section("DESCRIPTION")
        lines.extend("  " + l for l in (task.description.splitlines() if task.description else ["(none)"]))

        section("EVIDENCE / PRE-TASK ANALYSIS")
        lines.extend("  " + l for l in (task.evidence.splitlines() if task.evidence else ["(none)"]))

        section("BLOCKERS (EXTERNAL)")
        if task.blockers:
            lines.extend((f"  • {b}", alert) for b in task.blockers)
        else:
            lines.append(("  (none)", dim))

        section("DEPENDS ON")
        if task.depends_on:
            lines.extend(dep_line(i, emphasis=True) for i in task.depends_on)
        else:
            lines.append(("  (none)", dim))

        section("AFFECTS")
        if task.affects:
            lines.extend(dep_line(i, emphasis=False) for i in task.affects)
        else:
            lines.append(("  (none)", dim))

        section("BLOCKS (TASKS DEPENDING ON THIS)")
        dependents = [t for t in self.tasks if task.id in t.depends_on and t.id != task.id]
        if dependents:
            lines.extend(dep_line(t.id, emphasis=True) for t in dependents)
        else:
            lines.append(("  (none)", dim))

        lines.append("─")
        if task.status == DONE:
            lines.append(("STATE    done", good))
        elif is_blocked(task, dep_map):
            pending = unfinished_dep_ids(task, dep_map)
            why = []
            if pending:
                why.append("deps " + ",".join(f"#{i}" for i in pending))
            if task.blockers:
                why.append(f"{len(task.blockers)} external blocker(s)")
            lines.append(("STATE    BLOCKED — " + " + ".join(why), alert))
        elif is_ready(task, dep_map):
            lines.append(("STATE    ready to start", good))
        else:
            lines.append(("STATE    in flight", dim))
        lines.append((f"created {task.created_at}  ·  updated {task.updated_at}", dim))
        return lines

    def _scroll_popup(self, stdscr, title: str, lines: list) -> None:
        """lines: plain strings or (text, attr) tuples; '─' rows become separators."""
        height, width = stdscr.getmaxyx()
        wrapped: list[tuple[str, int]] = []
        for line in lines:
            if isinstance(line, tuple):
                text, attr = line
            else:
                text, attr = line, 0
            if text == "─":
                wrapped.append(("SEPARATOR", attr))
                continue
            for part in textwrap.wrap(text, width=max(10, width - 8)) or [""]:
                wrapped.append((part, attr))
        h, w, y, x = self._popup_geometry(wrapped, title, height, width)
        max_lines = max(1, h - 3)
        off = 0
        while True:
            win = self._draw_box(h, w, y, x, title)
            for i in range(max_lines):
                idx = off + i
                if idx >= len(wrapped):
                    break
                text, attr = wrapped[idx]
                if text == "SEPARATOR":
                    text, attr = "─" * (w - 4), attr | curses.A_DIM
                _safe(win, 1 + i, 2, text[: w - 3], attr)
            if off + max_lines < len(wrapped):
                _safe(win, h - 1, 2, " ↓ more ", curses.A_DIM)
            _safe(win, h - 1, max(2, w - 22), " ↑↓ scroll  q close ", curses.A_DIM)
            win.noutrefresh()
            curses.doupdate()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                off = max(0, off - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                off = min(max(0, len(wrapped) - max_lines), off + 1)
            elif ch in (curses.KEY_ENTER, 10, 13, ord("q"), ord("Q"), 27, curses.KEY_RESIZE):
                return

    def _menu(self, stdscr, title: str, options: list[str], current: int) -> int | None:
        """Arrow-select popup. Returns chosen index, or None on cancel."""
        sel = current
        height, width = stdscr.getmaxyx()
        h, w, y, x = self._popup_geometry(options, title, height, width)
        while True:
            win = self._draw_box(h, w, y, x, title)
            for i, opt in enumerate(options):
                attr = curses.A_REVERSE if i == sel else 0
                _safe(win, 1 + i, 2, f" {opt} "[: w - 3], attr)
            _safe(win, h - 1, 2, " ↑↓ select  Enter confirm  Esc cancel ", curses.A_DIM)
            win.noutrefresh()
            curses.doupdate()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel = min(len(options) - 1, sel + 1)
            elif ch in (curses.KEY_ENTER, 10, 13):
                return sel
            elif ch in (27, ord("q"), curses.KEY_RESIZE):
                return None

    def move_menu(self, stdscr) -> None:
        task = self.selected()
        if task is None:
            return
        options = [status_label(s) for s in STATUSES]
        idx = self._menu(stdscr, f"Move #{task.id}", options, STATUSES.index(task.status))
        if idx is not None:
            self.apply_move(task, STATUSES[idx])


def run_board(store: Store, project: str) -> int:
    return curses.wrapper(Board(store, project).run)
