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
            with patch.dict(os.environ, {"ORCHESTRA_SOURCE_COMMIT": ""}, clear=False), \
                    patch("version.subprocess.run", side_effect=OSError("git unavailable")):
                result = version.report(root)
        self.assertEqual(result["version"], "9.8.7")
        self.assertEqual(result["source_commit"], "unknown")

    def test_configured_source_commit_is_reported_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"ORCHESTRA_SOURCE_COMMIT": "abc123"}, clear=False):
                result = version.report(directory)
        self.assertEqual(result["source_commit"], "abc123")

    def test_report_line_is_stable_and_human_readable(self):
        self.assertEqual(
            version.line({"component": "client", "version": "0.1.0",
                          "source_commit": "abc", "path": "ignored"}),
            "Orchestra client: version 0.1.0, source abc",
        )
