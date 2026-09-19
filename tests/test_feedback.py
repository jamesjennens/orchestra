import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import feedback


def payload(operation_id, *, supersedes=None, body="Observed behavior"):
    return {
        "operation_id": operation_id,
        "created_at": "2026-09-19T23:00:00+00:00",
        "body": body,
        "source": {"task": "kittrial-5bb.13", "version": "base-1"},
        "evidence": ["test://fixture/1"],
        "triage": {"task": "kittrial-5bb.13", "label": "review"},
        "reminder": {"kind": "checkpoint", "text": "Mention during handoff"},
        "supersedes": supersedes,
    }


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / feedback.FEED_NAME

    def test_add_is_private_append_only_and_retry_is_idempotent(self):
        first = feedback.add(self.path, "session-one", payload("op-1"))
        retry = feedback.add(self.path, "session-one", payload("op-1"))
        self.assertFalse(first["reconciled"])
        self.assertTrue(retry["reconciled"])
        self.assertEqual(first["entry"], retry["entry"])
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)

    def test_conflicting_retry_is_rejected(self):
        feedback.add(self.path, "session-one", payload("op-1"))
        with self.assertRaisesRegex(ValueError, "different content"):
            feedback.add(self.path, "session-one", payload("op-1", body="changed"))

    def test_cursor_paging_is_stable(self):
        for number in range(3):
            feedback.add(self.path, "session-one", payload("op-%d" % number))
        page = feedback.list_entries(self.path, limit=2)
        self.assertEqual([item["sequence"] for item in page["entries"]], [1, 2])
        next_page = feedback.list_entries(self.path, limit=2, cursor=page["next_cursor"])
        self.assertEqual([item["sequence"] for item in next_page["entries"]], [3])
        self.assertIsNone(next_page["next_cursor"])

    def test_correction_supersedes_without_rewriting_source(self):
        original = feedback.add(self.path, "session-one", payload("op-1"))["entry"]
        correction = feedback.add(
            self.path, "session-two",
            payload("op-2", supersedes=original["entry_id"], body="Corrected observation"),
            kind="correction",
        )["entry"]
        entries = feedback.list_entries(self.path)["entries"]
        self.assertEqual(entries[0]["body"], "Observed behavior")
        self.assertEqual(correction["supersedes"], original["entry_id"])
        self.assertEqual(correction["kind"], "correction")

    def test_malformed_or_reordered_feed_is_rejected(self):
        self.path.write_text(json.dumps({"sequence": 2}) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            feedback.validate_feed_text(self.path.read_text(encoding="utf-8"))

    def test_malformed_cursor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid feedback cursor"):
            feedback.list_entries(self.path, cursor="not-base64")

    def test_command_requires_explicit_file_payload(self):
        with self.assertRaisesRegex(ValueError, "requires --file"):
            feedback.execute(self.path, "session-one", ["add"], {})


if __name__ == "__main__":
    unittest.main()
