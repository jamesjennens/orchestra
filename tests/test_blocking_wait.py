"""The blocking wait: poll cheaply, wake only when it is your move (kittrial-5bb.285).

Every test here drives the wait with a FAKE clock and a scripted read: nothing sleeps,
nothing reads a tracker, and the poll's cost is asserted by counting the reads. The
rules pinned are the task's: both forms return as soon as the move appears, time out,
and answer exit 3 when the service cannot be reached; the fingerprint makes a restart
safe; the interval's floor is 60 and its default 300; ``--timeout`` bounds the wait; one
cheap read happens per poll; the coordinator's four triggers each work; and the worker
and coordinator texts say run-wait-act-run-wait.
"""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import client
import waiting
import work

try:
    import sessions
    import test_http_agents
except ImportError:                       # endpoint/http service need POSIX
    sessions = test_http_agents = None

A = 'session-aaaaaaaa-1111-4111-8111-111111111111'


class FakeClock:
    """A clock that only moves when something sleeps, and remembers every nap."""

    def __init__(self, start=0.0):
        self.now = start
        self.naps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.naps.append(seconds)
        self.now += seconds


def scripted(views):
    """A read callable that answers with each view in turn, repeating the last one.

    An exception in ``views`` is raised instead of returned, so a transport failure is
    part of the script.
    """
    state = {'calls': 0}

    def read():
        index = min(state['calls'], len(views) - 1)
        state['calls'] += 1
        value = views[index]
        if isinstance(value, BaseException):
            raise value
        return value

    read.calls = lambda: state['calls']
    return read


def empty_worker():
    return waiting.worker_view([])


def worker(*items):
    return waiting.worker_view(list(items))


def lane(name, actor, held=None, **flags):
    row = {'lane': name, 'actor': actor, 'held': list(held or []),
           'flag_tasks': {key: [] for key, value in flags.items() if value}}
    row.update(flags)
    return row


def lanes_page(*rows):
    return {'schema': 'lanes-v1', 'items': list(rows)}


def empty_lanes():
    return lanes_page()


class WorkerFormTests(unittest.TestCase):
    """The worker form blocks, polls and returns on its own move, with no real sleep."""

    def test_it_returns_as_soon_as_the_move_appears(self):
        clock = FakeClock()
        read = scripted([empty_worker(), empty_worker(),
                         worker({'task': 't1', 'whose_move': 'you', 'review_state': 'changes-requested',
                                 'pending_review_items': 2})])
        code, view = waiting.run_wait(read, interval=60, timeout=600,
                                      clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual([row['task'] for row in view['items']], ['t1'])
        self.assertTrue(waiting.is_fingerprint(view['fingerprint']))
        self.assertEqual(clock.naps, [60, 60])          # two polls, two intervals, no more
        self.assertEqual(read.calls(), 3)

    def test_it_returns_at_once_when_the_first_read_already_has_work(self):
        clock = FakeClock()
        read = scripted([worker({'task': 't1', 'whose_move': 'you'})])
        code, view = waiting.run_wait(read, interval=60, timeout=600,
                                      clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(clock.naps, [])                # it never waited
        self.assertEqual(read.calls(), 1)

    def test_it_times_out_when_nothing_becomes_the_move(self):
        clock = FakeClock()
        read = scripted([empty_worker()])
        code, view = waiting.run_wait(read, interval=60, timeout=120,
                                      clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertTrue(view['timed_out'])
        self.assertEqual(clock.naps, [60, 60])          # bounded by --timeout, not forever
        self.assertLessEqual(sum(clock.naps), 120)

    def test_it_exits_three_when_the_service_cannot_be_reached(self):
        clock = FakeClock()
        read = scripted([waiting.WaitUnreachable('Local endpoint failed (1)')])
        code, view = waiting.run_wait(read, interval=60, timeout=600,
                                      clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_UNREACHABLE)
        self.assertIn('cannot be reached', view['error'])
        self.assertEqual(clock.naps, [])

    def test_an_endpoint_refusal_is_a_usage_error_not_an_unreachable_service(self):
        clock = FakeClock()
        read = scripted([waiting.WaitRefused('Invalid wait read')])
        code, _view = waiting.run_wait(read, interval=60, timeout=600,
                                       clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_USAGE)


class CoordinatorFormTests(unittest.TestCase):
    """The coordinator form uses the same loop over the four triggers (kittrial-5bb.263)."""

    def coordinator(self, queue=None, lanes=None):
        return waiting.coordinator_view(list(queue or []), lanes if lanes is not None else empty_lanes())

    def test_it_returns_as_soon_as_a_trigger_appears(self):
        clock = FakeClock()
        read = scripted([self.coordinator(),
                         self.coordinator(queue=[{'task': 't1', 'review_state': 'awaiting-review',
                                                  'contribution_id': 'c1'}])])
        code, view = waiting.run_wait(read, interval=60, timeout=600,
                                      clock=clock.clock, sleep=clock.sleep,
                                      form='coordinator')
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(view['form'], 'coordinator')
        self.assertEqual([row['task'] for row in view['triggers']['awaiting-review']], ['t1'])
        self.assertEqual(clock.naps, [60])

    def test_it_times_out_when_no_trigger_appears(self):
        clock = FakeClock()
        read = scripted([self.coordinator()])
        code, view = waiting.run_wait(read, interval=60, timeout=60,
                                      clock=clock.clock, sleep=clock.sleep,
                                      form='coordinator')
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertTrue(view['timed_out'])

    def test_it_exits_three_when_the_service_cannot_be_reached(self):
        clock = FakeClock()
        read = scripted([waiting.WaitUnreachable('SSH timed out')])
        code, view = waiting.run_wait(read, interval=60, timeout=600,
                                      clock=clock.clock, sleep=clock.sleep,
                                      form='coordinator')
        self.assertEqual(code, waiting.EXIT_UNREACHABLE)
        self.assertIn('cannot be reached', view['error'])

    # -- the four triggers, each on its own ------------------------------------------------

    def test_trigger_a_new_delivery_awaiting_review(self):
        view = self.coordinator(queue=[{'task': 't1', 'review_state': 'awaiting-review',
                                        'contribution_id': 'c1'},
                                       {'task': 't2', 'review_state': 'none'}])
        self.assertEqual([row['task'] for row in view['triggers']['awaiting-review']], ['t1'])
        self.assertEqual(view['triggers']['awaiting-integration'], [])

    def test_trigger_approved_work_awaiting_integration(self):
        for state in ('awaiting-integration', 'approved'):
            with self.subTest(state=state):
                view = self.coordinator(queue=[{'task': 't2', 'review_state': state,
                                                'contribution_id': 'c2'}])
                self.assertEqual([row['task'] for row in view['triggers']['awaiting-integration']],
                                 ['t2'])

    def test_trigger_a_checkpoint_addressed_to_the_coordinator(self):
        lanes = lanes_page(lane('lane-a', A, held=[
            {'task': 't3', 'state': 'waiting-on-coordinator', 'next_actor': 'coordinator',
             'whose_move': 'coordinator', 'flags': []}]))
        view = self.coordinator(lanes=lanes)
        self.assertEqual([row['task'] for row in view['triggers']['checkpoint-to-coordinator']],
                         ['t3'])
        # A declared waiting_on of the role counts too, without the state spelling it.
        other = self.coordinator(lanes=lanes_page(lane('lane-b', A, held=[
            {'task': 't4', 'state': 'working', 'waiting_on': 'coordinator', 'flags': []}])))
        self.assertEqual([row['task'] for row in other['triggers']['checkpoint-to-coordinator']],
                         ['t4'])

    def test_trigger_a_lane_quiet_on_its_own_move_or_past_a_declared_wait(self):
        for flag in waiting.LANE_FLAGS:
            with self.subTest(flag=flag):
                lanes = lanes_page(lane('lane-a', A, held=[], **{flag: True}))
                view = self.coordinator(lanes=lanes)
                self.assertEqual([row['lane'] for row in view['triggers']['lane-quiet']], ['lane-a'])
                self.assertIn(flag, view['triggers']['lane-quiet'][0]['flags'])

    def test_a_quiet_project_triggers_nothing(self):
        view = self.coordinator(queue=[{'task': 't1', 'review_state': 'none'}],
                                lanes=lanes_page(lane('lane-a', A, held=[
                                    {'task': 't1', 'state': 'working', 'flags': []}])))
        self.assertEqual(view['fingerprint'], waiting.digest({name: [] for name in waiting.COORDINATOR_TRIGGERS}))
        self.assertEqual(view['items'], [])


class FingerprintTests(unittest.TestCase):
    """``--since`` makes a restart safe: the same state blocks, a changed one returns."""

    def test_the_same_state_does_not_return_the_same_item_twice(self):
        clock = FakeClock()
        seen = worker({'task': 't1', 'whose_move': 'you', 'review_state': 'changes-requested'})
        read = scripted([seen, seen, seen])
        code, _view = waiting.run_wait(read, since=seen['fingerprint'], interval=60, timeout=120,
                                       clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)        # never returned it again
        self.assertGreaterEqual(read.calls(), 2)
        self.assertEqual(clock.naps, [60, 60])
        # Without --since the same read is returned: the baseline is what makes it safe.
        first = scripted([seen])
        code, _view = waiting.run_wait(first, interval=60, timeout=120,
                                       clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_WORK)

    def test_a_changed_item_returns_again(self):
        clock = FakeClock()
        before = worker({'task': 't1', 'whose_move': 'you', 'review_state': 'changes-requested',
                         'pending_review_items': 1})
        after = worker({'task': 't1', 'whose_move': 'you', 'review_state': 'changes-requested',
                        'pending_review_items': 3})
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        read = scripted([before, after])
        code, view = waiting.run_wait(read, since=before['fingerprint'], interval=60, timeout=600,
                                      clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(view['fingerprint'], after['fingerprint'])

    def test_the_fingerprint_ignores_field_order_and_read_times(self):
        one = worker({'task': 't1', 'whose_move': 'you', 'review_state': 'awaiting-review',
                      'open_items': 1})
        two = worker({'review_state': 'awaiting-review', 'open_items': 1, 'task': 't1',
                      'whose_move': 'you', 'generated_at': '2030-01-01T00:00:00Z'})
        self.assertEqual(one['fingerprint'], two['fingerprint'])

    def test_a_marker_change_alone_returns_again(self):
        """kittrial-5bb.369: a second other-party comment wakes through the wait marker."""
        clock = FakeClock()
        before = worker({'task': 't1', 'whose_move': 'you', 'review_state': 'in-progress',
                         'wait_newer_activity_marker': 't1-ca:aaaa'})
        after = worker({'task': 't1', 'whose_move': 'you', 'review_state': 'in-progress',
                        'wait_newer_activity_marker': 't1-cb:bbbb'})
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        read = scripted([before, after])
        code, view = waiting.run_wait(read, since=before['fingerprint'], interval=60, timeout=600,
                                      clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(view['fingerprint'], after['fingerprint'])
        # A marker inside the web queue row's attention block is lifted too: the two
        # transports must fingerprint the same task the same way.
        nested = worker({'task': 't1', 'whose_move': 'you', 'review_state': 'in-progress',
                         'attention': {'newer_activity': True,
                                       'wait_newer_activity_marker': 't1-cb:bbbb'}})
        self.assertEqual(nested['fingerprint'], after['fingerprint'])

    def test_the_callers_own_checkpoint_does_not_change_a_worker_fingerprint(self):
        """kittrial-5bb.369: the plan checkpoint must not wake the wait that wrote it."""
        before = worker({'task': 't1', 'whose_move': 'you', 'review_state': 'in_progress',
                         'owner': 'alice/session', 'status': 'in_progress',
                         'checkpoint_at': '2026-09-15T10:00:00Z', 'open_items': 0,
                         'blocking_items': 0, 'newer_activity': False,
                         'newer_activity_marker': 'fields:aaaa',
                         'wait_newer_activity_marker': 'fields:bbbb'})
        after = worker({'task': 't1', 'whose_move': 'you', 'review_state': 'in_progress',
                        'owner': 'alice/session', 'status': 'in_progress',
                        'checkpoint_at': '2026-09-15T11:00:00Z', 'open_items': 2,
                        'blocking_items': 1, 'newer_activity': True,
                        'newer_activity_marker': 't1-cdir:cccc',
                        'wait_newer_activity_marker': 'fields:bbbb'})
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        clock = FakeClock()
        read = scripted([after, after])
        code, _view = waiting.run_wait(read, since=before['fingerprint'], interval=60,
                                       timeout=120, clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)      # nothing new: the wait blocks
        # A coordinator comment moves the stable marker and the same wait returns.
        woke = worker({'task': 't1', 'whose_move': 'you', 'review_state': 'in_progress',
                       'wait_newer_activity_marker': 't1-cdir:dddd'})
        self.assertNotEqual(before['fingerprint'], woke['fingerprint'])
        read = scripted([woke])
        code, view = waiting.run_wait(read, since=before['fingerprint'], interval=60,
                                      timeout=120, clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(view['fingerprint'], woke['fingerprint'])

    def test_own_status_change_wakes_once_in_both_wait_implementations(self):
        import client
        for module in (waiting, client._InlineWait):
            with self.subTest(module=module):
                base = {'task': 't1', 'whose_move': 'you', 'owner': 'agent/sub0',
                        'status': 'open', 'wait_newer_activity_marker': 'fields:aaaa'}
                before = module.worker_view([base])
                after = module.worker_view([dict(base, status='blocked')])
                clock = FakeClock()
                code, view = module.run_wait(scripted([after]), since=before['fingerprint'],
                    interval=60, timeout=60, clock=clock.clock, sleep=clock.sleep)
                self.assertEqual(module.EXIT_WORK, code)
                code, _ = module.run_wait(scripted([after]), since=view['fingerprint'],
                    interval=60, timeout=60, clock=clock.clock, sleep=clock.sleep)
                self.assertEqual(module.EXIT_TIMEOUT, code)

    def test_everything_by_others_still_wakes_a_worker(self):
        base = {'task': 't1', 'whose_move': 'you', 'review_state': 'in_progress',
                'owner': 'alice/session', 'status': 'in_progress',
                'wait_newer_activity_marker': 'fields:bbbb', 'pending_review_items': 0}
        first = worker(base)['fingerprint']
        cases = {
            'review state': {'review_state': 'changes-requested'},
            'pending review items': {'pending_review_items': 3},
            'assignment': {'owner': 'someone/else'},
            'claim/release': {'status': 'open'},
            'contribution': {'contribution_id': 'c1'},
            'redelivered': {'redelivered': True},
            'guidance': {'guidance_version': 'v2'},
            'stable marker': {'wait_newer_activity_marker': 't1-cdir:dddd'},
        }
        for name, change in cases.items():
            with self.subTest(field=name):
                self.assertNotEqual(first, worker(dict(base, **change))['fingerprint'])
        # Whose move changes: the row leaves the worker's list.
        self.assertNotEqual(first, worker(dict(base, whose_move='them'))['fingerprint'])

    def test_the_wait_marker_is_lifted_from_the_attention_block(self):
        flat = worker({'task': 't1', 'whose_move': 'you',
                       'wait_newer_activity_marker': 't1-cdir:dddd'})
        nested = worker({'task': 't1', 'whose_move': 'you',
                         'attention': {'wait_newer_activity_marker': 't1-cdir:dddd'}})
        self.assertEqual(flat['fingerprint'], nested['fingerprint'])

    def test_the_coordinator_fingerprint_ignores_the_wait_marker(self):
        lanes = empty_lanes()
        one = waiting.coordinator_view([{'task': 't1', 'review_state': 'awaiting-review',
                                         'contribution_id': 'c1',
                                         'wait_newer_activity_marker': 'a'}], lanes)
        two = waiting.coordinator_view([{'task': 't1', 'review_state': 'awaiting-review',
                                         'contribution_id': 'c1',
                                         'wait_newer_activity_marker': 'b'}], lanes)
        self.assertEqual(one['fingerprint'], two['fingerprint'])

    def test_a_fingerprint_is_versioned(self):
        self.assertTrue(waiting.is_fingerprint(waiting.fingerprint([])))
        self.assertFalse(waiting.is_fingerprint('wait1:xyz'))
        self.assertFalse(waiting.is_fingerprint(''))


class BoundsTests(unittest.TestCase):
    """The interval floor, its default and the timeout bound (the task's numbers)."""

    def test_the_default_interval_is_300_and_the_floor_is_60(self):
        self.assertEqual(waiting.DEFAULT_INTERVAL, 300)
        self.assertEqual(waiting.MIN_INTERVAL, 60)
        self.assertEqual(waiting.parse_client_args([]).interval, 300)
        self.assertEqual(waiting.parse_client_args(['--interval', '60']).interval, 60)
        for too_short in ('0', '30', '59', '-1'):
            with self.subTest(interval=too_short):
                with self.assertRaises(ValueError):
                    waiting.parse_client_args(['--interval', too_short])

    def test_the_timeout_bounds_the_wait(self):
        clock = FakeClock()
        read = scripted([empty_worker()])
        code, _view = waiting.run_wait(read, interval=60, timeout=90,
                                       clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertLessEqual(clock.now, 90)
        self.assertLessEqual(sum(clock.naps), 90)       # the last nap is cut to the bound

    def test_since_takes_a_fingerprint_this_kit_printed(self):
        real = waiting.fingerprint([])
        self.assertEqual(waiting.parse_client_args(['--since', real]).since, real)
        for bad in ('nonsense', 'wait1:', 'wait1:xyz'):
            with self.subTest(since=bad):
                with self.assertRaises(ValueError):
                    waiting.parse_client_args(['--since', bad])


class OneCheapReadTests(unittest.TestCase):
    """One cheap read per poll: one export, no refresh, no read per task (kittrial-5bb.281)."""

    def row(self, task_id, assignee=A, status='open'):
        return dict(id=task_id, title='Task ' + task_id, issue_type='task', status=status,
                    assignee=assignee, description='Intent', acceptance_criteria='Acceptance',
                    labels=[], priority=2, created_by=assignee,
                    created_at='2026-09-01T00:00:00Z', updated_at='2026-09-15T09:00:00Z',
                    dependencies=[], comments=[])

    def runner(self, rows, calls):
        lines = '\n'.join(json.dumps(row) for row in rows)

        def run(argv):
            calls.append(list(argv))
            return lines
        return run

    def test_the_worker_read_is_exactly_one_export(self):
        calls = []
        result = work.execute(Path('.'), A, 'wait', [], {}, self.runner([self.row('t1')], calls),
                              operators=[A])
        self.assertEqual(calls, [['export', '--all']])
        self.assertEqual(result['schema'], 'wait-v1')
        self.assertEqual([item['task'] for item in result['items']], ['t1'])

    @unittest.skipIf(sessions is None, 'sessions.py needs a POSIX host')
    def test_the_coordinator_read_is_exactly_one_export_too(self):
        calls = []
        registry = {'schema_version': 1, 'records': {
            'r1': {'request_id': 'r1', 'actor': A, 'name': 'lane-a',
                   'created_at': '2026-09-01T00:00:00+00:00'}}}
        with mock.patch.object(sessions, 'read_registry', return_value=registry):
            result = work.execute(Path('.'), A, 'wait', ['--coordinator'], {},
                                  self.runner([self.row('t1')], calls), operators=[A])
        self.assertEqual(calls, [['export', '--all']])
        self.assertEqual(result['form'], 'coordinator')

    def test_the_read_args_never_carry_the_client_poll_bounds(self):
        options = waiting.parse_client_args(['--interval', '60', '--timeout', '120',
                                             '--since', waiting.fingerprint([])])
        self.assertEqual(waiting.read_args(options), [])
        coordinator = waiting.parse_client_args(['--coordinator', '--idle-minutes', '30'])
        self.assertEqual(waiting.read_args(coordinator), ['--coordinator', '--idle-minutes', '30'])


class ClientWiringTests(unittest.TestCase):
    """``client.py wait`` sends the read, prints the view and returns the exit codes."""

    def invoke(self, argv, request):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            with mock.patch.object(sys, 'argv', ['client.py', '--config', str(config),
                                                 '--project', 'example', '--actor', A,
                                                 '--', *argv]), \
                    mock.patch.object(client, 'request', request), \
                    mock.patch('sys.stdout', new_callable=io.StringIO) as output, \
                    mock.patch('sys.stderr', new_callable=io.StringIO) as error:
                code = client.main()
        return code, output.getvalue(), error.getvalue()

    def test_the_loop_blocks_in_the_client_and_the_endpoint_reads_once_per_poll(self):
        calls = []

        def request(_config, _project, _actor, args, action='bd', path=None):
            calls.append((action, list(args)))
            if len(calls) == 1:
                payload = waiting.worker_view([])
            else:
                payload = waiting.worker_view([{'task': 't1', 'whose_move': 'you'}])
            return {'returncode': 0, 'stdout': json.dumps(payload), 'stderr': ''}

        # The client's own clock is replaced so the wait cannot really sleep here.
        ticks = {'now': 0.0}
        with mock.patch('waiting.time.monotonic', lambda: ticks['now']), \
                mock.patch('waiting.time.sleep', lambda seconds: ticks.__setitem__('now', ticks['now'] + seconds)):
            code, out, _err = self.invoke(['wait', '--interval', '60', '--timeout', '600'], request)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(calls, [('wait', []), ('wait', [])])
        body = json.loads(out)
        self.assertEqual([row['task'] for row in body['items']], ['t1'])
        self.assertTrue(waiting.is_fingerprint(body['fingerprint']))

    def test_the_coordinator_form_asks_the_endpoint_for_the_coordinator_read(self):
        calls = []

        def request(_config, _project, _actor, args, action='bd', path=None):
            calls.append((action, list(args)))
            return {'returncode': 0, 'stdout': json.dumps(
                waiting.coordinator_view([{'task': 't1', 'review_state': 'awaiting-review',
                                           'contribution_id': 'c1'}], empty_lanes())), 'stderr': ''}

        code, out, _err = self.invoke(['wait', '--coordinator'], request)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(calls, [('wait', ['--coordinator', '--idle-minutes', '15'])])
        self.assertEqual([row['task'] for row in json.loads(out)['triggers']['awaiting-review']],
                         ['t1'])

    def test_it_exits_three_when_the_service_cannot_be_reached(self):
        def request(*_args, **_kwargs):
            raise RuntimeError('SSH failed (255); outcome may be uncertain.')

        code, out, _err = self.invoke(['wait'], request)
        self.assertEqual(code, waiting.EXIT_UNREACHABLE)
        self.assertIn('cannot be reached', json.loads(out)['error'])

    def test_a_too_short_interval_is_refused_before_any_read(self):
        def request(*_args, **_kwargs):
            raise AssertionError('no read may happen for a refused interval')

        code, out, err = self.invoke(['wait', '--interval', '30'], request)
        self.assertEqual(code, waiting.EXIT_USAGE)
        self.assertEqual(out, '')
        self.assertIn('--interval must be at least 60', err)


class DocumentPinTests(unittest.TestCase):
    """The worker and coordinator texts say run-wait-act-run-wait (the point of item 3)."""

    def flat(self, relative):
        return ' '.join((KIT / relative).read_text(encoding='utf-8')
                        .replace('`', '').lower().split())

    def test_finding_work_says_run_wait_act_run_wait(self):
        text = self.flat('docs/FINDING_WORK.md')
        self.assertIn('run wait; act on what it returns; run wait again', text)
        self.assertIn('wait --coordinator', text)

    def test_the_worker_poll_prompt_says_run_wait_act_run_wait(self):
        text = self.flat('templates/WORKER_POLL_PROMPT.md')
        self.assertIn('run wait; act on what it returns; run wait again', text)

    def test_the_coordinator_prompt_says_run_wait_act_run_wait(self):
        text = self.flat('templates/COORDINATOR_PROMPT.md')
        self.assertIn('run wait --coordinator; act on what it returns; run wait --coordinator again',
                      text)

    def test_the_agent_poll_prompt_names_the_same_read_for_an_agent(self):
        text = self.flat('templates/AGENT_POLL_PROMPT.md')
        self.assertIn('v1/agents/me/wait', text)
        self.assertIn('act on it; read it again', text)


@unittest.skipIf(test_http_agents is None, 'the http service needs a POSIX host')
class HttpAgentWaitTests(test_http_agents.AgentHarness):
    """The same read over HTTP for an agent (the bridge extension's route)."""

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.create_account(self.admin, 'alex', 'alex-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.project = self.create_project(self.alex, 'Alpha')
        self.agent_id, self.secret, _data = self.agent_secret(self.alex, projects=[self.project])

    def claim_a_task(self):
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project,
                            {'title': 'first'}, token=self.secret).data
        claimed = self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                               % (self.project, task['id']), token=self.secret)
        self.assertEqual(200, claimed.status, claimed.data)
        return task['id']

    def test_the_route_returns_the_agents_own_move_and_a_stable_fingerprint(self):
        task_id = self.claim_a_task()
        first = self.request('GET', '/v1/agents/me/wait', token=self.secret)
        self.assertEqual(200, first.status, first.data)
        body = first.data
        self.assertEqual('wait-v1', body['schema'])
        self.assertEqual('worker', body['form'])
        self.assertTrue(waiting.is_fingerprint(body['fingerprint']))
        self.assertIn(task_id, [row['task'] for row in body['items']])
        self.assertTrue(all(row['whose_move'] == 'you' for row in body['items']))
        # A second read of an unchanged state is the same fingerprint: polling the route
        # is not itself a change, so the caller can act only on real movement.
        second = self.request('GET', '/v1/agents/me/wait', token=self.secret)
        self.assertEqual(body['fingerprint'], second.data['fingerprint'])

    def test_the_route_is_one_read_of_the_agents_own_queue(self):
        self.claim_a_task()
        with mock.patch.object(type(self.backend), 'review_queue',
                               wraps=self.backend.review_queue) as counted:
            self.request('GET', '/v1/agents/me/wait', token=self.secret)
        self.assertEqual(counted.call_count, 1)

    def test_the_route_needs_an_agent_credential(self):
        response = self.request('GET', '/v1/agents/me/wait', token=self.alex)
        self.assertEqual(403, response.status, response.data)

    def test_a_blank_or_repeated_query_value_is_refused_not_repaired(self):
        fingerprint = waiting.fingerprint([])
        for path, name in (
                ('/v1/agents/me/wait?form=', 'form'),
                ('/v1/agents/me/wait?form=worker&form=coordinator', 'form'),
                ('/v1/agents/me/wait?since=%s&since=%s' % (fingerprint, fingerprint), 'since'),
                ('/v1/agents/me/wait?idle_minutes=', 'idle_minutes'),
                ('/v1/agents/me/wait?idle_minutes=1_5', 'idle_minutes'),
                ('/v1/agents/me/wait?idle_minutes=+15', 'idle_minutes'),
                ('/v1/agents/me/wait?idle_minutes=%2015', 'idle_minutes')):
            with self.subTest(path=path):
                refused = self.request('GET', path, token=self.secret)
                self.assertEqual(400, refused.status, refused.data)
                self.assertIn(name, refused.data['error']['message'])
        bare = self.request('GET', '/v1/agents/me/wait', token=self.secret)
        worker = self.request('GET', '/v1/agents/me/wait?form=worker', token=self.secret)
        self.assertEqual(200, bare.status, bare.data)
        self.assertEqual(200, worker.status, worker.data)
        self.assertEqual(bare.data['fingerprint'], worker.data['fingerprint'])


if __name__ == '__main__':
    unittest.main()


class IntegratedNotTheWorkersMoveTests(unittest.TestCase):
    """kittrial-5bb.336: an integrated-but-open row never wakes a worker.

    The worker form counts rows whose whose_move is 'you'; with the move fixed the
    row reads coordinator, so the wait times out (or stays asleep) instead of
    waking the worker for work that is the coordinator's to close."""

    def test_an_integrated_row_does_not_wake_the_worker(self):
        clock = FakeClock()
        read = scripted([worker({'task': 't-old', 'whose_move': 'coordinator',
                                 'review_state': 'integrated', 'next_actor': 'coordinator'}),
                         worker({'task': 't-old', 'whose_move': 'coordinator',
                                 'review_state': 'integrated', 'next_actor': 'coordinator'})])
        code, view = waiting.run_wait(read, interval=60, timeout=120,
                                      clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertEqual(view['items'], [])
        self.assertTrue(view['timed_out'])

    def test_a_real_move_still_wakes_beside_integrated_rows(self):
        clock = FakeClock()
        read = scripted([worker({'task': 't-old', 'whose_move': 'coordinator',
                                 'review_state': 'integrated', 'next_actor': 'coordinator'}),
                         worker({'task': 't-new', 'whose_move': 'you',
                                 'review_state': 'changes-requested', 'pending_review_items': 1})])
        code, view = waiting.run_wait(read, interval=60, timeout=600,
                                      clock=clock.clock, sleep=clock.sleep)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual([row['task'] for row in view['items']], ['t-new'])
