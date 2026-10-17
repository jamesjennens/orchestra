"""The wait backs off and prints the exact next wait (kittrial-5bb.327).

The owner watched a worker re-arm a 20-minute wait every time it timed out with nothing
to do. The fix is stateless on the server: a wait that times out with the fingerprint
unchanged carries ``next_timeout``, the ``next_command`` to run next and ``give_up``; a
return with items resets the schedule. No state file and no daemon exist, so "two empty
returns in a row" has to be derivable from the flags the client is given:

    the client assumes the idle spell named by ``--idle-since`` was spent in waits of
    the schedule's own durations, counts how many of them fit in the elapsed time (the
    wait that just ended included), and prints the next one.

So the first two empty waits are the base ``--timeout``, the third is double, and so on
up to ``--max-timeout`` (default cap 3600 s). After ``--give-up-after`` seconds of that
spell (12 h by default) the wait says to checkpoint and stop.

Every loop here is driven by a fake clock: nothing in this file sleeps.
"""
import io
import json
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
        # The fake clock advances only when the wait sleeps, so a bounded wait ends and
        # its elapsed time is deterministic; ``wall`` is the wall-clock instant it starts.
        ticks = {'now': 0.0}

        def sleep(seconds):
            ticks['now'] += seconds
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
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
        return code, output.getvalue(), error.getvalue(), str(config)

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


if __name__ == '__main__':
    unittest.main()
