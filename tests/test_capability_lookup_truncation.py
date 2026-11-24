"""Regression tests for kittrial-5bb.603: capability lookup must not silently cut record text at 200 chars.

Covers:
1. A 1,000-character record returned whole in CLI (`find`, `lookup`, `get`), HTTP (`/capabilities/find`,
   `/capabilities/{key}`), and web (pinned alongside `tests/web_records_screen.mjs`).
2. Exact boundary lengths 199, 200, and 201 characters across CLI and HTTP.
3. Multi-byte text (CJK, 4-byte emoji, combining accent, ZWJ sequences) across the old 200-char
   boundary and active bounds (`SUMMARY_LIMIT=200`, `VIEW_SUMMARY=300`, `SUMMARY_MAX=1200`):
   never splits inside a grapheme cluster, and keeps ZWJ (`U+200D`) stripped (including between
   two Cyrillic letters).
4. Newlines and Markdown preserved on capability `find` and `get` `summary` (`multiline=True`),
   with bidi overrides (`U+202E`, `U+2066`-`U+2069`) and `U+200B`/`U+200D` still stripped, while
   default `capabilities.clean()` / `excerpt()` callers keep single-line behaviour.
5. `--json` returns the full `summary` field unchanged.
6. Short listings (`views/CAPABILITIES.md` at `VIEW_SUMMARY=300`) use an explicit grapheme-safe
   marker (`truncated: N more characters; capability get KEY`), never a silent mid-sentence cut.
7. A large 100 KB record is bounded and stated on both write (refused at `SUMMARY_MAX=1200`) and
   defensive read (capped at `SUMMARY_MAX=1200` / `VIEW_SUMMARY=300` / `SUMMARY_LIMIT=200` with
   exact `omitted_chars` stated).
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]

import capabilities
import capability_records as cr
import client
import http_service
import test_http_review_fixes as fixes
from http_client import Client
from test_capability_records import CapabilityCase, entry
from test_reference_records import OPERATOR

STRICT_ENDPOINT = KIT / 'tools' / 'strict_canonical_endpoint.py'


def _make_text_of_length(length, prefix='Record details: ', tail='END-SENTENCE-VERIFIED.'):
    assert length > len(prefix) + len(tail) + 1
    body_len = length - len(prefix) - len(tail) - 1
    filler = ('alpha-beta-gamma-delta-epsilon-zeta-eta-theta-' * ((body_len // 40) + 2))[:body_len]
    out = prefix + filler + ' ' + tail
    assert len(out) == length
    return out


class CapabilityLookupTruncationUnitAndCliTests(CapabilityCase):
    def _propose_and_accept(self, key, name, summary, aliases=None, op_id='op-1'):
        self.propose(
            operation_id=op_id,
            key=key,
            name=name,
            aliases=aliases or [],
            summary=summary,
            owner='person:maintainer',
            tags=['basis'],
            code=['capability_records.py::find'],
            tests=['tests/test_capability_lookup_truncation.py'],
            anchors=['docs/CLI_CONTRACT.md#capability-records-the-index-on-the-endpoint'],
        )
        self.batch([self.item(key, 1)], operation_id='accept-' + op_id)

    def _run_client_lookup(self, repo_dir, phrase, find_result, extra_args=()):
        cfg_file = Path(repo_dir) / 'client.json'
        cfg_file.write_text(json.dumps({'transport': 'local', 'root': str(repo_dir)}), encoding='utf-8')
        calls = []

        def fake_request(config, project, actor, args, action='bd', path=None):
            calls.append((action, list(args)))
            return {'returncode': 0, 'stdout': json.dumps(find_result, ensure_ascii=False) + '\n', 'stderr': ''}

        argv = [
            '--config', str(cfg_file), '--project', 'demo', '--actor', 'alice', '--',
            'capability', 'lookup', phrase, '--repo', str(repo_dir), *extra_args,
        ]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['client.py', *argv]), \
             patch.object(client, 'request', side_effect=fake_request), \
             redirect_stdout(out), redirect_stderr(err):
            code = client.main()
        return code, out.getvalue(), err.getvalue(), calls

    def test_1000_char_record_whole_in_cli_find_lookup_get_and_json(self):
        summary_1000 = _make_text_of_length(1000, prefix='LGB basis rule: ', tail='TAIL-1000-INTACT.')
        self._propose_and_accept(
            'flag.basis-hold-net-roll',
            'Basis hold net roll',
            summary_1000,
            aliases=['basis hold net roll'],
            op_id='cap-1000',
        )

        # 1. Exact match via `capability find` and `capability find --json`
        for args in (['find', 'flag.basis-hold-net-roll'], ['find', 'flag.basis-hold-net-roll', '--json']):
            found = self.read(*args)
            self.assertTrue(found['found'])
            self.assertEqual(len(found['records']), 1)
            got = found['records'][0]['summary']
            self.assertEqual(got['text'], summary_1000)
            self.assertEqual(len(got['text']), 1000)
            self.assertEqual(got['omitted_chars'], 0)
            self.assertTrue(got['text'].endswith('TAIL-1000-INTACT.'))

        # 2. Candidate match via `capability find` (non-exact phrase)
        cand_found = self.read('find', 'LGB basis rule')
        self.assertFalse(cand_found['found'])
        self.assertGreaterEqual(len(cand_found['candidates']), 1)
        cand = [c for c in cand_found['candidates'] if c['key'] == 'flag.basis-hold-net-roll'][0]
        self.assertEqual(cand['summary']['text'], summary_1000)
        self.assertEqual(cand['summary']['omitted_chars'], 0)

        # 3. `capability get` and `capability get --json`
        for args in (['get', 'flag.basis-hold-net-roll'], ['get', 'flag.basis-hold-net-roll', '--json']):
            detail = self.read(*args)
            self.assertEqual(detail['record']['summary']['text'], summary_1000)
            self.assertEqual(detail['record']['summary']['omitted_chars'], 0)

        # 4. CLI `client.py -- capability lookup` (with and without `--json`)
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'mod.py').write_text('def noop():\n    """Noop."""\n    return 1\n', encoding='utf-8')
            find_payload = self.read('find', 'flag.basis-hold-net-roll')
            for extra in ((), ('--json',)):
                code, stdout, stderr, calls = self._run_client_lookup(
                    td, 'flag.basis-hold-net-roll', find_payload, extra_args=extra
                )
                self.assertEqual(code, 0, stderr)
                self.assertEqual(calls, [('capability', ['find', 'flag.basis-hold-net-roll', '--limit', '5'])])
                parsed = json.loads(stdout)
                self.assertEqual(len(parsed['records']), 1)
                self.assertEqual(parsed['records'][0]['summary']['text'], summary_1000)
                self.assertEqual(parsed['records'][0]['summary']['omitted_chars'], 0)

    def test_summary_exact_199_200_201_char_boundaries_cli(self):
        for length in (199, 200, 201):
            key = 'ops.boundary-%d' % length
            text = _make_text_of_length(length, prefix='Boundary %d: ' % length, tail='END%d.' % length)
            self._propose_and_accept(key, 'Boundary %d' % length, text, op_id='cap-b-%d' % length)
            found = self.read('find', key, '--json')
            self.assertEqual(len(found['records']), 1)
            got = found['records'][0]['summary']
            self.assertEqual(got['text'], text, 'length %d must be whole in find' % length)
            self.assertEqual(len(got['text']), length)
            self.assertEqual(got['omitted_chars'], 0)
            detail = self.read('get', key, '--json')
            self.assertEqual(detail['record']['summary']['text'], text)
            self.assertEqual(detail['record']['summary']['omitted_chars'], 0)

    def test_multibyte_cjk_emoji_combining_and_zwj_stripping_and_no_split(self):
        # Place CJK, 4-byte emoji, combining accent (e + U+0301), ZWJ emoji family, and
        # Cyrillic-ZWJ-Cyrillic across the old 200-char boundary.
        prefix = 'A' * 190
        zwj_emoji = '\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466'
        cyrillic_zwj = '\u0430\u200d\u0431'
        combining = 'cafe\u0301'
        cjk = '\u6f22\u5b57\u30c6\u30b9\u30c8'
        emoji = '\U0001f680\U0001f30d'
        raw_summary = f'{prefix} {cjk} {emoji} {combining} {zwj_emoji} {cyrillic_zwj} TAIL-MB.'
        self._propose_and_accept(
            'l1.manual-basis-recentre',
            'Manual basis recentre',
            raw_summary,
            op_id='cap-mb',
        )

        # 1. `find` and `get`: full text past 200 chars, combining/CJK/emoji intact, ZWJ stripped!
        for summary_obj in (
            self.read('find', 'l1.manual-basis-recentre')['records'][0]['summary'],
            self.read('get', 'l1.manual-basis-recentre')['record']['summary'],
        ):
            text = summary_obj['text']
            self.assertEqual(summary_obj['omitted_chars'], 0)
            self.assertIn(cjk, text)
            self.assertIn(emoji, text)
            self.assertIn(combining, text)
            self.assertTrue(text.endswith('TAIL-MB.'))
            # Condition 1: ZWJ (U+200D) MUST remain stripped everywhere (both emoji family & Cyrillic).
            self.assertNotIn('\u200d', text)
            self.assertIn('\u0430\u0431', text)
            self.assertNotIn(cyrillic_zwj, text)

        # 2. Condition 1 on default capabilities.clean(): ZWJ stripped between Cyrillic letters too.
        cleaned_default = capabilities.clean(f'{cyrillic_zwj} {zwj_emoji}')
        self.assertNotIn('\u200d', cleaned_default)
        self.assertNotIn(cyrillic_zwj, cleaned_default)

        # 3. Active truncation boundaries NEVER split inside a combining accent, skin-tone modifier,
        # or regional indicator flag pair (at SUMMARY_LIMIT=200, VIEW_SUMMARY=300, SUMMARY_MAX=1200).
        for limit in (capabilities.SUMMARY_LIMIT, cr.VIEW_SUMMARY, cr.SUMMARY_MAX):
            # Base 'e' at index limit-1, combining acute U+0301 at index limit.
            comb_str = 'x' * (limit - 1) + 'e\u0301' + 'z' * 10
            ex = capabilities.excerpt(comb_str, limit)
            self.assertLessEqual(len(ex['text']), limit)
            self.assertFalse(ex['text'].endswith('e'), 'split combining accent at limit=%d' % limit)
            self.assertEqual(ex['text'], 'x' * (limit - 1))
            self.assertEqual(ex['omitted_chars'], 12)

            # Emoji + skin-tone modifier straddling limit.
            mod_str = 'x' * (limit - 1) + '\U0001f44d\U0001f3fd' + 'z' * 5
            ex_mod = capabilities.excerpt(mod_str, limit)
            self.assertFalse(ex_mod['text'].endswith('\U0001f44d'), 'split skin-tone modifier at limit=%d' % limit)
            self.assertEqual(ex_mod['text'], 'x' * (limit - 1))
            self.assertEqual(ex_mod['omitted_chars'], 7)

            # Regional indicator flag pair straddling limit.
            flag_str = 'x' * (limit - 1) + '\U0001f1fa\U0001f1f8' + 'z' * 5
            ex_flag = capabilities.excerpt(flag_str, limit)
            self.assertFalse(ex_flag['text'].endswith('\U0001f1fa'), 'split flag pair at limit=%d' % limit)
            self.assertEqual(ex_flag['text'], 'x' * (limit - 1))
            self.assertEqual(ex_flag['omitted_chars'], 7)

        # 4. Direct slice_graphemes on raw uncleaned text never splits raw ZWJ or CRLF either.
        raw_zwj = 'x' * 5 + zwj_emoji + 'y'
        kept, omitted = capabilities.slice_graphemes(raw_zwj, 7)
        self.assertEqual(kept, 'x' * 5)
        self.assertEqual(omitted, len(raw_zwj) - 5)

    def test_lone_cr_normalized_to_lf_on_multiline_path(self):
        # Kills reviewer mutant O3 (removing CR normalisation at capabilities.py:233).
        self.assertEqual(capabilities.clean('x\ry', multiline=True), 'x\ny')
        self.assertEqual(capabilities.excerpt('x\ry', 50, multiline=True), {'text': 'x\ny', 'omitted_chars': 0})
        self._propose_and_accept(
            'ops.lone-cr-summary',
            'Lone CR summary',
            'line one\rline two\r\nline three',
            op_id='cap-lone-cr',
        )
        expected = 'line one\nline two\nline three'
        for summary_obj in (
            self.read('find', 'ops.lone-cr-summary')['records'][0]['summary'],
            self.read('get', 'ops.lone-cr-summary')['record']['summary'],
        ):
            self.assertEqual(summary_obj, {'text': expected, 'omitted_chars': 0})

    def test_regional_indicator_flag_pair_parity_limits_2_3_4(self):
        # Kills reviewer mutant O2 (returning True instead of (run % 2) == 1 at capabilities.py:277).
        us = '\U0001f1fa\U0001f1f8'
        gb = '\U0001f1ec\U0001f1e7'
        fr = '\U0001f1eb\U0001f1f7'
        three_flags = us + gb + fr
        for limit, expected_text, expected_omitted in (
            (2, us, 4),
            (3, us, 4),
            (4, us + gb, 2),
        ):
            kept, omitted = capabilities.slice_graphemes(three_flags, limit)
            self.assertEqual((kept, omitted), (expected_text, expected_omitted), 'limit=%d' % limit)
            self.assertEqual(
                capabilities.excerpt(three_flags, limit),
                {'text': expected_text, 'omitted_chars': expected_omitted},
                'excerpt limit=%d' % limit,
            )

    def test_newlines_markdown_and_bidi_control_stripping_in_summary(self):
        # Condition 2: multiline=True on find & get summary only; bidi U+202E, U+2066-2069,
        # U+200B, U+200D, and ESC still stripped on multiline path; default clean() stays one-line.
        md_summary = (
            '# LGB Basis Rule\r\n'
            '- **Hold** `net_roll` when manual basis applies.\n'
            '  - See [runbook](https://example.test/lgb) for details.\n'
            'Bidi test: \u202erev\u2066lri\u2067rli\u2068fsi\u2069pdi\u200bzws\u0430\u200d\u0431\x1b[31mred'
        )
        self._propose_and_accept(
            'ops.manual-basis-entry',
            'Manual basis entry',
            md_summary,
            op_id='cap-md',
        )

        expected = (
            '# LGB Basis Rule\n'
            '- **Hold** `net_roll` when manual basis applies.\n'
            '  - See [runbook](https://example.test/lgb) for details.\n'
            'Bidi test: revlrirlifsipdizws\u0430\u0431[31mred'
        )
        for summary_obj in (
            self.read('find', 'ops.manual-basis-entry')['records'][0]['summary'],
            self.read('get', 'ops.manual-basis-entry')['record']['summary'],
        ):
            self.assertEqual(summary_obj['omitted_chars'], 0)
            self.assertEqual(summary_obj['text'], expected)
            self.assertIn('\n- **Hold** `net_roll`', summary_obj['text'])
            for bad in ('\u202e', '\u2066', '\u2067', '\u2068', '\u2069', '\u200b', '\u200d', '\x1b', '\r'):
                self.assertNotIn(bad, summary_obj['text'])

        # Default capabilities.clean() / excerpt() callers MUST stay single-line (Condition 2).
        self.assertEqual(capabilities.clean('line1\nline2\r\nline3'), 'line1 line2 line3')
        self.assertEqual(capabilities.excerpt('line1\nline2', 50), {'text': 'line1 line2', 'omitted_chars': 0})

        # CLI JSON output encodes newlines unambiguously inside JSON strings.
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'mod.py').write_text('def noop():\n    """Noop."""\n    return 1\n', encoding='utf-8')
            find_payload = self.read('find', 'ops.manual-basis-entry')
            code, stdout, stderr, _ = self._run_client_lookup(td, 'ops.manual-basis-entry', find_payload)
            self.assertEqual(code, 0, stderr)
            self.assertIn('\\n- **Hold**', stdout)
            parsed = json.loads(stdout)
            self.assertEqual(parsed['records'][0]['summary']['text'], expected)

        # views/CAPABILITIES.md stays single-line inert Markdown (no raw newline heading injection).
        page = cr.capabilities_view(cr.read_rows(self.native), operators=[OPERATOR])
        self.assertNotIn('\n# LGB Basis Rule', page)
        self.assertIn('\\# LGB Basis Rule', page)
        for bad in ('\u202e', '\u2066', '\u2067', '\u2068', '\u2069', '\u200b', '\u200d', '\x1b'):
            self.assertNotIn(bad, page)

    def test_short_listing_capabilities_view_explicit_truncation_marker(self):
        # 1,000-char record with combining accent e\u0301 placed right across VIEW_SUMMARY=300.
        prefix = 'a' * (cr.VIEW_SUMMARY - 1) + 'e\u0301'
        long_summary = prefix + ' ' + ('word ' * 140)
        long_summary = long_summary[:1000]
        self._propose_and_accept(
            'ops.manual-basis-entry',
            'Manual basis entry',
            long_summary,
            op_id='cap-view-long',
        )
        short_summary = 'Short accepted capability summary.'
        self._propose_and_accept(
            'ops.short-entry',
            'Short entry',
            short_summary,
            op_id='cap-view-short',
        )

        page = cr.capabilities_view(cr.read_rows(self.native), operators=[OPERATOR])
        # Prefix has 299 'a's + 'e\u0301' (2 chars), so grapheme-safe cut at 300 keeps 299 chars
        # and omits len(cleaned_long) - 299 characters.
        cleaned_long = capabilities.clean(long_summary)
        expected_omitted = len(cleaned_long) - (cr.VIEW_SUMMARY - 1)
        marker = 'truncated: %d more characters; capability get ops.manual-basis-entry' % expected_omitted
        self.assertIn(marker, page)
        # Ensure combining accent e\u0301 was not split (the kept 299 'a's do not end with bare 'e').
        self.assertNotIn('a' * (cr.VIEW_SUMMARY - 1) + 'e...', page)
        self.assertNotIn('a' * (cr.VIEW_SUMMARY - 1) + 'e (truncated:', page)
        # Short entry must have NO truncation marker.
        self.assertNotIn('capability get ops.short-entry', page)

    def test_large_100kb_record_bounded_and_stated(self):
        huge = ('basis-rule-detail ' * 6000)[:102_400].rstrip()
        if len(huge) < 102_400:
            huge += 'x' * (102_400 - len(huge))
        self.assertEqual(len(huge), 102_400)

        # 1. Write path refuses 100 KB at SUMMARY_MAX=1200.
        with self.assertRaisesRegex(ValueError, 'at most 1200'):
            self.propose(operation_id='cap-huge', key='ops.huge-record', summary=huge)

        # 2. Defensive read path on a synthetic 100 KB summary exercised through find(), get(),
        # and capabilities_view().
        synth_record = {
            'revision': 1, 'name': 'Huge Record', 'summary': huge, 'aliases': [],
            'requirements': [], 'anchors': [], 'code': ['a.py::f'], 'tests': [],
            'owner': 'person:maintainer', 'tags': ['ops'], 'acceptance_state': 'accepted',
            'successor': None, 'sha256': '0' * 64,
        }
        synth_entry = {
            'key': 'ops.huge-synthetic', 'state': 'accepted', 'native_id': 'demo-99',
            'record': synth_record, 'record_comment_id': 'c1', 'acceptance': None,
            'acceptance_inert': False, 'inert_operator': None, 'proposed': None,
            'proposed_comment_id': None, 'aliases_pending': [], 'warnings': [],
            'candidate': None, 'verification_records': [],
        }
        with patch.object(cr, 'catalog', return_value=([synth_entry], [])):
            found = cr.find([], 'ops.huge-synthetic', [OPERATOR])
            self.assertEqual(len(found['records']), 1)
            got = found['records'][0]['summary']
            self.assertEqual(len(got['text']), cr.SUMMARY_MAX)
            self.assertEqual(got['omitted_chars'], 102_400 - cr.SUMMARY_MAX)

            page = cr.capabilities_view([], operators=[OPERATOR])
            self.assertIn(
                'truncated: %d more characters; capability get ops.huge-synthetic' % (102_400 - cr.VIEW_SUMMARY),
                page,
            )

        # 3. Local code-index path bounds a 100 KB docstring at SUMMARY_LIMIT=200 with omitted_chars stated.
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / 'big.py').write_text('def big_fn():\n    """%s"""\n    return 1\n' % huge, encoding='utf-8')
            code, stdout, stderr = capabilities.run(['lookup', 'big_fn', '--repo', td, '--source', 'ast'])
            self.assertEqual(code, 0, stderr)
            data = json.loads(stdout)
            s = data['matches'][0]['summary']
            self.assertEqual(len(s['text']), capabilities.SUMMARY_LIMIT)
            self.assertGreater(s['omitted_chars'], 0)


class HttpCapabilityLookupTruncationTests(unittest.TestCase):
    """End-to-end HTTP tests over EndpointBackend + strict_canonical_endpoint.py + http_client.Client."""

    def setUp(self):
        self.pid = 'pp'
        root_holder = {}

        class Harness(fixes.Harness):
            def make_backend(h):
                root = h.tmp / 'canonical'
                (root / self.pid / '.beads').mkdir(parents=True, exist_ok=True)
                (root / self.pid / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
                root_holder['root'] = root
                return http_service.EndpointBackend(
                    sys.executable, str(STRICT_ENDPOINT), str(root), service=h.service
                )

        self.harness = Harness(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness._stop_server)
        self.owner_token = self.harness.admin_token()
        reg = self.harness.request('POST', '/v1/projects', {'project_id': self.pid, 'name': 'Cap603'},
                                   token=self.owner_token)
        self.assertEqual(reg.status, 201, reg.data)

    def _propose_and_accept_http(self, key, name, summary, idem_prefix):
        r1 = self.harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities',
            {
                'key': key, 'name': name, 'summary': summary, 'aliases': [],
                'requirements': [], 'anchors': [], 'code': ['capability_records.py::find'],
                'tests': [], 'owner': 'person:maintainer', 'tags': ['basis'],
            },
            token=self.owner_token,
            headers={'Idempotency-Key': idem_prefix + '-propose'},
        )
        self.assertEqual(r1.status, 201, r1.data)
        r2 = self.harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/{key}/accept',
            {'revision': r1.data['revision'], 'record_sha256': r1.data['sha256'], 'evidence': 'Verified.'},
            token=self.owner_token,
            headers={'Idempotency-Key': idem_prefix + '-accept'},
        )
        self.assertEqual(r2.status, 200, r2.data)

    def test_http_find_and_get_1000_char_boundaries_multibyte_newlines_and_100kb(self):
        # 1. 1,000-character record
        summary_1000 = _make_text_of_length(1000, prefix='HTTP 1000: ', tail='HTTP-END-1000.')
        self._propose_and_accept_http('flag.basis-hold-net-roll', 'Basis Hold Net Roll', summary_1000, 'k1000')

        # 2. Exact 199, 200, 201 boundaries
        boundaries = {}
        for length in (199, 200, 201):
            key = f'ops.http-len-{length}'
            text = _make_text_of_length(length, prefix=f'HTTP {length}: ', tail=f'END{length}.')
            boundaries[key] = text
            self._propose_and_accept_http(key, f'Len {length}', text, f'k{length}')

        # 3. Multi-byte + ZWJ + multi-line Markdown + bidi
        mb_md = (
            '# Heading\n'
            + 'B' * 195 + ' \u6f22\u5b57 \U0001f680 cafe\u0301 \U0001f468\u200d\U0001f469 \u0430\u200d\u0431\n'
            + '- **bold** `code` \u202e\u2066\u2069\u200bTAIL-HTTP-MD.'
        )
        self._propose_and_accept_http('ops.http-mb-md', 'HTTP Multibyte Markdown', mb_md, 'kmbmd')

        # Verify via raw HTTP GET /capabilities/find and /capabilities/{key} AND http_client.Client
        api = Client(f'http://127.0.0.1:{self.harness.port}')
        api.token = self.owner_token

        r_find = self.harness.request(
            'GET', f'/v1/projects/{self.pid}/capabilities/find?q=flag.basis-hold-net-roll',
            token=self.owner_token,
        )
        self.assertEqual(r_find.status, 200, r_find.data)
        self.assertEqual(r_find.data['records'][0]['summary']['text'], summary_1000)
        self.assertEqual(r_find.data['records'][0]['summary']['omitted_chars'], 0)
        self.assertEqual(r_find.data['items'][0]['summary']['text'], summary_1000)

        cli_find = api.find_capabilities(self.pid, 'flag.basis-hold-net-roll')
        self.assertEqual(cli_find['records'][0]['summary']['text'], summary_1000)
        self.assertEqual(cli_find['records'][0]['summary']['omitted_chars'], 0)

        cli_get = api.get_capability(self.pid, 'flag.basis-hold-net-roll')
        self.assertEqual(cli_get['record']['summary']['text'], summary_1000)
        self.assertEqual(cli_get['record']['summary']['omitted_chars'], 0)

        for key, text in boundaries.items():
            res = api.find_capabilities(self.pid, key)
            self.assertEqual(res['records'][0]['summary']['text'], text)
            self.assertEqual(res['records'][0]['summary']['omitted_chars'], 0)

        res_mb = api.find_capabilities(self.pid, 'ops.http-mb-md')
        got_mb = res_mb['records'][0]['summary']
        self.assertEqual(got_mb['omitted_chars'], 0)
        self.assertIn('# Heading\n', got_mb['text'])
        self.assertIn('\n- **bold** `code` ', got_mb['text'])
        self.assertIn('\u6f22\u5b57 \U0001f680 cafe\u0301', got_mb['text'])
        self.assertIn('\u0430\u0431', got_mb['text'])
        self.assertTrue(got_mb['text'].endswith('TAIL-HTTP-MD.'))
        for bad in ('\u200d', '\u200b', '\u202e', '\u2066', '\u2069'):
            self.assertNotIn(bad, got_mb['text'])

        # 4. 100 KB propose refused via HTTP with 422 mentioning 1200
        r_huge = self.harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities',
            {
                'key': 'ops.http-huge', 'name': 'Huge', 'summary': 'H' * 102_400,
                'aliases': [], 'requirements': [], 'anchors': [],
                'code': ['capability_records.py::find'], 'tests': [],
                'owner': 'person:maintainer', 'tags': ['basis'],
            },
            token=self.owner_token,
            headers={'Idempotency-Key': 'khuge-propose'},
        )
        self.assertEqual(r_huge.status, 422, r_huge.data)
        self.assertIn('1200', json.dumps(r_huge.data))


if __name__ == '__main__':
    unittest.main()
