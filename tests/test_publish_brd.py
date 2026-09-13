"""Publisher tests for docs/REQUIREMENTS_CONTRACT.md contract version 1.

Focus: publisher behavior, recovery from interruption, exact artifact bytes and
concurrent writers. Everything runs offline in temporary directories; no server
is contacted and nothing in the repository is modified.
"""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import publish_brd
import requirements
from requirements import content_hash, load_json

BASELINE = ROOT / "docs" / "requirements-baseline.json"
NAME = "draft-0.1"
ARTIFACTS = ["BRD.md", "manifest.json", "receipt.json"]
RECEIPTED = ["BRD.md", "manifest.json"]
ACCEPTANCE_SECTION = "\n## Acceptance\n"


def real():
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def stamp(record):
    record = copy.deepcopy(record)
    record.pop("sha256", None)
    record["sha256"] = content_hash(record)
    return record


def restamp(manifest):
    manifest = copy.deepcopy(manifest)
    manifest["narrative"] = [stamp(record) for record in manifest["narrative"]]
    manifest["requirements"] = [stamp(record) for record in manifest["requirements"]]
    manifest.pop("sha256", None)
    manifest["sha256"] = content_hash(manifest)
    return manifest


def draft():
    return restamp(real())


def renamed(name):
    manifest = draft()
    manifest["baseline"] = name
    return restamp(manifest)


def amended():
    """Same baseline name, different selected content: a conflicting retry."""
    manifest = draft()
    manifest["requirements"][0]["description"] = manifest["requirements"][0]["description"] + (
        "\nAmended for a conflicting retry.\n"
    )
    return restamp(manifest)


def accepted():
    manifest = draft()
    manifest["state"] = "accepted"
    for record in manifest["narrative"] + manifest["requirements"]:
        record["acceptance_state"] = "accepted"
    manifest = restamp(manifest)
    acceptance = {
        "manifest_sha256": manifest["sha256"],
        "decision_id": "kittrial-pth.17",
        "owners": ["james"],
        "approvers": ["james"],
        "policy": "any-owner",
        "evidence": "Owner accepted the requirements-driven direction on 2026-09-13.",
    }
    return manifest, acceptance


def symlink_or_skip(case, target, link, directory):
    """Create a link or skip: Windows may refuse without Developer Mode."""
    try:
        os.symlink(str(target), str(link), target_is_directory=directory)
    except (OSError, NotImplementedError):
        case.skipTest("symbolic links are unavailable on this host")


class PublisherCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.out = Path(self.temporary.name) / "publications"

    def published(self, manifest, acceptance=None, output=None):
        return publish_brd.publish(manifest, output or self.out, acceptance)

    def pointer_bytes(self):
        return (self.out / "current.json").read_bytes()

    def staging_leftovers(self):
        return sorted(p.name for p in self.out.glob(".staging-*")) + sorted(
            p.name for p in self.out.glob(".current-*")
        )

    def assert_nothing_published(self):
        if self.out.exists():
            self.assertEqual(sorted(p.name for p in self.out.iterdir()), [])
        self.assertFalse((self.out / NAME).exists())


class DraftPublicationTests(PublisherCase):
    def test_publishes_named_directory_with_documented_artifacts(self):
        target = self.published(real())
        self.assertEqual(target, self.out / NAME)
        self.assertTrue(target.is_dir())
        self.assertEqual(sorted(p.name for p in target.iterdir()), ARTIFACTS)
        self.assertTrue((self.out / "current.json").is_file())
        self.assertEqual(self.staging_leftovers(), [])

    def test_manifest_json_is_canonical_and_keeps_content_identity(self):
        manifest = real()
        target = self.published(manifest)
        raw = (target / "manifest.json").read_bytes()
        self.assertEqual(raw, requirements.canonical_bytes(manifest))
        self.assertNotIn(b"\r", raw)
        reloaded = load_json(target / "manifest.json")
        self.assertIsNone(requirements.validate_manifest(reloaded))
        self.assertEqual(content_hash(reloaded), manifest["sha256"])

    def test_brd_renders_every_selected_description_verbatim(self):
        manifest = real()
        target = self.published(manifest)
        text = (target / "BRD.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("\r"), 0)
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("\n\n\n", text)
        for record in manifest["requirements"]:
            self.assertIn("### %s" % record["title"], text)
            self.assertEqual(text.count(record["description"]), 1)
            self.assertIn("- Source ID: %s" % record["id"], text)
            self.assertIn("- Key: %s" % record["key"], text)
            self.assertIn("- Revision: 1", text)
            self.assertIn("- SHA-256: %s" % record["sha256"], text)
        for record in manifest["narrative"]:
            self.assertIn(record["description"], text)
        self.assertLess(text.index("## Narrative"), text.index("## Requirements"))
        narrative_order = [record["title"] for record in manifest["narrative"]]
        self.assertLess(
            text.index(narrative_order[0]), text.index("## Requirements")
        )

    def test_draft_is_conspicuously_draft_and_carries_no_acceptance(self):
        target = self.published(real())
        text = (target / "BRD.md").read_text(encoding="utf-8")
        self.assertIn(publish_brd.DRAFT_BANNER, text)
        self.assertNotIn(publish_brd.ACCEPTED_BANNER, text)
        self.assertNotIn(ACCEPTANCE_SECTION, text)
        self.assertIn("- Content state: draft", text)
        self.assertNotIn("acceptance.json", "\n".join(p.name for p in target.iterdir()))

    def test_receipt_hashes_exact_bytes_and_excludes_itself(self):
        target = self.published(real())
        receipt = load_json(target / "receipt.json")
        self.assertEqual(sorted(receipt), sorted(publish_brd.RECEIPT_FIELDS))
        self.assertEqual(receipt["baseline"], NAME)
        self.assertEqual(receipt["state"], "draft")
        self.assertEqual(receipt["manifest_sha256"], real()["sha256"])
        self.assertEqual(sorted(receipt["files"]), RECEIPTED)
        for name, digest in receipt["files"].items():
            self.assertEqual(publish_brd.sha256_bytes((target / name).read_bytes()), digest)
        self.assertNotIn("receipt.json", receipt["files"])

    def test_current_pointer_names_baseline_and_receipt_hash(self):
        target = self.published(real())
        pointer = load_json(self.out / "current.json")
        self.assertEqual(pointer["baseline"], NAME)
        self.assertEqual(pointer["manifest_sha256"], real()["sha256"])
        self.assertEqual(
            pointer["receipt_sha256"],
            publish_brd.sha256_bytes((target / "receipt.json").read_bytes()),
        )
        self.assertNotIn("receipt.json", pointer)

    def test_render_is_free_of_machine_local_values(self):
        target = self.published(real())
        text = (target / "BRD.md").read_text(encoding="utf-8")
        self.assertNotIn(str(self.out), text)
        self.assertNotIn(str(self.temporary.name), text)
        self.assertNotIn("generated_at", text)
        self.assertNotIn("\r", text)

    def test_two_output_roots_are_byte_identical(self):
        other = Path(self.temporary.name) / "second"
        first = self.published(real())
        second = publish_brd.publish(real(), other)
        for name in ARTIFACTS + ["current.json"]:
            left = first / name if name in ARTIFACTS else self.out / name
            right = second / name if name in ARTIFACTS else other / name
            self.assertEqual(left.read_bytes(), right.read_bytes(), name)

    def test_repeat_publication_is_byte_identical_and_advances_pointer(self):
        target = self.published(real())
        before = {name: (target / name).read_bytes() for name in ARTIFACTS}
        pointer_before = self.pointer_bytes()
        again = self.published(real())
        self.assertEqual(again, target)
        for name in ARTIFACTS:
            self.assertEqual((target / name).read_bytes(), before[name], name)
        self.assertEqual(self.pointer_bytes(), pointer_before)

    def test_second_baseline_does_not_overwrite_the_first(self):
        first = self.published(real())
        before = {name: (first / name).read_bytes() for name in ARTIFACTS}
        second_manifest = renamed("draft-0.2")
        second = publish_brd.publish(second_manifest, self.out)
        self.assertNotEqual(first, second)
        for name in ARTIFACTS:
            self.assertEqual((first / name).read_bytes(), before[name], name)
        self.assertEqual(load_json(self.out / "current.json")["baseline"], "draft-0.2")


class AcceptedPublicationTests(PublisherCase):
    def test_accepted_manifest_writes_acceptance_and_accepted_banner(self):
        manifest, acceptance = accepted()
        target = self.published(manifest, acceptance)
        self.assertEqual(sorted(p.name for p in target.iterdir()), sorted(ARTIFACTS + ["acceptance.json"]))
        self.assertEqual((target / "acceptance.json").read_bytes(), requirements.canonical_bytes(acceptance))
        text = (target / "BRD.md").read_text(encoding="utf-8")
        self.assertIn(publish_brd.ACCEPTED_BANNER, text)
        self.assertNotIn(publish_brd.DRAFT_BANNER, text)
        self.assertIn(ACCEPTANCE_SECTION, text)
        self.assertIn("kittrial-pth.17", text)
        self.assertIn("Owner accepted the requirements-driven direction", text)
        receipt = load_json(target / "receipt.json")
        self.assertEqual(sorted(receipt["files"]), sorted(RECEIPTED + ["acceptance.json"]))
        self.assertEqual(
            receipt["files"]["acceptance.json"],
            publish_brd.sha256_bytes((target / "acceptance.json").read_bytes()),
        )
        self.assertEqual(receipt["state"], "accepted")

    def test_accepted_retry_is_identical(self):
        manifest, acceptance = accepted()
        target = self.published(manifest, acceptance)
        before = {p.name: p.read_bytes() for p in target.iterdir()}
        self.assertEqual(publish_brd.publish(manifest, self.out, acceptance), target)
        for name, data in before.items():
            self.assertEqual((target / name).read_bytes(), data, name)


class ValidationBeforeWriteTests(PublisherCase):
    def test_malformed_manifest_creates_nothing(self):
        manifest = real()
        manifest.pop("sha256")
        with self.assertRaises(requirements.ValidationError):
            self.published(manifest)
        self.assertFalse(self.out.exists())

    def test_unknown_field_creates_nothing(self):
        manifest = draft()
        manifest["extra"] = "not allowed"
        manifest = restamp(manifest)
        with self.assertRaises(requirements.ValidationError):
            self.published(manifest)
        self.assertFalse(self.out.exists())

    def test_traversal_baseline_is_rejected(self):
        for name in ("../escape", "a/b", "..", ".", "x\\y"):
            with self.assertRaises(ValueError):
                self.published(renamed(name))
            self.assert_nothing_published()

    def test_draft_with_acceptance_is_rejected(self):
        manifest, acceptance = accepted()
        manifest["state"] = "draft"
        for record in manifest["narrative"] + manifest["requirements"]:
            record["acceptance_state"] = "draft"
        manifest = restamp(manifest)
        with self.assertRaises(ValueError):
            self.published(manifest, acceptance)
        self.assert_nothing_published()

    def test_accepted_without_acceptance_is_rejected(self):
        manifest, _ = accepted()
        with self.assertRaises(ValueError):
            self.published(manifest)
        self.assert_nothing_published()

    def test_acceptance_hash_mismatch_is_rejected(self):
        manifest, acceptance = accepted()
        acceptance = dict(acceptance, manifest_sha256="a" * 64)
        with self.assertRaises(ValueError):
            self.published(manifest, acceptance)
        self.assert_nothing_published()

    def test_acceptance_policy_violations_are_rejected(self):
        manifest, acceptance = accepted()
        cases = [
            ("approver is not an owner", dict(acceptance, approvers=["someone-else"])),
            ("duplicate owner", dict(acceptance, owners=["james", "james"])),
            (
                "all-owners with unequal sets",
                dict(acceptance, policy="all-owners", owners=["james", "sam"], approvers=["james"]),
            ),
            ("unknown policy", dict(acceptance, policy="no-owner")),
            ("empty evidence", dict(acceptance, evidence="   ")),
        ]
        for label, case in cases:
            with self.assertRaises(ValueError, msg=label):
                self.published(manifest, case)
            self.assert_nothing_published()

    def test_duplicate_manifest_json_keys_are_rejected_at_the_boundary(self):
        path = Path(self.temporary.name) / "duplicate.json"
        path.write_bytes(b'{"baseline":"a","baseline":"b"}')
        with self.assertRaises(ValueError):
            load_json(path)

    def test_invalid_utf8_manifest_is_rejected_at_the_boundary(self):
        path = Path(self.temporary.name) / "bad.json"
        path.write_bytes(b'{"baseline":"\xff\xfe"}')
        with self.assertRaises(ValueError):
            load_json(path)

    def test_output_path_that_is_a_file_raises_oserror(self):
        path = Path(self.temporary.name) / "file"
        path.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(OSError):
            publish_brd.publish(real(), path)


class ReservedNameTests(PublisherCase):
    def test_current_json_baseline_is_rejected(self):
        for name in ("current.json", "CURRENT.JSON", "Current.Json"):
            with self.assertRaises(ValueError):
                self.published(renamed(name))
            self.assert_nothing_published()

    def test_windows_device_names_are_rejected(self):
        for name in ("CON", "con", "nul", "PRN", "aux", "COM1", "lpt9", "con.md", "NUL.json"):
            with self.assertRaises(ValueError):
                self.published(renamed(name))
            self.assert_nothing_published()

    def test_trailing_dot_baseline_is_rejected(self):
        with self.assertRaises(ValueError):
            self.published(renamed("draft-0.1."))
        self.assert_nothing_published()
        with self.assertRaises(ValueError):
            self.published(renamed("draft-0.1 "))
        self.assert_nothing_published()

    def test_reserved_names_do_not_create_the_output_root(self):
        with self.assertRaises(ValueError):
            self.published(renamed("current.json"))
        self.assertFalse(self.out.exists())

    def test_legitimate_names_are_still_accepted(self):
        for name in ("draft-0.1", "draft_0.2", "R1", "a" * 80):
            target = publish_brd.publish(renamed(name), self.out)
            self.assertEqual(target.name, name)


class RecoveryTests(PublisherCase):
    def test_interrupted_pointer_replacement_is_recoverable(self):
        manifest = real()
        with mock.patch.object(
            publish_brd, "_atomic_replace", side_effect=OSError("interrupted before pointer replacement")
        ):
            with self.assertRaises(OSError):
                self.published(manifest)
        target = self.out / NAME
        self.assertEqual(sorted(p.name for p in target.iterdir()), ARTIFACTS)
        self.assertFalse((self.out / "current.json").exists())
        self.assertEqual(self.staging_leftovers(), [])
        self.assertEqual(self.published(manifest), target)
        pointer = load_json(self.out / "current.json")
        self.assertEqual(pointer["baseline"], NAME)
        self.assertEqual(
            pointer["receipt_sha256"],
            publish_brd.sha256_bytes((target / "receipt.json").read_bytes()),
        )

    def test_completed_directory_without_pointer_is_repaired_on_retry(self):
        target = self.published(real())
        before = {name: (target / name).read_bytes() for name in ARTIFACTS}
        (self.out / "current.json").unlink()
        self.assertEqual(self.published(real()), target)
        for name in ARTIFACTS:
            self.assertEqual((target / name).read_bytes(), before[name], name)
        self.assertTrue((self.out / "current.json").is_file())

    def test_interrupted_staging_is_never_current(self):
        def half_written(directory, files):
            publish_brd._write_bytes(directory / "manifest.json", b"{}")
            raise OSError("interrupted during staging")

        with mock.patch.object(publish_brd, "_write_artifacts", side_effect=half_written):
            with self.assertRaises(OSError):
                self.published(real())
        self.assertFalse((self.out / NAME).exists())
        self.assertFalse((self.out / "current.json").exists())
        self.assertEqual(self.staging_leftovers(), [])
        self.assertEqual(self.published(real()), self.out / NAME)

    def test_failed_write_leaves_no_publication(self):
        with mock.patch.object(
            publish_brd, "_write_artifacts", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.published(real())
        self.assert_nothing_published()
        self.assertEqual(self.staging_leftovers(), [])

    def test_stale_staging_directory_is_ignored(self):
        self.out.mkdir(parents=True)
        stale = self.out / ".staging-draft-0.1-deadbeef"
        stale.mkdir()
        (stale / "manifest.json").write_bytes(b"{}")
        target = self.published(real())
        self.assertEqual(sorted(p.name for p in target.iterdir()), ARTIFACTS)
        self.assertEqual(load_json(self.out / "current.json")["baseline"], NAME)

    def test_previous_pointer_survives_a_failed_later_publication(self):
        self.published(real())
        pointer_before = self.pointer_bytes()
        later = renamed("draft-0.2")
        with mock.patch.object(
            publish_brd, "_atomic_replace", side_effect=OSError("interrupted before pointer replacement")
        ):
            with self.assertRaises(OSError):
                publish_brd.publish(later, self.out)
        self.assertEqual(self.pointer_bytes(), pointer_before)
        self.assertEqual(load_json(self.out / "current.json")["baseline"], NAME)
        self.assertEqual(
            sorted(p.name for p in (self.out / "draft-0.2").iterdir()), ARTIFACTS
        )
        publish_brd.publish(later, self.out)
        self.assertEqual(load_json(self.out / "current.json")["baseline"], "draft-0.2")

    def test_conflicting_retry_fails_without_changing_current(self):
        target = self.published(real())
        brd_before = (target / "BRD.md").read_bytes()
        pointer_before = self.pointer_bytes()
        with self.assertRaises(ValueError):
            self.published(amended())
        self.assertEqual(self.pointer_bytes(), pointer_before)
        self.assertEqual((target / "BRD.md").read_bytes(), brd_before)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ARTIFACTS)

    def test_tampered_artifact_is_detected_on_retry(self):
        target = self.published(real())
        pointer_before = self.pointer_bytes()
        (target / "BRD.md").write_bytes((target / "BRD.md").read_bytes() + b"tampered\n")
        with self.assertRaises(ValueError):
            self.published(real())
        self.assertEqual(self.pointer_bytes(), pointer_before)

    def test_tampered_receipt_digest_is_detected_on_retry(self):
        target = self.published(real())
        receipt = load_json(target / "receipt.json")
        receipt["files"]["BRD.md"] = "a" * 64
        (target / "receipt.json").write_bytes(requirements.canonical_bytes(receipt))
        with self.assertRaises(ValueError):
            self.published(real())

    def test_receipt_that_hashes_itself_is_detected(self):
        target = self.published(real())
        receipt = load_json(target / "receipt.json")
        receipt["files"]["receipt.json"] = "a" * 64
        (target / "receipt.json").write_bytes(requirements.canonical_bytes(receipt))
        with self.assertRaises(ValueError):
            self.published(real())

    def test_duplicate_keys_in_persisted_receipt_are_detected(self):
        target = self.published(real())
        (target / "receipt.json").write_bytes(b'{"state":"draft","state":"accepted"}')
        with self.assertRaises(ValueError):
            self.published(real())

    def test_extra_entry_in_publication_is_detected(self):
        target = self.published(real())
        (target / "extra.txt").write_text("unexpected", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.published(real())

    def test_missing_artifact_in_publication_is_detected(self):
        target = self.published(real())
        (target / "BRD.md").unlink()
        with self.assertRaises(ValueError):
            self.published(real())


class SymlinkTests(PublisherCase):
    def test_symlinked_output_directory_is_rejected(self):
        store = Path(self.temporary.name) / "store"
        store.mkdir()
        link = Path(self.temporary.name) / "output-link"
        symlink_or_skip(self, store, link, True)
        with self.assertRaises(ValueError):
            publish_brd.publish(real(), link)
        self.assertEqual(sorted(p.name for p in store.iterdir()), [])

    def test_symlinked_publication_directory_is_rejected(self):
        self.out.mkdir(parents=True)
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir()
        symlink_or_skip(self, elsewhere, self.out / NAME, True)
        with self.assertRaises(ValueError):
            self.published(real())
        self.assertFalse((self.out / "current.json").exists())

    def test_symlinked_current_pointer_is_rejected(self):
        self.published(real())
        elsewhere = Path(self.temporary.name) / "pointer-target.json"
        elsewhere.write_bytes(b"{}")
        pointer = self.out / "current.json"
        pointer.unlink()
        symlink_or_skip(self, elsewhere, pointer, False)
        with self.assertRaises(ValueError):
            self.published(real())

    def test_symlinked_staging_name_is_rejected(self):
        self.out.mkdir(parents=True)
        staging = self.out / (".staging-%s-%s" % (NAME, "0" * 16))
        elsewhere = Path(self.temporary.name) / "staging-target"
        elsewhere.mkdir()
        symlink_or_skip(self, elsewhere, staging, True)
        with mock.patch.object(publish_brd.secrets, "token_hex", return_value="0" * 16):
            with self.assertRaises(ValueError):
                self.published(real())

    def fake_link(self, *flagged):
        flagged = {str(Path(path)) for path in flagged}
        return lambda path: str(Path(path)) in flagged

    def test_output_directory_rejection_is_wired_without_host_symlink_support(self):
        self.out.mkdir(parents=True)
        with mock.patch.object(publish_brd, "_is_link", self.fake_link(self.out)):
            with self.assertRaises(ValueError):
                self.published(real())
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [])

    def test_publication_directory_rejection_is_wired_without_host_symlink_support(self):
        self.out.mkdir(parents=True)
        target = self.out / NAME
        with mock.patch.object(publish_brd, "_is_link", self.fake_link(target)):
            with self.assertRaises(ValueError):
                self.published(real())
        self.assertFalse(target.exists())
        self.assertFalse((self.out / "current.json").exists())

    def test_current_pointer_rejection_is_wired_without_host_symlink_support(self):
        self.published(real())
        pointer = self.out / "current.json"
        before = pointer.read_bytes()
        with mock.patch.object(publish_brd, "_is_link", self.fake_link(pointer)):
            with self.assertRaises(ValueError):
                self.published(real())
        self.assertEqual(pointer.read_bytes(), before)


class ConcurrencyTests(PublisherCase):
    def run_writers(self, manifests):
        count = len(manifests)
        barrier = threading.Barrier(count)
        results = {}

        def worker(index):
            manifest = copy.deepcopy(manifests[index])
            barrier.wait()
            try:
                results[index] = publish_brd.publish(manifest, self.out)
            except BaseException as error:  # noqa: BLE001 - recorded for assertions
                results[index] = error

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def test_identical_writers_agree_or_fail_but_never_corrupt(self):
        manifest = real()
        target = self.out / NAME
        results = self.run_writers([manifest] * 8)
        for index, result in results.items():
            self.assertIsInstance(result, Path, "writer %d: %r" % (index, result))
            self.assertEqual(result, target)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ARTIFACTS)
        self.assertEqual(self.staging_leftovers(), [])
        receipt = load_json(target / "receipt.json")
        for name, digest in receipt["files"].items():
            self.assertEqual(publish_brd.sha256_bytes((target / name).read_bytes()), digest)
        pointer = load_json(self.out / "current.json")
        self.assertEqual(pointer["baseline"], NAME)
        self.assertEqual(
            pointer["receipt_sha256"],
            publish_brd.sha256_bytes((target / "receipt.json").read_bytes()),
        )

    def test_conflicting_writers_produce_one_winner_and_no_mixed_state(self):
        first, second = real(), amended()
        target = self.out / NAME
        results = self.run_writers([first, second])
        winners = [r for r in results.values() if isinstance(r, Path)]
        losers = [r for r in results.values() if isinstance(r, BaseException)]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertIsInstance(losers[0], ValueError)
        self.assertEqual(winners[0], target)
        text = (target / "BRD.md").read_text(encoding="utf-8")
        amended_marker = "Amended for a conflicting retry."
        self.assertLessEqual(text.count(amended_marker), 1)
        published_amended = amended_marker in text
        expected = publish_brd._build_files(second if published_amended else first, None)
        for name in ARTIFACTS:
            self.assertEqual((target / name).read_bytes(), expected[name], name)
        receipt = load_json(target / "receipt.json")
        self.assertEqual(
            receipt["manifest_sha256"],
            second["sha256"] if published_amended else first["sha256"],
        )
        pointer = load_json(self.out / "current.json")
        self.assertEqual(
            pointer["receipt_sha256"],
            publish_brd.sha256_bytes((target / "receipt.json").read_bytes()),
        )
        self.assertEqual(
            pointer["manifest_sha256"], receipt["manifest_sha256"]
        )
        self.assertEqual(self.staging_leftovers(), [])


class CliTests(PublisherCase):
    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "publish_brd.py")] + list(arguments),
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(self.temporary.name),
        )

    def test_cli_publishes_and_exits_zero(self):
        result = self.run_cli(str(BASELINE), "--output", str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("published", result.stdout)
        self.assertEqual(sorted(p.name for p in (self.out / NAME).iterdir()), ARTIFACTS)

    def test_cli_accepts_acceptance_file(self):
        manifest, acceptance = accepted()
        manifest_path = Path(self.temporary.name) / "manifest.json"
        acceptance_path = Path(self.temporary.name) / "acceptance.json"
        manifest_path.write_bytes(requirements.canonical_bytes(manifest))
        acceptance_path.write_bytes(requirements.canonical_bytes(acceptance))
        result = self.run_cli(str(manifest_path), "--output", str(self.out), "--acceptance", str(acceptance_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.out / NAME / "acceptance.json").is_file())

    def test_cli_reports_malformed_input_without_traceback(self):
        manifest = real()
        manifest.pop("sha256")
        path = Path(self.temporary.name) / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        result = self.run_cli(str(path), "--output", str(self.out))
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("publication failed", result.stderr)
        self.assertFalse(self.out.exists())

    def test_cli_reports_conflicting_retry_without_traceback(self):
        target = self.published(real())
        pointer_before = self.pointer_bytes()
        (target / "BRD.md").write_bytes(b"tampered\n")
        result = self.run_cli(str(BASELINE), "--output", str(self.out))
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("publication failed", result.stderr)
        self.assertEqual(self.pointer_bytes(), pointer_before)

    def test_cli_requires_manifest_and_output(self):
        result = self.run_cli(str(BASELINE))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_rejects_reserved_baseline_without_traceback(self):
        manifest = renamed("current.json")
        path = Path(self.temporary.name) / "manifest.json"
        path.write_bytes(requirements.canonical_bytes(manifest))
        result = self.run_cli(str(path), "--output", str(self.out))
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.out / "current.json").exists())


class ModuleContractTests(PublisherCase):
    def test_contract_constants_are_exposed(self):
        self.assertEqual(publish_brd.CURRENT_NAME, "current.json")
        self.assertEqual(publish_brd.BANNER_BY_STATE["draft"], publish_brd.DRAFT_BANNER)
        self.assertEqual(publish_brd.BANNER_BY_STATE["accepted"], publish_brd.ACCEPTED_BANNER)
        self.assertIn("current.json", publish_brd.RESERVED_BASELINES)

    def test_repository_reference_manifest_still_validates(self):
        self.assertIsNone(requirements.validate_manifest(real()))
        self.assertEqual(requirements.content_hash(real()), real()["sha256"])

    def test_publish_does_not_modify_the_repository(self):
        before = BASELINE.read_bytes()
        self.published(real())
        self.assertEqual(BASELINE.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in ROOT.glob("draft-0.1")), [])


if __name__ == "__main__":
    unittest.main()
