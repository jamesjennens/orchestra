"""The web Lanes panel on the project page (kittrial-5bb.263; design section 4).

Two surfaces, as the neighbouring web tests are split:

* ``GET /v1/projects/<pid>/lanes`` -- the route the panel reads. It is readable by a project
  member (``CAP_READ``); a non-member and a member of another project are refused before
  anything is read; its threshold (``idle_minutes``) reaches the ONE board read unchanged,
  so the rows and the three flags change with it; a threshold outside 1..10080 is refused
  400; and the route scopes the read for its OWN viewer instead of inheriting the
  transport's answer: an owner or coordinator (``review_workflow.listed_operator_actor``
  over the host's operator allowlist and actor->principal map) sees every lane, and anyone
  else is scoped to their own agents' lanes, exactly the contributor route's rule.
* the page's own rendering, in the repository's page-test form: ``tests/web_lanes_page.mjs``
  under Node, driven by the Python test below, over a stubbed transport (no server needed).

Nothing here computes an age or a flag: the assertions read what ``lanes.read`` answered.
"""
import json
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import lane_state
from requirements import canonical_bytes
from test_http_service import ServerHarness


def without_request_id(body):
    """The answer without the per-request id, so two refusals can be compared."""
    return {key: value for key, value in (body or {}).items() if key != 'request_id'}

try:
    import endpoint
except ImportError:               # endpoint.py imports fcntl; POSIX only
    endpoint = None


class LanesRouteCase(ServerHarness):
    """An owner (olive), two contributors (carl, dana), an outsider (otto) and two lanes."""

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.ids = {}
        for name in ('olive', 'carl', 'dana', 'otto'):
            self.ids[name] = self.create_account(self.admin, name, name + '-password-1')
        for name in ('olive', 'carl', 'dana', 'otto'):
            setattr(self, name, self.login(name, name + '-password-1')[0])
        self.project = self.create_project(self.olive, 'Alpha')
        self.other = self.create_project(self.otto, 'Elsewhere')
        for name in ('carl', 'dana'):
            added = self.request('PUT', '/v1/projects/%s/members/%s'
                                 % (self.project, self.ids[name]), {'role': 'contributor'},
                                 token=self.olive)
            self.assertEqual(200, added.status, added.data)
        # Two lanes, both holding work: each contributor claims the task it holds.
        self.carl_task = self.create_task(self.olive, self.project, "Carl's work").data['id']
        self.dana_task = self.create_task(self.olive, self.project, "Dana's work").data['id']
        for token, task in ((self.carl, self.carl_task), (self.dana, self.dana_task)):
            claimed = self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                                   % (self.project, task), {}, token=token)
            self.assertEqual(200, claimed.status, claimed.data)

    # -- fixtures --------------------------------------------------------------
    def path(self, suffix=''):
        return '/v1/projects/%s%s' % (self.project, suffix)

    def lanes(self, token, query=''):
        return self.request('GET', self.path('/lanes' + query), token=token)

    def plant_coordination(self, operators=(), sessions=()):
        """The host's ``host-coordination`` answer: the endpoint's own operator inputs.

        The installation's operator allowlist and the project's session registry (each
        session actor with the principal it is mapped to) are what
        ``review_workflow.listed_operator_actor`` is given, so the route's scope is decided
        by the same definition -- and therefore the same data -- the endpoint itself uses.
        """
        document = {'schema_version': 1, 'operators': sorted(operators),
                    'sessions': [dict(item) for item in sessions], 'sessions_note': None}
        self.backend.host_coordination = lambda project_id, fresh=False: dict(document)
        return document

    def plant_declared_wait(self, task_id, actor, age_seconds=2 * 86400,
                            state='waiting-on-coordinator', waiting_on='coordinator',
                            expect_by=None):
        """Plant the checkpoint and the lane-state record a lane declared its wait with.

        Timestamps are set by the test, so the threshold's effect on the flags is the
        test's own data and never the machine's clock.
        """
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
        comments = self.backend.state['tasks'][task_id].setdefault('comments', [])
        comments.append({'id': 'comment_checkpoint_planted', 'issue_id': task_id, 'author': actor,
                         'text': 'a checkpoint', 'created_at': stamp})
        payload = {'schema_version': 1, 'task': task_id, 'checkpoint': 'comment_checkpoint_planted',
                   'state': state, 'waiting_on': waiting_on}
        if expect_by is not None:
            payload['expect_by'] = expect_by
        comments.append({'id': 'comment_lane_state_planted', 'issue_id': task_id, 'author': actor,
                         'text': lane_state.PREFIX + canonical_bytes(payload).decode(),
                         'created_at': stamp})
        return stamp

    def lane_row(self, payload, actor):
        return next(row for row in payload['items'] if row['actor'] == actor)

    def held_entry(self, payload, task_id):
        for row in payload['items']:
            for entry in row['held']:
                if entry['task'] == task_id:
                    return row, entry
        raise AssertionError('task %s is on no lane: %r' % (task_id, payload['items']))

    def plant_fault(self, task_id, actor, reason='the merge slot is stuck', age_seconds=600):
        """Plant a checkpoint and a needs-investigation record, as the checkpoint writer would."""
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
        comments = self.backend.state['tasks'][task_id].setdefault('comments', [])
        comments.append({'id': 'comment_fault_checkpoint', 'issue_id': task_id, 'author': actor,
                         'text': 'a checkpoint', 'created_at': stamp})
        payload = {'schema_version': 1, 'task': task_id, 'checkpoint': 'comment_fault_checkpoint',
                   'state': 'needs-investigation', 'reason': reason}
        comments.append({'id': 'comment_fault_planted', 'issue_id': task_id, 'author': actor,
                         'text': lane_state.PREFIX + canonical_bytes(payload).decode(), 'created_at': stamp})

    def test_a_raised_fault_is_on_the_lane_board_row_for_the_owner_and_the_lane_only(self):
        self.plant_coordination(operators=[self.ids['olive']])
        self.plant_fault(self.carl_task, self.ids['carl'])
        every = self.lanes(self.olive).data
        fault = self.lane_row(every, self.ids['carl'])['needs_investigation']
        self.assertEqual(('comment_fault_planted', 'the merge slot is stuck', self.carl_task),
                         (fault['record'], fault['reason'], fault['task']))
        self.assertGreaterEqual(fault['age_seconds'], 600)
        self.assertIsNone(self.lane_row(every, self.ids['dana'])['needs_investigation'])
        self.assertEqual(1, every['flags']['needs_investigation'])
        own = self.lanes(self.dana).data
        self.assertNotIn('the merge slot is stuck', json.dumps(own))
        mine = self.lane_row(self.lanes(self.carl).data, self.ids['carl'])['needs_investigation']
        self.assertEqual({k: v for k, v in fault.items() if k != 'age_seconds'},
                         {k: v for k, v in mine.items() if k != 'age_seconds'})

    # -- the threshold is the page's argument ----------------------------------
    def test_the_threshold_reaches_the_one_read_and_the_flags_change_with_it(self):
        self.plant_coordination(operators=[self.ids['olive']])
        self.plant_declared_wait(self.carl_task, self.ids['carl'])

        narrow = self.lanes(self.olive)
        self.assertEqual(200, narrow.status, narrow.data)
        narrow = narrow.data
        self.assertEqual(15, narrow['idle_minutes'])          # the read's own default
        row, entry = self.held_entry(narrow, self.carl_task)
        self.assertEqual('waiting-on-coordinator', entry['state'])
        self.assertEqual('coordinator', entry['waiting_on'])
        self.assertFalse(entry['inferred'])
        self.assertTrue(entry['declared'])
        self.assertTrue(170000 < entry['state_age_seconds'] < 175000, entry['state_age_seconds'])
        self.assertIn('waiting_too_long', entry['flags'])
        self.assertTrue(row['waiting_too_long'])
        self.assertEqual([self.carl_task], row['flag_tasks']['waiting_too_long'])
        self.assertEqual('lanes-v1', narrow['schema'])

        # The same board read with a wider threshold: the SAME declared state and the SAME
        # age, and no waiting_too_long flag. Nothing on this side decided either.
        wide = self.lanes(self.olive, '?idle_minutes=10080')
        self.assertEqual(200, wide.status, wide.data)
        wide = wide.data
        self.assertEqual(10080, wide['idle_minutes'])
        row, entry = self.held_entry(wide, self.carl_task)
        self.assertEqual('waiting-on-coordinator', entry['state'])
        self.assertEqual(narrow['items'][0]['held'][0]['state_age_seconds'],
                         entry['state_age_seconds'])
        self.assertNotIn('waiting_too_long', entry['flags'])
        self.assertFalse(row['waiting_too_long'])
        self.assertEqual({}, row['flag_tasks'])
        self.assertEqual(0, wide['flags']['waiting_too_long'])
        # A default read and an explicit 15 are the same read.
        self.assertEqual(narrow['flags'], self.lanes(self.olive, '?idle_minutes=15').data['flags'])

    def test_a_threshold_outside_the_reads_range_is_refused_400(self):
        for value in ('0', '10081', 'soon', '15.5', '-3'):
            with self.subTest(idle_minutes=value):
                refused = self.lanes(self.olive, '?idle_minutes=' + value)
                self.assertEqual(400, refused.status, refused.data)
                self.assertIn('idle_minutes', refused.data['error']['message'])
                self.assertIn('1', refused.data['error']['message'])
                self.assertIn('10080', refused.data['error']['message'])
        # A read that names no threshold at all is the read's own default, never a
        # refusal; a blank one is malformed, not the default (kittrial-5bb.389).
        self.assertEqual(200, self.lanes(self.olive).status)

    def test_a_blank_repeated_or_non_digit_threshold_is_refused_400(self):
        for value in ('', '1_5', '+15', '%2015'):
            with self.subTest(idle_minutes=value):
                refused = self.lanes(self.olive, '?idle_minutes=' + value)
                self.assertEqual(400, refused.status, refused.data)
                self.assertIn('idle_minutes', refused.data['error']['message'])
        repeated = self.lanes(self.olive, '?idle_minutes=15&idle_minutes=30')
        self.assertEqual(400, repeated.status, repeated.data)
        self.assertIn('idle_minutes', repeated.data['error']['message'])

    def test_an_owner_sees_every_lane_and_a_contributor_sees_only_their_own(self):
        # The route asks the HOST who an operator is (design section 4), and never the
        # transport: this service calls the endpoint without a bound key.
        self.plant_coordination(operators=[self.ids['olive']])
        every = self.lanes(self.olive)
        self.assertEqual(200, every.status, every.data)
        every = every.data
        self.assertEqual('all', every['scope'])
        self.assertEqual({self.ids['carl'], self.ids['dana']},
                         {row['actor'] for row in every['items']})
        self.assertIn(self.carl_task, [entry['task'] for row in every['items']
                                       for entry in row['held']])

        own = self.lanes(self.carl)
        self.assertEqual(200, own.status, own.data)
        own = own.data
        self.assertEqual('own', own['scope'])
        self.assertEqual([self.ids['carl']], [row['actor'] for row in own['items']])
        self.assertEqual([self.carl_task],
                         [entry['task'] for row in own['items'] for entry in row['held']])
        self.assertNotIn(self.ids['dana'], json.dumps(own))
        self.assertEqual(1, own['total'])

    def test_an_actor_of_a_listed_operators_principal_reads_every_lane_too(self):
        # The second half of the kit's ONE operator definition: an actor the project's
        # registry gives to a listed operator's principal. The owners map is read from the
        # same host answer, so the route cannot judge it differently from the endpoint.
        self.plant_coordination(
            operators=['session-op'],
            sessions=[{'actor': 'session-op', 'name': 'op-lane', 'principal': 'person:olive'},
                      {'actor': self.ids['olive'], 'name': 'olive-lane', 'principal': 'person:olive'}])
        every = self.lanes(self.olive)
        self.assertEqual(200, every.status, every.data)
        self.assertEqual('all', every.data['scope'])
        self.assertEqual(2, every.data['total'])

    def test_a_host_that_cannot_say_who_is_an_operator_is_scoped_not_widened(self):
        # No ``host_coordination`` at all (the disposable backend has no host): the answer
        # is the narrower view, never the wider one -- and olive's project ROLE alone does
        # not widen it, because the scope is the kit's operator definition, not a new rule.
        self.assertFalse(hasattr(self.backend, 'host_coordination'))
        scoped = self.lanes(self.olive)
        self.assertEqual(200, scoped.status, scoped.data)
        self.assertEqual('own', scoped.data['scope'])
        self.assertEqual([], scoped.data['items'])

    def test_a_non_member_and_a_member_of_another_project_read_nothing(self):
        planted = self.backend.lanes_read
        asked = []

        def watch(*args, **kwargs):
            asked.append(args)
            return planted(*args, **kwargs)
        self.backend.lanes_read = watch
        # otto owns another project and is no member here: the answer is the answer for a
        # project that does not exist, and the board is not read at all.
        hidden = self.request('GET', '/v1/projects/%s/lanes' % self.project, token=self.otto)
        missing = self.request('GET', '/v1/projects/proj_missing0000/lanes', token=self.otto)
        self.assertEqual(404, hidden.status, hidden.data)
        self.assertEqual(404, missing.status, missing.data)
        self.assertEqual(without_request_id(hidden.data), without_request_id(missing.data))
        self.assertEqual(401, self.request('GET', self.path('/lanes')).status)
        self.assertEqual([], asked)
        # A member of the project is answered from the read, so the watch proves it is called.
        self.assertEqual(200, self.lanes(self.carl).status)
        self.assertEqual(1, len(asked))

    def test_the_page_is_bounded_and_its_cursor_continues_the_same_read(self):
        self.plant_coordination(operators=[self.ids['olive']])
        first = self.lanes(self.olive, '?limit=1')
        self.assertEqual(200, first.status, first.data)
        first = first.data
        self.assertEqual(1, len(first['items']))
        self.assertEqual(2, first['total'])
        self.assertEqual(1, first['next_offset'])
        self.assertTrue(first['next_cursor'])
        second = self.request('GET', self.path('/lanes?limit=1&cursor=%s' % first['next_cursor']),
                              token=self.olive)
        self.assertEqual(200, second.status, second.data)
        second = second.data
        self.assertEqual(1, len(second['items']))
        self.assertNotEqual(first['items'][0]['actor'], second['items'][0]['actor'])
        self.assertIsNone(second['next_offset'])
        self.assertIsNone(second['next_cursor'])
        # A cursor belongs to the query that minted it: another threshold is a stale cursor.
        stale = self.lanes(self.olive, '?limit=1&idle_minutes=30&cursor=%s' % first['next_cursor'])
        self.assertEqual(409, stale.status, stale.data)
        # The read's own bound is the route's: 1..100.
        self.assertEqual(422, self.lanes(self.olive, '?limit=101').status)


class EndpointLanesReadCase(unittest.TestCase):
    """The production backend's request: what travels to the endpoint, unchanged."""

    def backend(self):
        from http_service import EndpointBackend
        requests = []

        class Recording(EndpointBackend):
            def __init__(self):
                # No endpoint process is ever launched: `_endpoint` answers for it below, so
                # the transport's own set-up (paths, the store, the runner) is not needed.
                pass

            def _endpoint(self, action, project, actor, args, *rest, **kwargs):
                requests.append({'action': action, 'project': project, 'actor': actor,
                                 'args': list(args), 'extra': kwargs.get('extra')})
                return {'returncode': 0, 'stdout': json.dumps({'schema': 'lanes-v1', 'items': [],
                                                               'total': 0}), 'stderr': ''}
        return Recording(), requests

    def test_the_work_action_carries_the_threshold_and_the_routes_own_scope(self):
        backend, requests = self.backend()
        read = backend.lanes_read('alpha',
                                  ['--idle-minutes', '45', '--limit', '10', '--offset', '10'],
                                  'alex', 'own')
        self.assertEqual('lanes-v1', read['schema'])
        self.assertEqual(1, len(requests))
        sent = requests[0]
        self.assertEqual('work', sent['action'])
        self.assertEqual('alpha', sent['project'])
        # The threshold and the page are the read's own arguments, handed on unchanged.
        self.assertEqual(['lanes', '--idle-minutes', '45', '--limit', '10', '--offset', '10'],
                         sent['args'])
        # The route's scope, as the request field the endpoint narrows its own scope to.
        self.assertEqual({'lanes_scope': 'own'}, sent['extra'])
        self.assertEqual('alex', sent['actor'])

    def test_a_read_that_does_not_answer_a_lanes_payload_is_an_uncertain_read(self):
        backend, _ = self.backend()
        backend._endpoint = lambda *args, **kwargs: {'returncode': 0, 'stdout': '"not an object"',
                                                     'stderr': ''}
        with self.assertRaises(Exception) as raised:
            backend.lanes_read('alpha', ['--idle-minutes', '15'], 'alex', 'all')
        self.assertEqual(503, getattr(raised.exception, 'status', None))


@unittest.skipIf(endpoint is None, 'endpoint.py imports fcntl; POSIX only')
class LanesScopeRequestCase(unittest.TestCase):
    """A request may NARROW the endpoint's scope; no request field can widen it."""

    def test_only_the_narrowing_request_is_honoured(self):
        host = endpoint.lanes_scope_for('someone', [], None)
        contributor = endpoint.lanes_scope_for('someone', [], 'a-bound-key')
        operator = endpoint.lanes_scope_for('ops', ['ops'], 'a-bound-key')
        self.assertEqual('all', host)
        self.assertEqual('own', contributor)
        self.assertEqual('all', operator)
        # The web route asks for 'own' explicitly (design section 4).
        self.assertEqual('own', endpoint.lanes_scope_requested(host, 'own'))
        # A request field never widens: 'all', an empty value or nonsense leaves it alone.
        for requested in ('all', '', None, 'ALL', 'any'):
            with self.subTest(requested=requested):
                self.assertEqual('own', endpoint.lanes_scope_requested(contributor, requested))
                self.assertEqual('all', endpoint.lanes_scope_requested(operator, requested))
        # And the endpoint's own callers keep their scope (no field travels with them).
        self.assertEqual('own', endpoint.lanes_scope_requested(contributor, None))


class LanesPageTests(unittest.TestCase):
    """The panel under Node: what it renders, and that the threshold is its argument."""

    def test_the_panel_renders_the_read_and_re_reads_with_a_new_threshold(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        done = test_http_web.run_node_module(
            self, node, 'await import(process.argv[1])',
            (KIT / 'tests' / 'web_lanes_page.mjs').as_uri(),
            (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
            (KIT / 'web' / 'js' / 'api.js').as_uri(),
            (KIT / 'web' / 'js' / 'views' / 'project.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])

        # The read the panel issues is the route's, with the threshold it starts from.
        self.assertIn('/v1/projects/alpha/lanes', seen['firstRead'])
        self.assertIn('idle_minutes=15', seen['firstRead'])
        self.assertEqual('15', seen['threshold'])
        # Every lane, its actor, and the tasks it holds with their review facts.
        self.assertEqual(['lane-a', 'lane-c', 'lane-b'], seen['lanes'])
        self.assertEqual(['alpha-1', 'alpha-3'], seen['taskIds'])
        text = seen['text']
        for wanted in ('session-a', 'session-b', 'session-c',
                       'Ship the lanes panel', 'An older checkpoint',
                       'changes-requested', 'whose move you', 'next assignee',
                       'waiting-on-coordinator', 'coordinator', '2026-10-12T00:00:00+00:00'):
            self.assertIn(wanted, text, wanted)
        # The ages are the server's, said as spans (2 days for the state, 2 h for the write).
        self.assertIn('in that state 2 days', text)
        self.assertIn('last write 2 h', text)
        # The three flags, each naming the tasks under it, and the free lane said as free.
        flags = seen['flagChips']
        self.assertTrue(any(f == 'waiting_too_long: Waiting too long: alpha-1' for f in flags), flags)
        self.assertTrue(any(f == 'expect_by_passed: Expect-by passed: alpha-1' for f in flags), flags)
        self.assertTrue(any(f == "your_move_idle: Holder's move idle: alpha-3" for f in flags), flags)
        self.assertTrue(seen['freeText'], seen['text'])
        self.assertIn('Holds nothing', seen['freeText'][0])
        self.assertIn('5 min', seen['freeText'][0])
        # The guessed row is marked as guessed; the declared one is not.
        self.assertEqual([None, 'true'], seen['inferredTasks'])
        self.assertEqual(1, len(seen['guessedChips']), seen['guessedChips'])
        self.assertIn('guessed', seen['guessedChips'][0].lower())
        # The counts line names all three flags and the threshold that was used.
        self.assertTrue(seen['countsLine'], seen['text'])
        self.assertIn('15 idle minutes', seen['countsLine'][0])
        self.assertIn('1 waiting too long', seen['countsLine'][0])
        # A raised fault shows its reason as text (never markup), where and how long, and the
        # older open reasons, beside the lane that raised it (kittrial-5bb.398).
        self.assertEqual(1, len(seen['fault']), seen['fault'])
        for wanted in ('Needs investigation', 'the <b>merge</b> slot is stuck', '1 h ago', 'alpha-4', 'alpha-5'):
            self.assertIn(wanted, seen['fault'][0], wanted)
        self.assertEqual(['Earlier open: credentials leaked'], seen['faultEarlier'])
        # The panel is composed into the project overview.
        self.assertEqual(1, seen['overviewHasPanel'])
        # Next reads the read's own next page, with the SAME threshold.
        self.assertIn('cursor=', seen['pagedRead'])
        self.assertIn('idle_minutes=15', seen['pagedRead'])
        # A value outside the read's range is refused before any read is issued.
        self.assertIn('1 to 10080', seen['refusedText'])
        self.assertTrue(seen['refusedIssuedNoRead'])
        # A new threshold re-reads from the first page with the new value, and what shows is
        # what that read answered (no flag at 45 minutes).
        self.assertIn('idle_minutes=45', seen['thresholdRead'])
        self.assertNotIn('cursor=', seen['thresholdRead'])
        self.assertIn('45 idle minutes', seen['afterThreshold'])
        self.assertEqual([], [c for c in seen['thresholdChips'] if c.startswith('waiting_too_long')],
                         seen['thresholdChips'])
        # A refused read says why and offers to read again.
        self.assertIn('No access', seen['refusal'])
        self.assertIn('Your access to this project has ended.', seen['refusal'])
        self.assertTrue(seen['refusalHasRetry'])


if __name__ == '__main__':
    unittest.main()
