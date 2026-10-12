"""A last test module that fails when an earlier one left a stream replaced.

kittrial-5bb.235: ``contextlib.redirect_stderr`` and ``redirect_stdout`` swap the PROCESS-WIDE
``sys.stderr``/``sys.stdout``. Two threads entering and leaving one in the wrong order (the
second saves the first's buffer and then restores it last) left a ``StringIO`` installed for the
rest of the run, so every later test that printed a NOTE to stderr - the node-skip NOTE of the
agents-page tests, for one - printed into that dead buffer and the run's output never showed it.
``tests/test_checkpoint_review3.py`` had that shape and is fixed; this is the guard that notices
the next one.

The streams are captured when this module is imported, and ``unittest discover`` imports every
module BEFORE it runs the first test, so these are the streams the run started with. The name
sorts after every other ``tests/test_*.py`` module, so these two tests run LAST: a module that
leaves a stream replaced fails the suite here instead of silently swallowing the output of every
later test.

Limit, stated rather than implied: a leak that happens while an earlier module is being IMPORTED,
before this module is imported, is baked into the baseline and is not caught. The leak this
guards against happens while tests run, which is where the reviewer found it.
"""
import sys
import unittest

#: The streams as this module found them, at import time, before any test ran.
STDERR_AT_IMPORT=sys.stderr
STDOUT_AT_IMPORT=sys.stdout


class StreamReplacementGuardTests(unittest.TestCase):
    """kittrial-5bb.235: the streams must be the ones the run started with."""

    def test_no_earlier_test_module_left_sys_stderr_replaced(self):
        self.assertIs(sys.stderr,STDERR_AT_IMPORT,
                      'an earlier test module left sys.stderr replaced with %r; a redirect_stderr entered '
                      'and left by two threads in the wrong order swaps the process-wide stream for the rest '
                      'of the run, and every later NOTE printed into it is lost'%(sys.stderr,))

    def test_no_earlier_test_module_left_sys_stdout_replaced(self):
        self.assertIs(sys.stdout,STDOUT_AT_IMPORT,
                      'an earlier test module left sys.stdout replaced with %r; a redirect_stdout entered '
                      'and left by two threads in the wrong order swaps the process-wide stream for the rest '
                      'of the run'%(sys.stdout,))


if __name__ == '__main__':
    unittest.main()
