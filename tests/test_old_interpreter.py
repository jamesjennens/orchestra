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

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = ('endpoint.py', 'client.py', 'admin.py', 'office_service.py')
#: What each one says to do, by a phrase of it.
ADVICE = {'endpoint.py': 'Set "python" in the client configuration to an interpreter of 3.10 or newer on the server',
          'client.py': 'Run the client with Python 3.10 or newer',
          'admin.py': 'on an office installation that is the bundled interpreter, INSTALL_ROOT/current/python-runtime/',
          'office_service.py': 'on an office installation that is the bundled interpreter, INSTALL_ROOT/current/python-runtime/'}

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
                                                    'Nothing was carried out. ' % (name, version, sys.executable)), said)
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
                        self.assertIn('usage:', done.stdout)

    def test_the_endpoint_says_it_whatever_it_is_sent(self):
        """Over SSH the client reads the endpoint's exit code and shows its stderr: 'SSH failed (2); ...'."""
        done = subprocess.run([sys.executable, '-c', AS_VERSION, '3.6.8', str(ROOT/'endpoint.py'), '--root', 'nowhere'],
                              input='{"project": "p", "actor": "a", "action": "bd", "args": ["list"]}',
                              capture_output=True, text=True, encoding='utf-8', timeout=120)
        self.assertEqual((done.returncode, done.stdout), (2, ''))
        self.assertIn('endpoint.py needs Python 3.10 or newer and was started with Python 3.6.8', done.stderr)


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

    def test_the_four_checks_are_the_same_lines(self):
        """They are written out in each file (client.py is copied alone to a worker's machine), so nothing but the
        program's name and what to do may differ."""
        shapes = set()
        for name in ENTRY_POINTS:
            source = (ROOT/name).read_text(encoding='utf-8')
            begins = source.index('import sys\nif sys.version_info < (3, 10):\n')
            block = source[begins:source.index('    sys.exit(2)\n', begins) + len('    sys.exit(2)\n')]
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

    def test_the_guard_of_that_test_bites(self):
        for newer in ('if (n := 1):\n    pass\n', 'def f(a, /):\n    pass\n'):
            with self.assertRaises(SyntaxError):
                ast.parse(newer, feature_version=(3, 6))


if __name__ == '__main__':
    unittest.main()
