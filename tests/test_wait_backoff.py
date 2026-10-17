"""The wait backs off and prints the exact next wait (kittrial-5bb.327).

The owner watched a worker re-arm a 20-minute wait every time it timed out with nothing
to do. The fix is stateless on the server: a wait that times out with the fingerprint
unchanged carries ``next_timeout``, the ``next_command`` to run next and ``give_up``; a
return with items resets the schedule. The BACKOFF needs no memory: "two empty returns
in a row" has to be derivable from the flags the client is given:

    the client assumes the idle spell named by ``--idle-since`` was spent in waits of
    the schedule's own durations, counts how many of them fit in the elapsed time (the
    wait that just ended included), and prints the next one.

So the first two empty waits are the base ``--timeout``, the third is double, and so on
up to ``--max-timeout`` (default cap 3600 s). After ``--give-up-after`` seconds of that
spell (12 h by default) the wait says to checkpoint and stop.

The client DOES keep one small local cache, beside its config, for the two review
points of this task (``StateFileTests``): the last item list, so a wake can print the
``changed`` block, and the last fingerprint, so a call without ``--since`` resumes
instead of re-reading the baseline (``--fresh`` ignores it). Every loop here is driven
by a fake clock: nothing in this file sleeps.
"""
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import client
import waiting

A = 'session-aaaaaaaa-1111-4111-8111-111111111111'
IDLE = '2026-10-17T19:00:00Z'
START = waiting.parse_instant(IDLE)
SENTENCE = 'checkpoint and stop: nothing has been your move since '


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
    """A read callable that answers with each view in turn, repeating the last one."""
    state = {'calls': 0}

    def read():
        index = min(state['calls'], len(views) - 1)
        state['calls'] += 1
        return views[index]

    return read


def empty():
    return waiting.worker_view([])


def moved(task='t1'):
    return waiting.worker_view([{'task': task, 'whose_move': 'you'}])


def run_timeout(views, **kwargs):
    """One bounded wait over ``views`` with a fake clock; returns ``(code, view, clock)``."""
    clock = FakeClock()
    code, view = waiting.run_wait(scripted(views), clock=clock.clock, sleep=clock.sleep,
                                  **kwargs)
    return code, view, clock


class ScheduleRuleTests(unittest.TestCase):
    """The stateless rule, and the two numbers it is built from."""

    def test_the_first_two_empty_returns_keep_the_base_and_the_third_doubles(self):
        self.assertEqual(waiting.schedule_timeout(1, 60, 3600), 60)
        self.assertEqual(waiting.schedule_timeout(2, 60, 3600), 60)
        self.assertEqual(waiting.schedule_timeout(3, 60, 3600), 120)
        self.assertEqual(waiting.schedule_timeout(4, 60, 3600), 240)
        self.assertEqual(waiting.schedule_timeout(5, 60, 3600), 480)

    def test_the_count_comes_from_the_elapsed_idle_spell_alone(self):
        # 60 + 60 + 120 + 240 = 480 seconds of scheduled empty waits.
        self.assertEqual(waiting.empty_returns(0, 60, 3600), 0)
        self.assertEqual(waiting.empty_returns(59, 60, 3600), 0)
        self.assertEqual(waiting.empty_returns(60, 60, 3600), 1)
        self.assertEqual(waiting.empty_returns(119, 60, 3600), 1)
        self.assertEqual(waiting.empty_returns(120, 60, 3600), 2)
        self.assertEqual(waiting.empty_returns(239, 60, 3600), 2)
        self.assertEqual(waiting.empty_returns(240, 60, 3600), 3)
        self.assertEqual(waiting.empty_returns(479, 60, 3600), 3)
        self.assertEqual(waiting.empty_returns(480, 60, 3600), 4)

    def test_the_defaults_are_the_tasks_numbers(self):
        options = waiting.parse_client_args([])
        self.assertIsNone(options.idle_since)
        self.assertIsNone(options.max_timeout)
        self.assertEqual(options.give_up_after, 12 * 60 * 60)
        self.assertEqual(waiting.DEFAULT_MAX_TIMEOUT, 3600)
        self.assertEqual(waiting.DEFAULT_GIVE_UP_AFTER, 12 * 60 * 60)

    def test_a_bad_idle_since_is_refused_before_any_read(self):
        for bad in ('2026-10-17T15:00:00', 'nonsense', '', '2026-02-30T00:00:00Z',
                    '2026-10-17T15:00:00+25:00', '2026-10-17'):
            with self.subTest(idle_since=bad):
                with self.assertRaises(ValueError):
                    waiting.parse_client_args(['--timeout', '60', '--idle-since', bad])
        for good in (IDLE, '2026-10-17T15:00:00-04:00', '2026-10-17T19:00:00.250000Z'):
            with self.subTest(idle_since=good):
                self.assertEqual(waiting.parse_client_args(['--idle-since', good]).idle_since,
                                 good)

    def test_max_timeout_and_give_up_after_must_be_positive(self):
        for args in (['--max-timeout', '0'], ['--max-timeout', '-1'],
                     ['--give-up-after', '0'], ['--give-up-after', '-1']):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    waiting.parse_client_args(args)


def walk(test, base=60, **kwargs):
    """Five empty waits, each using the timeout the previous one printed."""
    since = empty()['fingerprint']
    now = START
    next_timeout = base
    answers = []
    for _step in range(5):
        code, view, _clock = run_timeout([empty()], since=since, timeout=next_timeout,
                                         idle_since=IDLE, now=now, **kwargs)
        test.assertEqual(code, waiting.EXIT_TIMEOUT)
        answers.append(view['next_timeout'])
        next_timeout = view['next_timeout']
        now += next_timeout
    return answers


class DoublingTests(unittest.TestCase):
    """Run the printed next wait each time: it doubles after two empty returns in a row."""

    def test_it_doubles_after_two_empty_returns_in_a_row(self):
        self.assertEqual(walk(self), [60, 120, 240, 480, 960])

    def test_it_is_recomputed_from_the_flags_not_remembered(self):
        # The same invocation twice (same --timeout, same --idle-since, same start
        # instant) answers the same thing: there is no state to drift.
        since = empty()['fingerprint']
        first = run_timeout([empty()], since=since, timeout=120, idle_since=IDLE,
                            now=START + 120)[1]
        second = run_timeout([empty()], since=since, timeout=120, idle_since=IDLE,
                             now=START + 120)[1]
        self.assertEqual(first, second)
        self.assertEqual(first['next_timeout'], 240)


class CapTests(unittest.TestCase):
    """Never above --max-timeout or the 3600 s default; never below the base --timeout."""

    def test_max_timeout_caps_the_doubling(self):
        self.assertEqual(walk(self, base=60, max_timeout=120), [60, 120, 120, 120, 120])

    def test_the_default_cap_is_3600_seconds(self):
        code, view, _clock = run_timeout([empty()], since=empty()['fingerprint'], timeout=60,
                                         idle_since=IDLE, now=START + 2 * 60 * 60,
                                         give_up_after=10 ** 7)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertEqual(view['next_timeout'], 3600)

    def test_the_cap_never_shortens_the_base_timeout(self):
        code, view, _clock = run_timeout([empty()], since=empty()['fingerprint'], timeout=7200,
                                         idle_since=IDLE, now=START + 5 * 60 * 60,
                                         give_up_after=10 ** 7)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertEqual(view['next_timeout'], 7200)
        self.assertIn('--timeout 7200', view['next_command'])


class GiveUpTests(unittest.TestCase):
    """After the idle limit the wait says the exact sentence and stops the agent."""

    def test_the_default_limit_is_twelve_hours_from_idle_since(self):
        code, view, _clock = run_timeout([empty()], since=empty()['fingerprint'], timeout=60,
                                         idle_since=IDLE, now=START + 12 * 60 * 60 - 120)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertFalse(view['give_up'])

        code, view, _clock = run_timeout([empty()], since=empty()['fingerprint'], timeout=60,
                                         idle_since=IDLE, now=START + 12 * 60 * 60)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertTrue(view['give_up'])
        self.assertEqual(view['message'], SENTENCE + IDLE)

    def test_a_custom_limit_is_measured_from_idle_since(self):
        code, view, _clock = run_timeout([empty()], since=empty()['fingerprint'], timeout=60,
                                         idle_since=IDLE, now=START + 600,
                                         give_up_after=660)
        # 600 s idle before this wait + 60 s waited = 660 s: the limit is reached.
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertTrue(view['give_up'])
        self.assertEqual(view['message'], SENTENCE + IDLE)

    def test_the_sentence_is_in_the_printed_result(self):
        _code, view, _clock = run_timeout([empty()], since=empty()['fingerprint'], timeout=60,
                                          idle_since=IDLE, now=START + 12 * 60 * 60)
        printed = json.dumps(view)
        self.assertIn('checkpoint and stop: nothing has been your move since %s' % IDLE,
                      printed)


class ResetOnWorkTests(unittest.TestCase):
    """Work resets the schedule: the base timeout and a fresh idle-since."""

    def test_a_return_with_items_resets_to_the_base_timeout(self):
        code, view, _clock = run_timeout([moved('t1')], timeout=60, idle_since=IDLE,
                                         now=START + 3600,
                                         command_prefix='client.py --config c')
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(view['next_timeout'], 60)
        self.assertFalse(view['give_up'])
        self.assertIn('--timeout 60', view['next_command'])
        self.assertNotIn('--timeout 120', view['next_command'])
        self.assertIn('--since ' + view['fingerprint'], view['next_command'])
        self.assertNotIn('--idle-since ' + IDLE, view['next_command'])
        self.assertIn('--idle-since ' + waiting.format_instant(START + 3600),
                      view['next_command'])

    def test_the_reset_command_starts_a_clean_schedule(self):
        work = moved('t1')
        _code, view, _clock = run_timeout([work], timeout=600, idle_since=IDLE,
                                          now=START + 7200, command_prefix='client.py')
        options = waiting.parse_client_args(shlex.split(view['next_command'])[2:])
        self.assertEqual(options.timeout, 600)
        self.assertNotEqual(options.idle_since, IDLE)
        self.assertEqual(options.since, work['fingerprint'])
        # Run the printed command's own arguments over the same unchanged work row: the
        # reset has started a clean spell, so the first empty return is the base again.
        code, next_view, _clock = run_timeout([work], since=options.since,
                                              timeout=options.timeout,
                                              idle_since=options.idle_since,
                                              now=waiting.parse_instant(options.idle_since))
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertEqual(next_view['next_timeout'], 600)

    def test_the_printed_command_carries_every_flag_the_caller_gave(self):
        code, view, _clock = run_timeout([empty()], since=empty()['fingerprint'], timeout=60,
                                         idle_since=IDLE, now=START + 120,
                                         max_timeout=1200, give_up_after=7200,
                                         interval=120, command_prefix='b')
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        options = waiting.parse_client_args(shlex.split(view['next_command'])[2:])
        self.assertEqual((options.timeout, options.max_timeout, options.give_up_after,
                          options.interval, options.idle_since),
                         (view['next_timeout'], 1200, 7200, 120, IDLE))


class OldCallerTests(unittest.TestCase):
    """A caller that passes none of the new flags sees exactly today's view."""

    def test_a_timeout_carries_no_new_fields(self):
        _code, view, _clock = run_timeout([empty()], since=empty()['fingerprint'], timeout=120)
        self.assertEqual(sorted(view), ['fingerprint', 'form', 'items', 'polls', 'schema',
                                        'timed_out'])

    def test_the_work_view_carries_no_new_fields(self):
        _code, view, _clock = run_timeout([moved()], timeout=120)
        self.assertEqual(sorted(view), ['fingerprint', 'form', 'items', 'polls', 'schema'])

    def test_the_tuning_flags_alone_change_nothing(self):
        _code, view, _clock = run_timeout([empty()], since=empty()['fingerprint'], timeout=120,
                                      max_timeout=120, give_up_after=60)
        self.assertNotIn('next_timeout', view)
        self.assertNotIn('next_command', view)
        self.assertNotIn('give_up', view)

    def test_the_read_arguments_still_carry_no_client_poll_bounds(self):
        options = waiting.parse_client_args(['--timeout', '60', '--idle-since', IDLE,
                                            '--max-timeout', '120',
                                            '--give-up-after', '600'])
        self.assertEqual(waiting.read_args(options), [])


class ClientWiringTests(unittest.TestCase):
    """``client.py wait`` prints a runnable next_command with its own client prefix."""

    def invoke(self, argv, request, wall=START):
        # A fresh config directory per call keeps the wait's local state isolated, exactly
        # as a new worker machine starts. A test that needs two calls to share state uses
        # ``_invoke_at`` with its own directory.
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            code, out, err, _config, _state = self._invoke_at(config, argv, request, wall)
        return code, out, err, str(config)

    def _invoke_at(self, config, argv, request, wall=START):
        # The fake clock advances only when the wait sleeps, so a bounded wait ends and
        # its elapsed time is deterministic; ``wall`` is the wall-clock instant it starts.
        ticks = {'now': 0.0}

        def sleep(seconds):
            ticks['now'] += seconds
        with mock.patch.object(sys, 'argv', ['client.py', '--config', str(config),
                                             '--project', 'example', '--actor', A,
                                             '--', *argv]), \
                mock.patch.object(client, 'request', request), \
                mock.patch('waiting.time.monotonic', lambda: ticks['now']), \
                mock.patch('waiting.time.sleep', sleep), \
                mock.patch('waiting.time.time', lambda: wall), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output, \
                mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            code = client.main()
        form = 'coordinator' if '--coordinator' in argv else 'worker'
        state = waiting.state_path(str(config), 'example', A, form)
        saved = None
        if state and os.path.isfile(state):
            saved = Path(state).read_text(encoding='utf-8')
        return code, output.getvalue(), error.getvalue(), str(config), saved

    def test_the_timeout_prints_the_full_next_wait_command(self):
        payload = empty()

        def request(_config, _project, _actor, args, action='bd', path=None):
            return {'returncode': 0, 'stdout': json.dumps(payload), 'stderr': ''}

        code, out, _err, config = self.invoke(
            ['wait', '--interval', '60', '--timeout', '60', '--since',
             payload['fingerprint'], '--idle-since', IDLE], request)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        body = json.loads(out)
        self.assertIn('next_timeout', body)
        self.assertIn('next_command', body)
        self.assertFalse(body['give_up'])
        tokens = shlex.split(body['next_command'])
        self.assertEqual(tokens[0], sys.executable)
        self.assertIn('--config', tokens)
        self.assertEqual(tokens[tokens.index('--config') + 1], config)
        self.assertEqual(tokens[tokens.index('--project') + 1], 'example')
        self.assertEqual(tokens[tokens.index('--actor') + 1], A)
        self.assertIn('--since', tokens)
        self.assertEqual(tokens[tokens.index('--since') + 1], payload['fingerprint'])
        self.assertEqual(tokens[tokens.index('--idle-since') + 1], IDLE)
        self.assertEqual(tokens[tokens.index('--timeout') + 1], str(body['next_timeout']))
        # The printed line's own wait arguments are accepted by the parser.
        options = waiting.parse_client_args(tokens[tokens.index('wait') + 1:])
        self.assertEqual(options.idle_since, IDLE)
        self.assertEqual(options.timeout, body['next_timeout'])

    def test_the_coordinator_form_keeps_its_read_and_prints_it_in_the_next_command(self):
        seen = []

        def request(_config, _project, _actor, args, action='bd', path=None):
            seen.append(list(args))
            return {'returncode': 0, 'stdout': json.dumps(waiting.coordinator_view([], {})),
                    'stderr': ''}

        code, out, _err, _config = self.invoke(
            ['wait', '--coordinator', '--idle-minutes', '30', '--interval', '60',
             '--timeout', '60', '--idle-since', IDLE], request)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertEqual(len(seen), 2)  # one nap, then the timeout
        self.assertTrue(all(args == ["--coordinator", "--idle-minutes", "30"] for args in seen))
        command = json.loads(out)['next_command']
        self.assertIn('--coordinator', command)
        self.assertIn('--idle-minutes 30', command)

    def test_a_give_up_prints_the_sentence_and_the_client_stops_at_exit_one(self):
        payload = empty()

        def request(_config, _project, _actor, args, action='bd', path=None):
            return {'returncode': 0, 'stdout': json.dumps(payload), 'stderr': ''}

        code, out, _err, _config = self.invoke(
            ['wait', '--timeout', '60', '--since', payload['fingerprint'],
             '--idle-since', IDLE], request, wall=START + 12 * 60 * 60)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        body = json.loads(out)
        self.assertTrue(body['give_up'])
        self.assertEqual(body['message'], SENTENCE + IDLE)

    def test_an_old_caller_sees_the_old_output(self):
        payload = empty()

        def request(_config, _project, _actor, args, action='bd', path=None):
            return {'returncode': 0, 'stdout': json.dumps(payload), 'stderr': ''}

        code, out, _err, _config = self.invoke(
            ['wait', '--interval', '60', '--timeout', '0', '--since',
             payload['fingerprint']], request)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        body = json.loads(out)
        self.assertEqual(sorted(body), ['fingerprint', 'form', 'items', 'polls', 'schema',
                                       'timed_out'])
        self.assertTrue(body['timed_out'])

    def test_an_old_caller_gets_the_changed_block_and_the_state_beside_its_config(self):
        """Review items 6 and 7 from the real dispatch: additive on a wake, remembered."""
        payload = moved('t1')

        def request(_config, _project, _actor, args, action='bd', path=None):
            return {'returncode': 0, 'stdout': json.dumps(payload), 'stderr': ''}

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            code, out, err, _config, _saved = self._invoke_at(
                config, ['wait', '--interval', '60'], request)
            self.assertEqual(code, waiting.EXIT_WORK, err)
            self.assertEqual(err, '')
            body = json.loads(out)
            # The old view's keys are all there; `changed` is the one additive block.
            self.assertEqual(sorted(body), ['changed', 'fingerprint', 'form', 'items',
                                            'polls', 'schema'])
            self.assertEqual(body['changed']['added'], ['t1'])
            self.assertNotIn('next_timeout', body)
            state = waiting.state_path(str(config), 'example', A, 'worker')
            self.assertEqual(os.path.dirname(state), str(config.parent))
            self.assertTrue(os.path.isfile(state))
            stored = json.loads(Path(state).read_text(encoding='utf-8'))
            self.assertEqual(stored['fingerprint'], payload['fingerprint'])
            # Without --since the remembered fingerprint is used: unchanged, it blocks.
            code, out, _err, _config, _saved = self._invoke_at(
                config, ['wait', '--interval', '60', '--timeout', '0'], request)
            self.assertEqual(code, waiting.EXIT_TIMEOUT)
            self.assertTrue(json.loads(out)['timed_out'])
            # --fresh ignores it and returns the whole baseline again.
            code, out, _err, _config, _saved = self._invoke_at(
                config, ['wait', '--interval', '60', '--timeout', '0', '--fresh'], request)
            self.assertEqual(code, waiting.EXIT_WORK)
            self.assertEqual(json.loads(out)['items'], payload['items'])


class StateFileTests(unittest.TestCase):
    """Review items 6 and 7: the small local cache, the changed block and --fresh.

    The server stays stateless. The client keeps, beside its config, the last fingerprint
    it printed and the last item list; the path is derived from ``--config`` (never a
    hard-coded ``~/.orchestra``) and one file belongs to one caller identity.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = Path(self._tmp.name) / '.orchestra' / 'client.json'
        self.config.parent.mkdir()
        self.config.write_text('{"endpoint": "/not/read"}', encoding='utf-8')
        self.state = waiting.state_path(str(self.config), 'example', A, 'worker')

    def read(self):
        return json.loads(Path(self.state).read_text(encoding='utf-8'))

    def test_the_state_file_is_beside_the_config_and_derived_from_its_path(self):
        self.assertEqual(os.path.dirname(self.state), str(self.config.parent))
        self.assertTrue(self.state.startswith(str(self.config.parent) + os.sep))
        self.assertNotEqual(
            waiting.state_path(str(self.config), 'example', A, 'coordinator'), self.state)
        self.assertNotEqual(
            waiting.state_path(str(self.config), 'other', A, 'worker'), self.state)
        self.assertNotEqual(
            waiting.state_path(str(self.config), 'example', 'session-b', 'worker'), self.state)
        self.assertIsNone(waiting.state_path(None, 'example', A))
        self.assertIsNone(waiting.state_path('', 'example', A))

    def test_a_wake_writes_a_small_state_beside_the_config(self):
        payload = moved('t1')
        code, view, _clock = run_timeout([payload], timeout=60, state_file=self.state)
        self.assertEqual(code, waiting.EXIT_WORK)
        stored = self.read()
        self.assertEqual(sorted(stored), ['fingerprint', 'items', 'schema'])
        self.assertEqual(stored['schema'], waiting.WAIT_STATE_SCHEMA)
        self.assertEqual(stored['fingerprint'], payload['fingerprint'])
        self.assertEqual([row['task'] for row in stored['items']], ['t1'])
        self.assertLess(os.path.getsize(self.state), 4096)

    def test_the_state_carries_no_config_value_or_secret(self):
        self.config.write_text('{"secret": "SECRET-VALUE"}', encoding='utf-8')
        run_timeout([moved('t1')], timeout=60, state_file=self.state)
        self.assertNotIn('SECRET-VALUE', Path(self.state).read_text(encoding='utf-8'))

    def test_the_first_call_without_since_returns_the_whole_baseline(self):
        code, view, _clock = run_timeout([moved('t1')], timeout=60, state_file=self.state)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual([row['task'] for row in view['items']], ['t1'])
        self.assertEqual(view['changed'], {'tasks': [], 'added': ['t1'], 'removed': []})

    def test_the_next_call_without_since_uses_the_remembered_fingerprint(self):
        payload = moved('t1')
        run_timeout([payload], timeout=60, state_file=self.state)
        code, view, _clock = run_timeout([payload], timeout=60, state_file=self.state)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertTrue(view['timed_out'])
        self.assertNotIn('changed', view)

    def test_fresh_ignores_the_remembered_fingerprint_and_returns_the_baseline(self):
        payload = moved('t1')
        run_timeout([payload], timeout=60, state_file=self.state)
        code, view, _clock = run_timeout([payload], timeout=60, state_file=self.state,
                                         fresh=True)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual([row['task'] for row in view['items']], ['t1'])
        self.assertEqual(view['changed'], {'tasks': [], 'added': [], 'removed': []})
        self.assertEqual(self.read()['fingerprint'], payload['fingerprint'])

    def test_fresh_without_a_state_file_is_the_baseline_too(self):
        code, view, _clock = run_timeout([moved('t1')], timeout=60, state_file=self.state,
                                         fresh=True)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(view['changed']['added'], ['t1'])

    def test_a_wake_reports_each_changed_field_old_to_new(self):
        before = waiting.worker_view([{'task': 't1', 'whose_move': 'you',
                                       'review_state': 'awaiting-review',
                                       'pending_review_items': 1}])
        after = waiting.worker_view([{'task': 't1', 'whose_move': 'you',
                                      'review_state': 'changes-requested',
                                      'pending_review_items': 2}])
        run_timeout([before], timeout=60, state_file=self.state)
        code, view, _clock = run_timeout([after], timeout=60, state_file=self.state)
        self.assertEqual(code, waiting.EXIT_WORK)
        changed = view['changed']
        self.assertEqual(changed['added'], [])
        self.assertEqual(changed['removed'], [])
        self.assertEqual(len(changed['tasks']), 1)
        self.assertEqual(changed['tasks'][0]['task'], 't1')
        fields = dict((entry['field'], (entry['old'], entry['new']))
                      for entry in changed['tasks'][0]['fields'])
        self.assertEqual(fields, {'pending_review_items': (1, 2),
                                  'review_state': ('awaiting-review', 'changes-requested')})

    def test_a_wake_reports_the_items_added_and_removed(self):
        before = waiting.worker_view([{'task': 't1', 'whose_move': 'you'},
                                      {'task': 't2', 'whose_move': 'you'}])
        after = waiting.worker_view([{'task': 't2', 'whose_move': 'you'},
                                     {'task': 't3', 'whose_move': 'you'}])
        run_timeout([before], timeout=60, state_file=self.state)
        code, view, _clock = run_timeout([after], timeout=60, state_file=self.state)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(view['changed'], {'tasks': [], 'added': ['t3'], 'removed': ['t1']})

    def test_a_row_without_a_task_is_keyed_by_its_lane(self):
        changed = waiting.changed_items(
            [{'lane': 'lane-a', 'state': 'waiting-on-coordinator'}],
            [{'lane': 'lane-a', 'state': 'idle'}])
        self.assertEqual(changed, {
            'tasks': [{'task': 'lane-a',
                       'fields': [{'field': 'state', 'old': 'waiting-on-coordinator',
                                   'new': 'idle'}]}],
            'added': [], 'removed': []})

    def test_the_changed_block_is_additive_to_the_existing_keys(self):
        payload = moved('t1')
        run_timeout([payload], timeout=60, state_file=self.state)
        code, view, _clock = run_timeout([payload], timeout=60, state_file=self.state,
                                         fresh=True)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(sorted(view), ['changed', 'fingerprint', 'form', 'items', 'polls',
                                        'schema'])

    def test_a_corrupt_state_file_is_treated_as_absent_and_rewritten(self):
        Path(self.state).write_text('{not json', encoding='utf-8')
        code, view, _clock = run_timeout([moved('t1')], timeout=60, state_file=self.state)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(view['changed']['added'], ['t1'])
        self.assertEqual(self.read()['fingerprint'], moved('t1')['fingerprint'])

    def test_a_foreign_or_wrong_shaped_state_file_is_treated_as_absent(self):
        for raw in ('[]', '{"schema": "other", "fingerprint": "x", "items": []}',
                    '{"schema": "wait-state-v1", "fingerprint": "bad", "items": "nope"}',
                    '[' * 10000 + ']' * 10000):
            with self.subTest(state=raw[:40]):
                Path(self.state).write_text(raw, encoding='utf-8')
                code, view, _clock = run_timeout([moved('t1')], timeout=60,
                                                 state_file=self.state)
                self.assertEqual(code, waiting.EXIT_WORK)
                self.assertEqual(view['changed']['added'], ['t1'])

    def test_an_oversized_state_file_is_treated_as_absent(self):
        Path(self.state).write_bytes(b'x' * (waiting.MAX_STATE_BYTES + 1))
        code, view, _clock = run_timeout([moved('t1')], timeout=60, state_file=self.state)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(view['changed']['added'], ['t1'])

    def test_an_unreadable_state_path_does_not_break_the_wait(self):
        Path(self.state).mkdir()  # open() raises, os.replace() raises: both are ignored
        code, view, _clock = run_timeout([moved('t1')], timeout=60, state_file=self.state)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual([row['task'] for row in view['items']], ['t1'])

    def test_a_timeout_also_remembers_the_last_fingerprint_it_printed(self):
        payload = empty()
        code, _view, _clock = run_timeout([payload], timeout=0, state_file=self.state)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)
        self.assertEqual(self.read()['fingerprint'], payload['fingerprint'])

    def test_the_state_keeps_at_most_the_cap_of_items(self):
        rows = [{'task': 't%03d' % index, 'whose_move': 'you'}
                for index in range(waiting.MAX_STATE_ITEMS + 20)]
        run_timeout([waiting.worker_view(rows)], timeout=60, state_file=self.state)
        self.assertEqual(len(self.read()['items']), waiting.MAX_STATE_ITEMS)

    def test_an_oversized_write_keeps_the_fingerprint_and_drops_the_list(self):
        rows = [dict((name, 'x' * 200) for name in waiting.MOVE_FIELDS)
                for _index in range(waiting.MAX_STATE_ITEMS)]
        for index, row in enumerate(rows):
            row.update(task='t%03d' % index, whose_move='you')
        payload = waiting.worker_view(rows)
        code, _view, _clock = run_timeout([payload], timeout=60, state_file=self.state)
        self.assertEqual(code, waiting.EXIT_WORK)
        stored = self.read()
        self.assertEqual(stored['fingerprint'], payload['fingerprint'])
        self.assertEqual(stored['items'], [])
        self.assertLessEqual(os.path.getsize(self.state), waiting.MAX_STATE_BYTES + 1)
        # The fingerprint survived, so a call without --since still resumes from it.
        code, _view, _clock = run_timeout([payload], timeout=0, state_file=self.state)
        self.assertEqual(code, waiting.EXIT_TIMEOUT)

    def test_without_a_state_file_the_old_view_is_unchanged(self):
        code, view, _clock = run_timeout([moved('t1')], timeout=60)
        self.assertEqual(code, waiting.EXIT_WORK)
        self.assertEqual(sorted(view), ['fingerprint', 'form', 'items', 'polls', 'schema'])


if __name__ == '__main__':
    unittest.main()
