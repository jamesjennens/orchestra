"""An interpreter too old for the kit is told so, in one sentence, before anything is imported.

kittrial-5bb.191. The first use of the SSH client route against an office install on a RHEL 8
server ran the endpoint with the host's ``python3``, which is 3.6 there. Every command died
with ``sre_constants.error: unbalanced parenthesis`` from a pattern ``review_workflow``
compiles at import; ``client.py`` and ``admin.py`` die under 3.6 in other imports of their
own. None of it says that the interpreter is the matter.

The suite itself needs 3.10, so the old interpreter here is a stand-in: the program is run in
a child whose ``sys.version_info`` was replaced before the program began. That the check
comes before any import that can fail, and that 3.6 can read it, is asserted on the source.
"""
import ast
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SERVER = 'on an office installation that is the bundled interpreter, INSTALL_ROOT/current/python-runtime/'
OWN = 'Run it with Python 3.10 or newer.'
#: Every program that carries the check, with a phrase of what it says to do.
ADVICE = {'endpoint.py': 'Set "python" in the client configuration to an interpreter of 3.10 or newer on the server',
          'client.py': 'Run the client with Python 3.10 or newer',
          'admin.py': SERVER, 'office_service.py': SERVER, 'http_service.py': SERVER, 'tools/office_verify.py': SERVER,
          'capabilities.py': OWN, 'http_client.py': OWN, 'lifecycle.py': OWN, 'coordination.py': OWN,
          'requirement_records.py': OWN, 'setup_assistant.py': OWN, 'worker.py': OWN, 'worker_gate.py': OWN,
          'tools/http_rev3_probe.py': OWN, 'tools/http_rev4_probe.py': OWN, 'tools/http_rev5_probe.py': OWN,
          'tools/http_security_probe.py': OWN, 'tools/strict_canonical_endpoint.py': OWN,
          'tools/demonstrate_review_targeting.py': OWN}
ENTRY_POINTS = tuple(ADVICE)
#: Programs with a ``__main__`` block and NO check, and why. The first two run under the host's
#: Python 3.6 on purpose. The others were started with ``--help`` under a real Python 3.6.8
#: (almalinux 8 platform-python, 2026-10-08) and ended without an error; that is all that is
#: claimed of them. A new program belongs in ADVICE unless somebody has shown the same of it.
RUN_UNDER_3_6 = ('ssh_forced_command.py', 'tools/office_release.py')
STARTED_UNDER_3_6 = ('activity.py', 'bootstrap.py', 'export_requirements.py', 'publish_brd.py', 'requirement_impact.py',
                     'requirements.py')
#: Library-only modules: imported by programs, never run as one. No ``__main__`` block.
#: Four of them carry a shebang anyway (capability_verification.py, http_auth.py, http_authority.py,
#: proposal_records.py) — a library can be copied beside a program — so only the ``__main__`` block is required.
MODULES = ('actor_names.py', 'agent_prompts.py', 'artifacts.py', 'bd_refusals.py', 'briefing.py',
           'capability_misses.py', 'capability_records.py', 'capability_verification.py', 'credential_store.py',
           'feedback.py', 'field_limits.py', 'guidance.py', 'handoff.py', 'http_auth.py', 'http_authority.py',
           'keyed_entries.py', 'keyed_records.py', 'native.py', 'onboarding.py', 'open_items.py',
           'project_creation.py', 'project_setup.py', 'proposal_records.py', 'record_json.py', 'recovery.py',
           'reference_records.py', 'render.py', 'reserved_comments.py', 'review_recommendations.py',
           'review_state.py', 'review_workflow.py', 'sessions.py', 'version.py', 'work.py')

AS_VERSION = """
import collections, runpy, sys
version = tuple(int(part) for part in sys.argv[1].split('.'))
if version != (0, 0, 0):
    sys.version_info = collections.namedtuple('version_info', 'major minor micro releaselevel serial')(*version, 'final', 0)
program = sys.argv[2]
sys.argv = sys.argv[2:]
runpy.run_path(program, run_name='__main__')
"""


def started(name, version, *arguments):
    """``name`` run as a program by an interpreter that says it is ``version`` ('0.0.0': what it really is)."""
    return subprocess.run([sys.executable, '-c', AS_VERSION, version, str(ROOT/name), *arguments],
                          capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)


class TooOldTests(unittest.TestCase):

    def test_an_interpreter_older_than_3_10_is_told_so_in_one_sentence(self):
        for name in ENTRY_POINTS:
            for version in ('3.6.8', '3.9.18', '2.7.18'):
                with self.subTest(program=name, version=version):
                    done = started(name, version, '--help')
                    self.assertEqual(done.returncode, 2, done.stderr)
                    self.assertEqual(done.stdout, '')
                    said = done.stderr
                    self.assertEqual(len(said.strip().splitlines()), 1, said)
                    self.assertNotIn('Traceback', said)
                    self.assertTrue(said.startswith('%s needs Python 3.10 or newer and was started with Python %s (%s). '
                                                    'Nothing was carried out. ' % (name.rpartition('/')[2], version, sys.executable)), said)
                    self.assertIn(ADVICE[name], said)
                    self.assertTrue(said.endswith('.\n'), said)

    def test_python_3_10_and_the_interpreter_that_runs_the_suite_are_not_refused(self):
        """The bound is 3.10 itself: it is what README and OPERATIONS state and what CI runs."""
        for name in ENTRY_POINTS:
            for version in ('3.10.0', '0.0.0'):
                with self.subTest(program=name, version=version):
                    done = started(name, version, '--help')
                    self.assertNotIn('needs Python 3.10 or newer', done.stderr + done.stdout)
                    if name == 'client.py' or (version == '0.0.0' and sys.platform != 'win32'):
                        # The others import fcntl, which Windows has not: there they end in that
                        # ImportError, as they did before, and not in the sentence.
                        self.assertEqual(done.returncode, 0, done.stderr)
                        if name != 'tools/demonstrate_review_targeting.py':
                            # That one has no argument parser; --help just runs its demo body.
                            self.assertIn('usage', done.stdout)  # argparse's "usage:", or the JSON help of capabilities.py

    def test_the_endpoint_says_it_whatever_it_is_sent(self):
        """Over SSH the client reads the endpoint's exit code and shows its stderr: 'SSH failed (2); ...'."""
        done = subprocess.run([sys.executable, '-c', AS_VERSION, '3.6.8', str(ROOT/'endpoint.py'), '--root', 'nowhere'],
                              input='{"project": "p", "actor": "a", "action": "bd", "args": ["list"]}',
                              capture_output=True, text=True, encoding='utf-8', timeout=120)
        self.assertEqual((done.returncode, done.stdout), (2, ''))
        self.assertIn('endpoint.py needs Python 3.10 or newer and was started with Python 3.6.8', done.stderr)


class ClientSaysItTests(unittest.TestCase):
    """Through client.py the user read 'SSH failed (2); outcome may be uncertain. endpoint.py needs ... Nothing
    was carried out.': two statements that contradict each other (review of kittrial-5bb.191)."""

    SAID = ('endpoint.py needs Python 3.10 or newer and was started with Python 3.6.8 (/usr/libexec/platform-python). '
            'Nothing was carried out. Set "python" in the client configuration to an interpreter of 3.10 or newer on the '
            'server; on an office installation that is the bundled one, INSTALL_ROOT/current/python-runtime/..., as '
            'add-project prints it.\n')
    SSH = {'host': 'sample', 'endpoint': '/srv/kit/endpoint.py', 'root': '/srv/state'}

    def answered(self, returncode, stdout, stderr, config=None):
        import client
        done = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with mock.patch.object(client.subprocess, 'run', return_value=done), self.assertRaises(RuntimeError) as failed:
            client.request(config or self.SSH, 'alpha', 'alex/s1', ['list'])
        return str(failed.exception)

    def test_the_sentence_the_test_uses_is_the_one_the_endpoint_writes(self):
        source = (ROOT/'endpoint.py').read_text(encoding='utf-8')
        self.assertIn(self.SAID.strip().partition('Nothing was carried out. ')[2], source)

    def test_the_endpoints_sentence_is_shown_as_it_is(self):
        said = self.answered(2, '', self.SAID)
        self.assertEqual(said, 'SSH: ' + self.SAID.strip())
        self.assertNotIn('uncertain', said)
        local = self.answered(2, '', self.SAID, {'transport': 'local', 'python': '/usr/bin/python3',
                                                 'endpoint': '/srv/kit/endpoint.py', 'root': '/srv/state'})
        self.assertEqual(local, 'Local endpoint: ' + self.SAID.strip())

    def test_with_a_forced_command_the_advice_is_about_the_key_line(self):
        said = self.answered(2, '', self.SAID, dict(self.SSH, forced_command=True))
        self.assertTrue(said.startswith('SSH: endpoint.py needs Python 3.10 or newer and was started with Python 3.6.8 '
                                        '(/usr/libexec/platform-python). Nothing was carried out.'))
        self.assertIn('This key runs a forced command', said)
        self.assertIn('print that line again (admin.py authorized-keys) with an interpreter of 3.10 or newer', said)
        self.assertNotIn('uncertain', said)
        # The endpoint's own advice says to set "python" here, which is wrong under a forced
        # command: the note replaces it (kittrial-5bb.222, review item two-contradicting-instructions).
        self.assertNotIn('Set "python" in the client configuration', said)

    def test_with_a_forced_command_a_reworded_tail_is_shown(self):
        """The recogniser stays loose so a reworded advice is still recognised; a tail that is not
        the endpoint's own advice is shown and then the forced-command note (kittrial-5bb.222)."""
        reworded = ('endpoint.py needs Python 3.10 or newer and was started with Python 3.6.8 (/usr/bin/python3). '
                    'Nothing was carried out. Ask your operator for a newer interpreter.\n')
        said = self.answered(2, '', reworded, dict(self.SSH, forced_command=True))
        self.assertIn('Ask your operator for a newer interpreter.', said)
        self.assertIn('This key runs a forced command', said)
        self.assertNotIn('uncertain', said)

    def test_the_recogniser_accepts_any_spaced_tail(self):
        """A server whose advice was reworded in another release is still recognised (kittrial-5bb.222)."""
        for tail in ('Set "python" in the client configuration.',
                     'Ask your operator for a newer interpreter.',
                     'Something entirely different that a future release might say.',
                     ''):
            with self.subTest(tail=tail):
                stderr = ('endpoint.py needs Python 3.10 or newer and was started with Python 3.6.8 (/usr/bin/python3). '
                          'Nothing was carried out.' + (' ' + tail if tail else '') + '\n')
                said = self.answered(2, '', stderr)
                self.assertTrue(said.startswith('SSH: endpoint.py needs Python 3.10 or newer'), said)
                self.assertNotIn('uncertain', said)
                self.assertNotIn('SSH failed', said)

    def test_glued_text_is_not_the_sentence(self):
        """No space after the fixed sentences is not a tail: the uncertain warning stays (kittrial-5bb.222)."""
        glued = ('endpoint.py needs Python 3.10 or newer and was started with Python 3.6.8 (/usr/bin/python3). '
                 'Nothing was carried out.But kit-1 was written.\n')
        said = self.answered(2, '', glued)
        self.assertTrue(said.startswith('SSH failed (2); outcome may be uncertain. '), said)
        self.assertNotIn('This key runs a forced command', said)

    def test_the_tail_is_recognised_up_to_600_characters(self):
        """600 characters of tail are shown; 601 are not recognised (kittrial-5bb.222)."""
        head = ('endpoint.py needs Python 3.10 or newer and was started with Python 3.6.8 (/usr/bin/python3). '
                'Nothing was carried out. ')
        for n, recognised in ((600, True), (601, False)):
            with self.subTest(length=n):
                stderr = head + ('x' * n) + '\n'
                said = self.answered(2, '', stderr)
                if recognised:
                    self.assertTrue(said.startswith('SSH: endpoint.py needs Python 3.10 or newer'), said)
                    self.assertIn('x' * n, said)
                    self.assertNotIn('uncertain', said)
                else:
                    self.assertTrue(said.startswith('SSH failed (2); outcome may be uncertain. '), said)

    def test_local_transport_with_forced_command_shows_the_sentence(self):
        """A local-transport config with forced_command is still the local label; forced_command is an SSH
        concept and does not change the local advice (kittrial-5bb.222, mutant c12)."""
        said = self.answered(2, '', self.SAID, {'transport': 'local', 'python': '/usr/bin/python3',
                                                 'endpoint': '/srv/kit/endpoint.py', 'root': '/srv/state',
                                                 'forced_command': True})
        self.assertEqual(said, 'Local endpoint: ' + self.SAID.strip())
        self.assertNotIn('This key runs a forced command', said)

    def test_anything_else_keeps_what_the_client_always_said(self):
        for returncode, stdout, stderr in ((1, '', self.SAID), (2, 'x', self.SAID), (2, '', 'Traceback\n' + self.SAID),
                                           (2, '', self.SAID + 'and more\n'), (2, '', 'bash: python3: command not found\n'),
                                           (2, '', self.SAID.replace('endpoint.py', 'admin.py')),
                                           (2, '', self.SAID.replace('Nothing was carried out.', 'Something was.')), (255, '', '')):
            with self.subTest(returncode=returncode, stderr=stderr[:30]):
                said = self.answered(returncode, stdout, stderr)
                self.assertTrue(said.startswith('SSH failed (%d); outcome may be uncertain. ' % returncode), said)


class SourceTests(unittest.TestCase):
    """What a stand-in cannot show: the check runs on a real 3.6 only if 3.6 can read it and nothing precedes it."""

    def tree(self, name):
        return ast.parse((ROOT/name).read_text(encoding='utf-8'))

    def test_the_check_is_the_first_thing_each_program_does(self):
        for name in ENTRY_POINTS:
            with self.subTest(program=name):
                body = self.tree(name).body
                self.assertIsInstance(body[0], ast.Expr)                    # the docstring
                self.assertIsInstance(body[0].value, ast.Constant)
                first = body[1]
                self.assertIsInstance(first, ast.Import)
                self.assertEqual([alias.name for alias in first.names], ['sys'])
                check = body[2]
                self.assertIsInstance(check, ast.If)
                self.assertEqual(ast.unparse(check.test), 'sys.version_info < (3, 10)')
                self.assertEqual(check.orelse, [])
                self.assertEqual([ast.unparse(statement).split('(')[0] for statement in check.body],
                                 ['sys.stderr.write', 'sys.exit'])
                self.assertEqual(ast.unparse(check.body[1]), 'sys.exit(2)')

    def test_the_checks_are_the_same_lines(self):
        """They are written out in each file (client.py is copied alone to a worker's machine), so nothing but the
        program's name and what to do may differ."""
        shapes = set()
        for name in ENTRY_POINTS:
            source = (ROOT/name).read_text(encoding='utf-8')
            begins = source.index('import sys\nif sys.version_info < (3, 10):\n')
            block = source[begins:source.index('    sys.exit(2)\n', begins) + len('    sys.exit(2)\n')]
            name = name.rpartition('/')[2]
            self.assertIn("sys.stderr.write('%s needs Python 3.10 or newer" % name, block)
            advice = block[block.index('Nothing was carried out. ') + len('Nothing was carried out. '):block.index("% (sys.version_info[0]")]
            shapes.add(block.replace(advice, 'ADVICE ').replace(name, 'PROGRAM'))
        self.assertEqual(len(shapes), 1, shapes)

    def test_python_3_6_can_read_each_program(self):
        """A file is compiled whole before its first line runs: one piece of newer syntax anywhere in it and 3.6
        answers a SyntaxError instead of the sentence. (As far as ``ast`` judges a feature version: it knows the
        walrus, positional-only parameters, parenthesised context managers, match, and the like.)"""
        for name in ENTRY_POINTS:
            with self.subTest(program=name):
                ast.parse((ROOT/name).read_text(encoding='utf-8'), feature_version=(3, 6))

    def test_every_program_of_the_kit_has_the_check_or_is_known_to_start_without_it(self):
        """Review of kittrial-5bb.191: six programs had been missed. Every .py in the root and tools/ is
        in ADVICE (and so in every test above), on one of the two short lists, or in MODULES (library-only).
        A script without a ``__main__`` block (as tools/demonstrate_review_targeting.py is) must be seen
        too (kittrial-5bb.222, reviewer mutant l4)."""
        programs = set()
        for folder in (ROOT, ROOT/'tools'):
            for path in folder.glob('*.py'):
                programs.add(path.relative_to(ROOT).as_posix())
        self.assertGreaterEqual(len(programs), 27)
        listed = list(ADVICE) + list(RUN_UNDER_3_6) + list(STARTED_UNDER_3_6) + list(MODULES)
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(sorted(programs), sorted(listed))
        for name in RUN_UNDER_3_6 + STARTED_UNDER_3_6:
            self.assertNotIn('needs Python 3.10 or newer', (ROOT/name).read_text(encoding='utf-8'), name)
        # A library module grows a __main__ block only if someone starts running it as a program;
        # that file then needs the check (kittrial-5bb.222, review item the-listing-test-lost-a-guarantee).
        for name in MODULES:
            with self.subTest(module=name):
                self.assertFalse(any(isinstance(node, ast.If) and '__main__' in ast.unparse(node.test)
                                     and '__name__' in ast.unparse(node.test)
                                     for node in self.tree(name).body), name)

    def test_no_future_import_stands_where_python_3_6_would_stop_at_it(self):
        """``from __future__ import annotations`` is refused by 3.6 when the file is COMPILED, before its first
        line runs: setup_assistant.py had it and answered a SyntaxError. No program with the check has one."""
        for name in ENTRY_POINTS:
            with self.subTest(program=name):
                self.assertFalse([node for node in self.tree(name).body
                                  if isinstance(node, ast.ImportFrom) and node.module == '__future__'])

    def test_the_guard_of_that_test_bites(self):
        for newer in ('if (n := 1):\n    pass\n', 'def f(a, /):\n    pass\n'):
            with self.assertRaises(SyntaxError):
                ast.parse(newer, feature_version=(3, 6))


if __name__ == '__main__':
    unittest.main()
