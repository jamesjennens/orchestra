"""Coordinator regression cases for ambiguous or unencodable JSON input."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from requirements import ValidationError, canonical_bytes, load_json


class InputTests(unittest.TestCase):
    def test_unpaired_surrogate_is_validation_error(self):
        with self.assertRaises(ValidationError):
            canonical_bytes({'text': chr(0xd800)})

    def test_duplicate_fields_and_invalid_unicode_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input.json'
            for payload in ('{"state":"draft","state":"accepted"}',
                            '{"nested":{"id":"one","id":"two"}}',
                            '{"text":"\\ud800"}', '{"n":NaN}'):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding='utf-8')
                    with self.assertRaises(ValidationError):
                        load_json(path)
