import errno
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

import artifacts
from artifacts import ArtifactError, ArtifactStore


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = ArtifactStore(self.root)

    def tearDown(self):
        self.tempdir.cleanup()

    def _write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_dataset_registration_is_idempotent_and_verifiable(self):
        path = self._write("datasets/sample/prices.csv", b"date,value\n2026-01-01,10\n")
        first = self.store.register(
            "datasets/sample/prices.csv",
            kind="dataset",
            source="fixture",
            dataset="sample",
            as_of="2026-01-01",
            fields=["date", "value"],
            actor="worker",
            task="kittrial-5bb.14",
        )
        second = self.store.register(
            "datasets/sample/prices.csv",
            kind="dataset",
            source="fixture",
            dataset="sample",
            as_of="2026-01-01",
            fields=["date", "value"],
            actor="worker",
            task="kittrial-5bb.14",
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(
            self.store.verify("datasets/sample/prices.csv")[0]["bytes"],
            path.stat().st_size,
        )

    def test_changed_bytes_and_conflicting_duplicate_are_rejected(self):
        self._write("datasets/sample/data.bin", b"one")
        self.store.register("datasets/sample/data.bin", kind="dataset")
        self._write("datasets/sample/data.bin", b"two")
        with self.assertRaisesRegex(ArtifactError, "bytes changed"):
            self.store.register("datasets/sample/data.bin", kind="dataset")
        self._write("datasets/sample/data.bin", b"one")
        with self.assertRaisesRegex(ArtifactError, "conflicting duplicate"):
            self.store.register("datasets/sample/data.bin", kind="dataset", source="different")

    def test_results_concurrent_writers_do_not_lose_entries(self):
        errors = []
        for index in range(12):
            self._write(f"results/long-test/output-{index}.json", json.dumps({"case": index}).encode())

        def register(index):
            try:
                self.store.register(
                    f"results/long-test/output-{index}.json",
                    kind="test-output",
                    task="long-test",
                    metadata={"attempt": index},
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=register, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.entries()), 12)

    def test_traversal_and_absolute_paths_are_rejected(self):
        self._write("datasets/sample/data.bin", b"x")
        for path in ("../outside.bin", "/tmp/outside.bin", "datasets/../outside.bin", r"datasets\..\outside.bin"):
            with self.assertRaises(ArtifactError):
                self.store.register(path, kind="dataset")
        with self.assertRaises(ArtifactError):
            self.store.path_for("results", "..", "output.json")

    def test_checksum_tampering_is_detected(self):
        self._write("results/task/output.txt", b"stable")
        self.store.register("results/task/output.txt", kind="test-output", task="task")
        (self.root / "results/task/output.txt").write_bytes(b"tampered")
        with self.assertRaisesRegex(ArtifactError, "checksum or size mismatch"):
            self.store.entries()

    def test_optional_domain_metadata_is_opaque_json(self):
        self._write("results/task/report.json", b"{}")
        result = self.store.register(
            "results/task/report.json",
            kind="result",
            metadata={"strategy": "synthetic", "parameters": {"window": 20}},
            securities=["ABC"],
        )
        self.assertEqual(result["record"]["metadata"]["parameters"]["window"], 20)

    def test_symlink_escape_is_rejected_when_supported(self):
        outside = self.root.parent / "outside-artifact.txt"
        outside.write_bytes(b"private")
        link = self.root / "datasets/sample/link.txt"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not available")
        with self.assertRaises(ArtifactError):
            self.store.register("datasets/sample/link.txt", kind="dataset")

    def test_manifest_corruption_and_duplicate_are_rejected_without_rewrite(self):
        self._write("datasets/sample/data.bin", b"one")
        self.store.register("datasets/sample/data.bin", kind="dataset")
        original = self.store.manifest_path.read_bytes()
        with self.store.manifest_path.open("ab") as handle:
            handle.write(original)
        with self.assertRaisesRegex(ArtifactError, "duplicate path"):
            self.store.entries(verify=False)
        self.assertEqual(self.store.manifest_path.read_bytes(), original + original)

        self.store.manifest_path.write_text(json.dumps({"path": "bad"}) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ArtifactError, "invalid fields"):
            self.store.entries(verify=False)

    def test_manifest_checksum_and_size_types_are_validated_without_verify(self):
        self._write("results/task/output.txt", b"stable")
        self.store.register("results/task/output.txt", kind="test-output", task="task")
        row = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        row["bytes"] = True
        original = json.dumps(row, sort_keys=True) + "\n"
        self.store.manifest_path.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(ArtifactError, "byte count"):
            self.store.entries(verify=False)
        self.assertEqual(self.store.manifest_path.read_text(encoding="utf-8"), original)

    def test_manifest_and_lock_paths_are_reserved_for_default_and_custom_names(self):
        for store, name in ((self.store, "manifest.jsonl"), (ArtifactStore(self.root / "custom", "catalog.jsonl"), "catalog.jsonl")):
            before = store.manifest_path.read_bytes() if store.manifest_path.exists() else b""
            with self.assertRaisesRegex(ArtifactError, "reserved"):
                store.register(name, kind="result")
            self.assertEqual(store.manifest_path.read_bytes() if store.manifest_path.exists() else b"", before)
            with self.assertRaisesRegex(ArtifactError, "reserved"):
                store.path_for(".", name)

    def test_lock_file_size_is_stable_and_sidecar_symlinks_are_rejected(self):
        with artifacts._exclusive_lock(self.store.lock_path):
            first_size = self.store.lock_path.stat().st_size
        with artifacts._exclusive_lock(self.store.lock_path):
            second_size = self.store.lock_path.stat().st_size
        self.assertEqual(first_size, 1)
        self.assertEqual(second_size, first_size)

        symlink_root = self.root / "symlink-lock"
        symlink_root.mkdir()
        target = symlink_root / "target"
        target.write_bytes(b"x")
        link = symlink_root / ".manifest.jsonl.lock"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not available")
        with self.assertRaisesRegex(ArtifactError, "symlink sidecar"):
            ArtifactStore(symlink_root)

    def test_noncanonical_manifest_rows_fail_without_rewrite(self):
        self._write("results/task/data.txt", b"one")
        self.store.register("results/task/data.txt", kind="result")
        row = self.store.entries()[0]
        for alias in ("./results/task/data.txt", "results\\task\\data.txt"):
            row["path"] = alias
            payload = (json.dumps(row) + "\n") * 2
            self.store.manifest_path.write_text(payload, encoding="utf-8")
            with self.assertRaisesRegex(ArtifactError, "canonical"):
                self.store.entries(verify=False)
            with self.assertRaisesRegex(ArtifactError, "canonical"):
                self.store.register("results/task/data.txt", kind="result")
            self.assertEqual(self.store.manifest_path.read_text(encoding="utf-8"), payload)

    def test_windows_alias_components_cannot_poison_manifest(self):
        self._write("data.txt", b"one")
        self.store.register("data.txt", kind="result")
        before = self.store.manifest_path.read_bytes()
        for alias in ("manifest.jsonl.", "data.txt.", "CON", "NUL.txt", "results/COM1/data.txt"):
            with self.assertRaisesRegex(ArtifactError, "Windows path alias"):
                self.store.register(alias, kind="result")
        with self.assertRaisesRegex(ArtifactError, "Windows path alias"):
            ArtifactStore(self.root, "manifest.jsonl.")
        self.assertEqual(self.store.manifest_path.read_bytes(), before)
        self.assertEqual(len(self.store.entries()), 1)

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive filesystem")
    def test_windows_case_aliases_have_one_identity(self):
        self._write("Data.txt", b"one")
        self.store.register("Data.txt", kind="result")
        before = self.store.manifest_path.read_bytes()
        with self.assertRaisesRegex(ArtifactError, "conflicting duplicate"):
            self.store.register("data.txt", kind="result")
        self.assertEqual(self.store.manifest_path.read_bytes(), before)
        self.assertEqual(len(self.store.verify("DATA.TXT")), 1)
        row = self.store.entries()[0]
        row["path"] = "data.txt"
        with self.store.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        with self.assertRaisesRegex(ArtifactError, "duplicate path"):
            self.store.entries(verify=False)

    def test_dangling_manifest_inserted_after_init_is_rejected(self):
        target = self.root / "missing-target"
        try:
            self.store.manifest_path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not available")
        with self.assertRaisesRegex(ArtifactError, "symlink"):
            self.store.entries(verify=False)
        self._write("data.txt", b"one")
        with self.assertRaisesRegex(ArtifactError, "symlink"):
            self.store.register("data.txt", kind="result")
        self.assertTrue(self.store.manifest_path.is_symlink())
        self.assertFalse(target.exists())

    def test_windows_lock_permanent_failure_is_not_retried(self):
        class Permanent:
            LK_NBLCK = 1
            def __init__(self):
                self.calls = 0
            def locking(self, *args):
                self.calls += 1
                raise OSError(errno.EBADF, "bad handle")

        adapter = Permanent()
        with self.assertRaisesRegex(ArtifactError, "cannot lock"):
            with self.store.lock_path.open("a+b") as handle:
                artifacts._windows_lock(handle, self.store.lock_path, adapter=adapter, timeout=0.1, poll=0)
        self.assertEqual(adapter.calls, 1)

    def test_windows_lock_contention_is_bounded_and_success_releases(self):
        class Contended:
            LK_NBLCK = 1
            def locking(self, *args):
                raise OSError(errno.EACCES, "busy")

        with self.assertRaisesRegex(ArtifactError, "timed out"):
            with self.store.lock_path.open("a+b") as handle:
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                artifacts._windows_lock(handle, self.store.lock_path, adapter=Contended(), timeout=0.03, poll=0.001)

        class Successful:
            LK_NBLCK = 1
            def __init__(self):
                self.calls = 0
            def locking(self, *args):
                self.calls += 1

        adapter = Successful()
        with self.store.lock_path.open("a+b") as handle:
            artifacts._windows_lock(handle, self.store.lock_path, adapter=adapter, timeout=0.1, poll=0)
        self.assertEqual(adapter.calls, 1)


if __name__ == "__main__":
    unittest.main()
