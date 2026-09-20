import os
import hashlib
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import version


class VersionTests(unittest.TestCase):
    def write_manifest(self, root, source_commit="unknown", build_id="build-1"):
        files = {}
        for name in ("VERSION", "version.py"):
            files[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        (root / "provenance.json").write_text(json.dumps({
            "schema_version": 1, "component": "orchestra-kit",
            "version": "9.8.7", "source_commit": source_commit,
            "build_id": build_id, "files": files,
        }), encoding="utf-8")

    def test_report_uses_packaged_version_outside_git_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            (root / "version.py").write_text("# packaged\n", encoding="utf-8")
            self.write_manifest(root)
            result = version.report(root)
        self.assertEqual(result["version"], "9.8.7")
        self.assertEqual(result["source_commit"], "unknown")

    def test_packaged_manifest_source_commit_is_reported_without_git_or_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            (root / "version.py").write_text("# packaged\n", encoding="utf-8")
            self.write_manifest(root, source_commit="a" * 40)
            with patch.dict(os.environ, {"ORCHESTRA_SOURCE_COMMIT": "stale"}, clear=False):
                result = version.report(root)
        self.assertEqual(result["source_commit"], "a" * 40)
        self.assertEqual(result["build_id"], "build-1")

    def test_invalid_manifest_never_uses_unrelated_git_or_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
            (root / "version.py").write_text("# packaged\n", encoding="utf-8")
            (root / "provenance.json").write_text(json.dumps({
                "schema_version": 1, "component": "wrong", "version": "0.1.0",
                "source_commit": "bad", "build_id": "x", "files": {},
            }), encoding="utf-8")
            with patch.dict(os.environ, {"ORCHESTRA_SOURCE_COMMIT": "stale"}, clear=False):
                result = version.report(root)
        self.assertEqual(result["source_commit"], "unknown")
        self.assertEqual(result["build_id"], "unknown")

    def test_changed_packaged_file_invalidates_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            (root / "version.py").write_text("# packaged\n", encoding="utf-8")
            self.write_manifest(root, source_commit="a" * 40)
            (root / "version.py").write_text("# changed archive\n", encoding="utf-8")
            result = version.report(root)
        self.assertEqual(result["source_commit"], "unknown")
        self.assertEqual(result["build_id"], "unknown")

    def test_malformed_non_full_source_commit_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            (root / "version.py").write_text("# packaged\n", encoding="utf-8")
            self.write_manifest(root, source_commit="abc123")
            result = version.report(root)
        self.assertEqual(result["source_commit"], "unknown")

    def test_exact_git_archive_keeps_unreleased_source_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "kit.tar"
            extracted = Path(directory) / "extract"
            subprocess.run(
                ["git", "archive", "--format=tar", "HEAD", "-o", str(archive)],
                cwd=Path(__file__).resolve().parents[1], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            extracted.mkdir()
            with tarfile.open(archive) as bundle:
                bundle.extractall(extracted)
            root = next(extracted.iterdir())
            result = version.report(root)
        self.assertEqual(result["source_commit"], "unknown")
        self.assertEqual(result["build_id"], "unreleased-source")

    def test_report_line_is_stable_and_human_readable(self):
        self.assertEqual(
            version.line({"component": "client", "version": "0.1.0",
                          "source_commit": "abc", "path": "ignored"}),
            "Orchestra client: version 0.1.0, source abc",
        )
