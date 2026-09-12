"""Behavior tests for the AgentTasker domain core (stdlib unittest, no network)."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agenttasker.cli import cmd_ls
from agenttasker.core import (
    BACKLOG,
    DONE,
    IN_PROGRESS,
    TODO,
    Store,
    TaskError,
    display_ref,
    is_blocked,
    is_ready,
    next_status,
    normalize_status,
    parse_ref,
    prev_status,
)


def _ls_args(**over):
    base = dict(project="demo", all_projects=False, status=None, search=None,
                ready=False, blocked=False, json=False,
                priority=None, tag=None, stale=None)
    base.update(over)
    return Namespace(**base)


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_add_get_roundtrip_and_defaults(self):
        task = self.store.add_task("demo", "Write tests")
        self.assertEqual(task.status, BACKLOG)
        self.assertEqual(task.blockers, [])
        fetched = self.store.get("demo", "demo-1")
        self.assertEqual((fetched.id, fetched.name, fetched.status), (1, "Write tests", BACKLOG))
        self.assertEqual(display_ref("demo", fetched.id), "demo-1")

    def test_invalid_status_rejected(self):
        with self.assertRaises(TaskError):
            self.store.add_task("demo", "x", status="bogus")
        self.assertEqual(normalize_status("In Progress"), IN_PROGRESS)
        self.assertEqual(normalize_status("in-review"), "in_review")

    def test_dep_validation_missing_and_self(self):
        with self.assertRaises(TaskError):
            self.store.add_task("demo", "x", depends_on=["999"])
        a = self.store.add_task("demo", "a")
        with self.assertRaises(TaskError):
            self.store.update(a, depends_on=[a.id])

    def test_ready_and_blocked_computation(self):
        a = self.store.add_task("demo", "a", status=TODO)
        b = self.store.add_task("demo", "b", status=TODO, depends_on=[a.id])
        c = self.store.add_task("demo", "c", status=TODO, blockers=["waiting on creds"])

        self.assertTrue(self.store.is_ready(a))
        self.assertFalse(self.store.is_ready(b))   # dep unfinished
        self.assertFalse(self.store.is_ready(c))   # external blocker
        self.assertFalse(self.store.is_blocked(a))
        self.assertTrue(self.store.is_blocked(b))
        self.assertTrue(self.store.is_blocked(c))

        self.store.set_status(a, DONE)
        self.assertTrue(self.store.is_ready(b))
        self.assertFalse(self.store.is_blocked(b))
        # done tasks never count as blocked, even with stray blockers
        self.store.update(b, blockers=["stale note"])
        self.store.set_status(b, DONE)
        self.assertFalse(self.store.is_blocked(b))

    def test_ready_requires_startable_status(self):
        a = self.store.add_task("demo", "a", status=IN_PROGRESS)
        self.assertFalse(self.store.is_ready(a))

    def test_parse_ref_forms(self):
        self.assertEqual(parse_ref("12", "demo"), 12)
        self.assertEqual(parse_ref("#12", "demo"), 12)
        self.assertEqual(parse_ref("Demo-12", "demo"), 12)
        with self.assertRaises(TaskError):
            parse_ref("other-12", "demo")
        with self.assertRaises(TaskError):
            parse_ref("abc", "demo")

    def test_status_stepping_boundaries(self):
        self.assertEqual(next_status("in_review"), DONE)
        with self.assertRaises(TaskError):
            next_status(DONE)
        self.assertEqual(prev_status(BACKLOG), "deferred")
        with self.assertRaises(TaskError):
            prev_status("deferred")

    def test_delete_scrubs_references(self):
        a = self.store.add_task("demo", "a")
        b = self.store.add_task("demo", "b", depends_on=[a.id], affects=[a.id])
        notes = self.store.delete(a)
        self.assertEqual(notes, ["removed reference from demo-2 (b)"])
        fresh = self.store.get("demo", b.id)
        self.assertEqual(fresh.depends_on, [])
        self.assertEqual(fresh.affects, [])

    def test_list_ordering_and_filters(self):
        self.store.add_task("demo", "first", status=TODO)
        self.store.add_task("demo", "second", status=IN_PROGRESS)
        self.store.add_task("demo", "third", status=TODO)
        listed = self.store.list_tasks("demo")
        # board order first, then id
        self.assertEqual([t.name for t in listed], ["first", "third", "second"])
        only_todo = self.store.list_tasks("demo", statuses=[TODO])
        self.assertEqual([t.name for t in only_todo], ["first", "third"])
        hits = self.store.list_tasks("demo", search="third")
        self.assertEqual([t.name for t in hits], ["third"])

    def test_projects_are_isolated_namespaces(self):
        self.store.add_task("demo", "a")
        self.store.add_task("other", "b")
        self.assertEqual(len(self.store.list_tasks("demo")), 1)
        counts = self.store.projects()
        self.assertEqual(counts["demo"]["total"], 1)
        self.assertEqual(counts["other"]["total"], 1)
        with self.assertRaises(TaskError):
            self.store.get("demo", "other-2")  # foreign prefix rejected


class CliListingTest(unittest.TestCase):
    """`ls` rendering must derive dependency flags from the FULL project set,
    not the filtered listing (regression: ready listing showed done deps as blocking)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self._old_project = os.environ.get("AGENTTASKER_PROJECT")
        os.environ["AGENTTASKER_PROJECT"] = "demo"
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_project is None:
            os.environ.pop("AGENTTASKER_PROJECT", None)
        else:
            os.environ["AGENTTASKER_PROJECT"] = self._old_project

    def _run_ls(self, **over):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_ls(_ls_args(**over), self.store)
        return buf.getvalue()

    def test_filtered_listing_keeps_dep_flags_accurate(self):
        dep = self.store.add_task("demo", "dep", status=TODO)
        self.store.add_task("demo", "child", status=TODO, depends_on=[dep.id])
        self.store.set_status(dep, DONE)

        out = self._run_ls(ready=True)
        self.assertIn("#2", out)          # child is ready now
        self.assertNotIn("deps:#1", out)  # ...and its finished dep must not render as blocking
        self.assertIn("[ready]", out)

        out = self._run_ls()
        self.assertIn("[ready]", out)
        self.assertNotIn("deps:#1", out)


if __name__ == "__main__":
    unittest.main()
