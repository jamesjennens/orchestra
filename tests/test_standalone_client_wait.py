"""A stand-alone ``client.py wait``, and the inlined copy that agrees with waiting.py.

kittrial-5bb.304. A worker machine is handed ``client.py`` ON ITS OWN
(docs/CLI_CONTRACT.md), but the wait logic of kittrial-5bb.285 lived only in
``waiting.py``, so every stand-alone ``client.py wait`` failed with
``ModuleNotFoundError: No module named 'waiting'``. The client now falls back to its
inlined copy (:class:`client._InlineWait`) when no sibling ``waiting.py`` is present,
while ``waiting.py`` stays the module the endpoint and the kit's own tests use.

Two properties are pinned here, and both are mutation-checked:

* the inlined copy and ``waiting.py`` produce the same view, fingerprint, argument
  bounds and loop answers for the same rows (the agreement tests), so a drift between
  the two copies is a test failure; and
* a folder holding ONLY ``client.py`` and its config runs the real CLI (``wait`` through
  a subprocess) against a stubbed endpoint, with the inlined copy in play, and answers
  exactly what the kit's own client answers - with no ``ModuleNotFoundError`` anywhere.

Every loop here is driven by a fake clock: nothing in this file sleeps.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import client
import waiting

A = 'session-aaaaaaaa-1111-4111-8111-111111111111'

#: The stub endpoint the stand-alone CLI runs against. It answers the client's ``wait``
#: action with the view in ``WAIT_STUB_PAYLOAD`` and, when ``WAIT_STUB_WIRE`` names a
#: file, records the request envelope it was sent. It is not from the kit: the folder
#: under test holds no kit file but client.py.
STUB = '''import json, os, sys
wire = sys.stdin.read()
if os.environ.get('WAIT_STUB_WIRE'):
    with open(os.environ['WAIT_STUB_WIRE'], 'w', encoding='utf-8') as handle:
        handle.write(wire)
sys.stdout.write(json.dumps({'returncode': 0,
                             'stdout': os.environ['WAIT_STUB_PAYLOAD'],
                             'stderr': ''}))
'''

#: Rows covering the spellings the projection flattens: canonical and web rows, an
#: attention block, a contribution object, and rows that are not the caller's move.
WORKER_CORPUS = [
    ('empty', []),
    ('one move', [{'task': 't1', 'whose_move': 'you', 'review_state': 'changes-requested',
                   'pending_review_items': 2}]),
    ('attention lifted', [{'task': 't3', 'whose_move': 'you',
                           'attention': {'open_items': 3, 'redelivered': True},
                           'open_requests': 4}]),
    ('web spellings', [{'task': 't2', 'whose_move': 'you', 'open_requests': 2,
                        'contribution': {'id': 'c9'}}]),
    ('someone else', [{'task': 't4', 'whose_move': 'them', 'review_state': 'none'}]),
    ('mixed and unordered', [{'task': 'b', 'whose_move': 'you', 'open_items': 1},
                             {'task': 'a', 'whose_move': 'you', 'blocking_items': True},
                             {'task': 'c', 'whose_move': 'them'}]),
    ('not a dict and missing fields', [{'whose_move': 'you'}, 'nonsense', None]),
]

#: One lane board per coordinator case: each trigger, a declared wait, and a quiet lane.
LANES_CORPUS = [
    ('empty', {'schema': 'lanes-v1', 'items': []}),
    ('lane quiet', {'schema': 'lanes-v1', 'items': [
        {'lane': 'lane-a', 'actor': A, 'your_move_idle': True, 'held': []}]}),
    ('a checkpoint and a flag', {'schema': 'lanes-v1', 'items': [
        {'lane': 'lane-b', 'actor': A, 'waiting_too_long': True,
         'flag_tasks': {'expect_by_passed': []},
         'held': [{'task': 't5', 'state': 'waiting-on-coordinator', 'flags': []}]}]}),
    ('quiet lane', {'schema': 'lanes-v1', 'items': [
        {'lane': 'lane-c', 'actor': A, 'held': [{'task': 't6', 'state': 'working',
                                                 'flags': []}]}]}),
    ('not a payload', 'nonsense'),
]

QUEUE_CORPUS = [
    ('empty', []),
    ('awaiting review', [{'task': 'q1', 'review_state': 'awaiting-review',
                          'contribution_id': 'c1'}]),
    ('legacy review state', [{'task': 'q2', 'review_state': 'legacy-review-ready',
                              'contribution_id': 'c2'}]),
    ('awaiting integration', [{'task': 'q3', 'review_state': 'approved'}]),
    ('work_state spelling', [{'task': 'q4', 'work_state': 'awaiting-integration'}]),
    ('no contribution id is not a review', [{'task': 'q5', 'review_state': 'awaiting-review'}]),
]


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
        value = views[index]
        if isinstance(value, BaseException):
            raise value
        return value

    return read


def drive(module, views, **kwargs):
    """One scripted wait through ``module`` (waiting or the inlined class), fake clock."""
    clock = FakeClock()
    code, view = module.run_wait(scripted(views), clock=clock.clock, sleep=clock.sleep, **kwargs)
    return code, view, clock.naps


class InlineAgreementTests(unittest.TestCase):
    """The inlined copy and waiting.py are the SAME wait: a drift fails here."""

    def test_the_client_carries_an_inlined_copy_of_everything_the_wait_needs(self):
        for name in ('parse_client_args', 'parse_read_args', 'read_args', 'run_wait',
                     'normalize_item', 'digest', 'fingerprint', 'is_fingerprint', 'worker_view',
                     'coordinator_view', 'movement'):
            self.assertTrue(callable(getattr(client._InlineWait, name)), name)

    def test_the_worker_view_and_fingerprint_agree_for_the_same_rows(self):
        for label, rows in WORKER_CORPUS:
            with self.subTest(case=label):
                mine = client._InlineWait.worker_view(rows)
                theirs = waiting.worker_view(rows)
                self.assertEqual(mine, theirs)
                self.assertEqual(mine['fingerprint'], theirs['fingerprint'])

    def test_the_coordinator_view_and_fingerprint_agree_for_the_same_rows(self):
        for queue_label, queue in QUEUE_CORPUS:
            for lanes_label, lanes in LANES_CORPUS:
                with self.subTest(queue=queue_label, lanes=lanes_label):
                    mine = client._InlineWait.coordinator_view(queue, lanes)
                    theirs = waiting.coordinator_view(queue, lanes)
                    self.assertEqual(mine, theirs)
                    self.assertEqual(mine['fingerprint'], theirs['fingerprint'])

    def test_normalize_item_digest_and_fingerprint_agree(self):
        for label, rows in WORKER_CORPUS:
            for item in rows:
                with self.subTest(case=label, item=repr(item)[:40]):
                    self.assertEqual(client._InlineWait.normalize_item(item),
                                     waiting.normalize_item(item))
        for value in ({'a': [1, 2]}, {}, [1, 'x'], None, {'task': 'T', 'n': 3}):
            with self.subTest(value=repr(value)[:40]):
                self.assertEqual(client._InlineWait.digest(value), waiting.digest(value))
        self.assertEqual(client._InlineWait.MOVE_FIELDS, waiting.MOVE_FIELDS)
        self.assertEqual(client._InlineWait.COORDINATOR_TRIGGERS, waiting.COORDINATOR_TRIGGERS)
        self.assertEqual(client._InlineWait.LANE_FLAGS, waiting.LANE_FLAGS)
        self.assertEqual(client._InlineWait.FINGERPRINT, waiting.FINGERPRINT)

    def test_is_fingerprint_agrees(self):
        for value in (waiting.fingerprint([]), 'wait1:' + '0' * 64, 'wait1:xyz', '', None, 7,
                      waiting.fingerprint([]).upper()):
            with self.subTest(value=repr(value)[:40]):
                self.assertEqual(client._InlineWait.is_fingerprint(value),
                                 waiting.is_fingerprint(value))

    def test_the_argument_bounds_and_read_args_agree(self):
        cases = ([], ['--coordinator'], ['--interval', '60'], ['--interval', '30'],
                 ['--interval', '-1'], ['--timeout', '-1'], ['--timeout', '0'],
                 ['--since', waiting.fingerprint([])], ['--since', 'nonsense'],
                 ['--idle-minutes', '0'], ['--idle-minutes', '10081'],
                 ['--coordinator', '--idle-minutes', '30', '--interval', '75'])
        for args in cases:
            with self.subTest(args=args):
                mine = self._parsed(client._InlineWait, args)
                theirs = self._parsed(waiting, args)
                self.assertEqual(mine, theirs)
        options = waiting.parse_client_args(['--coordinator', '--idle-minutes', '30'])
        self.assertEqual(client._InlineWait.read_args(options), waiting.read_args(options))
        self.assertEqual(client._InlineWait.read_args(waiting.parse_client_args([])),
                         waiting.read_args(waiting.parse_client_args([])))
        for args in ([], ['--coordinator'], ['--idle-minutes', '30'], ['--idle-minutes', '0']):
            with self.subTest(read_args=args):
                self.assertEqual(self._parsed_read(client._InlineWait, args),
                                 self._parsed_read(waiting, args))

    @staticmethod
    def _parsed(module, args):
        """``None`` for the parsed options and the message of a ValueError, as a comparable."""
        try:
            options = module.parse_client_args(args)
        except ValueError as error:
            return ('refused', str(error))
        return ('accepted', options.coordinator, options.idle_minutes, options.interval,
                options.timeout, options.since)

    @staticmethod
    def _parsed_read(module, args):
        try:
            options = module.parse_read_args(args)
        except ValueError as error:
            return ('refused', str(error))
        return ('accepted', options.coordinator, options.idle_minutes)

    def test_the_loop_answers_agree_for_every_scripted_read(self):
        empty = waiting.worker_view([])
        work = waiting.worker_view([{'task': 't1', 'whose_move': 'you'}])
        later = waiting.worker_view([{'task': 't2', 'whose_move': 'you'}])
        base = {'interval': 60, 'timeout': 600}

        def scripts(module):
            return [
                ([empty, work], base),
                ([work], base),
                ([empty], dict(base, timeout=120)),
                ([empty, empty], dict(base, since=empty['fingerprint'], timeout=120)),
                ([empty, later], dict(base, since=empty['fingerprint'])),
                ([module.WaitUnreachable('Local endpoint failed (1)')], base),
                ([module.WaitRefused('Invalid wait read')], base),
                ([{'not': 'a view'}], base),
            ]

        for index, (views, kwargs) in enumerate(scripts(waiting)):
            with self.subTest(case=index):
                inline = scripts(client._InlineWait)[index][0]
                self.assertEqual(drive(waiting, views, **kwargs),
                                 drive(client._InlineWait, inline, **kwargs))


class StandaloneFolderTests(unittest.TestCase):
    """The real CLI in a folder holding ONLY client.py and its config, inlined copy in play."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.alone = base / 'alone'
        cls.alone.mkdir()
        shutil.copyfile(KIT / 'client.py', cls.alone / 'client.py')
        cls.stub_dir = base / 'stub'
        cls.stub_dir.mkdir()
        cls.stub = cls.stub_dir / 'fake_endpoint.py'
        cls.stub.write_text(STUB, encoding='utf-8')
        cls.config = cls.alone / 'config.json'
        cls.config.write_text(json.dumps({
            'transport': 'local', 'python': sys.executable,
            'endpoint': str(cls.stub), 'root': str(cls.stub_dir)}), encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def config_is_usable(self):
        """The local transport needs one Python path and absolute POSIX paths with no spaces."""
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', sys.executable) or os.name != 'posix':
            self.skipTest('the local transport needs an absolute POSIX interpreter path')

    def run_client(self, client_path, argv, payload, wire=None):
        # The stand-alone folder must see NOTHING of the kit: the caller's own PYTHONPATH
        # (the test runner's .:tests) is removed, so only client.py's directory is importable.
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        environment['WAIT_STUB_PAYLOAD'] = json.dumps(payload)
        if wire is not None:
            environment['WAIT_STUB_WIRE'] = str(wire)
        return subprocess.run([sys.executable, '-B', str(client_path), '--config', str(self.config),
                               '--project', 'example', '--actor', A, '--', *argv],
                              capture_output=True, text=True, encoding='utf-8',
                              env=environment, timeout=120)

    def test_the_folder_holds_only_the_client_and_its_config(self):
        self.assertEqual(sorted(path.name for path in self.alone.iterdir()),
                         ['client.py', 'config.json'])
        self.assertFalse((self.alone / 'waiting.py').exists())

    def test_a_standalone_client_chooses_the_inlined_copy_and_the_kit_uses_the_module(self):
        self.config_is_usable()
        script = ('import client; '
                  'print("inline" if client._waiting_module() is None else "sibling")')
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        alone = subprocess.run([sys.executable, '-B', '-c', script], cwd=str(self.alone),
                               capture_output=True, text=True, encoding='utf-8', env=environment)
        self.assertEqual(alone.returncode, 0, alone.stderr)
        self.assertEqual(alone.stdout.strip(), 'inline')
        kit = subprocess.run([sys.executable, '-B', '-c', script], cwd=str(KIT),
                             capture_output=True, text=True, encoding='utf-8', env=environment)
        self.assertEqual(kit.returncode, 0, kit.stderr)
        self.assertEqual(kit.stdout.strip(), 'sibling')

    def test_standalone_wait_answers_the_same_items_and_fingerprint_as_the_kit(self):
        self.config_is_usable()
        payload = waiting.worker_view([{'task': 't1', 'whose_move': 'you',
                                        'review_state': 'changes-requested',
                                        'pending_review_items': 2}])
        alone = self.run_client(self.alone / 'client.py', ['wait', '--interval', '60'], payload)
        kit = self.run_client(KIT / 'client.py', ['wait', '--interval', '60'], payload)
        self.assertEqual(alone.returncode, 0, alone.stderr)
        self.assertNotIn('ModuleNotFoundError', alone.stderr)
        self.assertEqual(alone.stderr, '')
        self.assertEqual(kit.returncode, 0, kit.stderr)
        self.assertEqual(alone.stdout, kit.stdout)
        body = json.loads(alone.stdout)
        self.assertEqual([row['task'] for row in body['items']], ['t1'])
        self.assertEqual(body['fingerprint'], payload['fingerprint'])
        self.assertTrue(waiting.is_fingerprint(body['fingerprint']))

    def test_standalone_wait_times_out_at_once_and_matches_the_kit(self):
        """--timeout 0 bounds the whole wait, so an empty read answers without sleeping."""
        self.config_is_usable()
        payload = waiting.worker_view([])
        alone = self.run_client(self.alone / 'client.py',
                                ['wait', '--interval', '60', '--timeout', '0'], payload)
        kit = self.run_client(KIT / 'client.py',
                              ['wait', '--interval', '60', '--timeout', '0'], payload)
        self.assertEqual(alone.returncode, 1, alone.stderr)
        self.assertNotIn('ModuleNotFoundError', alone.stderr)
        self.assertEqual(alone.stdout, kit.stdout)
        self.assertTrue(json.loads(alone.stdout)['timed_out'])

    def test_standalone_wait_refuses_a_too_short_interval_before_any_read(self):
        self.config_is_usable()
        payload = waiting.worker_view([{'task': 't1', 'whose_move': 'you'}])
        for argv in (['wait', '--interval', '30'], ['wait', '--since', 'nonsense'],
                     ['wait', '--timeout', '-1']):
            with self.subTest(argv=argv):
                alone = self.run_client(self.alone / 'client.py', argv, payload)
                kit = self.run_client(KIT / 'client.py', argv, payload)
                self.assertEqual(alone.returncode, 2, alone.stderr)
                self.assertEqual(alone.stdout, '')
                self.assertNotIn('ModuleNotFoundError', alone.stderr)
                self.assertEqual(alone.stderr, kit.stderr)

    def test_standalone_wait_sends_the_same_coordinator_read_as_the_kit(self):
        self.config_is_usable()
        payload = waiting.coordinator_view(
            [{'task': 't1', 'review_state': 'awaiting-review', 'contribution_id': 'c1'}],
            {'schema': 'lanes-v1', 'items': []})
        base = Path(self._tmp.name)
        alone_wire, kit_wire = base / 'alone.wire', base / 'kit.wire'
        alone = self.run_client(self.alone / 'client.py',
                                ['wait', '--coordinator', '--interval', '60'], payload, alone_wire)
        kit = self.run_client(KIT / 'client.py',
                              ['wait', '--coordinator', '--interval', '60'], payload, kit_wire)
        self.assertEqual(alone.returncode, 0, alone.stderr)
        self.assertEqual([row['task'] for row in json.loads(alone.stdout)['triggers']['awaiting-review']],
                         ['t1'])
        self.assertEqual(alone.stdout, kit.stdout)
        self.assertEqual(json.loads(alone_wire.read_text(encoding='utf-8'))['args'],
                         ['--coordinator', '--idle-minutes', '15'])
        self.assertEqual(alone_wire.read_text(encoding='utf-8'),
                         kit_wire.read_text(encoding='utf-8'))


class CopyInstructionTests(unittest.TestCase):
    """The docs that say what to copy name the client's stand-alone wait (kittrial-5bb.304)."""

    def flat(self, relative):
        return ' '.join((KIT / relative).read_text(encoding='utf-8')
                        .replace('`', '').lower().split())

    def test_finding_work_says_the_wait_logic_is_the_clients_own(self):
        text = self.flat('docs/FINDING_WORK.md')
        self.assertIn('a client.py copied on its own, with no other kit file beside it, runs wait',
                      text)

    def test_cli_contract_names_capabilities_as_the_only_companion_file(self):
        text = self.flat('docs/CLI_CONTRACT.md')
        self.assertIn('the only companion file a client-side command needs', text)

    def test_worker_guide_says_the_capability_commands_are_the_exception(self):
        text = self.flat('docs/WORKER_GUIDE.md')
        self.assertIn('the capability commands are the exception and need capabilities.py', text)

    def test_project_onboarding_says_wait_needs_no_other_kit_file(self):
        text = self.flat('templates/PROJECT_ONBOARDING.md')
        self.assertIn('wait needs no other kit file beside it', text)

    def test_the_endpoints_out_of_date_sentence_says_the_client_carries_wait(self):
        text = self.flat('endpoint.py')
        self.assertIn('it carries its own wait logic', text)


if __name__ == '__main__':
    unittest.main()
