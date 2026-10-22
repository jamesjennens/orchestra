"""A worker asking for help is answered with the commands it may run (kittrial-5bb.283).

``client.py ... help`` used to answer ``ValueError: Command is outside the contributor interface;
use admin.py for setup/maintenance``. A worker must never use ``admin.py`` - it is the installation
operator's host tool - so that sentence pointed an agent at the wrong door, and a worker who typed
help (or ``briefing``) got no help at all.

The answer is the commands a contributor may run, one line each, the two documents to read first,
and where ``admin.py`` belongs; an unknown command also names the nearest command. ``help`` is
answered by the client itself, without the server; the endpoint's own refusal carries the same
answer for an older client or a direct caller and still refuses everything outside the contributor
interface.
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import client

try:
    import endpoint
except ImportError:  # endpoint needs fcntl (POSIX)
    endpoint = None


class ContributorHelpTests(unittest.TestCase):
    """What the client answers, with no server involved."""

    def invoke(self, argv):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            with mock.patch.object(sys, 'argv', ['client.py', '--config', str(config),
                                                 '--project', 'example', '--actor', 'alice/session',
                                                 '--', *argv]), \
                    mock.patch.object(client, 'request',
                                      return_value={'stdout': '', 'stderr': '', 'returncode': 0}) as request, \
                    mock.patch('sys.stdout', new_callable=io.StringIO) as output, \
                    mock.patch('sys.stderr', new_callable=io.StringIO) as error:
                code = client.main()
        return code, output.getvalue(), error.getvalue(), request

    def test_help_prints_every_command_one_line_each_and_the_two_documents(self):
        code, out, err, request = self.invoke(['help'])
        self.assertEqual(code, 0)
        for usage, text in client.CONTRIBUTOR_COMMANDS:
            self.assertIn('  %s - %s\n' % (usage, text), out)
        self.assertIn('Read docs start and docs worker-guide first.', out)
        self.assertIn(client.ADMIN_TOOL_NOTE + '.', out)
        self.assertFalse(request.called, 'help is answered without the server')
        self.assertEqual(err, '')

    def test_no_command_at_all_prints_the_same_help(self):
        code, out, _err, request = self.invoke([])
        self.assertEqual(code, 0)
        self.assertIn('The commands a contributor may run, one line each:', out)
        self.assertFalse(request.called)

    def test_the_usage_help_prints_the_commands_too(self):
        """``--help`` after the actor: the task names this form as well."""
        with mock.patch.object(sys, 'argv', ['client.py', '--config', 'c.json', '--project', 'example',
                                             '--actor', 'alice/session', '--help']), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit) as raised:
                client.main()
        self.assertEqual(raised.exception.code, 0)
        text = output.getvalue()
        for usage, _text in client.CONTRIBUTOR_COMMANDS:
            self.assertIn('  %s - ' % usage, text)
        self.assertIn('Read docs start and docs worker-guide first.', text)

    def test_an_unknown_command_is_answered_with_the_list_and_the_nearest_command(self):
        for name, nearest in (('briefing', 'brief TASK'), ('workerguide', 'docs worker-guide'),
                              ('worker-guide', 'docs worker-guide'), ('nonsense-xyz', None)):
            with self.subTest(name=name):
                code, out, err, request = self.invoke([name])
                self.assertEqual(code, 1)
                self.assertEqual(out, '')
                self.assertIn('%r is outside the contributor interface. '
                              'The commands a contributor may run, one line each:' % name, err)
                for usage, text in client.CONTRIBUTOR_COMMANDS:
                    self.assertIn('  %s - %s\n' % (usage, text), err)
                self.assertIn('Read docs start and docs worker-guide first.', err)
                self.assertIn(client.ADMIN_TOOL_NOTE + '.', err)
                self.assertNotIn('use admin.py for setup/maintenance', err)
                if nearest is None:
                    self.assertNotIn('Did you mean', err)
                else:
                    self.assertIn('Did you mean `%s`?' % nearest, err)
                self.assertFalse(request.called, 'a command outside the interface is not sent')

    def test_the_table_names_every_action_the_client_dispatches_itself(self):
        """The dispatch is written from the same table, so the help cannot fall behind it."""
        names = client.contributor_command_names()
        for action in client.CLIENT_ACTIONS:
            self.assertIn(action, names, action)
        self.assertEqual(('brief', 'history', 'checkpoint'), names[:3])
        self.assertEqual(client.nearest_contributor_command('briefing'), 'brief TASK')
        self.assertEqual(client.nearest_contributor_command('workerguide'), 'docs worker-guide')
        self.assertIsNone(client.nearest_contributor_command('zzzzzzzz'))


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointRefusalTests(unittest.TestCase):
    """The answer an older client or a direct caller gets: the same words, still a refusal."""

    def refusal(self, args):
        """The endpoint's refusal for a command outside the contributor interface.

        Called where the endpoint raises it (`outside_contributor_interface`), because reaching it
        through `execute` would need a prepared runtime and the refusal happens before any native
        call; the wiring itself is pinned by the source test below.
        """
        with self.assertRaises(ValueError) as caught:
            endpoint.outside_contributor_interface(args[0])
        return str(caught.exception)

    def test_the_refusal_is_what_execute_raises(self):
        import inspect
        source = inspect.getsource(endpoint.execute)
        self.assertIn('if args[0] not in ALLOWED:', source)
        self.assertIn('outside_contributor_interface(args[0])', source)

    def test_the_two_copies_of_the_table_are_identical(self):
        self.assertEqual(client.CONTRIBUTOR_COMMANDS, endpoint.CONTRIBUTOR_COMMANDS)
        self.assertEqual(client.DOCS_TO_READ_FIRST, endpoint.DOCS_TO_READ_FIRST)
        self.assertEqual(client.ADMIN_TOOL_NOTE, endpoint.ADMIN_TOOL_NOTE)
        self.assertEqual(client.NEAREST_COMMANDS, endpoint.NEAREST_COMMANDS)
        for name in ('help', 'briefing', 'workerguide', 'nonsense-xyz'):
            self.assertEqual(client.contributor_help_text(name), endpoint.contributor_help_text(name))

    def test_the_endpoint_answers_help_with_the_list_and_the_admin_sentence(self):
        said = self.refusal(['help'])
        self.assertIn('The commands a contributor may run, one line each:', said)
        self.assertIn('  brief TASK - ', said)
        self.assertIn('Read docs start and docs worker-guide first.', said)
        self.assertIn(client.ADMIN_TOOL_NOTE, said)
        self.assertNotIn('use admin.py for setup/maintenance', said)

    def test_the_endpoint_answers_an_unknown_command_with_the_list_and_the_nearest(self):
        said = self.refusal(['briefing'])
        self.assertIn("'briefing' is outside the contributor interface", said)
        self.assertIn('  brief TASK - ', said)
        self.assertIn('Did you mean `brief TASK`?', said)
        self.assertIn(client.ADMIN_TOOL_NOTE, said)
        self.assertIn('Did you mean `docs worker-guide`?', self.refusal(['workerguide']))
        self.assertNotIn('Did you mean', self.refusal(['zzzzzzzz']))

    def test_each_client_side_command_is_answered_with_one_out_of_date_sentence(self):
        """kittrial-5bb.299: a name the CURRENT client runs locally is not answered with
        the contributor command list (which lists that same command -- a contradiction),
        and not with "Did you mean". One plain sentence: the command is the client's
        own, this client is older than the installation's kit, and how to update it."""
        from version import KIT_VERSION
        for name in endpoint.CLIENT_ACTIONS:
            said = self.refusal([name])
            self.assertIn('`%s` is run by the client itself' % name, said, name)
            self.assertIn('older than the installation', said, name)
            self.assertIn('client.py', said, name)
            self.assertIn(KIT_VERSION, said, name)
            self.assertNotIn('outside the contributor interface', said, name)
            self.assertNotIn('Did you mean', said, name)
            self.assertNotIn('The commands a contributor may run', said, name)
            # One sentence: no list of commands at all.
            self.assertNotIn('brief TASK - ', said, name)

    def test_a_real_outsider_name_is_still_refused_with_the_list(self):
        said = self.refusal(['zzzzzzzz'])
        self.assertIn("'zzzzzzzz' is outside the contributor interface", said)
        self.assertIn('The commands a contributor may run, one line each:', said)
        self.assertIn('  brief TASK - ', said)
        self.assertNotIn('run by the client itself', said)

    def test_the_endpoint_still_refuses_everything_outside_the_contributor_interface(self):
        """Only the ANSWER changed: the interface itself is the same, and its commands still pass."""
        import inspect
        source = inspect.getsource(endpoint.execute)
        self.assertIn("if args[0] not in ALLOWED:", source)
        self.assertEqual(endpoint.ALLOWED, {'list', 'show', 'ready', 'search', 'count', 'create',
                                            'update', 'close', 'reopen', 'comments', 'dep', 'state', 'lint'})


if __name__ == '__main__':
    unittest.main()
