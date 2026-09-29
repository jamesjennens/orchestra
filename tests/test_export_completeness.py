"""Renderer-scoped export-agreement checks for kittrial-5bb.4.

These are RENDERER-LEVEL tests only: they build one canonical in-memory
snapshot fixture and check that render() output, re-parsed JSONL, a local
file copy and the offline activity feed agree on issue/comment identity
sets. They do NOT exercise native `bd export`, the shared endpoint, its
locks, or client transport. Native completeness is covered separately in
reports/NATIVE_EXPORT_COMPLETENESS.md. Generic fixtures only; no private
exports, no live mutations.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from activity import build_entries, coverage_of, load_export
from render import render


def make_snapshot(n_issues=25, comments_each=12):
    rows = []
    for i in range(n_issues):
        issue = 'fix-%03d' % i
        comments = [
            {'id': 'c%04d' % j, 'created_at': '2026-09-%02dT10:%02d:00Z' % (10 + (j % 9), j % 60),
             'author': 'worker-%d' % (j % 3), 'text': 'note %d on %s' % (j, issue)}
            for j in range(comments_each)
        ]
        rows.append({'id': issue, 'title': 'Task %d' % i, 'status': 'open',
                     'issue_type': 'task', 'comments': comments})
    return rows


def identity_sets(rows):
    issues = {r['id'] for r in rows}
    comments = {(r['id'], str(c['id'])) for r in rows for c in (r.get('comments') or [])}
    return issues, comments


class RendererExportAgreementTests(unittest.TestCase):
    def test_large_render_comments_roundtrip(self):
        rows = make_snapshot()
        issues, comments = identity_sets(rows)
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / 'views'
            result = render(rows, dest)
            # Renderer output vs its own re-parsed JSONL agree without truncation.
            self.assertEqual(result['issues'], len(issues))
            self.assertEqual(result['comments'], len(comments))
            raw = (dest / 'issues.jsonl').read_text(encoding='utf-8')
            raw_bytes = (dest / 'issues.jsonl').read_bytes()
            digest = hashlib.sha256(raw_bytes).hexdigest()
            reread = [json.loads(line) for line in raw.splitlines() if line.strip()]
            self.assertEqual(identity_sets(reread), (issues, comments))
            # Local file copy is byte-identical (renderer result, not a native read).
            client_file = Path(d) / 'client-copy.jsonl'
            client_file.write_text(raw, encoding='utf-8')
            self.assertEqual(hashlib.sha256(client_file.read_bytes()).hexdigest(), digest)
            # Same-snapshot feed entries match the comment identity set.
            entries = build_entries(load_export(dest / 'issues.jsonl'))
            self.assertEqual({(e['issue_id'], e['entry_id'].split('-c', 1)[1]) for e in entries}, comments)
            cov = coverage_of(load_export(dest / 'issues.jsonl'))
            self.assertEqual(cov['comment_entries'], len(comments))
            self.assertFalse(cov['complete'])

    def test_sequential_double_render_agreement(self):
        # Two sequential in-process renders of one snapshot (NOT concurrent
        # endpoint refreshes): identical issue/comment totals and identity sets.
        rows = make_snapshot(n_issues=10, comments_each=5)
        with tempfile.TemporaryDirectory() as d:
            first = render(rows, Path(d) / 'v1')
            second = render(rows, Path(d) / 'v2')
            self.assertEqual((first['issues'], first['comments']),
                             (second['issues'], second['comments']))
            a = (Path(d) / 'v1/issues.jsonl').read_bytes()
            b = (Path(d) / 'v2/issues.jsonl').read_bytes()
            self.assertEqual(identity_sets([json.loads(l) for l in a.decode().splitlines()]),
                             identity_sets([json.loads(l) for l in b.decode().splitlines()]))

    def test_failure_behaviour(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / 'bad.jsonl'
            bad.write_text('{"id": "ok", "comments": []}\nnot json\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'not valid JSON'):
                load_export(bad)
            missing = Path(d) / 'absent.jsonl'
            with self.assertRaisesRegex(ValueError, 'cannot read export'):
                load_export(missing)
            nonlist = Path(d) / 'nonlist.jsonl'
            nonlist.write_text('{"id": "x", "comments": {}}\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'non-list comments'):
                load_export(nonlist)
            from activity import parse_moment
            with self.assertRaisesRegex(ValueError, 'timezone'):
                parse_moment('2026-09-19T10:00:00')


if __name__ == '__main__':
    unittest.main()
