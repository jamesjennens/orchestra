import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import version


class VersionTests(unittest.TestCase):
    def test_report_uses_packaged_version_outside_git_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            result = version.report(root)
        self.assertEqual(result["version"], "9.8.7")
        self.assertEqual(result["source_commit"], "unknown")

    def test_packaged_manifest_source_commit_is_reported_without_git_or_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            (root / "provenance.json").write_text('{"schema_version":1,"component":"orchestra-kit","version":"9.8.7","source_commit":"abc123","build_id":"build-1"}', encoding="utf-8")
            with patch.dict(os.environ, {"ORCHESTRA_SOURCE_COMMIT": "stale"}, clear=False):
                result = version.report(root)
        self.assertEqual(result["source_commit"], "abc123")
        self.assertEqual(result["build_id"], "build-1")

    def test_invalid_manifest_never_uses_unrelated_git_or_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
            (root / "provenance.json").write_text('{"schema_version":1,"component":"wrong","version":"0.1.0","source_commit":"bad","build_id":"x"}', encoding="utf-8")
            with patch.dict(os.environ, {"ORCHESTRA_SOURCE_COMMIT": "stale"}, clear=False):
                result = version.report(root)
        self.assertEqual(result["source_commit"], "unknown")
        self.assertEqual(result["build_id"], "unknown")

    def test_report_line_is_stable_and_human_readable(self):
        self.assertEqual(
            version.line({"component": "client", "version": "0.1.0",
                          "source_commit": "abc", "path": "ignored"}),
            "Orchestra client: version 0.1.0, source abc",
        )
