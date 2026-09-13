"""Tests for atomic claims, priorities/tags, export/import, and attachments."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agenttasker.core import (
    EXPORT_FORMAT,
    EXPORT_VERSION,
    Store,
    TaskError,
    claim_age_hours,
    normalize_priority,
    normalize_type,
)


class ClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.task = self.store.add_task("demo", "work item", status="todo")

    def test_claim_is_atomic_and_moves_to_in_progress(self):
        claimed = self.store.claim(self.task, "agent-a")
        self.assertEqual(claimed.owner, "agent-a")
        self.assertEqual(claimed.status, "in_progress")
        self.assertIsNotNone(claim_age_hours(claimed))

        # second agent, separate connection, must be refused
        with Store(Path(self.tmp.name) / "tasks.db") as other:
            fresh = other.get("demo", self.task.id)
            with self.assertRaises(TaskError) as ctx:
                other.claim(fresh, "agent-b")
            self.assertIn("agent-a", str(ctx.exception))

    def test_reclaim_by_same_owner_is_idempotent(self):
        self.store.claim(self.task, "agent-a")
        again = self.store.claim(self.task, "agent-a")
        self.assertEqual(again.owner, "agent-a")

    def test_force_takes_claim(self):
        self.store.claim(self.task, "agent-a")
        taken = self.store.claim(self.task, "agent-b", force=True)
        self.assertEqual(taken.owner, "agent-b")

    def test_release_requires_matching_owner(self):
        self.store.claim(self.task, "agent-a")
        with self.assertRaises(TaskError):
            self.store.release(self.task, owner="agent-b")
        released = self.store.release(self.task, owner="agent-a")
        self.assertEqual(released.owner, "")

    def test_handoff_guards_holder(self):
        self.store.claim(self.task, "agent-a")
        with self.assertRaises(TaskError):
            self.store.handoff(self.task, "agent-c", from_owner="agent-b")
        moved = self.store.handoff(self.task, "agent-c", from_owner="agent-a")
        self.assertEqual(moved.owner, "agent-c")

    def test_append_evidence_does_not_clobber(self):
        first = self.store.append_evidence(self.task, "first finding")
        # simulate a second writer working from a stale snapshot
        second = self.store.append_evidence(first, "second finding")
        self.assertIn("first finding", second.evidence)
        self.assertIn("second finding", second.evidence)


class PriorityTagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_normalize_priority(self):
        self.assertEqual(normalize_priority("P0"), 0)
        self.assertEqual(normalize_priority("p3"), 3)
        self.assertEqual(normalize_priority(1), 1)
        with self.assertRaises(TaskError):
            normalize_priority("P9")

    def test_priority_sorts_within_status(self):
        self.store.add_task("demo", "low", status="todo", priority=3)
        self.store.add_task("demo", "urgent", status="todo", priority=0)
        self.store.add_task("demo", "mid", status="todo", priority=2)
        names = [t.name for t in self.store.list_tasks("demo")]
        self.assertEqual(names, ["urgent", "mid", "low"])

    def test_tag_filter_and_priority_filter(self):
        self.store.add_task("demo", "a", tags=["infra", "backend"])
        self.store.add_task("demo", "b", tags=["frontend"])
        self.store.add_task("demo", "c", priority=0)
        self.assertEqual([t.name for t in self.store.list_tasks("demo", tags=["infra"])], ["a"])
        self.assertEqual([t.name for t in self.store.list_tasks("demo", priorities=[0])], ["c"])


class ExportImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def _seed(self):
        a = self.store.add_task("proj", "first", status="done", evidence="did it", priority=0,
                                tags=["infra"])
        b = self.store.add_task("proj", "second", depends_on=[a.id], affects=[a.id],
                                blockers=["waiting"], description="desc")
        return a, b

    def test_round_trip_preserves_everything(self):
        a, b = self._seed()
        payload = self.store.export_tasks("proj")
        self.assertEqual(payload["format"], EXPORT_FORMAT)
        self.assertEqual(payload["version"], EXPORT_VERSION)

        with tempfile.TemporaryDirectory() as other_dir:
            with Store(Path(other_dir) / "tasks.db") as dest:
                report = dest.import_tasks(payload)
                self.assertEqual(len(report["created"]), 2)
                imported = dest.list_tasks("proj")
                self.assertEqual([t.name for t in imported], ["second", "first"])  # board order
                by_name = {t.name: t for t in imported}
                first, second = by_name["first"], by_name["second"]
                self.assertEqual(first.status, "done")
                self.assertEqual(first.priority, 0)
                self.assertEqual(first.tags, ["infra"])
                self.assertEqual(first.evidence, "did it")
                # dependency remapped to the new id
                self.assertEqual(second.depends_on, [first.id])
                self.assertEqual(second.affects, [first.id])
                self.assertEqual(second.blockers, ["waiting"])

    def test_merge_skips_existing_and_links_to_it(self):
        a, b = self._seed()
        payload = self.store.export_tasks("proj")
        with tempfile.TemporaryDirectory() as other_dir:
            with Store(Path(other_dir) / "tasks.db") as dest:
                existing = dest.add_task("proj", "first", status="todo")
                report = dest.import_tasks(payload)
                self.assertEqual(len(report["existing"]), 1)
                self.assertEqual(len(report["created"]), 1)
                second = dest.get("proj", existing.id + 1)
                # dep now points at the pre-existing task's id
                self.assertEqual(second.depends_on, [existing.id])

    def test_replace_mode_and_dry_run(self):
        self._seed()
        payload = self.store.export_tasks("proj")
        report = self.store.import_tasks(payload, mode="replace", dry_run=True)
        self.assertTrue(any("would create" in line for line in report["created"]))
        self.assertEqual(len(self.store.list_tasks("proj")), 2)  # nothing written
        self.store.import_tasks(payload, mode="replace")
        names = [t.name for t in self.store.list_tasks("proj")]
        self.assertEqual(names, ["second", "first"])  # board order

    def test_rejects_unknown_format_and_version(self):
        with self.assertRaises(TaskError):
            self.store.import_tasks({"format": "nope", "version": 1, "tasks": []})
        with self.assertRaises(TaskError):
            self.store.import_tasks({"format": EXPORT_FORMAT, "version": 99, "tasks": []})


class AttachmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.task = self.store.add_task("demo", "with files")

    def test_attach_list_read_detach(self):
        source = Path(self.tmp.name) / "blob.bin"
        source.write_bytes(b"\x00\x01\x02binary!")
        att = self.store.add_attachment(self.task, source)
        self.assertEqual(att["size"], 10)

        listed = self.store.list_attachments(self.task)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["filename"], "blob.bin")
        # file lives outside the db file itself, content-addressed
        self.assertTrue(Path(listed[0]["path"]).is_file())

        data = self.store.read_attachment_bytes(self.task, listed[0]["id"])
        self.assertEqual(data, b"\x00\x01\x02binary!")

        name = self.store.remove_attachment(self.task, listed[0]["id"])
        self.assertEqual(name, "blob.bin")
        self.assertEqual(self.store.list_attachments(self.task), [])
        self.assertFalse(Path(listed[0]["path"]).exists())

    def test_delete_task_removes_files(self):
        source = Path(self.tmp.name) / "gone.txt"
        source.write_text("bye")
        self.store.add_attachment(self.task, source)
        path = self.store.list_attachments(self.task)[0]["path"]
        self.store.delete(self.task)
        self.assertFalse(Path(path).exists())

    def test_export_embeds_base64_and_import_restores(self):
        import base64

        source = Path(self.tmp.name) / "img.png"
        payload_bytes = b"\x89PNG\r\n\x1a\nfake-image"
        source.write_bytes(payload_bytes)
        self.store.add_attachment(self.task, source, filename="chart.png")

        export = self.store.export_tasks("demo")
        entry = export["tasks"][0]["attachments"][0]
        self.assertEqual(entry["filename"], "chart.png")
        self.assertEqual(base64.b64decode(entry["content_b64"]), payload_bytes)

        with tempfile.TemporaryDirectory() as other_dir:
            with Store(Path(other_dir) / "tasks.db") as dest:
                dest.import_tasks(export)
                imported = dest.list_tasks("demo")[0]
                atts = dest.list_attachments(imported)
                self.assertEqual(len(atts), 1)
                self.assertEqual(dest.read_attachment_bytes(imported, atts[0]["id"]), payload_bytes)


class TaskTypeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "tasks.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_normalize_type_aliases_and_errors(self):
        self.assertEqual(normalize_type("BUG"), "bugfix")
        self.assertEqual(normalize_type("fix"), "bugfix")
        self.assertEqual(normalize_type("feat"), "feature")
        self.assertEqual(normalize_type("refactor"), "improvement")
        self.assertEqual(normalize_type("Task"), "task")
        with self.assertRaises(TaskError):
            normalize_type("epic")

    def test_type_roundtrip_and_update(self):
        t1 = self.store.add_task("demo", "crash on boot", type="bugfix", priority=0)
        t2 = self.store.add_task("demo", "new dashboard", type="feature")
        t3 = self.store.add_task("demo", "plain")
        self.assertEqual((t1.type, t2.type, t3.type), ("bugfix", "feature", "task"))
        updated = self.store.update(t3, type="chore")
        self.assertEqual(updated.type, "chore")
        # default sort stays status/priority/id; types ride along
        self.assertEqual([t.name for t in self.store.list_tasks("demo")],
                         ["crash on boot", "new dashboard", "plain"])

    def test_type_survives_export_import(self):
        self.store.add_task("proj", "typed", type="bugfix", priority=1)
        payload = self.store.export_tasks("proj")
        with tempfile.TemporaryDirectory() as other:
            with Store(Path(other) / "tasks.db") as dest:
                dest.import_tasks(payload)
                self.assertEqual(dest.list_tasks("proj")[0].type, "bugfix")
class ListOrderTest(unittest.TestCase):
    """The list view orders by status (todo, backlog, deferred, in-flight, done), then priority."""

    def test_list_view_order(self):
        from agenttasker.tui import Board

        with tempfile.TemporaryDirectory() as tmp:
            with Store(Path(tmp) / "tasks.db") as store:
                store.add_task("d", "done old", status="done")
                store.add_task("d", "review", status="in_review")
                store.add_task("d", "inprog", status="in_progress")
                store.add_task("d", "deferred", status="deferred")
                store.add_task("d", "backlog low", status="backlog", priority=3)
                store.add_task("d", "backlog high", status="backlog", priority=0)
                store.add_task("d", "todo p2", status="todo")
                store.add_task("d", "todo p0", status="todo", priority=0)
                board = Board(store, "d")
                board.reload()
                names = [t.name for t in board.list_tasks_sorted()]
                self.assertEqual(
                    names,
                    ["todo p0", "todo p2", "backlog high", "backlog low",
                     "deferred", "inprog", "review", "done old"],
                )




if __name__ == "__main__":
    unittest.main()
