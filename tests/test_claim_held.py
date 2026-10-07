"""A claim does not take a task from whoever holds it, and does not reopen a closed one (kittrial-5bb.187).

On the endpoint backend the web claim route sent bd a plain update of status and assignee.
bd carried it out whatever the row was: a second member's claim took the task from the
first, with no word to either, and a claim of a closed task reopened it. Every rule that
asks "is the caller the assignee" then held for whoever claimed last.

bd has a claim of its own, ``update ID --claim``, which checks and writes in one step. The
route uses it now, and bd's two refusals are read as what they are: 409 naming the holder,
409 for a task that is not open.

``RealStackTests`` runs the real web service in front of the real endpoint and a real bd
(skipped where there is none): the route, the race, and the other claim paths. The classes
after it run everywhere, against the stub, which answers a claim as bd was measured to.
"""
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import bd_refusals
import http_service
import test_bd_label_aliases as rb
import test_http_review_fixes as fixes

CLAIMED, NOT_CLAIMABLE = bd_refusals.CLAIMED, bd_refusals.NOT_CLAIMABLE


def message(response):
    return (response.data.get('error') or {}).get('message', '') if isinstance(response.data, dict) else ''


class SentenceTests(unittest.TestCase):
    """bd 1.2.2's own answers to a claim it refuses, as measured."""

    def test_the_two_refusals_of_a_claim_are_read_as_what_they_are(self):
        for stderr, kind, sentence in (
                ('Error claiming pp-kqt: issue already claimed by alex\n', CLAIMED, 'issue already claimed by alex'),
                ('Error claiming pp-kqt: issue already claimed by usr_0123456789abcdef\n', CLAIMED,
                 'issue already claimed by usr_0123456789abcdef'),
                ('Error claiming pp-kqt: issue already claimed by worker-a/night\n', CLAIMED, 'issue already claimed by worker-a/night'),
                (rb_warning() + 'Error claiming pp-7qf: issue not claimable: status closed\n', NOT_CLAIMABLE,
                 'issue not claimable: status closed'),
                ('Error claiming pp-3az: issue not claimable: status blocked\n', NOT_CLAIMABLE, 'issue not claimable: status blocked')):
            with self.subTest(said=stderr[-40:]):
                self.assertEqual(bd_refusals.refusal(1, '', stderr), (kind, sentence))

    def test_what_only_looks_like_one_is_not(self):
        for code, stdout, stderr in (
                (0, '', 'Error claiming pp-1: issue already claimed by alex\n'),
                (1, '[{"id": "pp-1"}]\n', 'Error claiming pp-1: issue already claimed by alex\n'),
                (1, '', 'Error claiming pp-1: issue already claimed by alex and then lost\n'),
                (1, '', 'Error claiming pp-1: issue already claimed by \n'),
                (1, '', 'Error claiming pp-1: the issue already claimed by alex\n'),
                (1, '', 'Error claiming pp-1: issue not claimable: status\n'),
                (1, '', 'Error claiming pp-1: issue not claimable: status closed; retrying\n'),
                (1, '', 'Error claiming pp-1: could not write the claim\n')):
            with self.subTest(said=stderr[-45:]):
                self.assertIsNone(bd_refusals.refusal(code, stdout, stderr))

    def test_the_holder_and_the_status_are_read_from_the_line_the_endpoint_hands_on(self):
        self.assertEqual(bd_refusals.holder('ValueError: bd refused: issue already claimed by alex'), 'alex')
        self.assertEqual(bd_refusals.status('ValueError: bd refused: issue not claimable: status in_progress'), 'in_progress')
        for other in (None, '', 'ValueError: Refusing update', 'ValueError: bd refused: title cannot be empty',
                      'issue already claimed by alex\nand more'):
            self.assertIsNone(bd_refusals.holder(other))
            self.assertIsNone(bd_refusals.status(other))


def rb_warning():
    return ('warning: beads.role not configured (GH#2950).\n  Fix: git config beads.role maintainer\n'
            '  Or:  git config beads.role contributor\n')


class Members:
    """A project with its owner (alex) and two contributors (casey, drew), and how to ask."""

    def members(self, project):
        admin = self.admin_token()
        self.pid = project
        self.tokens, self.ids = dict(getattr(self, 'tokens', {})), dict(getattr(self, 'ids', {}))
        for name, role in (('alex', 'owner'), ('casey', 'contributor'), ('drew', 'contributor')):
            if name in self.tokens:
                continue
            self.ids[name] = self.create_account(admin, name, name + '-password-1')
            added = self.request('PUT', '/v1/projects/%s/members/%s' % (project, self.ids[name]), {'role': role}, token=admin)
            self.assertIn(added.status, (200, 201), added.data)
            self.tokens[name] = self.login(name, name + '-password-1')[0]
        self.tasks = '/v1/projects/%s/tasks' % project

    def new(self, title='a task'):
        made = self.request('POST', self.tasks, {'title': title}, token=self.tokens['alex'])
        self.assertEqual(201, made.status, made.data)
        return made.data['id']

    def claim(self, who, task, key=None, token=None):
        return self.request('POST', '%s/%s/claim' % (self.tasks, task), {}, token=token or self.tokens[who], key=key)

    def held(self, task):
        row = self.request('GET', '%s/%s' % (self.tasks, task), token=self.tokens['alex']).data
        return row.get('status'), row.get('assignee')

    def outcomes(self):
        listed = self.request('GET', '/v1/projects/%s/audit?limit=100' % self.pid, token=self.tokens['alex'])
        return [entry['outcome'] for entry in listed.data['items']]

    def the_rules(self):
        """What the task decided, on whichever backend this is."""
        casey, drew, alex = self.ids['casey'], self.ids['drew'], self.ids['alex']
        one = self.new('one')
        first = self.claim('casey', one, key='claim-key-0001')
        self.assertEqual(200, first.status, first.data)
        self.assertEqual(('in_progress', casey), self.held(one))
        # Somebody else's: refused, naming the holder as people know them, and nothing changes.
        for who in ('drew', 'alex'):                                   # a member, and the OWNER: nobody takes over by a claim
            with self.subTest(taker=who):
                key = 'take-key-%s' % who
                refused = self.claim(who, one, key=key)
                again = self.claim(who, one, key=key)
                self.assertEqual((409, 409), (refused.status, again.status), refused.data)
                self.assertEqual(('conflict', 'Task is already claimed by casey'), (refused.data['error']['code'], message(refused)))
                self.assertEqual({'held_by': casey}, refused.data['error']['detail'])
                self.assertEqual(('in_progress', casey), self.held(one))
                # The key was not kept: it claims a free task.
                self.assertEqual(200, self.claim(who, self.new('free for ' + who), key=key).status)
        # One's own, again: answered as before, unchanged.
        mine = self.claim('casey', one)
        self.assertEqual(200, mine.status, mine.data)
        self.assertEqual(('in_progress', casey), self.held(one))
        # A task that is not open.
        two = self.new('two')
        closed = self.request('PATCH', '%s/%s' % (self.tasks, two), {'status': 'closed'}, token=self.tokens['alex'])
        self.assertEqual(200, closed.status, closed.data)
        for who in ('drew', 'alex'):
            with self.subTest(closed_for=who):
                refused = self.claim(who, two, key='closed-key-%s' % who)
                self.assertEqual((409, 'Task is not open (it is closed)'), (refused.status, message(refused)), refused.data)
                self.assertEqual({'status': 'closed'}, refused.data['error']['detail'])
                self.assertEqual(('closed', None), self.held(two))
        self.assertNotIn('unknown', self.outcomes())
        # And the holder goes on as the holder: a contribution of the one who was refused is not taken.
        body = dict(fixes.CONTRIBUTION, operation='contribute', schema_version=1, operation_id='op-claim-1', previous=None)
        reviews = '%s/%s/reviews' % (self.tasks, one)
        self.assertGreaterEqual(self.request('POST', reviews, body, token=self.tokens['drew']).status, 400)
        self.assertEqual(201, self.request('POST', reviews, body, token=self.tokens['casey']).status)


def without_inherited_tests(cls):
    """The real-bd fixture is a TestCase with tests of its own; a class that only borrows its
    runtime does not run them a second time."""
    for name in dir(rb.RealBdLabelAliasTests):
        if name.startswith('test_') and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@without_inherited_tests
class RealStackTests(Members, fixes.Harness, rb.RealBdLabelAliasTests):
    """The real web service, the real endpoint.py, a real bd."""

    def make_backend(self):
        return http_service.EndpointBackend(sys.executable, str(KIT / 'endpoint.py'), str(self.root), service=self.service)

    def setUp(self):
        fixes.Harness.setUp(self)
        admin = self.admin_token()
        registered = self.request('POST', '/v1/projects', {'project_id': 'pp', 'name': 'PP'}, token=admin)
        self.assertIn(registered.status, (200, 201, 409), registered.data)
        self.members('pp')

    @classmethod
    def tearDownClass(cls):
        # The fixture's own, spelled out: `Harness._stop_server` (the web server of one test) has the
        # name of the fixture's class method that stops the scratch Dolt server.
        for signum, previous in getattr(cls, '_signals', {}).items():
            rb.signal.signal(signum, previous)
        cls._signals = {}
        rb.RealBdLabelAliasTests.__dict__['_stop_server'].__func__(cls)
        cls._tmp.cleanup()

    def bd_row(self, task):
        found = json.loads(self.bd('show', task, '--json').stdout)
        found = found[0] if isinstance(found, list) else found
        return found.get('status'), found.get('assignee')

    def test_a_first_claim_of_a_free_open_task_is_as_before(self):
        task = self.new()
        claimed = self.claim('casey', task)
        self.assertEqual(200, claimed.status, claimed.data)
        self.assertEqual(('in_progress', self.ids['casey']), self.bd_row(task))
        self.assertIsNotNone(claimed.headers.get('x-server-time'))

    def test_nobody_takes_a_held_task_and_a_closed_one_stays_closed(self):
        self.the_rules()

    def test_of_several_claims_at_once_exactly_one_is_carried_out(self):
        """The check and the write are bd's one step, under the project's lock: nothing gets between them."""
        for round_ in range(3):
            task = self.new('race %d' % round_)
            answers = {}

            def claim(who):
                answers[who] = self.claim(who, task, key='race-%d-%s' % (round_, who))
            threads = [threading.Thread(target=claim, args=(who,)) for who in ('alex', 'casey', 'drew')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(120)
            won = [who for who, answer in answers.items() if answer.status == 200]
            self.assertEqual(1, len(won), {who: (a.status, message(a)) for who, a in answers.items()})
            for who, answer in answers.items():
                if who != won[0]:
                    self.assertEqual((409, 'Task is already claimed by %s' % won[0]), (answer.status, message(answer)))
            self.assertEqual(('in_progress', self.ids[won[0]]), self.bd_row(task))
        self.assertNotIn('unknown', self.outcomes())

    def test_several_claims_at_once_through_the_endpoint_itself(self):
        task = self.new('host race')
        answers = {}

        def claim(actor):
            answers[actor] = rb.endpoint.execute(self.root, {'project': 'pp', 'actor': actor, 'action': 'bd',
                                                             'args': ['update', task, '--claim', '--json'], 'attachments': {}})
        threads = [threading.Thread(target=claim, args=('racer-%d' % n,)) for n in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        won = [actor for actor, answer in answers.items() if answer['returncode'] == 0]
        self.assertEqual(1, len(won), answers)
        for actor, answer in answers.items():
            if actor != won[0]:
                self.assertEqual((2, CLAIMED, 'ValueError: bd refused: issue already claimed by %s\n' % won[0]),
                                 (answer['returncode'], answer.get('refused'), answer['stderr']))
        self.assertEqual(('in_progress', won[0]), self.bd_row(task))

    def test_the_other_claim_paths(self):
        """The host client's claim is bd's own and was never the hole; a plain update on the host route
        still reassigns, which is the coordinator's explicit action and is not changed here."""
        held = self.new('held')
        self.assertEqual(200, self.claim('casey', held).status)
        closed = self.new('closed')
        self.bd('close', closed, '--json')

        def host(actor, args):
            return rb.endpoint.execute(self.root, {'project': 'pp', 'actor': actor, 'action': 'bd', 'args': args, 'attachments': {}})
        taken = host('session-worker', ['update', held, '--claim', '--json'])
        self.assertEqual((2, CLAIMED), (taken['returncode'], taken.get('refused')), taken)
        reopened = host('session-worker', ['update', closed, '--claim', '--json'])
        self.assertEqual((2, NOT_CLAIMABLE), (reopened['returncode'], reopened.get('refused')), reopened)
        self.assertEqual((('in_progress', self.ids['casey']), ('closed', None)), (self.bd_row(held), self.bd_row(closed)))
        # A worker credential and an agent claim through the same route and are refused the same way.
        credential = self.issue_credential(self.tokens['alex'], 'pp', label='w', actor='worker-a')['secret']
        refused = self.claim(None, held, token=credential)
        self.assertEqual((409, 'Task is already claimed by casey'), (refused.status, message(refused)), refused.data)
        free = self.new('free')
        self.assertEqual(200, self.claim(None, free, token=credential).status)
        self.assertEqual(('in_progress', 'worker-a'), self.bd_row(free))
        named = self.claim('drew', free)
        self.assertEqual((409, 'Task is already claimed by worker-a'), (named.status, message(named)))
        # The explicit reassignment on the host route, as it was.
        moved = host('session-coordinator', ['update', held, '--status', 'in_progress', '--assignee', 'session-worker', '--json'])
        self.assertEqual(0, moved['returncode'], moved)
        self.assertEqual(('in_progress', 'session-worker'), self.bd_row(held))


class StubCase(Members, fixes.EndpointCase):
    """The same rules everywhere the suite runs: the stub answers a claim as bd was measured to."""

    def owned_project(self):
        alex, project = self.setup_project()                           # makes alex and the project he owns
        self.tokens = {'alex': alex}
        self.ids = {'alex': self.request('GET', '/v1/sessions/current', token=alex).data['user']['id']}
        return project

    def test_nobody_takes_a_held_task_and_a_closed_one_stays_closed(self):
        project = self.owned_project()
        self.members(project)
        self.the_rules()

    def test_the_route_sends_bds_own_claim_under_the_callers_actor(self):
        project = self.owned_project()
        self.members(project)
        task = self.new()
        with mock.patch.object(self.backend, '_run', wraps=self.backend._run) as asked:
            self.assertEqual(200, self.claim('casey', task).status)
        writes = [(call.args[2], call.args[3]) for call in asked.call_args_list if call.args[0] == 'bd' and call.args[3][:1] == ['update']]
        self.assertEqual([(self.ids['casey'], ['update', task, '--claim', '--json'])], writes)

    def test_a_holder_the_service_does_not_know_is_named_by_its_label(self):
        project = self.owned_project()
        self.members(project)
        task = self.new()
        credential = self.issue_credential(self.tokens['alex'], project, label='w', actor='worker-a')['secret']
        self.assertEqual(200, self.claim(None, task, token=credential).status)
        refused = self.claim('casey', task)
        self.assertEqual((409, 'Task is already claimed by worker-a', {'held_by': 'worker-a'}),
                         (refused.status, message(refused), refused.data['error']['detail']))


class CheckedTests(unittest.TestCase):
    checked = staticmethod(lambda reply, **more: http_service.EndpointBackend._checked(reply, 'bd', **more))

    def test_bds_two_refusals_are_conflicts_with_what_the_route_needs(self):
        with self.assertRaises(http_service.HttpError) as held:
            self.checked(bd_refusals.envelope(CLAIMED, 'issue already claimed by usr_0123456789abcdef'))
        self.assertEqual((409, 'conflict', 'usr_0123456789abcdef', {'held_by': 'usr_0123456789abcdef'}),
                         (held.exception.status, held.exception.code, held.exception.held_by, held.exception.detail))
        for said, shown in (('closed', 'Task is not open (it is closed)'), ('in_progress', 'Task is not open (it is in progress)')):
            with self.assertRaises(http_service.HttpError) as closed:
                self.checked(bd_refusals.envelope(NOT_CLAIMABLE, 'issue not claimable: status %s' % said))
            self.assertEqual((409, shown, {'status': said}), (closed.exception.status, closed.exception.message, closed.exception.detail))
            self.assertFalse(hasattr(closed.exception, 'held_by'))


if __name__ == '__main__':
    unittest.main()
