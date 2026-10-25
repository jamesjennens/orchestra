"""The wait path imports and answers with fcntl unavailable (kittrial-5bb.403).

Windows has no ``fcntl``.  ``wake_log`` imports it lazily (``wake_log._fcntl``) so the
worker wait runs there, but a regression to a module-level ``import fcntl`` passes
every test on Linux: only the Windows CI jobs would catch it.  This module is the
Linux guard for that regression.

Every case runs in a SUBPROCESS that puts ``None`` in ``sys.modules['fcntl']`` BEFORE
importing the module under test.  That is what ``import fcntl`` meets on a platform
with no ``fcntl``: the import raises ``ImportError``.  Nothing here patches a module
after it was imported, so a module-level ``import fcntl`` cannot slip through, and a
lazy importer sees exactly the ``ImportError`` it is written to swallow.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]

#: Every case gets a hard budget so a hang FAILS the test instead of stalling the suite
#: (coordinator note on the verified plan, 2026-10-24T22:31Z).  These are imports, one
#: ``observe`` call and two waits that answer at once, so 60s only ever means "hung".
TIMEOUT_SECONDS = 60

#: Runs BEFORE anything else: ``sys.modules['fcntl'] = None`` makes a later
#: ``import fcntl`` raise ``ImportError``, which is what a platform without it does.
BLOCK = 'import sys\nsys.modules["fcntl"] = None\n'

#: One observe call with fcntl blocked, the way tests/test_empty_wake.py's
#: FcntlUnavailableTests does it: a fingerprint change with no row that is the
#: caller's move.  It prints one JSON result; the test asserts on that result.
OBSERVE_CASE = BLOCK + '''
import json
import shutil
import tempfile
from pathlib import Path

import waiting
import wake_log

ACTOR = 'session-00000000-0000-0000-0000-000000000000'


def a_row(task='t1', whose_move='you', review_state='changes-requested', **extra):
    row = {'task': task, 'id': task, 'review_state': review_state, 'whose_move': whose_move,
           'next_actor': 'assignee' if whose_move == 'you' else 'coordinator',
           'owner': ACTOR, 'assignee': ACTOR, 'status': 'open', 'contribution_id': 'c1',
           'pending_review_items': 0, 'redelivered': False,
           'wait_newer_activity_marker': 'm1',
           'newer_activity_by_others': 0, 'newer_activity_own': 0}
    row.update(extra)
    return row


root = Path(tempfile.mkdtemp(prefix='no-fcntl-observe-'))
try:
    observe_root = root / 'projects' / 'observe'
    observe_root.mkdir(parents=True)
    changed = [a_row(whose_move='coordinator', review_state='integrated',
                     newer_activity_by_others=1)]
    view = waiting.worker_view([])
    since = waiting.fingerprint([a_row()])
    if since == view['fingerprint']:
        raise SystemExit('the case is not a fingerprint change, so it proves nothing')
    warnings = wake_log.observe(observe_root, ACTOR, since, view, changed)
    print(json.dumps({
        'fcntl_in_sys_modules_is_none': sys.modules['fcntl'] is None,
        'warnings': warnings,
        'empty_wakes_24h': wake_log.read(observe_root, ACTOR)['empty_wakes_24h'],
        'directory_exists': (observe_root / wake_log.DIRECTORY).exists(),
        'paths_under_project': sorted(str(p.relative_to(observe_root))
                                      for p in observe_root.rglob('*')),
    }))
finally:
    shutil.rmtree(root, ignore_errors=True)
'''


#: The wait-facing half of the same claim.  Coordinator note on the verified plan: the
#: observe case must also show the wait's answer is the one it gives with fcntl present,
#: not only that nothing was written.  This mirrors
#: tests/test_empty_wake.py::FcntlUnavailableTests::test_the_wait_answers_identically_when_fcntl_is_unavailable
#: and therefore patches sys.modules IN-PROCESS, because it needs both arms; the
#: import cases above are the ones that block fcntl before anything is imported.
PARITY_CASE = '''
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import wake_log
import work

ACTOR = 'session-00000000-0000-0000-0000-000000000000'


def row():
    return {'id': 't1', 'title': 'Task t1', 'issue_type': 'task', 'status': 'open',
            'assignee': ACTOR, 'description': 'Intent', 'acceptance_criteria': 'Acceptance',
            'labels': [], 'priority': 2, 'created_by': ACTOR,
            'created_at': '2026-09-01T00:00:00Z', 'updated_at': '2026-09-15T09:00:00Z',
            'dependencies': [], 'comments': []}


def run_wait(project):
    lines = json.dumps(row())

    def runner(_argv):
        return lines
    return work.execute(project, ACTOR, 'wait', [], {}, runner, operators=[ACTOR])


root = Path(tempfile.mkdtemp(prefix='no-fcntl-wait-'))
try:
    present = root / 'projects' / 'present'
    present.mkdir(parents=True)
    expected = run_wait(present)
    blocked = root / 'projects' / 'blocked'
    blocked.mkdir(parents=True)
    with mock.patch.dict(sys.modules, {'fcntl': None}):
        blocked_arm_saw_none = sys.modules['fcntl'] is None
        answered = run_wait(blocked)
    print(json.dumps({
        'blocked_arm_saw_none': blocked_arm_saw_none,
        'expected': expected,
        'answered': answered,
        'blocked_empty_wakes_24h': wake_log.read(blocked, ACTOR)['empty_wakes_24h'],
        'blocked_directory_exists': (blocked / wake_log.DIRECTORY).exists(),
    }))
finally:
    shutil.rmtree(root, ignore_errors=True)
'''


def run_source(label, source):
    """Run ``source`` in its own interpreter with cwd at the kit, and return the process.

    A hang is a FAILURE rather than a stall, so the suite always finishes.
    """
    try:
        return subprocess.run([sys.executable, '-c', source], cwd=str(KIT),
                              capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as expired:
        raise AssertionError(
            'the {0} case did not finish within {1}s, so it hung\n'
            'stdout: {2}\nstderr: {3}'.format(label, TIMEOUT_SECONDS, expired.stdout,
                                              expired.stderr))


def parse_result(label, done):
    """Return the JSON ``done`` printed, failing the test with its output if it did not."""
    if done.returncode != 0:
        raise AssertionError(
            'the {0} case did not exit 0 (it was {1})\n'
            'stdout: {2}\nstderr: {3}'.format(label, done.returncode, done.stdout,
                                              done.stderr))
    return json.loads(done.stdout.strip().splitlines()[-1])


class NoFcntlImportTests(unittest.TestCase):
    """Each module the wait path imports must import with fcntl unavailable."""

    def assert_imports_without_fcntl(self, module):
        """Import ``module`` in a subprocess whose fcntl was blocked before any import."""
        source = BLOCK + 'import {0}\nprint("imported", {0}.__name__)\n'.format(module)
        done = run_source('import ' + module, source)
        self.assertEqual(
            done.returncode, 0,
            'import {0} with fcntl unavailable did not exit 0 (it was {1})\n'
            'stdout: {2}\nstderr: {3}'.format(module, done.returncode, done.stdout,
                                              done.stderr))
        self.assertIn('imported ' + module, done.stdout)

    def test_work_imports_without_fcntl(self):
        self.assert_imports_without_fcntl('work')

    def test_waiting_imports_without_fcntl(self):
        self.assert_imports_without_fcntl('waiting')

    def test_wake_log_imports_without_fcntl(self):
        self.assert_imports_without_fcntl('wake_log')

    def test_client_imports_without_fcntl(self):
        self.assert_imports_without_fcntl('client')

    def test_lanes_imports_without_fcntl(self):
        self.assert_imports_without_fcntl('lanes')

    def test_http_service_imports_without_fcntl(self):
        self.assert_imports_without_fcntl('http_service')


class NoFcntlObserveTests(unittest.TestCase):
    """With fcntl unavailable the log is UNAVAILABLE, not broken.

    So ``wake_log.observe`` records nothing and warns nothing, and the wait's answer is
    exactly the answer it gives with fcntl present (coordinator note on the verified
    plan): the platform loses the log, never a different wait.
    """

    def test_observe_records_nothing_and_the_wait_answers_identically(self):
        observed = parse_result('observe', run_source('observe', OBSERVE_CASE))
        self.assertTrue(observed['fcntl_in_sys_modules_is_none'], observed)
        self.assertEqual(observed['warnings'], [], observed)
        self.assertEqual(observed['empty_wakes_24h'], 0, observed)
        self.assertFalse(observed['directory_exists'], observed)
        self.assertEqual(observed['paths_under_project'], [], observed)

        parity = parse_result('wait parity', run_source('wait parity', PARITY_CASE))
        self.assertTrue(parity['blocked_arm_saw_none'], parity)
        self.assertEqual(parity['answered'], parity['expected'],
                         'the wait answered differently with fcntl unavailable')
        self.assertEqual(parity['blocked_empty_wakes_24h'], 0, parity)
        self.assertFalse(parity['blocked_directory_exists'], parity)


if __name__ == '__main__':
    unittest.main()
