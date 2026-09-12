"""Interactive curses kanban board (TUI) for humans.

Keys:
    h/Left  l/Right   select column        j/Down  k/Up  select task
    Enter             task details         m            move task (status menu)
    < ,    >  .       move card left/right r            reload from db
    ?                 help                 q            quit
Legend: '!' = blocked (unfinished deps or external blockers), dim = done.
"""

from __future__ import annotations

import curses
import textwrap


from . import render
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

CARD_HEIGHT = 3  # two text lines + one blank separator
MIN_COL_WIDTH = 14

_HELP = """\
Board keys
  h / Left, l / Right    select column
  j / Down, k / Up       select task
  Enter                  task details (scroll with arrows, q/Enter closes)
  m                      move task to another status (menu)
  < or ,  /  > or .      move task one column left / right
  r                      reload tasks from the database
  ?                      this help
  q                      quit

Legend
  !    blocked: unfinished dependencies or external blockers
  dim  done tasks
  card line 2 shows dependency ids / external blocker count
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
        self.tasks = self.store.list_tasks(self.project)
        self.by_id = {t.id: t for t in self.tasks}
        self.cols = {s: [t for t in self.tasks if t.status == s] for s in STATUSES}
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
            }
            for i, (status, color) in enumerate(palette.items(), start=1):
                curses.init_pair(i, color, -1)
                self.pairs[status] = curses.color_pair(i)
        except curses.error:
            self.pairs = {s: 0 for s in STATUSES}

    # --- main loop
    def run(self, stdscr) -> int:
        curses.curs_set(0)
        stdscr.keypad(True)
        self.init_colors()
        self.reload()
        while not self.done:
            self.draw(stdscr)
            self.handle(stdscr, stdscr.getch())
        return 0

    # --- drawing
    def draw(self, stdscr) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        _safe(stdscr, 0, 0, f" {self.project} — agenttasker board", curses.A_BOLD)
        _safe(
            stdscr, 1, 0,
            " ←→ column  ↑↓ task  Enter: details  m: move  </>: shift  r: reload  ?: help  q: quit",
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
        max_cards = max(0, body_h // CARD_HEIGHT)

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

        footer = self.message if self.message else self.footer_text()
        _safe(stdscr, height - 1, 0, footer[: width - 1], curses.A_DIM)
        stdscr.noutrefresh()
        curses.doupdate()

    def draw_card(self, stdscr, task: Task, y: int, x: int, w: int, selected: bool) -> None:
        dep_map = self.dep_status_map()
        blocked = is_blocked(task, dep_map)
        mark = "!" if blocked else ("*" if task.status in STARTABLE_STATUSES and is_ready(task, dep_map) else " ")
        attr = self.pairs.get(task.status, 0)
        if task.status == DONE:
            attr |= curses.A_DIM
        if selected:
            attr |= curses.A_REVERSE

        line1 = f" #{task.id} {mark} {task.name}"
        _safe(stdscr, y, x, line1[:w], attr)

        sub = ""
        if task.depends_on:
            sub = " deps:" + ",".join(f"#{i}" for i in task.depends_on[:4])
            if len(task.depends_on) > 4:
                sub += "…"
        elif task.blockers:
            sub = f" ext:{len(task.blockers)} blocker(s)"
        sub_attr = curses.A_DIM | (curses.A_REVERSE if selected else 0)
        _safe(stdscr, y + 1, x, sub[:w], sub_attr)

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
    def _popup_geometry(self, lines: list[str], title: str, scr_h: int, scr_w: int):
        text_w = max([len(title)] + [len(line) for line in lines]) + 4
        w = max(30, min(text_w, scr_w - 2))
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
        """Scrollable full-detail view."""
        self._scroll_popup(stdscr, f"#{task.id} {task.name}",
                           render.detail(task, self.by_id).splitlines())

    def help_popup(self, stdscr) -> None:
        self._scroll_popup(stdscr, "Help", _HELP.splitlines())

    def _scroll_popup(self, stdscr, title: str, lines: list[str]) -> None:
        height, width = stdscr.getmaxyx()
        wrapped: list[str] = []
        for line in lines:
            wrapped.extend(textwrap.wrap(line, width=max(10, width - 8)) or [""])
        h, w, y, x = self._popup_geometry(wrapped, title, height, width)
        max_lines = max(1, h - 3)
        off = 0
        while True:
            win = self._draw_box(h, w, y, x, title)
            for i in range(max_lines):
                line = wrapped[off + i] if off + i < len(wrapped) else ""
                _safe(win, 1 + i, 2, line[: w - 3])
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
