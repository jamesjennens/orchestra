import os
import subprocess
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

    def test_git_probe_hides_windows_process_and_handles_failure_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("version.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch("version.subprocess.run", return_value=type("Result", (), {"returncode": 0, "stdout": "abc\n"})()) as run:
            self.assertEqual(version._source_commit(directory), "abc")
            self.assertEqual(run.call_args.args[0], ["git", "-C", str(Path(directory)), "rev-parse", "--verify", "HEAD"])
            self.assertEqual(run.call_args.kwargs["timeout"], 5)
            self.assertTrue(run.call_args.kwargs["capture_output"])
            self.assertEqual(version.report(directory)["source_commit"], "abc")
            self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)
        with tempfile.TemporaryDirectory() as directory, \
                patch("version.subprocess.run", return_value=type("Result", (), {"returncode": 7, "stdout": "ignored\n"})()):
            self.assertEqual(version._source_commit(directory), "unknown")
        with tempfile.TemporaryDirectory() as directory, \
                patch("version.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
            self.assertEqual(version.report(directory)["source_commit"], "unknown")

    def test_report_line_is_stable_and_human_readable(self):
        self.assertEqual(
            version.line({"component": "client", "version": "0.1.0",
                          "source_commit": "abc", "path": "ignored"}),
            "Orchestra client: version 0.1.0, source abc",
        )
