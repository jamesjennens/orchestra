import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.assertEqual(next_page["watermark"]["sequence"], 3)

    def test_cursor_is_feed_scoped_and_rejects_ahead_or_rollback(self):
        for number in range(3):
            feedback.add(self.path, "session-one", payload("op-%d" % number))
        page = feedback.list_entries(self.path, limit=1)
        other = Path(self.temp.name) / "other.jsonl"
        feedback.add(other, "session-one", payload("other"))
        with self.assertRaisesRegex(ValueError, "Invalid feedback cursor"):
            feedback.list_entries(other, cursor=page["next_cursor"])
        with self.assertRaisesRegex(ValueError, "ahead"):
            feedback.list_entries(self.path, cursor=feedback.encode_cursor(
                self.path, 9, {"sequence": 9, "digest": "x"}))
        self.path.write_text("\n".join(
            json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in feedback._read(self.path)[:2]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ahead"):
            feedback.list_entries(self.path, cursor=page["next_cursor"])

    def test_cursor_rejects_rollback_then_regrow_with_replacement_entries(self):
        for number in range(3):
            feedback.add(self.path, "session-one", payload("op-%d" % number))
        page = feedback.list_entries(self.path, limit=1)
        retained = feedback._read(self.path)[0]
        replacement = dict(retained)
        replacement["operation_id"] = "replacement"
        replacement["entry_id"] = feedback._entry_id("replacement")
        replacement["sequence"] = 2
        third = dict(replacement)
        third["operation_id"] = "replacement-2"
        third["entry_id"] = feedback._entry_id("replacement-2")
        third["sequence"] = 3
        self.path.write_text(
            "\n".join(json.dumps(item, sort_keys=True, separators=(",", ":"))
                      for item in (retained, replacement, third)) + "\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "match the current feed"):
            feedback.list_entries(self.path, cursor=page["next_cursor"])

    def test_cursor_rejects_changes_to_consumed_or_observed_prefix_after_growth(self):
        for number in range(3):
            feedback.add(self.path, "session-one", payload("op-%d" % number))
        page = feedback.list_entries(self.path, limit=1)
        original = feedback._read(self.path)

        changed_consumed = [dict(entry) for entry in original]
        changed_consumed[0]["body"] = "changed consumed entry"
        self.path.write_text("\n".join(
            json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in changed_consumed) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "match the current feed"):
            feedback.list_entries(self.path, cursor=page["resume_cursor"])

        changed_observed = [dict(entry) for entry in original]
        changed_observed[1]["body"] = "changed observed but not consumed entry"
        self.path.write_text("\n".join(
            json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in changed_observed) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "match the current feed"):
            feedback.list_entries(self.path, cursor=page["resume_cursor"])

        self.path.write_text("\n".join(
            json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in original) + "\n", encoding="utf-8")
        for number in range(3, 5):
            feedback.add(self.path, "session-one", payload("op-%d" % number))
        resumed = feedback.list_entries(self.path, cursor=page["resume_cursor"])
        self.assertEqual([entry["sequence"] for entry in resumed["entries"]], [2, 3, 4, 5])

    def test_append_after_page_is_visible_on_live_resume(self):
        feedback.add(self.path, "session-one", payload("op-1"))
        page = feedback.list_entries(self.path, limit=1)
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(page["watermark"]["sequence"], 1)
        feedback.add(self.path, "session-one", payload("op-2"))
        resumed = feedback.list_entries(self.path, cursor=feedback.encode_cursor(
            self.path, page["watermark"]["sequence"], page["watermark"]))
        self.assertEqual([entry["sequence"] for entry in resumed["entries"]], [2])

    def test_partial_tail_is_quarantined_and_acknowledged_prefix_survives(self):
        feedback.add(self.path, "session-one", payload("op-1"))
        with self.path.open("ab") as stream:
            stream.write(b'{"partial":')
        entries = feedback.list_entries(self.path)["entries"]
        self.assertEqual([entry["sequence"] for entry in entries], [1])
        self.assertEqual(len(list(self.path.parent.glob(self.path.name + ".*.incomplete"))), 1)
        feedback.add(self.path, "session-one", payload("op-2"))
        self.assertEqual(len(feedback.list_entries(self.path)["entries"]), 2)

    def test_first_torn_append_is_quarantined_and_repeated_recovery_is_idempotent(self):
        self.path.write_bytes(b'{"partial":')
        self.assertEqual(feedback.list_entries(self.path)["entries"], [])
        self.assertEqual(feedback.list_entries(self.path)["entries"], [])
        self.assertEqual(len(list(self.path.parent.glob(self.path.name + ".*.incomplete"))), 1)

    def test_unicode_jsonl_separators_round_trip_and_exact_retry(self):
        body = "before\u0085middle\u2028line\u2029paragraph"
        source = payload("unicode-op", body=body)
        result = feedback.add(self.path, "session-one", source)
        self.assertEqual(feedback.list_entries(self.path)["entries"][0]["body"], body)
        retry = feedback.add(self.path, "session-one", source)
        self.assertTrue(retry["reconciled"])
        self.assertEqual(result["entry"], retry["entry"])

    def test_valid_final_record_without_newline_is_normalized_before_append(self):
        entry = feedback._make_entry(1, "session-one", payload("op-1"), "feedback")
        self.path.write_text(json.dumps(entry, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        self.assertEqual(feedback.list_entries(self.path)["entries"][0]["sequence"], 1)
        feedback.add(self.path, "session-one", payload("op-2"))
        self.assertEqual([entry["sequence"] for entry in feedback.list_entries(self.path)["entries"]], [1, 2])

    def test_recovery_failure_does_not_lose_acknowledged_prefix(self):
        first = feedback.add(self.path, "session-one", payload("op-1"))["entry"]
        with self.path.open("ab") as stream:
            stream.write(b'{"partial":')
        original = feedback._atomic_replace
        with patch.object(feedback, "_atomic_replace", side_effect=OSError("injected recovery interruption")):
            with self.assertRaisesRegex(OSError, "injected"):
                feedback.list_entries(self.path)
        acknowledged = json.dumps(first, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertTrue(self.path.read_bytes().startswith(acknowledged + b"\n"))
        self.assertIs(feedback._atomic_replace, original)

    def test_quarantine_write_interruption_preserves_feed_and_retries(self):
        first = feedback.add(self.path, "session-one", payload("op-1"))["entry"]
        tail = b'{"interrupted":'
        with self.path.open("ab") as stream:
            stream.write(tail)
        original_bytes = self.path.read_bytes()

        def interrupted(fd, content):
            with feedback.os.fdopen(fd, "wb") as stream:
                stream.write(content[:3])
                stream.flush()
            raise OSError("injected quarantine write interruption")

        with patch.object(feedback, "_write_quarantine_temporary", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "injected"):
                feedback.list_entries(self.path)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertFalse(list(self.path.parent.glob(self.path.name + ".*.incomplete")))

        self.assertEqual(feedback.list_entries(self.path)["entries"], [first])
        quarantine = next(self.path.parent.glob(self.path.name + ".*.incomplete"))
        self.assertEqual(quarantine.read_bytes(), tail)

    def test_quarantine_publish_interruption_preserves_tail_and_retries(self):
        feedback.add(self.path, "session-one", payload("op-1"))
        tail = b'{"interrupted":'
        with self.path.open("ab") as stream:
            stream.write(tail)
        original_bytes = self.path.read_bytes()
        with patch.object(feedback.os, "replace", side_effect=OSError("injected quarantine publish interruption")):
            with self.assertRaisesRegex(OSError, "injected"):
                feedback.list_entries(self.path)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertFalse(list(self.path.parent.glob(self.path.name + ".*.incomplete")))
        self.assertEqual(len(feedback.list_entries(self.path)["entries"]), 1)
        quarantine = next(self.path.parent.glob(self.path.name + ".*.incomplete"))
        self.assertEqual(quarantine.read_bytes(), tail)

    def test_injected_append_failure_recovers_on_exact_retry(self):
        feedback.add(self.path, "session-one", payload("op-1"))
        original = feedback._write_append

        def interrupted(path, entry):
            with path.open("ab") as stream:
                stream.write(b'{"partial":')
            raise OSError("injected append interruption")

        with patch.object(feedback, "_write_append", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "injected"):
                feedback.add(self.path, "session-one", payload("op-2"))
        result = feedback.add(self.path, "session-one", payload("op-2"))
        self.assertFalse(result["reconciled"])
        self.assertEqual([entry["sequence"] for entry in feedback.list_entries(self.path)["entries"]], [1, 2])
        self.assertIs(feedback._write_append, original)

    def test_empty_page_exposes_durable_watermark(self):
        empty = feedback.list_entries(self.path)
        self.assertEqual(empty["entries"], [])
        self.assertEqual(empty["watermark"], {"sequence": 0, "digest": ""})
        self.assertIsNone(empty["next_cursor"])
        self.assertIsNotNone(empty["resume_cursor"])
        feedback.add(self.path, "session-one", payload("op-1"))
        resumed = feedback.list_entries(self.path, cursor=empty["resume_cursor"])
        self.assertEqual([entry["sequence"] for entry in resumed["entries"]], [1])

    def test_final_page_exposes_public_resume_cursor(self):
        feedback.add(self.path, "session-one", payload("op-1"))
        final = feedback.list_entries(self.path)
        self.assertIsNone(final["next_cursor"])
        self.assertIsNotNone(final["resume_cursor"])
        feedback.add(self.path, "session-one", payload("op-2"))
        resumed = feedback.list_entries(self.path, cursor=final["resume_cursor"])
        self.assertEqual([entry["sequence"] for entry in resumed["entries"]], [2])

    def test_feed_symlink_is_rejected_without_mutating_target(self):
        target = Path(self.temp.name) / "target.jsonl"
        feedback.add(target, "session-one", payload("target"))
        try:
            self.path.symlink_to(target)
        except OSError as exc:
            self.skipTest("Symlink privilege unavailable: %s" % exc)
        original = target.read_bytes()
        for operation in (lambda: feedback.list_entries(self.path),
                          lambda: feedback.add(self.path, "session-one", payload("new"))):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    operation()
                self.assertEqual(target.read_bytes(), original)

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
