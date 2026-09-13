import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from requirements import content_hash
import publish_brd


class PublisherRegressionTests(unittest.TestCase):
    def snapshot(self):
        root = Path(publish_brd.__file__).parent
        return json.loads((root/'docs/requirements-baseline.json').read_text(encoding='utf-8'))

    def test_normalizes_markdown_newlines_but_preserves_manifest(self):
        m = self.snapshot()
        m['narrative'][0]['description'] = 'First\r\nSecond\rThird\n'
        m['narrative'][0]['sha256'] = content_hash(m['narrative'][0])
        m['sha256'] = content_hash(m)
        with tempfile.TemporaryDirectory() as tmp:
            result = publish_brd.publish(m, Path(tmp)/'out')
            self.assertFalse(b'\r' in (result/'BRD.md').read_bytes())
            self.assertEqual(json.loads((result/'manifest.json').read_bytes()), m)

    def test_fsync_failure_never_advances_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'out'
            with patch.object(publish_brd.os, 'fsync', side_effect=OSError('sync failed')):
                with self.assertRaises(OSError):
                    publish_brd.publish(self.snapshot(), out)
            self.assertFalse((out/'current.json').exists())

    def test_verifies_staged_bytes_before_current(self):
        write = publish_brd._write_artifacts
        def corrupt(directory, files):
            write(directory, files)
            (directory/'BRD.md').write_bytes(b'corrupt staging')
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out'
            with patch.object(publish_brd, '_write_artifacts', side_effect=corrupt):
                with self.assertRaises(ValueError):
                    publish_brd.publish(self.snapshot(), out)
            self.assertFalse((out/'current.json').exists())

    def test_rejects_symlink_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); target=root/'real';target.mkdir()
            link=root/'linked'
            try:link.symlink_to(target, target_is_directory=True)
            except OSError as exc:self.skipTest('symlink privilege unavailable: '+str(exc))
            with self.assertRaises(ValueError):
                publish_brd.publish(self.snapshot(), link/'nested'/'out')
            self.assertFalse((target/'nested').exists())

    def test_reparse_point_detected_on_python_without_isjunction(self):
        info = SimpleNamespace(st_file_attributes=0x400)
        with patch.object(Path, 'is_symlink', return_value=False), patch.object(Path, 'lstat', return_value=info), patch.object(Path, 'is_dir', return_value=True), patch.object(publish_brd.os.path, 'isjunction', None, create=True):
            self.assertTrue(publish_brd._is_link(Path('junction')))
