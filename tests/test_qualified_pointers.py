"""kittrial-5bb.449: project-qualified pointers (docs/design/PROGRAM_DESIGN.md §3, slice 2 of §9).

`req:`, `design:` and a new `decision:` take a `<project>/` qualifier; the part before the first `/` is always a
project. In the trace read a qualified token resolves in that project only when the caller may read it (the web:
check_authority per project, passed as the service's own --readable; the host: the key's projects). Otherwise it
reads `not-visible`, the same row for a project that does not exist, with no read of either. `outside-program` is
an information flag on the web route; the summary counts resolved, broken, not-visible and not-checked.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import briefing
import design_documents
import http_service
import program_isolation
import test_bd_label_aliases as fixture
import trace_read
from briefing import ContextPointers, parse_context_pointers, pointer_target
from test_program_links import LinkCase

NOW = trace_read.snapshot_now()


def task(ident, description, project='prog'):
    return {'id': ident, 'project_id': project, 'title': 'Task ' + ident, 'description': description,
            'status': 'open', 'issue_type': 'task', 'labels': [], 'comments': []}


def decision(ident, project):
    return {'id': ident, 'project_id': project, 'title': 'Decided ' + ident, 'description': '', 'status': 'open',
            'issue_type': 'decision', 'labels': [], 'comments': []}


class GrammarTests(unittest.TestCase):

    def parsed(self, text):
        return [(p['kind'], p['value'], pointer_target(p, 'here')) for p in parse_context_pointers(text)]

    def test_each_kind_with_and_without_a_project(self):
        self.assertEqual(self.parsed('req:k@2 req:platform/k@2 design:x#y design:platform/x#y decision:d-1 '
                                     'decision:platform/d-1'),
                         [('req', 'k@2', ('here', 'k@2')), ('req', 'platform/k@2', ('platform', 'k@2')),
                          ('design', 'x#y', ('here', 'x#y')), ('design', 'platform/x#y', ('platform', 'x#y')),
                          ('decision', 'd-1', ('here', 'd-1')), ('decision', 'platform/d-1', ('platform', 'd-1'))])

    def test_the_first_slash_splits_so_a_key_with_a_slash_is_cited_qualified(self):
        self.assertEqual(self.parsed('req:a/b/c'), [('req', 'a/b/c', ('a', 'b/c'))])
        self.assertEqual(self.parsed('req:here/a/b'), [('req', 'here/a/b', ('here', 'a/b'))])

    def test_what_does_not_match_stays_prose_and_the_old_kinds_are_unchanged(self):
        self.assertEqual([], self.parsed('decision:x@1 design:a/B design:a/ req:a/@2 decision:/x'))
        self.assertEqual([(p['kind'], p['value']) for p in parse_context_pointers('task:a/b ref:x.y')],
                         [('task', 'a/b'), ('ref', 'x.y')])
        self.assertEqual(('here', 'a/b'), pointer_target(parse_context_pointers('task:a/b')[0], 'here'))


class ResolverTests(unittest.TestCase):
    """briefing.ContextPointers, as a brief uses it: one project, never another's."""

    def test_another_projects_token_is_named_and_never_read(self):
        reads = []

        class Rows(list):
            def __iter__(self):
                reads.append(1)
                return super().__iter__()
        pointers = ContextPointers(Rows([decision('n-1', 'north')]), 'prog')
        for token in ('decision:north/n-1', 'req:north/k', 'design:north/x'):
            entry = pointers.resolve(parse_context_pointers(token)[0])
            self.assertEqual((entry['state'], entry['project']), ('cross-project', 'north'), token)
        self.assertEqual([], reads)

    def test_this_projects_qualified_token_resolves_here_and_decision_resolves(self):
        pointers = ContextPointers([decision('p-1', 'prog'), task('p-2', '')], 'prog')
        live = pointers.resolve(parse_context_pointers('decision:prog/p-1')[0])
        self.assertEqual((live['state'], live['flags'], live['project']), ('live', [], 'prog'))
        self.assertEqual('live', pointers.resolve(parse_context_pointers('decision:p-1')[0])['state'])
        for token in ('decision:p-2', 'decision:p-9'):
            self.assertEqual(['unknown-decision'], pointers.resolve(parse_context_pointers(token)[0])['flags'], token)


class TraceTests(unittest.TestCase):
    """trace_read with the loader the endpoint gives it."""

    TOKENS = 'decision:north/n-1 decision:south/s-1 decision:ghost/g-1 design:north/plan#x req:north/K'

    def snapshot(self, readable, others=None, description=None):
        self.reads = []
        projects = {'north': [decision('n-1', 'north')], 'south': [decision('s-1', 'south')]}

        def read_project(project):
            self.reads.append(project)
            return ContextPointers(projects[project], project) if project in projects else None
        loader = others or trace_read.others_loader(readable, read_project)
        rows = [task('t-1', description or self.TOKENS), decision('p-1', 'prog')]
        return trace_read.Snapshot(rows, 'prog', now=NOW, others=loader)

    def cites(self, snap):
        return {c['pointer']: c for c in trace_read.trace(snap, 'tasks')['rows'][0]['cites']}

    def test_a_readable_project_resolves_and_the_others_read_not_visible_with_no_read(self):
        cites = self.cites(self.snapshot({'north'}))
        self.assertEqual((cites['decision:north/n-1']['state'], cites['decision:north/n-1']['project']), ('live', 'north'))
        self.assertEqual(['unknown-scope'], cites['design:north/plan#x']['flags'])
        for token in ('decision:south/s-1', 'decision:ghost/g-1'):
            self.assertEqual((cites[token]['state'], cites[token]['flags']), ('not-visible', []), token)
        self.assertEqual(['north'], self.reads)                   # south (unreadable) and ghost (missing): no read

    def test_not_visible_is_byte_identical_for_a_missing_and_an_unreadable_project(self):
        # §8, the second row: the two answers differ only in the text the caller wrote. ghost is in the caller's set but
        # does not exist (looked for, nothing there); south exists but is outside the set (never read).
        snap = self.snapshot({'north', 'ghost'}, description='decision:south/s-1 decision:ghost/s-1')
        rows = {c['pointer']: c for c in trace_read.trace(snap, 'tasks')['rows'][0]['cites']}
        south = json.dumps(rows['decision:south/s-1'], sort_keys=True).replace('south', 'X')
        ghost = json.dumps(rows['decision:ghost/s-1'], sort_keys=True).replace('ghost', 'X')
        self.assertEqual(south, ghost)
        tokens = [json.dumps(r, sort_keys=True).replace('south', 'X').replace('ghost', 'X')
                  for r in trace_read.tokens_view(snap)]
        self.assertEqual(1, len(set(tokens)))
        self.assertEqual(['ghost'], self.reads)                   # south, outside the set, is never read

    def test_at_most_50_other_projects_then_not_checked(self):
        description = ' '.join('decision:p%02d/x-1' % n for n in range(51))
        reads = []

        def read_project(project):
            reads.append(project)
            return None
        snap = self.snapshot(None, others=trace_read.others_loader(None, read_project), description=description)
        states = {p['pointer']: snap.resolve(p)['state'] for p in parse_context_pointers(description)}
        self.assertEqual('not-checked', states['decision:p50/x-1'])
        self.assertEqual('not-visible', states['decision:p49/x-1'])
        self.assertEqual(50, len(reads))

    def test_the_local_views_never_take_another_projects_token(self):
        self.assertIsNone(trace_read.local_req_key('req:north/K', 'prog'))
        self.assertEqual('K', trace_read.local_req_key('req:K@2', 'prog'))
        self.assertEqual('a/b', trace_read.local_req_key('req:prog/a/b', 'prog'))

    def test_the_summary_counts_resolved_broken_not_visible_and_not_checked(self):
        snap = self.snapshot({'north'}, description=self.TOKENS + ' decision:p-1 decision:p-9')
        line = {row['view']: row for row in trace_read.trace(snap, 'summary')['rows']}['pointers']
        self.assertEqual(line['counts'], {'resolved': 2, 'broken': 3, 'not-visible': 2, 'not-checked': 0})

    def test_a_key_with_a_slash_is_listed_with_its_qualified_spelling(self):
        snap = self.snapshot({'north'}, description='')
        with patch.object(trace_read.Snapshot, 'requirement_keys', lambda self: ['a/b', 'plain']):
            lint = [row for row in trace_read.tokens_view(snap) if 'qualify-key' in row['flags']]
        self.assertEqual(lint, [{'pointer': None, 'where': {'requirement': 'a/b'}, 'flags': ['qualify-key'],
                                 'cite_as': 'req:prog/a/b'}])

    def test_the_readable_argument(self):
        self.assertEqual((['tasks', '--limit', '5'], {'a', 'b'}),
                         trace_read.split_readable(['tasks', '--readable', 'a', '--readable', 'b', '--limit', '5']))
        self.assertEqual((['tasks'], set()), trace_read.split_readable(['tasks', '--readable']))
        self.assertEqual((['tasks'], None), trace_read.split_readable(['tasks']))


class DesignIndexTests(unittest.TestCase):
    """Q5: an index re-derives byte-identically unless the document holds a qualified design: token."""

    DOC = '<!-- design-v1\nscope: plat\nstatus: candidate\n-->\n# Plat\n\n## Storage\nSee req:K and req:north/K and design:x#y here.\n'

    def test_a_document_without_a_qualified_design_token_re_derives_unchanged(self):
        index = design_documents.derive_index(self.DOC, 'design.plat', 'docs/plat.md', 'a' * 40)
        self.assertEqual(['req:K', 'req:north/K', 'design:x#y'], index['headings'][1]['tokens'])
        changed = design_documents.derive_index(self.DOC.replace('design:x#y', 'design:north/x#y'), 'design.plat',
                                                'docs/plat.md', 'a' * 40)
        self.assertIn('design:north/x#y', changed['headings'][1]['tokens'])


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointArgumentTests(unittest.TestCase):

    def test_a_caller_other_than_the_web_service_cannot_give_a_readable_set(self):
        # Q1 at VERIFIED: the set is honoured only from the web service's own call; anyone else is refused, before
        # anything is read (the project path does not even exist here).
        with self.assertRaises(ValueError) as refused:
            fixture.endpoint.trace_answer(Path('/nonexistent-root'), Path('/nonexistent-root/projects/pp'), 'pp',
                                          ['tasks', '--readable', 'qq'], 'op', authority_config=None,
                                          key_projects=frozenset({'pp'}), by_service=False)
        self.assertEqual(trace_read.READABLE_REFUSED, str(refused.exception))


class WebTests(LinkCase):
    """The web route: the caller's readable set, outside-program, the write warning."""

    def setUp(self):
        super().setUp()
        self.calls = []

        def trace(project_id, view, scope=None, offset=0, limit=50, generation=None, readable=()):
            self.calls.append(sorted(readable))
            return {'view': view, 'generation': 'g', 'next_offset': None,
                    'rows': [{'id': 't-1', 'cites': [
                        {'pointer': 'decision:north/n-1', 'where': 'description', 'state': 'live', 'flags': [],
                         'project': 'north'},
                        {'pointer': 'decision:east/e-1', 'where': 'description', 'state': 'live', 'flags': [],
                         'project': 'east'},
                        {'pointer': 'decision:south/s-1', 'where': 'description', 'state': 'not-visible',
                         'flags': [], 'project': 'south'}]}]}
        self.backend.trace = trace
        self.assertEqual(201, self.request('POST', '/v1/projects', {'project_id': 'east', 'name': 'East'},
                                           token=self.admin).status)
        self.admit('north')
        self.join('north')

    def test_the_caller_reads_with_its_own_readable_projects(self):
        for name, expected in (('carl', ['prog']), ('vera', ['north', 'prog']), ('wren', ['prog'])):
            self.calls.clear()
            answer = self.request('GET', '/v1/projects/prog/trace?view=tasks', token=self.tokens[name])
            self.assertEqual(200, answer.status, (name, answer.data))
            self.assertEqual([expected], self.calls, name)

    def test_outside_program_marks_a_readable_unlinked_project_only(self):
        cites = {c['pointer']: c for c in
                 self.request('GET', '/v1/projects/prog/trace?view=tasks', token=self.tokens['pat']).data['rows'][0]['cites']}
        self.assertEqual([], cites['decision:north/n-1']['flags'])                     # linked
        self.assertEqual(['outside-program'], cites['decision:east/e-1']['flags'])     # readable, not linked
        self.assertEqual([], cites['decision:south/s-1']['flags'])                     # not visible: nothing added

    def test_a_requirement_key_with_a_slash_is_kept_with_a_warning(self):
        with patch.object(http_service.ApiHandler, '_requirements_write',
                          lambda self, ctx, action, status=200: (201, {'id': 'r-1'})):
            made = self.request('POST', '/v1/projects/prog/requirements', {'key': 'a/b', 'kind': 'requirement'},
                                token=self.tokens['pat'], key='req-slash-key-0001')
            plain = self.request('POST', '/v1/projects/prog/requirements', {'key': 'ab', 'kind': 'requirement'},
                                 token=self.tokens['pat'], key='req-slash-key-0002')
        self.assertEqual(201, made.status, made.data)
        self.assertEqual([http_service.QUALIFY_KEY_WARNING % {'key': 'a/b', 'project': 'prog'}], made.data['warnings'])
        self.assertNotIn('warnings', plain.data)

    def test_outsiders_learn_nothing_through_the_trace(self):
        # The harness, extended to the trace read: an outsider's trace of an area is the answer for no project.
        self.assertIn(('trace', '/v1/projects/{project}/trace'), program_isolation.AREA_READS)
        program_isolation.assert_outsiders_see_nothing(
            self, self.request, 'north', 'nowhere',
            {'program member': self.tokens['carl'], 'nobody': self.tokens['otto'],
             'agent without the grant': self.tokens['wren']})


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeTraceTests(unittest.TestCase):
    """Two real projects on the real endpoint: pp cites a decision in qq."""

    @classmethod
    def setUpClass(cls):
        cls.runtime = type('QualifiedRuntime', (fixture.RealBdLabelAliasTests,), {})
        cls.runtime.setUpClass()
        r = cls.runtime
        cls.root = r.root
        cls.qq = r.root / 'projects' / 'qq'
        cls.qq.mkdir(parents=True)
        init = subprocess.run([str(r.bd_path), 'init', '--server', '--external', '--server-host', '127.0.0.1',
                               '--server-port', str(r.port), '--server-user', 'root', '--prefix', 'qq',
                               '--database', 'qq', '--skip-agents', '--skip-hooks', '--non-interactive'],
                              cwd=str(cls.qq), env=fixture.admin.environment(r.root), capture_output=True,
                              text=True, timeout=300)
        if init.returncode:
            cls.runtime.tearDownClass()
            raise unittest.SkipTest('bd init of a second project failed: %s' % (init.stderr or init.stdout)[:200])

    @classmethod
    def tearDownClass(cls):
        cls.runtime.tearDownClass()

    def bd(self, project, *args):
        r = self.runtime
        return subprocess.run([str(r.bd_path), '--directory', str(self.root / 'projects' / project), '--sandbox',
                               '--actor', 'op', *args], env=fixture.admin.environment(self.root),
                              capture_output=True, text=True, timeout=120)

    def created(self, project, *args):
        made = self.bd(project, 'create', *args, '--json')
        self.assertEqual(0, made.returncode, made.stderr)
        return json.loads(made.stdout)['id']

    def cites(self, args, **kw):
        answer, _warnings = fixture.endpoint.trace_answer(self.root, self.root / 'projects' / 'pp', 'pp',
                                                          ['tasks'] + args, 'op', **kw)
        return {c['pointer']: c for row in answer['rows'] for c in row['cites']}

    def test_native_the_caller_gate_decides_on_both_routes(self):
        theirs = self.created('qq', 'Use the shared broker', '--type', 'decision')
        token = 'decision:qq/' + theirs
        self.created('pp', 'Cites qq', '--description', token + ' decision:ghost/x-1')
        # Host: a key bound to pp alone reads nothing of qq; one bound to both, or an operator's unbound key, does.
        self.assertEqual('not-visible', self.cites([], key_projects=frozenset({'pp'}))[token]['state'])
        self.assertEqual('live', self.cites([], key_projects=frozenset({'pp', 'qq'}))[token]['state'])
        self.assertEqual('live', self.cites([], key_projects=None)[token]['state'])
        # The web service: its caller's set decides, and an empty set reads nothing.
        self.assertEqual('live', self.cites(['--readable', 'qq'], by_service=True)[token]['state'])
        shown = self.cites(['--readable'], by_service=True)
        self.assertEqual(('not-visible', 'not-visible'), (shown[token]['state'], shown['decision:ghost/x-1']['state']))
        with self.assertRaises(ValueError):
            self.cites(['--readable', 'qq'], key_projects=frozenset({'pp'}), by_service=False)

    def test_native_the_dispatch_honours_the_set_from_the_service_launch_alone(self):
        # Through the endpoint's own dispatch. The web service launches its reads with --authority-store and without
        # --require-authority; that launch is the service, and its set decides. A host key's request is refused.
        theirs = self.created('qq', 'Pick the queue', '--type', 'decision')
        token = 'decision:qq/' + theirs
        self.created('pp', 'Cites the queue', '--description', token)
        request = {'action': 'trace', 'project': 'pp', 'actor': 'http/read', 'args': ['tasks', '--readable', 'qq']}
        config = fixture.endpoint.AuthorityConfig(str(self.root / 'unread-authority.json'))
        answer = fixture.endpoint.execute(self.root, request, authority_config=config, require_authority=False)
        cites = {c['pointer']: c for row in json.loads(answer['stdout'])['rows'] for c in row['cites']}
        self.assertEqual('live', cites[token]['state'])
        for key_projects in (frozenset({'pp'}), None):           # a bound key, and an operator's unbound key
            with self.assertRaises(ValueError) as refused:
                fixture.endpoint.execute(self.root, dict(request, actor='op'), key_projects=key_projects)
            self.assertEqual(trace_read.READABLE_REFUSED, str(refused.exception))


if __name__ == '__main__':
    unittest.main()
