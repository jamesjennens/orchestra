"""kittrial-5bb.434: what a web-only coordinator still could not do over HTTP.

(1) A child task, or a decision, under a parent: POST /v1/projects/{pid}/tasks/{tid}/children, under
CAP_COORDINATE, sending the host's own `coordinate create-child` (coordination.apply_native). (2) A
read-only view of the installation switches that refuse a web write: GET /v1/projects/{pid}/switches.
Each runs against tools/strict_canonical_endpoint.py (Windows) and the real endpoint with real bd.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]

import http_service
import test_bd_label_aliases as fixture
import test_http_review_fixes as fixes
import test_web_records as records
from test_coordinator_rules_endpoint import RulesHelpers

STRICT_ENDPOINT = KIT / 'tools' / 'strict_canonical_endpoint.py'
ABSENT = (404, 'not_found', 'Task not found')
SWITCHES = ['review_workflow_writes', 'checkpoint_provenance_writes', 'web_lifecycle_mark_writes']


class _GapsMixin(records._RecordsScenarioMixin):

    def people(self):
        people = self._setup_actors(self.harness)
        granted = self.harness.request('PUT', f'/v1/projects/{self.pid}/agents/{people["coord1_agent"]["id"]}/coordinator',
                                       {}, token=self.owner_token)
        self.assertIn(granted.status, (200, 201), granted.data)
        return people

    def child(self, token, parent, body, key, project=None):
        return self.harness.request('POST', f'/v1/projects/{project or self.pid}/tasks/{parent}/children', body,
                                    token=token, key='gaps-key-' + key)

    def task(self, token, title):
        made = self.harness.request('POST', f'/v1/projects/{self.pid}/tasks', {'title': title}, token=token)
        self.assertEqual(made.status, 201, made.data)
        return made.data['id']

    def error(self, answer):
        return (answer.status, answer.data['error']['code'], answer.data['error']['message'])

    # -- (1) a child task -----------------------------------------------------------------------
    def run_children_suite(self):
        harness = self.harness
        people = self.people()
        coordinator = people['coord1']['token']
        parent = self.task(coordinator, 'Parent of children')
        body = {'title': 'A child', 'description': 'Do the thing.', 'type': 'task', 'priority': 1}
        made = self.child(coordinator, parent, body, 'child-1')
        self.assertEqual(made.status, 201, made.data)
        child = made.data['id']
        self.assertNotEqual(child, parent)
        shown = harness.request('GET', f'/v1/projects/{self.pid}/tasks/{child}', token=coordinator)
        self.assertEqual(shown.status, 200, shown.data)
        self.assertEqual(shown.data.get('title'), 'A child')
        self.assertEqual(self.parent_of(child), parent)
        # The same key and body: the same child, never a second one.
        again = self.child(coordinator, parent, body, 'child-1')
        self.assertIn(again.status, (200, 201), again.data)
        self.assertEqual(again.data['id'], child)
        self.assertEqual(self.children_of(parent), [child])
        # A coordinator-grant agent may; a decision is a child type.
        by_agent = self.child(people['coord1_agent']['token'], parent,
                              {'title': 'Agent child', 'description': 'x', 'type': 'bug'}, 'child-agent')
        self.assertEqual(by_agent.status, 201, by_agent.data)
        # Without CAP_COORDINATE: a contributor, a viewer and a worker agent without the grant get 403.
        for name in ('contrib', 'viewer', 'coord2_agent', 'contrib_records_agent'):
            refused = self.child(people[name]['token'], parent, body, 'child-refused-' + name)
            self.assertEqual(refused.status, 403, (name, refused.data))
        # ... and before the parent is read: a contributor learns nothing about which tasks exist.
        blind = self.child(people['contrib']['token'], 'kittrial-5bb.99999', body, 'child-refused-absent')
        self.assertEqual(blind.status, 403, blind.data)
        # One 404 for an outsider, an unknown project, a parent that is absent, in another project, or a
        # record anchor; nothing is written for any of them.
        harness.create_account(self.owner_token, 'gaps-outsider', 'rec-password-1')
        outsider, _, _ = harness.login('gaps-outsider', 'rec-password-1')
        foreign = self.foreign_parent()
        anchor = harness.request('POST', f'/v1/projects/{self.pid}/references',
                                 records._valid_ref_propose(key='gaps.anchor', statement='s'),
                                 token=people['contrib']['token'], key='gaps-anchor')
        self.assertEqual(anchor.status, 201, anchor.data)
        before = self.row_count()
        absent = [self.child(outsider, parent, body, 'child-outsider'),
                  self.child(coordinator, parent, body, 'child-unknown-project', project='no-such-project'),
                  self.child(coordinator, 'kittrial-5bb.99999', body, 'child-absent'),
                  self.child(coordinator, anchor.data['native_id'], body, 'child-anchor')]
        absent.append(self.child(coordinator, foreign, body, 'child-foreign'))
        self.assertEqual({self.error(answer) for answer in absent}, {ABSENT})
        self.assertEqual(self.row_count(), before)
        # The request's shape: the type list is exactly create-child's (no epic); unknown fields; a key.
        for wrong, why in (({**body, 'type': 'epic'}, 'epic'), ({**body, 'type': 'story'}, 'story'),
                           ({**body, 'parent': 'x'}, 'field'), ({**body, 'priority': 9}, 'priority'),
                           ({**body, 'title': ''}, 'title'), ({**body, 'assignee': 3}, 'assignee')):
            refused = self.child(coordinator, parent, wrong, 'child-bad-' + why)
            self.assertEqual(refused.status, 422, (why, refused.data))
        no_key = harness.request('POST', f'/v1/projects/{self.pid}/tasks/{parent}/children', body, token=coordinator)
        self.assertEqual(no_key.status, 422, no_key.data)
        self.assertEqual(self.row_count(), before)
        return people, parent

    # -- (2) the installation switches ----------------------------------------------------------
    def run_switches_suite(self):
        harness = self.harness
        people = self.people()
        path = f'/v1/projects/{self.pid}/switches'
        self.set_switches()
        read = harness.request('GET', path, token=people['coord1']['token'])
        self.assertEqual(read.status, 200, read.data)
        self.assertEqual([item['name'] for item in read.data['items']], SWITCHES)
        self.assertEqual({item['state'] for item in read.data['items']}, {'off'})
        for item in read.data['items']:
            self.assertEqual(set(item), {'name', 'state', 'unlocks', 'command'})
            self.assertTrue(item['command'].startswith('admin.py ') and item['command'].endswith(' on --actor OPERATOR'))
        self.set_switches(review_workflow_writes=True, checkpoint_provenance_writes=True,
                          web_lifecycle_mark_writes='yes')
        states = {item['name']: item['state']
                  for item in harness.request('GET', path, token=people['coord1']['token']).data['items']}
        self.assertEqual(states, {'review_workflow_writes': 'on', 'checkpoint_provenance_writes': 'on',
                                  'web_lifecycle_mark_writes': 'unreadable'})
        # No path, value or actor travels.
        text = json.dumps(harness.request('GET', path, token=people['coord1']['token']).data)
        for secret in (str(self.root), 'deployment.private.json', 'yes', 'ops'):
            self.assertNotIn(secret, text)
        self.assertEqual(harness.request('GET', path, token=people['coord1_agent']['token']).status, 200)
        for name in ('contrib', 'viewer', 'coord2_agent'):
            self.assertEqual(harness.request('GET', path, token=people[name]['token']).status, 403, name)
        harness.create_account(self.owner_token, 'switch-outsider', 'rec-password-1')
        outsider, _, _ = harness.login('switch-outsider', 'rec-password-1')
        self.assertEqual(harness.request('GET', path, token=outsider).status, 404)
        self.assertEqual(harness.request('GET', path).status, 401)


class StrictWebCoordinatorGapsTests(_GapsMixin, unittest.TestCase):
    """Against tools/strict_canonical_endpoint.py, on every platform."""

    def setUp(self):
        super().setUp()
        self.pid = 'pp'
        root_holder = {}

        class Harness(fixes.Harness):
            def make_backend(h):
                root = h.tmp / 'canonical'
                (root / self.pid / '.beads').mkdir(parents=True, exist_ok=True)
                (root / self.pid / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
                root_holder['root'] = root
                return http_service.EndpointBackend(sys.executable, str(STRICT_ENDPOINT), str(root), service=h.service)

        self.harness = Harness(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness._stop_server)
        self.root = root_holder['root']
        self.owner_token = self.harness.admin_token()
        reg = self.harness.request('POST', '/v1/projects', {'project_id': self.pid, 'name': 'Gaps'}, token=self.owner_token)
        self.assertEqual(reg.status, 201, reg.data)

    def rows(self):
        path = self.root / 'canonical.json'
        return json.loads(path.read_text(encoding='utf-8'))['rows'] if path.exists() else []

    def row_count(self):
        return len(self.rows()) - sum(1 for r in self.rows() if r.get('project_id') == 'qq')

    def foreign_parent(self):
        # The emulated store holds every project's rows in one file; a row of project qq says so, as the
        # canonical read's project filter expects (EndpointBackend.get_task).
        path = self.root / 'canonical.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        state['rows'].append({'id': 'qq-foreign-1', 'project_id': 'qq', 'title': 'Foreign', 'description': '',
                              'status': 'open', 'issue_type': 'task', 'labels': [], 'comments': [],
                              'dependencies': [], 'priority': 2})
        path.write_text(json.dumps(state), encoding='utf-8')
        return 'qq-foreign-1'

    def parent_of(self, task):
        row = next(r for r in self.rows() if r['id'] == task)
        return next((d['depends_on_id'] for d in row.get('dependencies') or [] if d.get('type') == 'parent-child'), None)

    def children_of(self, parent):
        return [r['id'] for r in self.rows() if self.parent_of(r['id']) == parent and r['title'] == 'A child']

    def set_switches(self, **values):
        document = {'password': 'x'}
        document.update(values)
        (self.root / 'deployment.private.json').write_text(json.dumps(document), encoding='utf-8')

    def test_a_coordinator_files_a_child_task_and_nobody_else_does(self):
        self.run_children_suite()

    def test_the_switches_read_names_states_and_commands_only(self):
        self.run_switches_suite()

    def test_the_in_process_backend_answers_501(self):
        backend = http_service.InProcessBackend.__new__(http_service.InProcessBackend)
        self.assertNotIn('tasks.create-child', http_service.InProcessBackend.ROUTES)
        self.assertIsNone(getattr(backend, 'switches', None))


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeWebCoordinatorGapsTests(_GapsMixin, RulesHelpers, unittest.TestCase):
    """Against the real endpoint.py and pinned native bd: the endpoint's own authority re-check, the merge slot
    and a decision's required headings (native validation)."""

    def setUp(self):
        super().setUp()
        self.pid = 'pp'
        root = self.root

        class Harness(fixes.Harness):
            def make_backend(h):
                return http_service.EndpointBackend(sys.executable, str(KIT / 'endpoint.py'), str(root), service=h.service)

        self.harness = Harness(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness._stop_server)
        self.owner_token = self.harness.admin_token()
        reg = self.harness.request('POST', '/v1/projects', {'project_id': self.pid, 'name': 'Native Gaps'},
                                   token=self.owner_token)
        self.assertEqual(reg.status, 201, reg.data)

    def native_rows(self):
        result = self.r.bd('export', '--all')
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def row_count(self):
        return len(self.native_rows())

    def foreign_parent(self):
        # Each project is its own tracker: another project's task id is simply not in this one.
        return 'qq-1'

    def parent_of(self, task):
        row = next(r for r in self.native_rows() if r['id'] == task)
        return next((d.get('depends_on_id') for d in row.get('dependencies') or [] if d.get('type') == 'parent-child'), None)

    def children_of(self, parent):
        return [r['id'] for r in self.native_rows() if r.get('title') == 'A child' and self.parent_of(r['id']) == parent]

    def set_switches(self, **values):
        self.deployment(operators=['ops'], coordinator_writes=True, **values)

    def test_native_a_coordinator_files_a_child_task_and_nobody_else_does(self):
        self.deployment(operators=['ops'], coordinator_writes=True)
        people, parent = self.run_children_suite()
        coordinator = people['coord1']['token']
        # The merge slot takes no child: it is not a task, so it reads as absent here too.
        slot = self.child(coordinator, 'pp-merge-slot', {'title': 'x', 'description': 'x', 'type': 'task'}, 'child-slot')
        self.assertEqual(self.error(slot), ABSENT)
        # A decision needs create-child's required headings (native validation), and says which.
        bare = self.child(coordinator, parent, {'title': 'Pick one', 'description': 'No headings.', 'type': 'decision'},
                          'child-decision-bare')
        self.assertEqual(bare.status, 422, bare.data)
        self.assertIn('missing required sections', bare.data['error']['detail'])
        # The endpoint's detail bound can cut the kit's heading sentence off (real bd, 8f7f085), so it is
        # given whole in `problems`.
        self.assertEqual(bare.data['error']['problems'],
                         ['Required decision section headers: ## Decision, ## Rationale, ## Alternatives Considered.'])
        full = self.child(coordinator, parent, {'title': 'Pick one', 'type': 'decision', 'description':
                          '## Decision\nB.\n\n## Rationale\nCheaper.\n\n## Alternatives Considered\nA.\n'},
                          'child-decision-full')
        self.assertEqual(full.status, 201, full.data)

    def test_native_the_switches_read_names_states_and_commands_only(self):
        self.run_switches_suite()


class WebGapsTests(unittest.TestCase):
    """The task page's child form and the settings switches panel: shown on the server's word only, text only."""

    def run_web(self, module, code, payload):
        import shutil
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views' / module).as_uri(), json.dumps(payload))
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_the_child_form_follows_the_server_and_names_a_decisions_headings(self):
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const ctx={href:(p)=>'#'+p,api:{}};
const shown=view.childForm(ctx,{id:'pp',can_coordinate:true},{id:'t-1'});
const hidden=[view.childForm(ctx,{id:'pp',can_coordinate:false},{id:'t-1'}),view.childForm(ctx,{id:'pp'},{id:'t-1'}),
  view.childForm(ctx,{id:'pp',can_coordinate:true,archived:true},{id:'t-1'})];
const type=shown.querySelectorAll('select')[0];const hint=shown.querySelectorAll('p')[0];
const before=hint.hidden;type.value='decision';type.listeners.change[0]();
console.log(JSON.stringify({hidden:hidden.map((x)=>x===null),before,after:hint.hidden,hint:hint.textContent,
  types:view.CHILD_TYPES,headings:view.DECISION_HEADINGS,options:Array.from(type.querySelectorAll('option')).map((o)=>o.textContent)}));"""
        out = self.run_web('children.js', code, {})
        import coordination
        self.assertEqual(out['hidden'], [True, True, True])
        self.assertEqual((out['before'], out['after']), (True, False))
        self.assertEqual(out['types'], list(coordination.CHILD_TYPES))
        self.assertEqual(out['options'], list(coordination.CHILD_TYPES))
        self.assertEqual(out['headings'], list(coordination.REQUIRED_SECTIONS['decision']))
        self.assertIn('## Decision, ## Rationale, ## Alternatives Considered', out['hint'])

    def test_the_switches_panel_follows_the_server_and_renders_text(self):
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const input=JSON.parse(process.argv[3]);
const ctx={href:(p)=>'#'+p,api:{switches:async()=>{if(input.fail)throw Object.assign(new Error('x'),{status:input.fail});return input.answer;}}};
const shown=view.switchesPanel(ctx,{id:'pp',can_coordinate:true});
const none=view.switchesPanel(ctx,{id:'pp',can_coordinate:false});
await new Promise((r)=>setTimeout(r,0));await new Promise((r)=>setTimeout(r,0));
console.log(JSON.stringify({text:shown.textContent,hidden:shown.hidden,none:none.hidden,
  scripts:shown.querySelectorAll('script').length+shown.querySelectorAll('img').length}));"""
        answer = {'items': [
            {'name': 'review_workflow_writes', 'state': 'off', 'unlocks': '<script>x</script>', 'command': 'admin.py review-writes on --actor OPERATOR'},
            {'name': 'checkpoint_provenance_writes', 'state': 'on', 'unlocks': 'u', 'command': 'admin.py checkpoint-provenance-writes on --actor OPERATOR'},
            {'name': 'web_lifecycle_mark_writes', 'state': 'unreadable', 'unlocks': 'u', 'command': 'admin.py web_lifecycle_mark_writes on --actor OPERATOR'}]}
        out = self.run_web('switches.js', code, {'answer': answer})
        self.assertTrue(out['none'])
        self.assertFalse(out['hidden'])
        self.assertEqual(out['scripts'], 0)
        self.assertIn('<script>x</script>', out['text'])
        self.assertIn('admin.py review-writes on --actor OPERATOR', out['text'])
        self.assertNotIn('admin.py checkpoint-provenance-writes', out['text'])   # an on switch needs no command
        self.assertIn('unreadable: an operator repairs the setting', out['text'])
        for status in (403, 404, 501):
            self.assertTrue(self.run_web('switches.js', code, {'answer': answer, 'fail': status})['hidden'], status)


if __name__ == '__main__':
    unittest.main()
