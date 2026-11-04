"""Retired task-holder flag on Lanes and optional claim release on Agents (kittrial-5bb.510).

Covers:
1. Flagging tasks held by a retired holder on ``work lanes`` (CLI), ``GET /v1/projects/{pid}/lanes``,
   and the web Lanes panel (``web/js/views/project.js``):
   - (a) an agent whose record is disabled (``enabled: false``);
   - (b) an agent no longer granted on the project (``project not in agent['projects']``);
   - (c) an actor with no live registration (neither in ``sessions.read_registry(path)`` at
     ``sessions.py:38-60``, nor a live agent/credential/member in ``http-state.json``);
   - Addition 1 named case: a live SSH lane registered ONLY in the project's host session
     registry (``sessions.read_registry(path)`` at ``sessions.py:38-60``), with no web agent
     and no worker credential, holding a task, is NOT flagged (tested on real ``bd`` with
     ``HOME`` and ``TMPDIR`` under ``~/work/t510``).
2. ``POST /v1/agents/{aid}/release-claims`` (``http_service.py:10565-10683``) ``tasks`` narrowing:
   - releases the intersection of ``tasks`` and what the dry run would release, never widening;
   - Addition 2 named cases:
     * a task held by another actor in ``tasks`` is refused ``400`` and nothing is released;
     * an empty list (``tasks: []``) releases nothing;
     * omitting ``tasks`` behaves as before;
   - owner-only check (``http_service.py:10608`` ``self.service.get_agent`` -> ``http_auth.py:3010-3019``).
3. Disabling an agent on the Agents page (``web/js/views/agents.js``):
   - owner-only;
   - Addition 3 named case: disable completes first; declining the release (``Keep claims``)
     leaves the agent disabled, leaves its claims held, and leaves the ``holder_retired`` flag
     on the Lanes page;
   - accepting the release (``Release its claims``) sends ``tasks`` from the dry run and
     releases only what the dry run showed.
"""
from datetime import datetime, timezone
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

if not os.environ.get('ORCHESTRA_BD_BIN'):
    for _candidate in ('/opt/orchestra-bd/bin/bd', str(KIT.parent / '.tmp' / 'runtime' / 'bin' / 'bd')):
        if Path(_candidate).is_file():
            os.environ['ORCHESTRA_BD_BIN'] = _candidate
            break

import host_reads
import lanes
import sessions
import test_bd_label_aliases as rb
import test_http_web
from test_http_service import ServerHarness

if rb.BD is None and os.environ.get('ORCHESTRA_BD_BIN'):
    rb.BD = Path(os.environ['ORCHESTRA_BD_BIN'])


FIXED_NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
LIVE_REQ = '11111111-1111-1111-1111-111111111111'
LIVE_ACTOR = 'session-' + LIVE_REQ
RET_REQ = '22222222-2222-2222-2222-222222222222'
RET_ACTOR = 'session-' + RET_REQ


def _task_row(task_id, assignee, status='in_progress'):
    return {
        'id': task_id,
        'title': 'Task ' + task_id,
        'status': status,
        'issue_type': 'task',
        'assignee': assignee,
        'labels': [],
        'dependencies': [],
        'comments': [
            {
                'id': 'c-' + task_id,
                'author': assignee,
                'text': 'working on ' + task_id,
                'created_at': '2026-09-15T11:55:00Z',
            }
        ],
    }


class RetiredHolderUnitAndWebTests(ServerHarness):
    """Unit, HTTP route, and Node UI tests for retired-holder flagging and claim release."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import http_auth
        cls._orig_scrypt_n = http_auth.SCRYPT_N
        cls._orig_kwdefaults = dict(http_auth.hash_password.__kwdefaults__)
        http_auth.SCRYPT_N = 16
        http_auth.hash_password.__kwdefaults__['n'] = 16

    @classmethod
    def tearDownClass(cls):
        import http_auth
        http_auth.SCRYPT_N = cls._orig_scrypt_n
        http_auth.hash_password.__kwdefaults__.update(cls._orig_kwdefaults)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.olive_id = self.create_account(self.admin, 'olive', 'olive-password-1')
        self.carl_id = self.create_account(self.admin, 'carl', 'carl-password-1')
        self.olive = self.login('olive', 'olive-password-1')[0]
        self.carl = self.login('carl', 'carl-password-1')[0]
        self.project = self.create_project(self.olive, 'Alpha')
        added = self.request(
            'PUT',
            '/v1/projects/%s/members/%s' % (self.project, self.carl_id),
            {'role': 'contributor'},
            token=self.olive,
        )
        self.assertEqual(200, added.status, added.data)

    def wire_in_process_assign(self):
        """Wire ``tasks.assign`` on InProcessBackend so release-claims can execute in-process."""
        backend = self.backend
        orig_dispatch = backend._dispatch

        def dispatch(route, principal, project_id, payload):
            if route == 'tasks.assign':
                tid = payload['task_id']
                expected = payload.get('expected_assignee')
                new_assignee = payload.get('assignee')
                row = backend._task(project_id, tid)
                if expected is not None and row.get('assignee') != expected:
                    from http_auth import conflict
                    raise conflict('Assignee changed')
                row['assignee'] = new_assignee
                if new_assignee is None and row.get('status') == 'in_progress':
                    row['status'] = 'open'
                elif new_assignee is not None and row.get('status') == 'open':
                    row['status'] = 'in_progress'
                return {'id': tid, 'assignee': new_assignee, 'status': row.get('status')}
            return orig_dispatch(route, principal, project_id, payload)

        backend._dispatch = dispatch
        if 'tasks.assign' not in backend.ROUTES:
            backend.ROUTES = tuple(backend.ROUTES) + ('tasks.assign',)

    def make_agent(self, name='WorkerAgent', token=None, projects=None, claim_limit=4):
        resp = self.request(
            'POST',
            '/v1/agents',
            {
                'name': name,
                'working_directory': '/home/olive/' + name.lower(),
                'projects': [self.project] if projects is None else projects,
                'claim_limit': claim_limit,
                'scopes': ['read', 'tasks', 'checkpoints', 'reviews'],
            },
            token=token or self.olive,
        )
        self.assertEqual(201, resp.status, resp.data)
        return resp.data['agent']['id'], resp.data['credential']['secret']

    def test_disabled_agent_holding_task_is_flagged_holder_retired_on_cli_and_web(self):
        """Case (a): an enabled agent is NOT flagged; disabling it flags its held task."""
        aid, secret = self.make_agent('DisabledCase')
        tid = self.create_task(self.olive, self.project, 'Held by agent').data['id']
        claimed = self.request(
            'POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, tid), {}, token=secret
        )
        self.assertEqual(200, claimed.status, claimed.data)

        # While enabled and granted on self.project, holder_retired is False.
        live_lanes = self.request('GET', '/v1/projects/%s/lanes' % self.project, token=self.olive)
        self.assertEqual(200, live_lanes.status, live_lanes.data)
        row = next(r for r in live_lanes.data['items'] if r['actor'] == aid)
        self.assertFalse(row['holder_retired'])
        self.assertNotIn('holder_retired', row['held'][0]['flags'])
        self.assertEqual(0, live_lanes.data['flags']['holder_retired'])

        # Disable the agent: now both CLI lanes.read and GET /v1/projects/{pid}/lanes flag it.
        dis = self.request(
            'POST', '/v1/agents/%s/disable' % aid, {}, token=self.olive, key='disable-agent-key-1'
        )
        self.assertEqual(200, dis.status, dis.data)
        self.assertFalse(dis.data['enabled'])

        cli_board = lanes.read(
            [_task_row(tid, aid)],
            self.olive_id,
            {'schema_version': 1, 'records': {}},
            [],
            now=FIXED_NOW,
            project=self.project,
            authority_state=self.service.state,
        )
        cli_row = next(r for r in cli_board['items'] if r['actor'] == aid)
        self.assertTrue(cli_row['holder_retired'])
        self.assertIn('holder_retired', cli_row['held'][0]['flags'])
        self.assertEqual([tid], cli_row['flag_tasks']['holder_retired'])
        self.assertEqual(1, cli_board['flags']['holder_retired'])

        web_lanes = self.request('GET', '/v1/projects/%s/lanes' % self.project, token=self.olive)
        self.assertEqual(200, web_lanes.status, web_lanes.data)
        web_row = next(r for r in web_lanes.data['items'] if r['actor'] == aid)
        self.assertTrue(web_row['holder_retired'])
        self.assertIn('holder_retired', web_row['held'][0]['flags'])
        self.assertEqual([tid], web_row['flag_tasks']['holder_retired'])
        self.assertEqual(1, web_lanes.data['flags']['holder_retired'])

    def test_agent_no_longer_granted_on_project_holding_task_is_flagged_holder_retired(self):
        """Case (b): an enabled agent whose project grant was removed is flagged holder_retired."""
        other_project = self.create_project(self.olive, 'Beta')
        aid, secret = self.make_agent('UngrantedCase', projects=[self.project, other_project])
        tid = self.create_task(self.olive, self.project, 'Held before ungrant').data['id']
        claimed = self.request(
            'POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, tid), {}, token=secret
        )
        self.assertEqual(200, claimed.status, claimed.data)

        # Remove self.project from the agent's projects while keeping the agent enabled.
        patched = self.request(
            'PATCH',
            '/v1/agents/%s' % aid,
            {'projects': [other_project]},
            token=self.olive,
            key='ungrant-project-key-1',
        )
        self.assertEqual(200, patched.status, patched.data)
        self.assertTrue(patched.data['enabled'])

        web_lanes = self.request('GET', '/v1/projects/%s/lanes' % self.project, token=self.olive)
        self.assertEqual(200, web_lanes.status, web_lanes.data)
        web_row = next(r for r in web_lanes.data['items'] if r['actor'] == aid)
        self.assertTrue(web_row['holder_retired'])
        self.assertIn('holder_retired', web_row['held'][0]['flags'])
        self.assertEqual([tid], web_row['flag_tasks']['holder_retired'])
        self.assertEqual(1, web_lanes.data['flags']['holder_retired'])

    def test_actor_with_no_live_registration_holding_task_is_flagged_holder_retired(self):
        """Case (c): an actor with no live session, agent, credential, or member account is flagged."""
        tid = self.create_task(self.olive, self.project, 'Held by vanished session').data['id']
        self.backend.state['tasks'][tid]['assignee'] = 'session-vanished-999'
        self.backend.state['tasks'][tid]['status'] = 'in_progress'

        web_lanes = self.request('GET', '/v1/projects/%s/lanes' % self.project, token=self.olive)
        self.assertEqual(200, web_lanes.status, web_lanes.data)
        web_row = next(r for r in web_lanes.data['items'] if r['actor'] == 'session-vanished-999')
        self.assertTrue(web_row['holder_retired'])
        self.assertIn('holder_retired', web_row['held'][0]['flags'])
        self.assertEqual([tid], web_row['flag_tasks']['holder_retired'])
        self.assertEqual(1, web_lanes.data['flags']['holder_retired'])

        # And a retired session record in .sessions.json is excluded by sessions.read_registry /
        # host_reads.coordination and flagged by lanes.read.
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp)
            (pdir / '.sessions.json').write_text(
                json.dumps({
                    'schema_version': 1,
                    'records': {
                        LIVE_REQ: {
                            'request_id': LIVE_REQ,
                            'name': 'live-ssh',
                            'actor': LIVE_ACTOR,
                            'created_at': '2026-09-15T10:00:00Z',
                        },
                        RET_REQ: {
                            'request_id': RET_REQ,
                            'name': 'retired-ssh',
                            'actor': RET_ACTOR,
                            'created_at': '2026-09-15T09:00:00Z',
                            'retired': True,
                        },
                    },
                    'owners': {
                        LIVE_ACTOR: 'person:olive',
                        RET_ACTOR: 'person:olive',
                    },
                }),
                encoding='utf-8',
            )
            reg = sessions.read_registry(pdir)
            coord_reply = host_reads.coordination({'action': 'host-coordination'}, pdir, ['olive'])
            coord = json.loads(coord_reply['stdout'])
            self.assertEqual([LIVE_ACTOR], [s['actor'] for s in coord['sessions']])
            board = lanes.read(
                [_task_row('t-live', LIVE_ACTOR), _task_row('t-ret', RET_ACTOR)],
                'olive',
                reg,
                [],
                now=FIXED_NOW,
                project=self.project,
                authority_state=self.service.state,
            )
            by_actor = {r['actor']: r for r in board['items']}
            self.assertFalse(by_actor[LIVE_ACTOR]['holder_retired'])
            self.assertTrue(by_actor[RET_ACTOR]['holder_retired'])
            self.assertEqual(1, board['flags']['holder_retired'])

    def test_release_claims_tasks_field_narrows_to_intersection_and_never_widens(self):
        """Addition 2: ``tasks`` narrows what release-claims releases to the dry-run set."""
        self.wire_in_process_assign()
        aid, secret = self.make_agent('NarrowAgent', claim_limit=4)
        t1 = self.create_task(self.olive, self.project, 'Claim 1').data['id']
        t2 = self.create_task(self.olive, self.project, 'Claim 2').data['id']
        self.assertEqual(
            200,
            self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, t1), {}, token=secret).status,
        )

        # Dry run sees only t1.
        dry = self.request(
            'POST', '/v1/agents/%s/release-claims' % aid, {'dry_run': True}, token=self.olive
        )
        self.assertEqual(200, dry.status, dry.data)
        shown = dry.data['planned'][0]['tasks']
        self.assertEqual([t1], shown)

        # Before the owner confirms release, the agent claims t2.
        self.assertEqual(
            200,
            self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, t2), {}, token=secret).status,
        )

        # Releasing with tasks=shown releases ONLY t1 and leaves t2 held by aid.
        rel = self.request(
            'POST',
            '/v1/agents/%s/release-claims' % aid,
            {'dry_run': False, 'tasks': shown},
            token=self.olive,
            key='rel-narrow-key-1',
        )
        self.assertEqual(200, rel.status, rel.data)
        self.assertEqual([t1], [r['task'] for r in rel.data['released']])
        self.assertIsNone(self.backend.state['tasks'][t1]['assignee'])
        self.assertEqual(aid, self.backend.state['tasks'][t2]['assignee'])

    def test_release_claims_refuses_400_when_task_is_held_by_another_actor_and_releases_nothing(self):
        """Addition 2 named case 1: a task held by another actor in ``tasks`` is refused 400; nothing released."""
        self.wire_in_process_assign()
        aid, secret = self.make_agent('StrictAgent', claim_limit=4)
        t_mine = self.create_task(self.olive, self.project, 'Agent task').data['id']
        t_carl = self.create_task(self.olive, self.project, 'Carl task').data['id']
        self.assertEqual(
            200,
            self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, t_mine), {}, token=secret).status,
        )
        self.assertEqual(
            200,
            self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, t_carl), {}, token=self.carl).status,
        )

        refused = self.request(
            'POST',
            '/v1/agents/%s/release-claims' % aid,
            {'dry_run': False, 'tasks': [t_mine, t_carl]},
            token=self.olive,
            key='rel-refuse-other-key',
        )
        self.assertEqual(400, refused.status, refused.data)
        # Neither t_mine nor t_carl was released!
        self.assertEqual(aid, self.backend.state['tasks'][t_mine]['assignee'])
        self.assertEqual(self.carl_id, self.backend.state['tasks'][t_carl]['assignee'])

    def test_release_claims_empty_tasks_list_releases_nothing(self):
        """Addition 2 named case 2: ``tasks: []`` releases nothing and leaves held tasks intact."""
        self.wire_in_process_assign()
        aid, secret = self.make_agent('EmptyListAgent', claim_limit=4)
        t1 = self.create_task(self.olive, self.project, 'Held task').data['id']
        self.assertEqual(
            200,
            self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, t1), {}, token=secret).status,
        )

        empty_rel = self.request(
            'POST',
            '/v1/agents/%s/release-claims' % aid,
            {'dry_run': False, 'tasks': []},
            token=self.olive,
            key='rel-empty-list-key',
        )
        self.assertEqual(200, empty_rel.status, empty_rel.data)
        self.assertEqual([], empty_rel.data['released'])
        self.assertEqual(0, empty_rel.data['candidate_total'])
        self.assertEqual(aid, self.backend.state['tasks'][t1]['assignee'])

    def test_release_claims_omitting_tasks_behaves_exactly_as_today(self):
        """Addition 2 named case 3: omitting ``tasks`` releases every held undelivered claim."""
        self.wire_in_process_assign()
        aid, secret = self.make_agent('OmitTasksAgent', claim_limit=4)
        t1 = self.create_task(self.olive, self.project, 'Held 1').data['id']
        t2 = self.create_task(self.olive, self.project, 'Held 2').data['id']
        for tid in (t1, t2):
            self.assertEqual(
                200,
                self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, tid), {}, token=secret).status,
            )

        rel = self.request(
            'POST', '/v1/agents/%s/release-claims' % aid, {}, token=self.olive, key='rel-omit-tasks-key'
        )
        self.assertEqual(200, rel.status, rel.data)
        self.assertEqual(sorted([t1, t2]), sorted(r['task'] for r in rel.data['released']))
        self.assertIsNone(self.backend.state['tasks'][t1]['assignee'])
        self.assertIsNone(self.backend.state['tasks'][t2]['assignee'])

    def test_release_claims_and_disable_are_owner_only(self):
        """Non-owner is refused 404 on both disable and release-claims (http_service.py:10608 -> http_auth.py:3010-3019)."""
        self.wire_in_process_assign()
        aid, secret = self.make_agent('OwnerOnlyAgent', claim_limit=2)
        t1 = self.create_task(self.olive, self.project, 'Owner only task').data['id']
        self.assertEqual(
            200,
            self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, t1), {}, token=secret).status,
        )

        refused_dis = self.request(
            'POST', '/v1/agents/%s/disable' % aid, {}, token=self.carl, key='carl-disable-key-1'
        )
        self.assertEqual(404, refused_dis.status, refused_dis.data)
        refused_rel = self.request(
            'POST',
            '/v1/agents/%s/release-claims' % aid,
            {'dry_run': False, 'tasks': [t1]},
            token=self.carl,
            key='carl-release-key-1',
        )
        self.assertEqual(404, refused_rel.status, refused_rel.data)
        self.assertEqual(aid, self.backend.state['tasks'][t1]['assignee'])

    def test_agents_page_disable_and_optional_release_claims_flow(self):
        """Addition 3 named case + web UI flow under Node against the real HTTP service."""
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        self.wire_in_process_assign()

        decline_aid, decline_secret = self.make_agent('DeclineAgent', claim_limit=2)
        release_aid, release_secret = self.make_agent('ReleaseAgent', claim_limit=4)
        t_decline = self.create_task(self.olive, self.project, 'Keep me held').data['id']
        t_release = self.create_task(self.olive, self.project, 'Release me').data['id']
        t_extra = self.create_task(self.olive, self.project, 'Assigned after dry run').data['id']

        self.assertEqual(
            200,
            self.request(
                'POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, t_decline), {}, token=decline_secret
            ).status,
        )
        self.assertEqual(
            200,
            self.request(
                'POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, t_release), {}, token=release_secret
            ).status,
        )
        # Allow direct in-process assignment of t_extra to release_aid in the test script:
        # wire_in_process_assign already supports tasks.assign, or we can expose a test hook or
        # let the script assign via a comment/hook. Wait: HTTP /v1/projects/{pid}/tasks/{tid}/assign
        # calls backend.task_assign on InProcessBackend. Let's also wire backend.task_assign!
        def task_assign(project_id, principal, task_id, body, idempotency_key, authorize=None):
            if callable(authorize):
                authorize()
            row = self.backend._task(project_id, task_id)
            row['assignee'] = body.get('assignee')
            row['status'] = 'in_progress' if body.get('assignee') else 'open'
            return {'task': dict(row), 'reconciled': False}

        self.backend.task_assign = task_assign

        web = KIT / 'web' / 'js'
        done = test_http_web.run_node_module(
            self,
            node,
            'await import(process.argv[1])',
            (KIT / 'tests' / 'web_retired_holder_agents.mjs').as_uri(),
            (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
            (web / 'api.js').as_uri(),
            (web / 'views' / 'agents.js').as_uri(),
            (web / 'views' / 'project.js').as_uri(),
            'http://127.0.0.1:%d' % self.port,
            self.project,
            'olive=%s=%s' % (self.olive_id, self.olive),
            'carl=%s=%s' % (self.carl_id, self.carl),
            t_extra,
        )
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])

        # 1. Non-owner sees neither Disable agent nor Release its claims.
        self.assertFalse(seen['nonOwnerHasDisable'])
        self.assertFalse(seen['nonOwnerHasRelease'])

        # 2. Addition 3 named case: disable completes first; declining the release ('Keep claims')
        #    leaves the agent disabled, leaves t_decline held, and leaves holder_retired on Lanes.
        self.assertTrue(seen['ownerHasDisableInitially'])
        self.assertEqual([t_decline], seen['declineClaimTasks'])
        self.assertTrue(seen['declineHasReleaseBtn'])
        self.assertTrue(seen['declineHasKeepBtn'])
        self.assertFalse(seen['afterDeclineHasReleaseBtn'])
        self.assertFalse(self.service.state['agents'][decline_aid]['enabled'])
        self.assertEqual(decline_aid, self.backend.state['tasks'][t_decline]['assignee'])
        self.assertTrue(
            any(c == 'holder_retired: Holder retired: ' + t_decline for c in seen['lanesAfterDeclineChips']),
            seen['lanesAfterDeclineChips'],
        )
        self.assertIn('1 holder retired', seen['lanesAfterDeclineText'])

        # 3. ReleaseAgent: dry run showed [t_release]; t_extra was assigned before clicking
        #    'Release its claims', and clicking 'Release its claims' sent tasks=[t_release],
        #    releasing ONLY t_release and leaving t_extra held.
        self.assertEqual([t_release], seen['releasePromptTasks'])
        self.assertIn('Released 1 claim (%s)' % t_release, seen['afterReleaseText'])
        self.assertIsNone(self.backend.state['tasks'][t_release]['assignee'])
        self.assertEqual(release_aid, self.backend.state['tasks'][t_extra]['assignee'])


@unittest.skipIf(rb.endpoint is None, 'real bd lane test needs POSIX endpoint imports')
@unittest.skipIf(rb.BD is None, 'real bd lane test needs ORCHESTRA_BD_BIN or bd on PATH')
class RealBdRetiredHolderLanesTests(unittest.TestCase):
    """Addition 1 named case on real ``bd`` with ``HOME`` and ``TMPDIR`` under ``~/work/t510``."""

    bd = rb.RealBdLabelAliasTests.bd
    export_rows = rb.RealBdLabelAliasTests.export_rows

    @classmethod
    def setUpClass(cls):
        t510 = Path('/home/agyargon/work/t510')
        t510.mkdir(parents=True, exist_ok=True)
        cls._env_patch = mock.patch.dict(os.environ, {'HOME': str(t510), 'TMPDIR': str(t510)})
        cls._env_patch.start()
        cls._old_tempdir = tempfile.tempdir
        tempfile.tempdir = str(t510)
        try:
            base = rb.RealBdLabelAliasTests
            base.setUpClass()
            for name in ('_tmp', '_signals', 'root', 'bd_path', 'port', 'project', 'server'):
                setattr(cls, name, getattr(base, name))
        except Exception:
            tempfile.tempdir = cls._old_tempdir
            cls._env_patch.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            rb.RealBdLabelAliasTests.tearDownClass()
        finally:
            tempfile.tempdir = cls._old_tempdir
            cls._env_patch.stop()

    def test_live_ssh_lane_registered_only_in_host_session_registry_is_not_flagged_on_real_bd(self):
        """Addition 1 named case: an actor registered only in sessions.read_registry(path)
        (sessions.py:38-60), with no web agent and no worker credential, holding a task,
        is NOT flagged holder_retired on real bd."""
        t510 = Path('/home/agyargon/work/t510')
        self.assertTrue(str(Path(os.environ['HOME'])).startswith(str(t510)))
        self.assertTrue(str(Path(os.environ['TMPDIR'])).startswith(str(t510)))

        # Create two tasks in real bd: one held by a live SSH session, one by a retired SSH session.
        res_live = self.bd('create', 'SSH live task', '--assignee', LIVE_ACTOR, '--json')
        self.assertEqual(res_live.returncode, 0, res_live.stderr)
        t_live = json.loads(res_live.stdout)['id']
        self.assertEqual(0, self.bd('update', t_live, '--status', 'in_progress').returncode)

        res_ret = self.bd('create', 'SSH retired task', '--assignee', RET_ACTOR, '--json')
        self.assertEqual(res_ret.returncode, 0, res_ret.stderr)
        t_ret = json.loads(res_ret.stdout)['id']
        self.assertEqual(0, self.bd('update', t_ret, '--status', 'in_progress').returncode)

        # Write the project's host session registry (<project>/.sessions.json), read by
        # sessions.read_registry(path) at sessions.py:38-60.
        (self.project / '.sessions.json').write_text(
            json.dumps({
                'schema_version': 1,
                'records': {
                    LIVE_REQ: {
                        'request_id': LIVE_REQ,
                        'name': 'ssh-live',
                        'actor': LIVE_ACTOR,
                        'created_at': '2026-09-15T10:00:00Z',
                    },
                    RET_REQ: {
                        'request_id': RET_REQ,
                        'name': 'ssh-retired',
                        'actor': RET_ACTOR,
                        'created_at': '2026-09-15T09:00:00Z',
                        'retired': True,
                    },
                },
                'owners': {
                    LIVE_ACTOR: 'person:alex',
                    RET_ACTOR: 'person:alex',
                },
            }),
            encoding='utf-8',
        )

        # Write an http-state.json that has NO web agent and NO worker credential for LIVE_ACTOR.
        (self.root / 'http-state.json').write_text(
            json.dumps({
                'schema_version': 1,
                'users': {'usr_alex': {'id': 'usr_alex', 'username': 'alex', 'status': 'active'}},
                'memberships': {'pp': {'usr_alex': 'owner'}},
                'agents': {},
                'credentials': {},
            }),
            encoding='utf-8',
        )

        with mock.patch.dict(os.environ, {'PATH': f'{self.bd_path.parent}:{os.environ.get("PATH", "")}'}):
            reply = rb.endpoint.execute(
                self.root,
                {'action': 'work', 'project': 'pp', 'actor': LIVE_ACTOR, 'args': ['lanes']},
            )
        self.assertEqual(0, reply['returncode'], reply.get('stderr'))
        board = json.loads(reply['stdout'])
        by_actor = {row['actor']: row for row in board['items']}

        # Named case (Addition 1): LIVE_ACTOR is registered only in the host session
        # registry (sessions.read_registry at sessions.py:38-60), with no web agent and no
        # worker credential -> NOT flagged!
        live_row = by_actor[LIVE_ACTOR]
        self.assertFalse(live_row['holder_retired'])
        self.assertNotIn('holder_retired', live_row['held'][0]['flags'])
        self.assertNotIn('holder_retired', live_row['flag_tasks'])

        # Meanwhile, RET_ACTOR IS flagged holder_retired.
        ret_row = by_actor[RET_ACTOR]
        self.assertTrue(ret_row['holder_retired'])
        self.assertIn('holder_retired', ret_row['held'][0]['flags'])
        self.assertEqual([t_ret], ret_row['flag_tasks']['holder_retired'])
        self.assertEqual(1, board['flags']['holder_retired'])


if __name__ == '__main__':
    unittest.main()
