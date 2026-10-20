"""The per-project claiming setting (kittrial-5bb.352).

Unit tests that run everywhere: the record (absent/damaged/written), the one
claimability predicate, the work/wait/next sentences, both claim gates' shared
sentence, and the backup entry shape. The real-bd end-to-end tests are in
``test_claiming_real_bd.py``.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import claiming
import work
import waiting
try:
    import endpoint
except ImportError:  # endpoint imports fcntl (POSIX-only); the kit itself runs on Windows too
    endpoint = None
import http_service


def free_row(task_id='p-1', **extra):
    row = {'id': task_id, 'title': 'a free task', 'issue_type': 'task', 'status': 'open',
           'assignee': None, 'labels': [], 'comments': []}
    row.update(extra)
    return row


def held_row(task_id='p-2', holder='worker-a', **extra):
    row = free_row(task_id, status='in_progress', assignee=holder)
    row.update(extra)
    return row


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()


class RecordTests(ProjectCase):
    def test_absent_means_open_today_behaviour(self):
        state = claiming.read(self.project)
        self.assertEqual(('open', 'default'), (state['claiming'], state['source']))
        self.assertEqual('open', claiming.mode(self.project))
        self.assertFalse(claiming.is_restrictive(self.project))

    def test_a_write_records_who_when_and_by_which_route(self):
        first = claiming.write(self.project, 'assigned', 'owner-1', via='web',
                               at='2026-10-20T17:00:00Z')
        self.assertEqual('assigned', first['claiming'])
        self.assertEqual(('owner-1', 'web', '2026-10-20T17:00:00Z'),
                         (first['set_by'], first['set_via'], first['set_at']))
        second = claiming.write(self.project, 'open', 'operator-1', via='host',
                                at='2026-10-20T18:00:00Z')
        self.assertEqual('open', claiming.mode(self.project))
        self.assertEqual({'claiming': 'assigned', 'set_by': 'owner-1',
                          'set_at': '2026-10-20T17:00:00Z', 'set_via': 'web'},
                         second['previous'])
        self.assertEqual('assigned', second['history'][-1]['claiming'])
        on_disk = json.loads((self.project / claiming.FILENAME).read_text(encoding='utf-8'))
        self.assertEqual('open', on_disk['claiming'])
        self.assertEqual(1, len(on_disk['history']))
        self.assertLessEqual(len(on_disk['history']), claiming.HISTORY_LIMIT)

    def test_a_write_refuses_a_value_this_kit_does_not_use(self):
        for value in ('maybe', None, 1, ''):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    claiming.write(self.project, value, 'owner-1')
        self.assertFalse((self.project / claiming.FILENAME).exists())

    def test_a_damaged_file_reads_restrictive_with_a_warning_never_open(self):
        for text in ('not json', '[]', '{}', '{"schema_version":1,"claiming":"maybe"}',
                     '{"schema_version":1,"claiming":"open"}'):
            with self.subTest(text=text[:20]):
                (self.project / claiming.FILENAME).write_text(text, encoding='utf-8')
                state = claiming.read(self.project)
                self.assertEqual(('assigned', 'damaged'), (state['claiming'], state['source']))
                self.assertTrue(state['warning'])
                self.assertTrue(claiming.is_restrictive(self.project))

    def test_a_damaged_file_is_backed_up_verbatim_and_restores_damaged(self):
        for raw in (b'not json at all', b'{"claiming":"open"}\n', b'\xff\xfe\x00broken'):
            with self.subTest(raw=raw[:12]):
                (self.project / claiming.FILENAME).write_bytes(raw)
                backup = claiming.backup_record(self.project)
                self.assertIsNotNone(backup)
                claiming.restore_record(backup)
                # Write the entry through the same branch restore_coordination uses.
                target = self.project / claiming.FILENAME
                if set(backup) == {'text'}:
                    target.write_text(backup['text'], encoding='utf-8', newline='')
                elif set(backup) == {'base64'}:
                    import base64
                    target.write_bytes(base64.b64decode(backup['base64'], validate=True))
                else:
                    from coordination import atomic
                    atomic(target, backup)
                self.assertEqual('assigned', claiming.mode(self.project))
                self.assertEqual(raw, target.read_bytes())

    def test_a_valid_record_backs_up_and_restores(self):
        written = claiming.write(self.project, 'assigned', 'owner-1', at='2026-10-20T17:00:00Z')
        backup = claiming.backup_record(self.project)
        self.assertEqual(written['claiming'], backup['claiming'])
        self.assertEqual('assigned', claiming.restore_record(backup)['claiming'])
        with self.assertRaises(ValueError):
            claiming.restore_record({'claiming': 'maybe'})


class PredicateTests(unittest.TestCase):
    def test_open_unassigned_is_claimable_only_in_an_open_project(self):
        self.assertTrue(work.claimable('task', 'open', None, None, 'none'))
        self.assertTrue(work.claimable('task', 'open', None, None, 'none', claiming='open'))
        self.assertFalse(work.claimable('task', 'open', None, None, 'none', claiming='assigned'))
        # A held task is not claimable under either value; starting it is the claim
        # route's own separate rule.
        self.assertFalse(work.claimable('task', 'open', 'worker-a', None, 'none', claiming='open'))
        self.assertFalse(work.claimable('task', 'open', 'worker-a', None, 'none', claiming='assigned'))
        # The other bounded exclusions are unchanged.
        self.assertFalse(work.claimable('epic', 'open', None, None, 'none', claiming='open'))
        self.assertFalse(work.claimable('task', 'open', None, None, 'none', claiming='open',
                                        open_dependencies=['p-9']))


class ListingTests(ProjectCase):
    def test_work_offer_vanishes_and_the_empty_answer_says_who_assigns(self):
        rows = [free_row()]
        opened = work.queue(rows, 'worker-a', [], journal=self.project)
        self.assertEqual(('open', True, 1),
                         (opened['claiming'], opened['items'][0]['claimable'], opened['total']))
        self.assertNotIn('empty_message', opened)
        claiming.write(self.project, 'assigned', 'owner-1', at='2026-10-20T17:00:00Z')
        # The SAME row list (the tracker read cache's answer) is reused: the setting is
        # read fresh from the project directory, so the answer changes anyway.
        assigned = work.queue(rows, 'worker-a', ['--claimable'], journal=self.project)
        self.assertEqual(('assigned', 0), (assigned['claiming'], assigned['total']))
        self.assertEqual(claiming.EMPTY_SENTENCE, assigned['empty_message'])

    def test_a_damaged_setting_is_reported_on_the_queue(self):
        (self.project / claiming.FILENAME).write_text('nonsense', encoding='utf-8')
        page = work.queue([free_row()], 'worker-a', ['--claimable'], journal=self.project)
        self.assertEqual('assigned', page['claiming'])
        self.assertTrue(page['claiming_warning'])
        self.assertEqual(0, page['total'])

    def test_wait_carries_the_sentence_only_when_empty(self):
        empty = waiting.worker_view([], claiming='assigned', empty_message=claiming.EMPTY_SENTENCE)
        self.assertEqual(claiming.EMPTY_SENTENCE, empty['empty_message'])
        self.assertEqual('assigned', empty['claiming'])
        # A row whose move is not the caller's is not a move: the answer is still empty.
        other = waiting.worker_view([dict(free_row(), whose_move='reviewer')],
                                    claiming='assigned', empty_message=claiming.EMPTY_SENTENCE)
        self.assertEqual(claiming.EMPTY_SENTENCE, other['empty_message'])
        row = dict(free_row(), whose_move='you')
        busy = waiting.worker_view([row], claiming='assigned', empty_message=claiming.EMPTY_SENTENCE)
        self.assertNotIn('empty_message', busy)

    def test_the_next_action_names_the_coordinators_not_a_claim(self):
        task = dict(free_row(), status='open', review_state='none')
        action = http_service.next_action(task, claiming='assigned')
        self.assertEqual(('coordinator', claiming.EMPTY_SENTENCE), (action['who'], action['text']))
        task_held = dict(free_row(), review_state='none', assignee='worker-a')
        action = http_service.next_action(task_held, claiming='assigned')
        self.assertEqual(('assignee', 'Deliver a contribution'), (action['who'], action['text']))


class GateTests(ProjectCase):
    # The two endpoint routes need endpoint.py, which imports fcntl (POSIX-only); the HTTP
    # backend below and the record tests run everywhere (kittrial-5bb.352 Windows CI).
    POSIX = unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')

    @POSIX
    def test_the_host_claim_guard_refuses_only_an_unassigned_claim(self):
        claiming.write(self.project, 'assigned', 'owner-1')
        with self.assertRaises(ValueError) as caught:
            endpoint._guard_claiming_on_claim(self.project, free_row())
        self.assertIn(claiming.CLAIM_REFUSAL, str(caught.exception))
        # Already assigned: its holder may start it; bd decides anybody else.
        endpoint._guard_claiming_on_claim(self.project, held_row(holder='worker-a'))
        claiming.write(self.project, 'open', 'owner-1')
        endpoint._guard_claiming_on_claim(self.project, free_row())

    @POSIX
    def test_an_assignee_is_recognised_in_every_bd_spelling(self):
        # kittrial-5bb.352 review item B1: every spelling bd accepts, including the short
        # joined forms and an `-a` at the end of a run of boolean shorthands (pflag
        # clusters -q/-v/-h with a value-taking -a), and nothing after `--`.
        self.assertFalse(endpoint._names_assignee(['update', 'p-1', '--claim', '--json']))
        for args in (['update', 'p-1', '--assignee', 'worker-a'],
                     ['update', 'p-1', '--assignee=worker-a'],
                     ['update', 'p-1', '-a', 'worker-a'],
                     ['update', 'p-1', '-a=worker-a'],
                     ['update', 'p-1', '-aworker-a'],
                     ['update', 'p-1', '-qa', 'worker-a'],
                     ['update', 'p-1', '-qaW'],
                     ['update', 'p-1', '-va=W'],
                     ['update', 'p-1', '-qva', 'W'],
                     ['create', 'work', '--assignee=worker-a'],
                     ['create', 'work', '-qa', 'worker-a']):
            with self.subTest(args=args):
                self.assertTrue(endpoint._names_assignee(args))
        self.assertFalse(endpoint._names_assignee(
            ['update', 'p-1', '--', '--assignee', 'worker-a']))
        # A value that begins with `-` is a value, not a flag: pflag gives `-qa` to
        # --title here, so it does not assign the row.
        self.assertFalse(endpoint._names_assignee(
            ['update', 'p-1', '--title', '-qa', 'worker-a']))

    @POSIX
    def test_a_status_shorthand_is_recognised_in_every_bd_spelling(self):
        # kittrial-5bb.352 review item B2: `_claims_the_row` drove BOTH the claiming and
        # the open-dependency guards, and its `-s` scan missed the boolean-cluster and
        # `=value` forms. Every form below is one bd accepts for in_progress.
        for args in (['update', 'p-1', '--status', 'in_progress'],
                     ['update', 'p-1', '--status=in_progress'],
                     ['update', 'p-1', '-s', 'in_progress'],
                     ['update', 'p-1', '-s=in_progress'],
                     ['update', 'p-1', '-sin_progress'],
                     ['update', 'p-1', '-qs', 'in_progress'],
                     ['update', 'p-1', '-qsin_progress'],
                     ['update', 'p-1', '-q', '-sin_progress']):
            with self.subTest(args=args):
                self.assertTrue(endpoint._claims_the_row(args))
        # Not a claim: another status, and a value that names in_progress in notes.
        for args in (['update', 'p-1', '--status', 'open'],
                     ['update', 'p-1', '--status=deferred'],
                     ['update', 'p-1', '--notes', 'in_progress'],
                     ['update', 'p-1', '--title', '-sin_progress']):
            with self.subTest(args=args):
                self.assertFalse(endpoint._claims_the_row(args))

    @POSIX
    def test_an_assignment_through_bd_is_refused_under_assigned_unless_authorised(self):
        # A plain worker cannot hand work out (B1); a listed operator can; and the live
        # web CAP_COORDINATE answer is the other authority the setting names.
        import tempfile
        from unittest.mock import patch
        root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        (root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['operator-1']}), encoding='utf-8')
        project = root / 'projects' / 'pp'
        project.mkdir(parents=True)
        claiming.write(project, 'assigned', 'owner-1')
        attempts = (['update', 'p-1', '--assignee', 'worker-a'],
                    ['update', 'p-1', '--claim', '--assignee', 'worker-a'],
                    ['update', 'p-1', '--status', 'in_progress', '-a', 'worker-a'],
                    ['update', 'p-1', '--assignee=worker-a'],
                    ['update', 'p-1', '-a=worker-a'],
                    ['create', 'work', '--assignee', 'worker-a'])
        for args in attempts:
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as caught:
                    endpoint._guard_claiming_on_assignment(root, project, 'worker-a', None, {}, args)
                self.assertIn(claiming.CLAIM_REFUSAL, str(caught.exception))
        # A listed installation operator is the host hand-out case: allowed.
        endpoint._guard_claiming_on_assignment(
            root, project, 'operator-1', None, {}, ['update', 'p-1', '--assignee', 'worker-a'])
        # A live coordinate descriptor is the web hand-out case: allowed only when the
        # live check says yes, and never inferred from the request itself.
        with patch.object(endpoint, 'verified_web_authority', return_value=True) as check:
            endpoint._guard_claiming_on_assignment(
                root, project, 'usr_0123456789abcdef', object(), {'authority': {}},
                ['update', 'p-1', '--assignee', 'worker-a'])
            check.assert_called_once()
        with patch.object(endpoint, 'verified_web_authority', return_value=False):
            with self.assertRaises(ValueError):
                endpoint._guard_claiming_on_assignment(
                    root, project, 'usr_0123456789abcdef', object(), {'authority': {}},
                    ['update', 'p-1', '--assignee', 'worker-a'])
        # Under 'open' an explicit assignment is unchanged.
        claiming.write(project, 'open', 'owner-1')
        endpoint._guard_claiming_on_assignment(
            root, project, 'worker-a', None, {}, ['update', 'p-1', '--assignee', 'worker-a'])

    def test_the_agent_attention_view_carries_the_empty_sentence(self):
        # kittrial-5bb.352 review item S1: the sentence the attention read computes must
        # reach /v1/agents/me/next, which builds its answer through this view.
        attention = {'state': 'idle', 'summary': 'nothing', 'counts': {}, 'actions': [],
                     'truncated': False, 'snapshot_truncated': False,
                     'own_tasks_truncated': False, 'actions_truncated': False,
                     'computed_at': None, 'empty_message': claiming.EMPTY_SENTENCE}
        view = http_service.ApiHandler._agent_attention_view({'id': 'agent-1'}, attention)
        self.assertEqual(claiming.EMPTY_SENTENCE, view['empty_message'])
        attention.pop('empty_message')
        self.assertNotIn('empty_message',
                         http_service.ApiHandler._agent_attention_view({'id': 'agent-1'}, attention))

    def test_the_http_backend_remembers_the_setting_and_reads_it_fresh(self):
        service = type('S', (), {'state': {'canonical': {}}, 'store': object()})()
        backend = http_service.InProcessBackend(service)
        self.assertEqual('open', backend.claiming_state('pp')['claiming'])
        record = backend.set_claiming('pp', 'assigned', 'owner-1', via='web')
        self.assertEqual(('assigned', 'owner-1', 'web'),
                         (record['claiming'], record['set_by'], record['set_via']))
        self.assertEqual('assigned', backend.claiming_state('pp')['claiming'])
        backend.set_claiming('pp', 'open', 'owner-1')
        self.assertEqual('open', backend.claiming_state('pp')['claiming'])
        self.assertEqual('assigned', backend.claiming_state('pp')['history'][-1]['claiming'])


class BackupValidationTests(ProjectCase):
    def test_the_coordination_backup_accepts_the_record_and_the_damaged_forms(self):
        import admin
        claiming.write(self.project, 'assigned', 'owner-1')
        record = claiming.backup_record(self.project)
        admin.validate_coordination_files({'.claiming.json': record})
        admin.validate_coordination_files({'.claiming.json': {'text': 'not json'}})
        admin.validate_coordination_files({'.claiming.json': {'base64': 'bm90IGpzb24='}})
        for bad in ({'claiming': 'maybe'}, {'text': 1}, {'base64': '***'}, [], 'x'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    admin.validate_coordination_files({'.claiming.json': bad})


class BackupRestoreTests(unittest.TestCase):
    """The setting travels in the coordination backup and comes back with it."""

    def setUp(self):
        import sys as _sys
        import types
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'projects' / 'source'
        self.destination = self.root / 'projects' / 'destination'
        self.source.mkdir(parents=True)
        self.destination.mkdir()
        (self.root / 'backups' / 'source').mkdir(parents=True)
        self.bundle = self.root / 'backups' / 'source.coordination.json'
        fake = types.SimpleNamespace(flock=lambda *a, **k: None, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        self.patch_fcntl = patch.dict(_sys.modules, {'fcntl': fake})
        self.patch_fcntl.start()
        self.addCleanup(self.patch_fcntl.stop)
        import admin
        for name, value in (('backup_lifecycle_void_family', {}),
                            ('restore_lifecycle_void_family', False)):
            patcher = patch.object(admin, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_an_assigned_setting_survives_backup_and_restore(self):
        from unittest.mock import patch
        import admin
        claiming.write(self.source, 'assigned', 'owner-1', via='web',
                       at='2026-10-20T17:00:00Z')
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'source')
        sidecar = json.loads(self.bundle.read_text(encoding='utf-8'))
        self.assertEqual('assigned', sidecar['files']['.claiming.json']['claiming'])
        self.assertEqual('owner-1', sidecar['files']['.claiming.json']['set_by'])
        admin.restore_coordination(self.root, 'source', 'destination')
        restored = claiming.read(self.destination)
        self.assertEqual(('assigned', 'owner-1', 'web'),
                         (restored['claiming'], restored['set_by'], restored['set_via']))

    def test_a_damaged_setting_restores_damaged_not_open(self):
        from unittest.mock import patch
        import admin
        (self.source / claiming.FILENAME).write_text('not json', encoding='utf-8')
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'source')
        sidecar = json.loads(self.bundle.read_text(encoding='utf-8'))
        self.assertEqual({'text': 'not json'}, sidecar['files']['.claiming.json'])
        admin.restore_coordination(self.root, 'source', 'destination')
        restored = claiming.read(self.destination)
        self.assertEqual(('assigned', 'damaged'), (restored['claiming'], restored['source']))


@unittest.skipIf(os.name == 'nt', 'the host command takes the project lock with fcntl')
class HostCommandTests(unittest.TestCase):
    """The host operator tool: read for an operator, set only on the allowlist."""

    def setUp(self):
        import subprocess
        self.subprocess = subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'projects' / 'pp'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'backups' / 'pp').mkdir(parents=True)
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['operator-1']}), encoding='utf-8')

    def admin(self, *args):
        return self.subprocess.run(
            [sys.executable, str(KIT / 'admin.py'), '--root', str(self.root), 'project-setting'] + list(args),
            capture_output=True, text=True, encoding='utf-8', timeout=120)

    def test_the_host_tool_reads_open_and_sets_only_for_an_operator(self):
        read = self.admin('pp')
        self.assertEqual(0, read.returncode, read.stderr)
        self.assertEqual('open', json.loads(read.stdout)['claiming'])
        refused = self.admin('pp', '--claiming', 'assigned', '--actor', 'worker-1')
        self.assertNotEqual(0, refused.returncode, refused.stdout)
        self.assertIn('not a server-side configured operator', refused.stderr)
        self.assertEqual('open', claiming.mode(self.project))
        allowed = self.admin('pp', '--claiming', 'assigned', '--actor', 'operator-1')
        self.assertEqual(0, allowed.returncode, allowed.stderr)
        self.assertEqual('assigned', claiming.read(self.project)['claiming'])
        self.assertEqual('host', claiming.read(self.project)['set_via'])


if __name__ == '__main__':
    unittest.main()
