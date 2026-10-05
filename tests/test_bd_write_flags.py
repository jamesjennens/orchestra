"""The table of bd write flags agrees with the scanners, and with a real bd when one is available.

Each of the five round-two findings of kittrial-5bb.113 was a bd write the guard had not
anticipated. ``reserved_comments.WRITE_FLAGS`` now lists every flag of every writing
command a contributor may run and says how it bears on rows. This module fails when

* the scanners know a flag the table does not, or the reverse (always run), or
* the help of a real bd lists a flag for one of those commands that the table does not
  know (run where a bd binary is available: set ``ORCHESTRA_BD_BIN`` or put bd on PATH).
"""
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reserved_comments as rc

KINDS = {'row', 'new', 'chosen', 'file', 'label', 'plain'}


def _bd_binary():
    configured = os.environ.get('ORCHESTRA_BD_BIN')
    if configured:
        return Path(configured)
    found = shutil.which('bd')
    return Path(found) if found else None


BD = _bd_binary()


class TableTests(unittest.TestCase):
    def test_every_entry_has_a_known_kind(self):
        for command, flags in rc.WRITE_FLAGS.items():
            for flag, kind in flags.items():
                self.assertIn(kind, KINDS, (command, flag))
                self.assertTrue(flag.startswith('--'), (command, flag))

    def test_the_scanner_inventories_and_the_table_name_the_same_flags(self):
        for command in ('create', 'update', 'close', 'reopen'):
            known = (rc.BD_LONG_VALUE_FLAGS.get(command, set()) | rc.BD_LONG_BOOL_FLAGS.get(command, set())) - {'--help'}
            self.assertEqual(sorted(known), sorted(rc.WRITE_FLAGS[command]), command)

    def test_what_the_table_says_is_what_the_scanner_does(self):
        other, third = 'pp-abc', 'pp-def'
        for command, flags in rc.WRITE_FLAGS.items():
            for flag, kind in flags.items():
                with self.subTest(command=command, flag=flag):
                    # The joined spelling, so a flag that takes no value cannot make its value an operand.
                    value = 'true' if kind == 'chosen' else third
                    argv = command.split() + ([] if command == 'create' else [other]) + ['%s=%s' % (flag, value)]
                    found = rc.write_targets(argv)
                    if kind == 'row':
                        self.assertIn(third, found['targets'])
                        self.assertIsNone(found['refusal'])
                    elif kind == 'new':
                        self.assertEqual(found['new_id'], third)
                    elif kind in ('chosen', 'file'):
                        self.assertTrue(found['refusal'], argv)
                    else:
                        self.assertNotIn(third, found['targets'], argv)
                        self.assertIsNone(found['refusal'], (argv, found['refusal']))

    def test_the_label_flags_are_the_ones_the_label_guard_reads(self):
        for command in ('create', 'update'):
            labels = {flag for flag, kind in rc.WRITE_FLAGS[command].items() if kind == 'label'}
            self.assertEqual(labels, {flag for flag in rc.LABEL_WRITE_FLAGS[command] if flag.startswith('--')})

    def test_the_dependency_flags_are_the_ones_the_scanner_resolves(self):
        rows = {flag for command in ('dep', 'dep add') for flag, kind in rc.WRITE_FLAGS[command].items() if kind == 'row'}
        self.assertEqual(rows, {flag for flag, names in rc._DEP_VALUE_FLAGS.items() if names is True})


@unittest.skipIf(BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class RealBdHelpTests(unittest.TestCase):
    """The flags bd itself lists for each writing command are all in the table."""

    #: Flags the help does not print but the binary accepts (measured, kittrial-5bb.30).
    HIDDEN = {'create': {'--label'}}

    def flags(self, command):
        done = subprocess.run([str(BD), *command.split(), '--help'], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        text = done.stdout
        self.assertIn('\nFlags:\n', text, command)
        section = text.split('\nFlags:\n', 1)[1].split('\nGlobal Flags:\n', 1)[0]
        return set(re.findall(r'^\s+(?:-\w, )?(--[a-z][a-z-]*)', section, re.M)) - {'--help'}

    def test_no_writing_command_has_a_flag_the_table_does_not_know(self):
        for command, known in rc.WRITE_FLAGS.items():
            with self.subTest(command=command):
                listed = self.flags(command)
                self.assertEqual(sorted(listed - set(known)), [], 'bd lists a flag for `%s` that the table does not know' % command)
                self.assertEqual(sorted(set(known) - listed - self.HIDDEN.get(command, set())), [],
                                 'the table knows a flag for `%s` that bd no longer lists' % command)

    def test_the_writing_subcommands_are_the_ones_the_table_has(self):
        done = subprocess.run([str(BD), 'dep', '--help'], capture_output=True, text=True, timeout=60)
        section = done.stdout.split('Available Commands:\n', 1)[1].split('\n\n', 1)[0]
        listed = set(re.findall(r'^\s+([a-z]+)\s', section, re.M))
        self.assertEqual(listed, {'add', 'cycles', 'list', 'relate', 'remove', 'tree', 'unrelate'})
        done = subprocess.run([str(BD), 'comments', '--help'], capture_output=True, text=True, timeout=60)
        section = done.stdout.split('Available Commands:\n', 1)[1].split('\n\n', 1)[0]
        self.assertEqual(set(re.findall(r'^\s+([a-z]+)\s', section, re.M)), {'add', 'list'})


if __name__ == '__main__':
    unittest.main()
