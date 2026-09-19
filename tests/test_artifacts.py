import json
import tempfile
import threading
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
