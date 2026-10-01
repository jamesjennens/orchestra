"""Read-only capability lookup (capabilities.py and `client.py -- capability`).

The lookup runs in the caller's own checkout and writes nothing. These tests pin
the ast and Markdown index, exact and near matches, pointer resolution (the drift
primitive), the versioned JSON/help/error shapes, the client routing, and the
defensive handling of graphify's graph.json as untrusted input.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import capabilities
import client

GIT = shutil.which('git')

CORE = '''"""Core engine."""
from pkg.util import tidy


class Engine:
    """Runs the pipeline.

    More detail that is not the summary.
    """

    def start(self):
        """Start the engine."""
        self._check()
        return tidy('x')

    def _check(self):
        return True


def helper():
    def local_only():
        return 1
    return local_only()


if True:
    def conditional():
        return 2
'''

UTIL = '''def tidy(value):
    """Tidy a value."""
    return value.strip()


def run():
    return 1
'''

OTHER = '''def run():
    """Another run."""
    return 2
'''

TEST_CORE = '''import unittest
from pkg.core import Engine


class EngineTests(unittest.TestCase):
    def test_start(self):
        self.assertTrue(Engine().start() is not None)
'''

DESIGN = '''# Design

Intro paragraph.

## `work`: list/queue shape

The work queue.

```sh
# not a heading
```

## Merge slots

- First.

## Merge slots

Second.

## See [the guide](guide.md)

Link heading.

## Fenced first

```text
code line
```

After the fence.
'''

APP = '''export function render() {
  return 1;
}
'''


def write(root, rel, text):
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding='utf-8', newline='\n')
    return path


def fixture(root):
    write(root, 'pkg/__init__.py', '')
    write(root, 'pkg/core.py', CORE)
    write(root, 'pkg/util.py', UTIL)
    write(root, 'pkg/other.py', OTHER)
    write(root, 'tests/test_core.py', TEST_CORE)
    write(root, 'docs/DESIGN.md', DESIGN)
    write(root, 'web/app.ts', APP)


def call(*args):
    code, stdout, stderr = capabilities.run(list(args))
    return code, (json.loads(stdout) if code == 0 else None), stdout, stderr


GOOD_NODES = [
    {'id': 'pkg_core', 'label': 'core.py', 'file_type': 'code', 'source_file': 'pkg/core.py',
     'source_location': 'L1'},
    {'id': 'pkg_core_engine', 'label': 'Engine', 'file_type': 'code', 'source_file': 'pkg/core.py',
     'source_location': 'L5'},
    {'id': 'pkg_core_engine_start', 'label': '.start()', 'file_type': 'code',
     'source_file': 'pkg/core.py', 'source_location': 'L99'},
    {'id': 'pkg_core_ghost', 'label': 'ghost()', 'file_type': 'code', 'source_file': 'pkg/core.py',
     'source_location': 'L40'},
    {'id': 'pkg_util_tidy', 'label': 'tidy()', 'file_type': 'code', 'source_file': 'pkg/util.py',
     'source_location': 'L1'},
    {'id': 'core_rationale_6', 'label': 'Runs the pipeline.', 'file_type': 'rationale',
     'source_file': 'pkg/core.py', 'source_location': 'L6'},
    {'id': 'web_app', 'label': 'app.ts', 'file_type': 'code', 'source_file': 'web/app.ts',
     'source_location': 'L1'},
    {'id': 'web_app_render', 'label': 'render()', 'file_type': 'code', 'source_file': 'web/app.ts',
     'source_location': 'L1'},
    {'id': 'tests_test_core_test_start', 'label': '.test_start()', 'file_type': 'code',
     'source_file': 'tests/test_core.py', 'source_location': 'L6'},
    {'id': 'concurrent_futures', 'label': 'concurrent.futures', 'file_type': 'concept',
     'type': 'external', 'external': True, 'source_file': ''},
]
GOOD_LINKS = [
    {'source': 'pkg_core', 'target': 'pkg_core_engine', 'relation': 'contains', 'confidence': 'EXTRACTED'},
    {'source': 'pkg_core_engine', 'target': 'pkg_core_engine_start', 'relation': 'method',
     'confidence': 'EXTRACTED'},
    {'source': 'core_rationale_6', 'target': 'pkg_core_engine', 'relation': 'rationale_for',
     'confidence': 'EXTRACTED'},
    {'source': 'pkg_core_engine_start', 'target': 'pkg_util_tidy', 'relation': 'calls',
     'confidence': 'EXTRACTED'},
    {'source': 'tests_test_core_test_start', 'target': 'pkg_core_engine_start', 'relation': 'calls',
     'confidence': 'INFERRED'},
]


def graph_text(nodes=GOOD_NODES, links=GOOD_LINKS, commit=None, key='links', **extra):
    document = {'directed': False, 'multigraph': False, 'graph': {}, 'nodes': nodes, key: links,
                'hyperedges': []}
    if commit:
        document['built_at_commit'] = commit
    document.update(extra)
    return json.dumps(document)


class Checkout(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'repo'
        self.root.mkdir()
        fixture(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def lookup(self, phrase, *extra):
        code, result, _, stderr = call('lookup', phrase, '--repo', str(self.root), *extra)
        self.assertEqual(code, 0, stderr)
        return result

    def ids(self, rows):
        return [row['id'] for row in rows]


class AstIndexTests(Checkout):
    def index(self, *extra):
        code, result, _, stderr = call('index', '--repo', str(self.root), *extra)
        self.assertEqual(code, 0, stderr)
        return result

    def test_index_lists_modules_classes_functions_methods_and_headings(self):
        result = self.index()
        self.assertEqual(result['schema'], 'capability-index-v1')
        self.assertEqual(result['trust'], 'repository-content')
        self.assertEqual(result['index']['code_source'], 'ast')
        entries = {row['id']: row for row in result['entries']}
        for pointer, kind in (('pkg/core.py', 'module'), ('pkg/core.py::Engine', 'class'),
                              ('pkg/core.py::Engine.start', 'method'), ('pkg/core.py::helper', 'function'),
                              ('pkg/core.py::conditional', 'function'),
                              ('docs/DESIGN.md#merge-slots', 'doc-section')):
            self.assertEqual(entries[pointer]['kind'], kind, pointer)
        self.assertNotIn('pkg/core.py::helper.local_only', entries)
        self.assertNotIn('pkg/core.py::local_only', entries)
        start = entries['pkg/core.py::Engine.start']
        self.assertEqual((start['line'], start['end_line']), (11, 14))
        self.assertEqual(start['summary'], {'text': 'Start the engine.', 'omitted_chars': 0})
        self.assertEqual(entries['pkg/core.py::Engine']['summary']['text'], 'Runs the pipeline.')
        self.assertEqual(result['entries'], sorted(result['entries'], key=lambda row: (row['file'], row['line'] or 0, row['id'])))

    def test_calls_resolve_through_imports_and_self_and_tests_through_references(self):
        entries = {row['id']: row for row in self.index()['entries']}
        start = entries['pkg/core.py::Engine.start']
        outgoing = {(row['relation'], row['id'], row['confidence']) for row in start['related']
                    if row['direction'] == 'out'}
        self.assertEqual(outgoing, {('calls', 'pkg/core.py::Engine._check', 'INFERRED'),
                                    ('calls', 'pkg/util.py::tidy', 'INFERRED')})
        tidy = entries['pkg/util.py::tidy']
        self.assertIn({'relation': 'calls', 'direction': 'in', 'id': 'pkg/core.py::Engine.start',
                       'confidence': 'INFERRED'}, tidy['related'])
        self.assertEqual(entries['pkg/core.py::Engine']['tests'], ['tests/test_core.py::EngineTests.test_start'])
        self.assertIn('tests/test_core.py', entries['pkg/core.py']['tests'])
        self.assertTrue(entries['tests/test_core.py::EngineTests.test_start']['test'])
        # Tests are listed under `tests`, never as related code.
        self.assertFalse(any('tests/' in row['id'] for row in entries['pkg/core.py::Engine']['related']))

    def test_markdown_anchors_follow_github_rules_and_ignore_fences(self):
        entries = {row['id']: row for row in self.index()['entries'] if row['kind'] == 'doc-section'}
        self.assertEqual(sorted(entries), sorted([
            'docs/DESIGN.md#design', 'docs/DESIGN.md#work-listqueue-shape', 'docs/DESIGN.md#merge-slots',
            'docs/DESIGN.md#merge-slots-1', 'docs/DESIGN.md#see-the-guide', 'docs/DESIGN.md#fenced-first']))
        self.assertEqual(entries['docs/DESIGN.md#work-listqueue-shape']['summary']['text'], 'The work queue.')
        self.assertEqual(entries['docs/DESIGN.md#merge-slots']['summary']['text'], 'First.')
        self.assertEqual(entries['docs/DESIGN.md#fenced-first']['summary']['text'], 'After the fence.')
        self.assertEqual(entries['docs/DESIGN.md#see-the-guide']['name']['text'], 'See the guide')

    def test_unparseable_files_are_counted_and_skipped(self):
        write(self.root, 'pkg/broken.py', 'def (:\n')
        write(self.root, 'pkg/nul.py', b'x = 1\x00\n')
        result = self.index()
        self.assertEqual(result['index']['skipped'].get('parse-error'), 2)
        self.assertFalse(any(row['file'] in ('pkg/broken.py', 'pkg/nul.py') for row in result['entries']))

    def test_walk_fallback_skips_dependency_and_hidden_directories(self):
        write(self.root, 'node_modules/dep/x.py', 'def dependency():\n    pass\n')
        write(self.root, '.venv/lib/y.py', 'def venv_thing():\n    pass\n')
        write(self.root, '.hidden/z.py', 'def hidden_thing():\n    pass\n')
        names = {row['name']['text'] for row in self.index()['entries']}
        self.assertFalse({'dependency', 'venv_thing', 'hidden_thing'} & names)

    def test_repository_text_is_cleaned_and_bounded(self):
        docstring = '\x1b[31mRed\u202e alert\u200b ' + 'x' * 400
        write(self.root, 'pkg/noisy.py', 'def noisy():\n    """%s"""\n' % docstring)
        entries = {row['id']: row for row in self.index()['entries']}
        summary = entries['pkg/noisy.py::noisy']['summary']
        self.assertFalse(any(ch in summary['text'] for ch in '\x1b\u202e\u200b'))
        self.assertTrue(summary['text'].startswith('[31mRed alert'))
        self.assertEqual(len(summary['text']), capabilities.SUMMARY_LIMIT)
        self.assertGreater(summary['omitted_chars'], 0)

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks unavailable')
    def test_a_link_out_of_the_checkout_is_not_read(self):
        outside = Path(self._tmp.name) / 'outside.py'
        outside.write_text('def secret_outside():\n    pass\n', encoding='utf-8')
        try:
            os.symlink(outside, self.root / 'pkg' / 'linked.py')
        except (OSError, NotImplementedError):
            self.skipTest('cannot create symlinks here')
        result = self.index()
        self.assertNotIn('secret_outside', {row['name']['text'] for row in result['entries']})
        self.assertGreaterEqual(result['index']['skipped'].get('unreadable', 0), 1)


class LookupTests(Checkout):
    def test_exact_matches_by_name_qualname_dotted_name_and_pointer(self):
        for phrase in ('start', 'Engine.start', 'pkg.core.Engine.start', 'pkg/core.py::Engine.start'):
            result = self.lookup(phrase)
            self.assertTrue(result['found'], phrase)
            self.assertEqual(result['match_type'], 'exact')
            self.assertEqual(result['matches'][0]['id'], 'pkg/core.py::Engine.start', phrase)
            self.assertEqual(result['candidates'], [])
            self.assertIsNone(result['hint'])
        self.assertEqual(self.lookup('Merge slots')['total_matches'], 2)
        self.assertEqual(self.lookup('work list queue shape')['matches'][0]['id'],
                         'docs/DESIGN.md#work-listqueue-shape')

    def test_result_shape_is_versioned(self):
        result = self.lookup('Engine')
        self.assertEqual(result['schema'], 'capability-lookup-v1')
        self.assertEqual(result['contract'], 'cli-contract-v1')
        self.assertEqual(result['trust'], 'repository-content')
        self.assertEqual(set(result), {'schema', 'contract', 'trust', 'query', 'found', 'match_type',
                                       'matches', 'total_matches', 'candidates', 'hint', 'index',
                                       'warnings'})
        match = result['matches'][0]
        for field in ('id', 'kind', 'name', 'file', 'line', 'end_line', 'summary', 'source', 'test',
                      'aliases', 'tests', 'tests_total', 'related', 'related_total'):
            self.assertIn(field, match)
        self.assertEqual(result['query']['phrase'], {'text': 'Engine', 'omitted_chars': 0})

    def test_ambiguous_name_returns_every_match(self):
        result = self.lookup('run')
        self.assertEqual(result['total_matches'], 2)
        self.assertEqual(self.ids(result['matches']), ['pkg/other.py::run', 'pkg/util.py::run'])

    def test_miss_returns_ranked_candidates_and_a_hint(self):
        result = self.lookup('engine starter')
        self.assertFalse(result['found'])
        self.assertEqual(result['matches'], [])
        self.assertIn('pkg/core.py::Engine.start', self.ids(result['candidates']))
        scores = [row['score'] for row in result['candidates']]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIn('alias', result['hint']['text'])
        self.assertLessEqual(len(result['candidates']), capabilities.LIMIT_DEFAULT)

    def test_misspelled_word_is_corrected_and_reported(self):
        result = self.lookup('tidyy value')
        self.assertEqual(result['query']['corrections'], {'tidyy': 'tidy'})
        self.assertEqual(result['candidates'][0]['id'], 'pkg/util.py::tidy')

    def test_nothing_related_gives_no_candidates(self):
        result = self.lookup('zebra quantum')
        self.assertFalse(result['found'])
        self.assertEqual(result['candidates'], [])
        self.assertIsNotNone(result['hint'])

    def test_limits_are_validated(self):
        for extra in (('--limit', '0'), ('--limit', '21')):
            code, _, stdout, stderr = call('lookup', 'Engine', '--repo', str(self.root), *extra)
            self.assertEqual(code, 2)
            self.assertEqual(stdout, '')
            self.assertTrue(stderr.startswith('ValueError: --limit: expected 1..20'), stderr)
        for phrase in ('x' * 201, '   ', '\u200b'):
            code, _, stdout, stderr = call('lookup', phrase, '--repo', str(self.root))
            self.assertEqual(code, 2, phrase)
            self.assertEqual(stdout, '')
        self.assertEqual(len(self.lookup('run', '--limit', '1')['matches']), 1)


class ResolveTests(Checkout):
    def resolve(self, *pointers, extra=()):
        code, result, _, stderr = call('resolve', *pointers, '--repo', str(self.root), *extra)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result['schema'], 'capability-resolve-v1')
        return {row['pointer']: row for row in result['results']}, result

    def test_pointers_resolve_or_say_why_not(self):
        rows, result = self.resolve(
            'pkg/core.py::Engine.start', 'pkg/core.py::conditional', 'pkg/core.py', 'docs/DESIGN.md#merge-slots-1',
            'pkg/core.py::Engine.stop', 'pkg/core.py::helper.local_only', 'pkg/missing.py::x',
            'docs/DESIGN.md#nope', 'web/app.ts::render')
        self.assertEqual((rows['pkg/core.py::Engine.start']['resolved'], rows['pkg/core.py::Engine.start']['kind'],
                          rows['pkg/core.py::Engine.start']['line']), (True, 'method', 11))
        self.assertTrue(rows['pkg/core.py::conditional']['resolved'])
        self.assertEqual(rows['pkg/core.py']['kind'], 'module')
        self.assertEqual(rows['docs/DESIGN.md#merge-slots-1']['basis'], 'markdown')
        self.assertEqual(rows['pkg/core.py::Engine.stop']['reason'], 'symbol-missing')
        self.assertEqual(rows['pkg/core.py::helper.local_only']['reason'], 'symbol-missing')
        self.assertEqual(rows['pkg/missing.py::x']['reason'], 'file-missing')
        self.assertEqual(rows['docs/DESIGN.md#nope']['reason'], 'anchor-missing')
        self.assertIsNone(rows['web/app.ts::render']['resolved'])
        self.assertEqual(rows['web/app.ts::render']['reason'], 'unsupported-file-type')
        self.assertEqual(result['summary'], {'resolved': 4, 'missing': 4, 'unknown': 1})

    def test_unsafe_pointers_are_refused_without_reading(self):
        pointers = ['../outside.py::f', '/etc/passwd', 'C:/x.py::f', 'pkg\\core.py::Engine',
                    'pkg/core.py::', 'pkg//core.py', './pkg/core.py', 'pkg/co\x00re.py']
        code, result, _, stderr = call('resolve', *[p for p in pointers if '\x00' not in p],
                                       '--repo', str(self.root))
        self.assertEqual(code, 0, stderr)
        self.assertTrue(all(row['reason'] == 'invalid-pointer' and row['resolved'] is False
                            for row in result['results']))
        self.assertEqual(capabilities.resolve_pointer(capabilities.open_repo(str(self.root)),
                                                      'pkg/co\x00re.py')['reason'], 'invalid-pointer')

    def test_drift_after_a_rename_is_reported(self):
        rows, _ = self.resolve('pkg/core.py::Engine.start')
        self.assertTrue(rows['pkg/core.py::Engine.start']['resolved'])
        write(self.root, 'pkg/core.py', CORE.replace('def start(self)', 'def launch(self)'))
        rows, result = self.resolve('pkg/core.py::Engine.start')
        self.assertEqual(rows['pkg/core.py::Engine.start']['reason'], 'symbol-missing')
        self.assertEqual(result['summary']['missing'], 1)

    def test_pointer_count_is_bounded(self):
        code, _, stdout, stderr = call('resolve', *['pkg/core.py'] * 101, '--repo', str(self.root))
        self.assertEqual((code, stdout), (2, ''))
        self.assertIn('1..100', stderr)


class HelpAndErrorTests(unittest.TestCase):
    def test_help_is_json_on_stdout(self):
        for args, command in ((['--help'], 'capability'), (['lookup', '-h'], 'capability lookup'),
                              (['resolve', '--help'], 'capability resolve'), (['index', '-h'], 'capability index')):
            code, result, _, stderr = call(*args)
            self.assertEqual((code, stderr), (0, ''))
            self.assertEqual(result['contract'], 'cli-contract-v1')
            self.assertEqual(result['command'], command)
            self.assertIn('usage', result)
        self.assertEqual(call('lookup', '--help')[1]['output']['schema'], 'capability-lookup-v1')

    def test_a_help_token_that_is_an_option_value_is_not_help(self):
        code, _, stdout, stderr = call('lookup', 'x', '--limit', '-h')
        self.assertEqual((code, stdout), (2, ''))
        self.assertTrue(stderr.startswith('ValueError: '))

    def test_unknown_or_missing_commands_fail_with_exit_2(self):
        for args in ([], ['bogus'], ['lookup'], ['lookup', 'x', '--nope'], ['lookup', 'x', '--source', 'magic']):
            code, _, stdout, stderr = call(*args)
            self.assertEqual((code, stdout), (2, ''), args)
            self.assertTrue(stderr.startswith('ValueError: '), stderr)

    def test_graph_option_conflicts_with_the_ast_source(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _, _, stderr = call('lookup', 'x', '--repo', directory, '--source', 'ast', '--graph', 'g.json')
        self.assertEqual(code, 2)
        self.assertIn('--graph needs --source auto or graphify', stderr)


class GraphTests(Checkout):
    def put_graph(self, text, rel=capabilities.DEFAULT_GRAPH):
        return write(self.root, rel, text)

    def test_graph_replaces_the_code_index_when_present(self):
        self.put_graph(graph_text())
        result = self.lookup('Engine')
        self.assertEqual(result['index']['code_source'], 'graphify')
        self.assertEqual(result['index']['graph']['path'], 'graphify-out/graph.json')
        match = result['matches'][0]
        self.assertEqual((match['id'], match['source'], match['graph_id']),
                         ('pkg/core.py::Engine', 'graphify', 'pkg_core_engine'))
        self.assertEqual(match['summary']['text'], 'Runs the pipeline.')
        self.assertTrue(match['verified'])
        self.assertEqual(self.lookup('Engine', '--source', 'ast')['matches'][0]['source'], 'ast')

    def test_python_matches_from_the_graph_are_rechecked_with_ast(self):
        self.put_graph(graph_text())
        start = self.lookup('Engine.start')['matches'][0]
        self.assertEqual((start['id'], start['verified'], start['line']), ('pkg/core.py::Engine.start', True, 11))
        self.assertIn({'relation': 'calls', 'direction': 'out', 'id': 'pkg/util.py::tidy',
                       'confidence': 'EXTRACTED'}, start['related'])
        self.assertEqual(start['tests'], ['tests/test_core.py::test_start'])
        ghost = self.lookup('ghost')['matches'][0]
        self.assertFalse(ghost['verified'])
        render = self.lookup('render')['matches'][0]
        self.assertIsNone(render['verified'])

    def test_resolve_uses_the_graph_only_for_files_ast_cannot_read(self):
        self.put_graph(graph_text())
        code, result, _, stderr = call('resolve', 'web/app.ts::render', 'web/app.ts::missing',
                                       'pkg/core.py::ghost', '--repo', str(self.root))
        self.assertEqual(code, 0, stderr)
        rows = {row['pointer']: row for row in result['results']}
        self.assertEqual((rows['web/app.ts::render']['resolved'], rows['web/app.ts::render']['basis']), (True, 'graph'))
        self.assertEqual(rows['web/app.ts::missing']['reason'], 'symbol-missing')
        self.assertEqual((rows['pkg/core.py::ghost']['basis'], rows['pkg/core.py::ghost']['reason']),
                         ('ast', 'symbol-missing'))

    def test_edges_key_is_accepted_and_bom_is_tolerated(self):
        self.put_graph(b'\xef\xbb\xbf' + graph_text(key='edges').encode('utf-8'))
        start = self.lookup('Engine.start')['matches'][0]
        self.assertEqual(start['source'], 'graphify')
        self.assertTrue(any(row['id'] == 'pkg/util.py::tidy' for row in start['related']))

    def test_explicit_graph_path_implies_graphify(self):
        path = write(Path(self._tmp.name), 'elsewhere/graph.json', graph_text())
        result = self.lookup('Engine', '--graph', str(path))
        self.assertEqual(result['index']['code_source'], 'graphify')
        self.assertEqual(result['index']['graph']['path'], '<outside the checkout>')

    def test_missing_graph_with_explicit_source_fails(self):
        code, _, stdout, stderr = call('lookup', 'Engine', '--repo', str(self.root), '--source', 'graphify')
        self.assertEqual((code, stdout), (2, ''))
        self.assertIn('graph.json is missing', stderr)


class HostileGraphTests(Checkout):
    SECRET = 'PRIVATE-TOKEN-5c1f'

    def put_graph(self, data):
        write(self.root, capabilities.DEFAULT_GRAPH, data)

    def refused(self, fragment):
        """auto falls back to ast with a warning; an explicit graphify source fails."""
        result = self.lookup('Engine')
        self.assertEqual(result['index']['code_source'], 'ast')
        self.assertTrue(any(fragment in warning and 'using the ast index' in warning
                            for warning in result['warnings']), result['warnings'])
        code, _, stdout, stderr = call('lookup', 'Engine', '--repo', str(self.root), '--source', 'graphify')
        self.assertEqual((code, stdout), (2, ''))
        self.assertIn(fragment, stderr)
        self.assertNotIn(self.SECRET, stderr)
        return stderr

    def test_oversized_graph_is_refused_before_parsing(self):
        self.put_graph(graph_text(graph={'padding': 'x' * 1_100_000}))
        code, _, _, stderr = call('lookup', 'Engine', '--repo', str(self.root), '--source', 'graphify',
                                  '--max-graph-mb', '1')
        self.assertEqual(code, 2)
        self.assertIn('larger than the 1 MB limit', stderr)
        code, result, _, _ = call('lookup', 'Engine', '--repo', str(self.root), '--max-graph-mb', '1')
        self.assertEqual((code, result['index']['code_source']), (0, 'ast'))
        for bad in ('0', '513'):
            self.assertEqual(call('lookup', 'Engine', '--repo', str(self.root), '--max-graph-mb', bad)[0], 2)

    def test_deep_nesting_is_refused(self):
        self.put_graph('[' * 200_000 + ']' * 200_000)
        self.refused('nests too deeply')

    def test_non_finite_numbers_are_refused(self):
        self.put_graph('{"nodes": [{"id": "a", "weight": NaN}], "links": []}')
        self.refused('not valid JSON')

    def test_invalid_json_is_refused_without_echoing_content(self):
        self.put_graph('{"nodes": [%s' % self.SECRET)
        self.refused('not valid JSON')

    def test_invalid_utf8_is_refused(self):
        self.put_graph(b'{"nodes": ["\xff\xfe"], "links": []}')
        self.refused('not UTF-8')

    def test_wrong_top_level_shapes_are_refused(self):
        self.put_graph('[]')
        self.refused('must be a JSON object')
        self.put_graph('{"nodes": {}, "links": []}')
        self.refused('must be lists')

    def test_node_and_link_counts_are_bounded(self):
        self.put_graph(graph_text())
        with patch.object(capabilities, 'GRAPH_NODES_MAX', 3):
            self.refused('more than 3 nodes')

    def test_hostile_nodes_and_links_are_counted_and_skipped(self):
        base = {'label': 'evil()', 'file_type': 'code', 'source_location': 'L1'}
        nodes = list(GOOD_NODES) + ['not a node', 7, None] + [
            dict(base, id='abs', source_file='/etc/passwd'),
            dict(base, id='drive', source_file='C:\\Windows\\evil.py'),
            dict(base, id='drive2', source_file='C:evil.py'),
            dict(base, id='up', source_file='../outside.py'),
            dict(base, id='mid', source_file='pkg/../../outside.py'),
            dict(base, id='back', source_file='pkg\\core.py'),
            dict(base, id='nul', source_file='pkg/co\x00re.py'),
            dict(base, id='esc', source_file='pkg/\x1b[31mcore.py'),
            dict(base, id='dot', source_file='./pkg/core.py'),
            dict(base, id='long', source_file='a/' * 300 + 'x.py'),
            dict(base, id='bad id', source_file='pkg/core.py'),
            dict(base, id='x' * 300, source_file='pkg/core.py'),
            dict(base, id=12, source_file='pkg/core.py'),
            dict(base, id='dup', label='dupe()', source_file='pkg/core.py'),
            dict(base, id='dup', label='dupe()', source_file='pkg/core.py'),
            dict(base, id='huge', label='a' * 5000 + '()', source_file='pkg/core.py'),
            dict(base, id='sep', label='a::b()', source_file='pkg/core.py'),
            dict(base, id='slash', label='a/b', source_file='pkg/core.py'),
            dict(base, id='listlabel', label=['x'], source_file='pkg/core.py'),
            dict(base, id='ansi', label='\x1b[2J\u202eevil_twin()', source_file='pkg/core.py'),
            dict(base, id='zw', label='\u200btidy_more()', source_file='pkg/util.py', source_location='L99999999999'),
        ]
        links = list(GOOD_LINKS) + [
            'x', {'source': 1, 'target': 'pkg_core_engine', 'relation': 'calls'},
            {'source': 'pkg_core_engine', 'target': 'missing', 'relation': 'calls'},
            {'source': 'pkg_core_engine', 'target': 'pkg_util_tidy', 'relation': 'Calls Evil'},
            {'source': 'pkg_core_engine', 'target': 'pkg_util_tidy', 'relation': 'uses', 'confidence': 'TOTALLY'},
            {'source': 'dup', 'target': 'pkg_util_tidy', 'relation': 'calls'},
        ]
        self.put_graph(graph_text(nodes, links))
        code, result, stdout, stderr = call('index', '--repo', str(self.root))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result['index']['code_source'], 'graphify')
        skipped = result['index']['skipped']
        self.assertEqual(skipped['graph-node-shape'], 3)
        self.assertEqual(skipped['graph-unsafe-path'], 10)
        self.assertEqual(skipped['graph-node-id'], 3)
        self.assertEqual(skipped['graph-duplicate-id'], 1)
        self.assertEqual(skipped['graph-label'], 5)
        self.assertEqual(skipped['graph-link-shape'], 3)
        self.assertEqual(skipped['graph-link-dangling'], 2)
        for row in result['entries']:
            self.assertIsNotNone(capabilities.safe_relpath(row['file']), row['file'])
        self.assertFalse(any(ch in stdout for ch in ('\\u001b', '\\u202e', '\\u200b', '\x1b', '\u202e', '\u200b')))
        entries = {row['id']: row for row in result['entries']}
        self.assertNotIn('pkg/core.py::dupe', entries)
        more = entries['pkg/util.py::tidy_more']
        self.assertEqual((more['name']['text'], more['line']), ('tidy_more', None))
        uses = [row for row in entries['pkg/core.py::Engine']['related'] if row['relation'] == 'uses']
        self.assertEqual(uses, [{'relation': 'uses', 'direction': 'out', 'id': 'pkg/util.py::tidy',
                                 'confidence': None}])

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks unavailable')
    def test_a_default_graph_linked_out_of_the_checkout_is_ignored(self):
        outside = write(Path(self._tmp.name), 'outside/graph.json', graph_text())
        (self.root / 'graphify-out').mkdir()
        try:
            os.symlink(outside, self.root / capabilities.DEFAULT_GRAPH)
        except (OSError, NotImplementedError):
            self.skipTest('cannot create symlinks here')
        result = self.lookup('Engine')
        self.assertEqual(result['index']['code_source'], 'ast')
        self.assertTrue(any('outside the checkout' in warning for warning in result['warnings']))


@unittest.skipUnless(GIT, 'git is not installed')
class GitCheckoutTests(Checkout):
    def git(self, *args):
        return subprocess.run([GIT, '-C', str(self.root), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                               '-c', 'commit.gpgsign=false', *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def setUp(self):
        super().setUp()
        try:
            self.git('init', '-q')
            write(self.root, '.gitignore', 'ignored.py\n')
            write(self.root, 'ignored.py', 'def ignored_thing():\n    pass\n')
            self.git('add', '-A')
            self.git('commit', '-q', '-m', 'fixture')
        except subprocess.CalledProcessError as exc:
            self.skipTest('git fixture unavailable: %s' % exc.stderr)
        self.head = self.git('rev-parse', 'HEAD')

    def test_commit_is_reported_and_ignored_files_are_not_indexed(self):
        write(self.root, 'pkg/new.py', 'def brand_new():\n    pass\n')
        result = self.lookup('brand_new')
        self.assertTrue(result['found'])
        self.assertEqual(result['index']['repo'], {'git': True, 'commit': self.head, 'dirty': False})
        self.assertFalse(self.lookup('ignored_thing')['found'])
        code, sub_result, _, _ = call('lookup', 'Engine', '--repo', str(self.root / 'pkg'))
        self.assertEqual(sub_result['matches'][0]['file'], 'pkg/core.py')

    def test_a_graph_from_another_commit_is_reported_stale(self):
        write(self.root, capabilities.DEFAULT_GRAPH, graph_text(commit='0' * 40))
        result = self.lookup('Engine')
        self.assertTrue(result['index']['graph']['stale'])
        self.assertTrue(any('was built at 000000000000' in warning for warning in result['warnings']))
        write(self.root, capabilities.DEFAULT_GRAPH, graph_text(commit=self.head))
        result = self.lookup('Engine')
        self.assertIs(result['index']['graph']['stale'], False)
        self.assertEqual(result['warnings'], [])


class ClientRoutingTests(Checkout):
    def main(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['client.py', *argv]), patch('sys.stdout', stdout), \
                patch('sys.stderr', stderr), patch('client.request', side_effect=AssertionError('endpoint called')):
            code = client.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_capability_runs_locally_without_config_project_or_actor(self):
        code, stdout, stderr = self.main('--', 'capability', 'lookup', 'Engine', '--repo', str(self.root))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)['matches'][0]['id'], 'pkg/core.py::Engine')
        code, stdout, _ = self.main('--config', 'unused.json', '--project', 'p', '--actor', 'a/b', '--',
                                    'capability', '--help')
        self.assertEqual((code, json.loads(stdout)['command']), (0, 'capability'))

    def test_failures_keep_the_contract_envelope(self):
        code, stdout, stderr = self.main('--', 'capability', 'lookup')
        self.assertEqual((code, stdout), (2, ''))
        self.assertTrue(stderr.startswith('ValueError: '))

    def test_out_writes_utf8_without_bom_and_nothing_on_failure(self):
        target = Path(self._tmp.name) / 'lookup.json'
        code, stdout, stderr = self.main('--out', str(target), '--', 'capability', 'lookup', 'Engine',
                                         '--repo', str(self.root))
        self.assertEqual((code, stdout), (0, ''), stderr)
        raw = target.read_bytes()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\r\n', raw)
        self.assertEqual(json.loads(raw.decode('utf-8'))['schema'], 'capability-lookup-v1')
        missing = Path(self._tmp.name) / 'missing.json'
        code, _, _ = self.main('--out', str(missing), '--', 'capability', 'lookup', '--limit', '0', 'x')
        self.assertEqual(code, 2)
        self.assertFalse(missing.exists())

    def test_an_unrelated_capabilities_module_is_never_used(self):
        impostor = type(sys)('capabilities')
        impostor.run = lambda args: (0, 'IMPOSTOR\n', '')
        with patch.dict(sys.modules, {'capabilities': impostor}):
            code, stdout, _ = self.main('--', 'capability', 'lookup', 'Engine', '--repo', str(self.root))
        self.assertEqual(code, 0)
        self.assertNotIn('IMPOSTOR', stdout)

    def test_standalone_client_needs_the_sibling_module(self):
        alone = Path(self._tmp.name) / 'alone'
        alone.mkdir()
        shutil.copy(KIT / 'client.py', alone / 'client.py')
        run = subprocess.run([sys.executable, str(alone / 'client.py'), '--', 'capability', 'lookup', 'Engine',
                              '--repo', str(self.root)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(run.returncode, 2)
        self.assertIn('capabilities.py', run.stderr)
        self.assertEqual(run.stdout, '')
        shutil.copy(KIT / 'capabilities.py', alone / 'capabilities.py')
        run = subprocess.run([sys.executable, str(alone / 'client.py'), '--', 'capability', 'lookup', 'Engine',
                              '--repo', str(self.root)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['matches'][0]['id'], 'pkg/core.py::Engine')


def run_tool(*args):
    """capabilities.py in a child process, so a regression that crashes the
    interpreter fails one test instead of killing the suite."""
    env = dict(os.environ)
    env.pop('PYTHONDONTWRITEBYTECODE', None)
    return subprocess.run([sys.executable, str(KIT / 'capabilities.py'), *args], capture_output=True,
                          text=True, encoding='utf-8', env=env, timeout=600)


class NestingGuardTests(Checkout):
    """CPython 3.10 crashes (no RecursionError) converting a very deep expression."""

    def index_in_child(self):
        run = run_tool('index', '--repo', str(self.root), '--source', 'ast')
        self.assertEqual(run.returncode, 0, run.stderr[-500:])
        return json.loads(run.stdout)

    def test_a_long_operator_chain_is_skipped_not_parsed(self):
        write(self.root, 'pkg/deep.py', 'x = ' + '+'.join(['1'] * 200_000) + '\n')
        result = self.index_in_child()
        self.assertEqual(result['index']['skipped'].get('too-complex'), 1)
        self.assertFalse(any(row['file'] == 'pkg/deep.py' for row in result['entries']))
        self.assertTrue(any(row['id'] == 'pkg/core.py::Engine' for row in result['entries']))

    def test_deep_chains_hidden_in_fstrings_brackets_and_keywords_are_caught(self):
        terms = capabilities.NESTING_MAX + 10
        sources = {
            'fstring': 'x = f"{' + '+'.join(['1'] * terms) + '}"\n',
            'multiline': 'x = (\n' + '+\n'.join(['1'] * terms) + '\n)\n',
            'keywords': 'x = ' + 'not ' * terms + 'True\n',
            'attributes': 'x = a' + '.b' * terms + '\n',
        }
        for name, source in sources.items():
            with self.assertRaises(capabilities.TooComplex, msg=name):
                capabilities.parse_python(source.encode('utf-8'), name + '.py')

    def test_many_ordinary_lines_are_still_parsed(self):
        source = ''.join('value_%d = a.b(c[1] + 2 - 3) if not d else e\n' % n for n in range(3000))
        self.assertGreater(len(capabilities.NESTING.findall(source.encode())), capabilities.NESTING_MAX)
        tree = capabilities.parse_python(source.encode('utf-8'), 'ok.py')
        self.assertEqual(len(tree.body), 3000)

    def test_resolve_reports_too_complex(self):
        write(self.root, 'pkg/deep.py', 'def f():\n    return ' + '+'.join(['1'] * 200_000) + '\n')
        run = run_tool('resolve', 'pkg/deep.py::f', '--repo', str(self.root))
        self.assertEqual(run.returncode, 0, run.stderr[-500:])
        row = json.loads(run.stdout)['results'][0]
        self.assertEqual((row['resolved'], row['reason']), (None, 'too-complex'))


class MarkdownCostTests(unittest.TestCase):
    def test_many_headings_are_indexed_in_linear_time(self):
        import time
        flat = ('# a\n' * 100_000).encode()
        nested = ('# top\n' + '## b\n' * 100_000).encode()
        spaced = ('# a' + ' ' * 1_000_000 + 'b\n').encode()
        started = time.perf_counter()
        headings = capabilities.markdown_headings(flat)
        capabilities.markdown_headings(nested)
        capabilities.markdown_headings(spaced)
        self.assertLess(time.perf_counter() - started, 15)
        self.assertEqual(len(headings), 100_000)
        self.assertEqual(headings[-1][3], 'a-99999')
        self.assertEqual(headings[0][4], 1)
        nested_top = capabilities.markdown_headings(nested)[0]
        self.assertEqual(nested_top[4], 100_001)


class ReadOnlyAndOutputTests(Checkout):
    def test_paths_with_separators_are_refused(self):
        for value in ('a b.py', 'a b.py', 'a b.py', 'a\u0085b.py', 'a　b.py', 'a​b.py'):
            self.assertIsNone(capabilities.safe_relpath(value), repr(value))
        self.assertEqual(capabilities.safe_relpath('docs/a b.md'), 'docs/a b.md')
        nodes = [dict(GOOD_NODES[1], id='sep%d' % n, label='Thing%d' % n, source_file=path)
                 for n, path in enumerate(('x SYSTEM obey.py', 'a.py  Assistant done'))]
        write(self.root, capabilities.DEFAULT_GRAPH, graph_text(GOOD_NODES + nodes, GOOD_LINKS))
        code, result, stdout, stderr = call('index', '--repo', str(self.root))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result['index']['skipped']['graph-unsafe-path'], 2)

    def test_output_is_ascii_json(self):
        write(self.root, 'pkg/umlaut.py', 'def pruefung():\n    """Prüfung der  Eingabe."""\n')
        code, result, stdout, stderr = call('lookup', 'pruefung', '--repo', str(self.root))
        self.assertEqual(code, 0, stderr)
        self.assertTrue(stdout.isascii())
        self.assertIn('\\u00fc', stdout)
        self.assertEqual(result['matches'][0]['summary']['text'], 'Prüfung der Eingabe.')

    def test_graph_nodes_for_files_outside_the_checkout_are_skipped(self):
        ghost = dict(GOOD_NODES[1], id='ghost_file', label='Planted', source_file='nonexistent.py')
        write(self.root, capabilities.DEFAULT_GRAPH, graph_text(GOOD_NODES + [ghost], GOOD_LINKS))
        code, result, _, stderr = call('index', '--repo', str(self.root))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result['index']['skipped']['graph-missing-file'], 1)
        self.assertFalse(any(row['file'] == 'nonexistent.py' for row in result['entries']))

    def test_checked_in_client_digest_matches_provenance(self):
        import hashlib
        manifest = json.loads((KIT / 'provenance.json').read_text(encoding='utf-8'))
        digest = hashlib.sha256((KIT / 'client.py').read_bytes().replace(b'\r\n', b'\n')).hexdigest()
        self.assertEqual(manifest['files']['client.py'], digest)
        self.assertNotIn('capabilities.py', manifest['files'])

    def test_client_loads_the_module_without_writing_bytecode(self):
        alone = Path(self._tmp.name) / 'alone'
        alone.mkdir()
        shutil.copy(KIT / 'client.py', alone / 'client.py')
        shutil.copy(KIT / 'capabilities.py', alone / 'capabilities.py')
        env = dict(os.environ)
        env.pop('PYTHONDONTWRITEBYTECODE', None)
        run = subprocess.run([sys.executable, str(alone / 'client.py'), '--', 'capability', 'lookup', 'Engine',
                              '--repo', str(self.root)], capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(sorted(path.name for path in alone.iterdir()), ['capabilities.py', 'client.py'])


@unittest.skipUnless(GIT, 'git is not installed')
class GitIndexTests(Checkout):
    def test_lookup_never_rewrites_the_git_index(self):
        run = lambda *args: subprocess.run([GIT, '-C', str(self.root), '-c', 'user.name=t',
                                            '-c', 'user.email=t@example.invalid', '-c', 'commit.gpgsign=false',
                                            *args], capture_output=True, text=True)
        if run('init', '-q').returncode or run('add', '-A').returncode or \
                run('commit', '-q', '-m', 'fixture').returncode:
            self.skipTest('git fixture unavailable')
        core = self.root / 'pkg' / 'core.py'
        later = core.stat().st_mtime + 120
        os.utime(core, (later, later))
        index = self.root / '.git' / 'index'
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        code, result, _, stderr = call('lookup', 'Engine', '--repo', str(self.root))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result['index']['repo']['dirty'], False)
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)
        self.assertFalse((self.root / '.git' / 'index.lock').exists())


class KitSelfIndexTests(unittest.TestCase):
    """The kit's own checkout is a realistic fixture: known symbols and doc sections resolve."""

    def test_the_kit_finds_its_own_code_and_contract(self):
        code, result, _, stderr = call('resolve', 'endpoint.py::_guard_reserved_labels',
                                       'capabilities.py::lookup', 'docs/CLI_CONTRACT.md#documented-limits',
                                       '--repo', str(KIT), '--source', 'ast')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result['summary'], {'resolved': 3, 'missing': 0, 'unknown': 0})


if __name__ == '__main__':
    unittest.main()
