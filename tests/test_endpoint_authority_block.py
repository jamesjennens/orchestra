"""The endpoint's own reading of the authority block the web service sends (kittrial-5bb.261).

In process, with a stub tracker in place of bd, so every run of the suite on a POSIX host
holds these decisions; until now only tests/test_coordinator_grant_real.py did, and that
file needs a real bd:

* the endpoint authorizes an approval a SECOND time from the live state: a request that
  reaches ``endpoint.execute`` with a block claiming ``reviews.approve`` for an agent that
  holds no coordinator grant is refused, whatever let it through before;
* the host rule ``coordinator_writes`` is about names on the host route: an approval that
  carries a verified web authority (an owner, a coordinator by role, an agent with the
  grant) passes it with nobody listed, and a plain name on the host route does not.

``endpoint.py`` imports fcntl, so the module is skipped where that import fails (Windows).
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import http_authority
import review_workflow as rw
from requirements import canonical_bytes

try:
    import endpoint
except ImportError:                                   # fcntl: the endpoint is POSIX only
    endpoint = None

POSIX = unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
PROJECT = 'pp'
OTHER_PROJECT = 'pq'
TASK = 'pp-task'
OWNER = 'usr_00000000000000a1'
COORDINATOR = 'usr_00000000000000c1'
CONTRIBUTOR = 'usr_00000000000000d1'
AGENT = 'agent_00000000000000e1'
CREDENTIAL = 'cred_agent'
AUTHOR = 'usr_00000000000000f1'
SESSIONS = {OWNER: 'sess_owner', COORDINATOR: 'sess_coordinator', CONTRIBUTOR: 'sess_contributor'}


class _Completed:
    def __init__(self, stdout='', returncode=0, stderr=''):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class Tracker:
    """The least bd a review needs: one task, its comments, and a record of every call."""

    def __init__(self):
        self.row = dict(id=TASK, title='Synthetic task', issue_type='task', status='open', assignee=AUTHOR,
                        labels=[], comments=[], created_by='someone', created_at='2026-09-15T00:00:00Z')
        self.calls = []
        self.add(dict(schema_version=1, operation='contribute', operation_id='send-1', task=TASK, previous=None,
                      repository='ssh://git/example', commit='a' * 40, base_commit='b' * 40,
                      delivery=dict(kind='bundle', path='server:/rev.bundle', sha256='c' * 64),
                      summary='Contribution delivered', supersedes=None), 'delivery', AUTHOR)

    def add(self, body, cid, author):
        self.row['comments'].append(dict(id=cid, author=author, created_at='2026-09-16T00:00:00Z',
                                         text=rw.PREFIX + canonical_bytes(body).decode()))

    @property
    def writes(self):
        return [(actor, command) for actor, command in self.calls if command[:1] != ['export']]

    def run(self, command, env=None, **options):
        """In place of ``native.run`` and ``subprocess.run``: the argv ``native.argv`` builds."""
        rest = list(command[command.index('--sandbox') + 1:])
        actor = None
        if rest[:1] == ['--actor']:
            actor, rest = rest[1], rest[2:]
        self.calls.append((actor, rest))
        if rest[:2] == ['export', '--all']:
            return _Completed(json.dumps(self.row) + '\n')
        if rest[:2] == ['comments', 'add']:
            cid = 'comment-%d' % (len(self.row['comments']) + 1)
            self.row['comments'].append(dict(id=cid, author=actor, created_at='2026-09-17T00:00:00Z', text=rest[3]))
            return _Completed(json.dumps(dict(id=cid)))
        if rest[:1] == ['show']:
            return _Completed(json.dumps([self.row]))
        return _Completed(json.dumps(dict(id=TASK)))


def authority_state(now=None):
    """An owner, a coordinator by role with one personal agent, and a contributor, in one project."""
    moment = time.time() if now is None else now
    return {
        'schema_version': 1,
        'users': {user: {'id': user, 'disabled': False} for user in SESSIONS},
        'sessions': {session: {'user_id': user, 'revoked': False, 'absolute_expires': moment + 3600,
                               'idle_expires': moment + 3600} for user, session in SESSIONS.items()},
        'agents': {AGENT: {'id': AGENT, 'owner': COORDINATOR, 'enabled': True, 'projects': [PROJECT, OTHER_PROJECT]}},
        'credentials': {CREDENTIAL: {'id': CREDENTIAL, 'user_id': COORDINATOR, 'agent_id': AGENT, 'actor': AGENT,
                                     'revoked': False, 'expires_at': moment + 3600,
                                     'scopes': ['tasks', 'checkpoints', 'reviews', 'feedback', 'proposals']}},
        'projects': {PROJECT: {'id': PROJECT, 'archived': False}, OTHER_PROJECT: {'id': OTHER_PROJECT, 'archived': False}},
        'memberships': {PROJECT: {OWNER: 'owner', COORDINATOR: 'coordinator', CONTRIBUTOR: 'contributor'},
                        OTHER_PROJECT: {OWNER: 'owner', COORDINATOR: 'coordinator', CONTRIBUTOR: 'contributor'}},
    }


def session_block(user, capability, project=PROJECT):
    return {'via': 'session', 'user_id': user, 'credential_id': None, 'session_hash': SESSIONS[user],
            'project': project, 'capability': capability, 'now': time.time() + 5}


def agent_block(capability, project=PROJECT):
    return {'via': 'credential', 'user_id': COORDINATOR, 'credential_id': CREDENTIAL, 'session_hash': None,
            'project': project, 'capability': capability, 'now': time.time() + 5}


class EndpointHarness(unittest.TestCase):
    """A runtime root with two initialized-looking projects, a live authority store and a stub tracker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='endpoint-authority-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'rt'
        for name in (PROJECT, OTHER_PROJECT):
            (self.root / 'projects' / name / '.beads').mkdir(parents=True)
            (self.root / 'projects' / name / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'bin').mkdir()
        self.deployment()
        self.store = Path(self.tmp.name) / 'authority.json'
        self.config = http_authority.AuthorityConfig(str(self.store))
        self.state = authority_state()
        self.save()
        self.tracker = Tracker()
        self.count = 0
        for target in (endpoint.native, endpoint.subprocess):
            patcher = mock.patch.object(target, 'run', self.tracker.run)
            patcher.start()
            self.addCleanup(patcher.stop)

    def deployment(self, **settings):
        (self.root / 'deployment.private.json').write_text(
            json.dumps(dict({'password': 'x', 'operators': []}, **settings)), encoding='utf-8')

    def save(self):
        self.store.write_text(json.dumps(self.state), encoding='utf-8')

    def grant(self):
        self.state['coordinator_grants'] = {PROJECT: {AGENT: {'granted_by': OWNER, 'granted_at': '2026-10-10T00:00:00Z'}}}
        self.save()

    def review_request(self, actor, operation='approve', project=PROJECT, **fields):
        self.count += 1
        body = dict(schema_version=1, operation=operation, operation_id='op-%d' % self.count, task=TASK,
                    previous='delivery', contribution='delivery')
        body.update(fields if fields else {'summary': 'Reviewed and approved'})
        return {'project': project, 'actor': actor, 'action': 'review', 'args': [TASK, '@attachment:0'],
                'attachments': {'0': {'flag': '--file', 'text': json.dumps(body)}},
                'operation_id': 'request-%d' % self.count}

    def web(self, request, block):
        """As the web service sends it: launched with the store, live authority required."""
        return endpoint.execute(self.root, dict(request, authority=block), authority_config=self.config,
                                require_authority=True)

    def approvals(self):
        found = []
        for comment in self.tracker.row['comments']:
            record = json.loads(comment['text'][len(rw.PREFIX):])
            if record.get('operation') == 'approve':
                found.append(comment['author'])
        return found

    def assertRefused(self, answer, status=403):
        self.assertEqual((126, status), (answer.get('returncode'), answer.get('authority_status')), answer)
        self.assertEqual([], self.tracker.writes)


@POSIX
class SecondAuthorizationTests(EndpointHarness):
    """Item 2a: the endpoint reads the live state itself before an agent's approval is written."""

    def test_an_agent_without_the_grant_is_refused_though_its_block_claims_the_approval(self):
        refused = self.web(self.review_request(AGENT), agent_block(http_authority.CAP_APPROVE))
        self.assertRefused(refused)
        self.assertIn('Credential scope does not permit this operation', refused['stderr'])
        self.assertEqual([], self.approvals())
        self.assertEqual([], self.tracker.calls)              # refused before the tracker is asked anything

    def test_the_same_request_is_carried_out_once_the_grant_is_on_record(self):
        self.grant()
        done = self.web(self.review_request(AGENT), agent_block(http_authority.CAP_APPROVE))
        self.assertEqual(0, done['returncode'], done)
        self.assertEqual([AGENT], self.approvals())

    def test_a_grant_in_another_project_and_a_grant_whose_account_was_lowered_give_nothing(self):
        self.state['coordinator_grants'] = {OTHER_PROJECT: {AGENT: {'granted_by': OWNER, 'granted_at': 'x'}}}
        self.save()
        self.assertRefused(self.web(self.review_request(AGENT), agent_block(http_authority.CAP_APPROVE)))
        self.grant()
        self.state['memberships'][PROJECT][COORDINATOR] = 'contributor'
        self.save()
        self.assertRefused(self.web(self.review_request(AGENT), agent_block(http_authority.CAP_APPROVE)))
        self.assertEqual([], self.approvals())

    def test_a_contributor_session_that_claims_the_approval_is_refused_too(self):
        refused = self.web(self.review_request(CONTRIBUTOR), session_block(CONTRIBUTOR, http_authority.CAP_APPROVE))
        self.assertRefused(refused)
        self.assertEqual([], self.approvals())


@POSIX
class CoordinatorWritesExemptionTests(EndpointHarness):
    """Item 2b: with ``coordinator_writes`` on and nobody listed, the web route's authority decides."""

    def setUp(self):
        super().setUp()
        self.deployment(coordinator_writes=True)

    def test_an_owner_a_coordinator_by_role_and_a_granted_agent_pass_the_rule(self):
        self.grant()
        for actor, block in ((OWNER, session_block(OWNER, http_authority.CAP_APPROVE)),
                             (COORDINATOR, session_block(COORDINATOR, http_authority.CAP_APPROVE)),
                             (AGENT, agent_block(http_authority.CAP_APPROVE))):
            with self.subTest(actor=actor):
                self.tracker.__init__()
                done = self.web(self.review_request(actor), block)
                self.assertEqual(0, done['returncode'], done)
                self.assertEqual([actor], self.approvals())

    def test_a_plain_name_on_the_host_route_is_refused_by_the_rule(self):
        # The host route: no launch configuration, so no web authority. A block the caller
        # sends is not one: it is ignored there and the rule still refuses.
        for extra in ({}, {'authority': session_block(OWNER, http_authority.CAP_APPROVE)}):
            with self.subTest(sent=sorted(extra)), self.assertRaises(ValueError) as refused:
                endpoint.execute(self.root, dict(self.review_request('worker'), **extra))
            self.assertIn('coordinator_writes', str(refused.exception))
        self.assertEqual(([], []), (self.approvals(), self.tracker.writes))

    def test_an_agent_without_the_grant_is_refused_with_the_rule_on_as_with_it_off(self):
        self.assertRefused(self.web(self.review_request(AGENT), agent_block(http_authority.CAP_APPROVE)))
        self.assertEqual([], self.approvals())

    def test_with_the_rule_off_the_plain_name_approves_as_before(self):
        self.deployment()
        done = endpoint.execute(self.root, self.review_request('worker'))
        self.assertEqual(0, done['returncode'], done)
        self.assertEqual(['worker'], self.approvals())


OWNER_HOLDS = sorted(http_authority.ROLE_CAPABILITIES['owner'])


@POSIX
class BlockMatchesRequestTests(EndpointHarness):
    """Item 7: the block must be FOR the request it travels with: its project and its capability.

    Every block here is one the live state grants (the owner holds every capability of a
    project, in both projects), so only the comparison with the request can refuse it.
    """

    WRONG_PROJECT = 'The live-authority descriptor names another project than the one this request is for'
    WRONG_CAPABILITY = 'The live-authority descriptor does not name the capability this request needs (%s)'

    def setUp(self):
        super().setUp()
        import briefing
        import proposal_records
        import work
        self.reached = []
        for module, name in ((work, 'execute'), (briefing, 'execute'), (proposal_records, 'write')):
            patcher = mock.patch.object(module, name, side_effect=self.library(module.__name__))
            patcher.start()
            self.addCleanup(patcher.stop)

    def library(self, name):
        def carried_out(*args, **options):
            self.reached.append(name)
            return '{}' if name == 'briefing' else {'carried_out': name}
        return carried_out

    def payload_request(self, action, args, body=None, project=PROJECT):
        self.count += 1
        built = {'project': project, 'actor': OWNER, 'action': action, 'args': list(args),
                 'operation_id': 'request-%d' % self.count}
        if body is not None:
            built['args'].append('@attachment:0')
            built['attachments'] = {'0': {'flag': '--file', 'text': json.dumps(body)}}
        return built

    def cases(self):
        """(label, a request builder, the capability http_service.py asks for its route, the library reached)."""
        review = lambda operation: (lambda: self.payload_request('review', [TASK], {
            'schema_version': 1, 'operation': operation, 'operation_id': 'op', 'task': TASK}))
        proposal = lambda command: (lambda: self.payload_request('proposal', [command], {'schema_version': 1}))
        return (
            ('review approve', review('approve'), 'reviews.approve', 'work'),
            ('review request-changes', review('request-changes'), 'reviews.write', 'work'),
            ('review contribute', review('contribute'), 'reviews.write', 'work'),
            ('review recommend', review('recommend'), 'reviews.write', 'work'),
            ('checkpoint', lambda: self.payload_request('checkpoint', [TASK], {'schema_version': 1, 'task': TASK}),
             'checkpoints.write', 'briefing'),
            ('claim', lambda: self.payload_request('bd', ['update', TASK, '--claim', '--json']), 'tasks.write', None),
            ('task create', lambda: self.payload_request('bd', ['create', 'A title', '--json']), 'tasks.write', None),
            ('task comment', lambda: self.payload_request('bd', ['comments', 'add', TASK, 'Literal contributor remark']), 'tasks.write', None),
            ('proposal submit', proposal('submit'), 'proposals.write', 'proposal_records'),
            ('proposal revise', proposal('revise'), 'proposals.write', 'proposal_records'),
            ('proposal triage', proposal('review'), 'reviews.approve', 'proposal_records'),
            ('proposal decide', proposal('decide'), 'project.admin', 'proposal_records'),
        )

    def test_the_table_is_what_the_service_asks_for_each_route(self):
        for label, build, needed, _ in self.cases():
            with self.subTest(request=label):
                self.assertEqual(needed, http_authority.required_capability(build()))
        # Not in the table: reads, and actions the web service sends no block for.
        for action, args in (('review', [TASK]), ('proposal', ['get', 'k']), ('proposal', ['list']), ('work', []),
                             ('handoff', [TASK, '@attachment:0']), ('bd', ['dep', 'add', TASK, 'pp-other']),
                             ('bd', ['close', TASK]), ('lifecycle', ['{}']), ('coordinate', ['{}']), ('ref', ['propose']),
                             ('capability', ['propose']), ('feedback', ['add']), ('session', ['show']),
                             ('guidance', ['ack']), ('owner-requirements', ['accept']), ('set-onboarding', ['set'])):
            with self.subTest(action=action, args=args):
                self.assertIsNone(http_authority.required_capability({'project': PROJECT, 'action': action, 'args': args}))

    def test_a_block_for_another_capability_is_refused_before_anything_is_read_or_written(self):
        for label, build, needed, _ in self.cases():
            for claimed in OWNER_HOLDS:
                if claimed == needed:
                    continue
                with self.subTest(request=label, block=claimed):
                    refused = self.web(build(), session_block(OWNER, claimed))
                    self.assertEqual((126, 403), (refused.get('returncode'), refused.get('authority_status')), refused)
                    self.assertEqual(self.WRONG_CAPABILITY % needed + '; nothing was changed\n', refused['stderr'])
        self.assertEqual(([], []), (self.reached, self.tracker.calls))
        for name in (PROJECT, OTHER_PROJECT):                              # no lock taken, no identity reserved
            self.assertEqual(['.beads'], sorted(p.name for p in (self.root / 'projects' / name).iterdir()))

    def test_a_block_for_another_project_is_refused_before_anything_is_read_or_written(self):
        for label, build, needed, _ in self.cases():
            with self.subTest(request=label):
                request = dict(build(), project=OTHER_PROJECT)
                refused = self.web(request, session_block(OWNER, needed, project=PROJECT))
                self.assertEqual((126, 403), (refused.get('returncode'), refused.get('authority_status')), refused)
                self.assertEqual(self.WRONG_PROJECT + '; nothing was changed\n', refused['stderr'])
                # A block that names no project at all is not one for this project either.
                refused = self.web(build(), session_block(OWNER, needed, project=None))
                self.assertEqual(126, refused.get('returncode'), refused)
        self.assertEqual(([], []), (self.reached, self.tracker.calls))

    def test_the_matching_block_is_carried_out(self):
        for label, build, needed, library in self.cases():
            for project in (PROJECT, OTHER_PROJECT):
                with self.subTest(request=label, project=project):
                    del self.reached[:], self.tracker.calls[:]
                    done = self.web(dict(build(), project=project), session_block(OWNER, needed, project=project))
                    self.assertEqual(0, done.get('returncode'), done)
                    if library is None:                               # bd itself: the stub tracker was written to
                        self.assertEqual(1, len([command for _, command in self.tracker.writes
                                                 if command[:1] in (['update'], ['create'], ['comments'])]), self.tracker.calls)
                    else:
                        self.assertEqual([library], self.reached)

    def test_the_reviewed_cases_through_the_real_review_workflow(self):
        """As reviewed, with nothing stubbed but the tracker: an approval under a block naming
        `reviews.write` was carried out with the host rule off, and a block naming project A was
        carried out in project B."""
        mock.patch.stopall()
        for target in (endpoint.native, endpoint.subprocess):
            patcher = mock.patch.object(target, 'run', self.tracker.run)
            patcher.start()
            self.addCleanup(patcher.stop)
        refused = self.web(self.review_request(OWNER), session_block(OWNER, 'reviews.write'))
        self.assertRefused(refused)
        refused = self.web(self.review_request(OWNER, project=OTHER_PROJECT), session_block(OWNER, 'reviews.approve'))
        self.assertRefused(refused)
        # A contributor holds reviews.write and not reviews.approve: its block is a true one, and no approval.
        refused = self.web(self.review_request(CONTRIBUTOR), session_block(CONTRIBUTOR, 'reviews.write'))
        self.assertRefused(refused)
        self.assertEqual(([], []), (self.approvals(), self.tracker.calls))
        # The matching blocks: the owner's approval, and the same contributor's request for changes.
        done = self.web(self.review_request(OWNER), session_block(OWNER, 'reviews.approve'))
        self.assertEqual((0, [OWNER]), (done['returncode'], self.approvals()), done)
        self.tracker.__init__()
        asked = self.web(self.review_request(CONTRIBUTOR, operation='request-changes', items=[
            {'id': 'item-1', 'text': 'Add the missing test.'}]), session_block(CONTRIBUTOR, 'reviews.write'))
        self.assertEqual(0, asked['returncode'], asked)
        self.assertEqual(1, len([command for _, command in self.tracker.writes if command[:2] == ['comments', 'add']]))

    def test_the_service_only_actions_keep_their_own_check_and_their_own_sentence(self):
        # set-onboarding and owner-requirements compare the block themselves (capability and
        # project); this check leaves them to it, so their answers are the ones they had.
        import project_creation
        for action in project_creation.SERVICE_ACTIONS:
            with self.subTest(action=action):
                self.assertIsNone(http_authority.block_mismatch_denial(
                    {'project': OTHER_PROJECT, 'actor': OWNER, 'action': action, 'args': [],
                     'authority': session_block(OWNER, 'read')}, self.config))
        with self.assertRaises(ValueError) as refused:
            self.web({'project': OTHER_PROJECT, 'actor': OWNER, 'action': 'set-onboarding', 'args': ['clear'],
                      'operation_id': 'onboarding-1'}, session_block(OWNER, 'project.admin'))
        self.assertIn('set-onboarding needs a descriptor for this project', str(refused.exception))
        with self.assertRaises(ValueError) as refused:
            self.web({'project': PROJECT, 'actor': OWNER, 'action': 'set-onboarding', 'args': ['clear'],
                      'operation_id': 'onboarding-2'}, session_block(OWNER, 'reviews.approve'))
        self.assertIn('set-onboarding needs a session descriptor for project.admin', str(refused.exception))
        self.assertEqual([], self.tracker.calls)

    def test_the_host_route_ignores_a_block_as_before(self):
        # No launch configuration: the block is not read at all, whatever it says.
        request = dict(self.payload_request('review', [TASK], {'schema_version': 1, 'operation': 'approve', 'task': TASK}),
                       actor='worker', authority=session_block(OWNER, 'read', project=OTHER_PROJECT))
        done = endpoint.execute(self.root, request)
        self.assertEqual((0, ['work']), (done['returncode'], self.reached))

    def test_an_operation_the_table_does_not_know_is_refused_when_a_block_is_present(self):
        # Dependencies still have no web mutation route. An otherwise valid block
        # must not let a service mistake reach a host-only dependency effect.
        comment = lambda project=PROJECT: self.payload_request('bd', ['dep', 'add', TASK, 'pp-other'], project=project)
        refused = self.web(comment(OTHER_PROJECT), session_block(OWNER, 'tasks.write'))
        self.assertEqual((126, self.WRONG_PROJECT + '; nothing was changed\n'), (refused['returncode'], refused['stderr']))
        self.assertEqual([], self.tracker.calls)
        for claimed in ('tasks.write', 'read'):
            with self.subTest(block=claimed):
                del self.tracker.calls[:]
                refused = self.web(comment(), session_block(OWNER, claimed))
                self.assertRefused(refused)
                self.assertEqual([], self.tracker.calls)
                self.assertIn('table does not allow this action or operation', refused['stderr'])


    def test_contributor_comments_require_a_current_matching_tasks_block(self):
        request = dict(self.payload_request('bd', ['comments', 'add', TASK, 'Literal contributor remark']),
                       actor=CONTRIBUTOR)
        refused = self.web(request, session_block(CONTRIBUTOR, 'read'))
        self.assertRefused(refused)
        self.assertIn('capability this request needs (tasks.write)', refused['stderr'])
        self.assertEqual([], self.tracker.calls)
        self.assertFalse(http_authority.journal_path(self.root / 'projects' / PROJECT).exists())
        refused = self.web(request, session_block(CONTRIBUTOR, 'tasks.write', project=OTHER_PROJECT))
        self.assertRefused(refused)
        self.assertEqual([], self.tracker.calls)
        done = self.web(request, session_block(CONTRIBUTOR, 'tasks.write'))
        self.assertEqual(0, done['returncode'], done)
        writes = [(actor, command) for actor, command in self.tracker.calls
                  if command[:2] == ['comments', 'add']]
        self.assertEqual(1, len(writes))
        self.assertEqual(CONTRIBUTOR, writes[0][0])
        self.assertEqual('Literal contributor remark', self.tracker.row['comments'][-1]['text'])

if __name__ == '__main__':
    unittest.main()
