"""An SSH key names its projects (kittrial-5bb.193).

Rule 1 of docs/COORDINATORS_PER_PROJECT_DESIGN.md. The forced command of an authorized_keys
line may carry ``--project NAME``; the endpoint is then started with ``--key-project NAME``
and refuses every request that names another project, with the answer it gives for a
project that does not exist. ``admin.py authorized-keys --project`` prints such a line and
``admin.py authorized-keys-list`` reads the lines that are installed.

The test that matters is ``test_every_action_is_refused_for_another_project``: every action
the endpoint has, raw bd and the session actions included, with a bound key, naming a
project the key is not bound to.
"""
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import admin
import ssh_forced_command as forced

try:
    import endpoint
except ImportError:                                   # fcntl: the endpoint is POSIX only
    endpoint = None

POSIX = unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
KEY_BODY = base64.b64encode(b'orchestra-synthetic-test-key').decode()
OTHER_BODY = base64.b64encode(b'orchestra-second-synthetic-key').decode()
UNKNOWN = 'Unknown/uninitialized project'


def runtime(base, *projects):
    """A runtime root with initialized-looking projects, each with one view to read."""
    root = Path(base)/'rt'
    for name in projects:
        (root/'projects'/name/'.beads').mkdir(parents=True)
        (root/'projects'/name/'.beads'/'metadata.json').write_text('{}', encoding='utf-8')
        (root/'projects'/name/'views').mkdir()
        (root/'projects'/name/'views'/'CURRENT.md').write_text('the view of %s\n' % name, encoding='utf-8')
    return root


def tree(folder):
    return {str(path.relative_to(folder)): (path.read_bytes() if path.is_file() else None)
            for path in sorted(Path(folder).rglob('*'))}


def actions_of_the_endpoint():
    """Every action name ``endpoint.execute`` compares the request's action with, read from its source."""
    source = (KIT/'endpoint.py').read_text(encoding='utf-8')
    body = source[source.index('\ndef execute('):source.index('\ndef main(')]
    names = set(re.findall(r"action'?\)?\s*[!=]=\s*'([a-z-]+)'", body))        # `action!='bd'` is how the default is named
    for group in re.findall(r"action in \(([^)]*)\)", body):
        names.update(re.findall(r"'([a-z-]+)'", group))
    return names


@POSIX
class BoundEndpointTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(self.tmp.name, 'alpha', 'beta')
        self.bound = frozenset({'alpha'})

    def refused(self, request, key_projects=None, said=UNKNOWN):
        """``request`` over the bound key: refused with ``said``, and nothing was run or read for it."""
        reached = AssertionError('the request reached a program or a lock')
        with mock.patch.object(subprocess, 'run', side_effect=reached), \
                mock.patch.object(endpoint.native, 'run', side_effect=reached), \
                mock.patch.object(endpoint.fcntl, 'flock', side_effect=reached), \
                mock.patch.object(endpoint, 'project_dir', side_effect=reached):
            with self.assertRaises(ValueError) as refusal:
                endpoint.execute(self.root, request, key_projects=self.bound if key_projects is None else key_projects)
        self.assertEqual(str(refusal.exception), said)

    def test_the_list_of_actions_is_read_from_the_endpoint(self):
        """So that an action added later is in the test by itself; these are the ones of today."""
        actions = actions_of_the_endpoint()
        for known in ('session', 'handoff', 'review', 'work', 'set-onboarding', 'onboard', 'docs', 'actor-standing',
                      'setup-status', 'guidance', 'anchors', 'ref', 'capability', 'proposal', 'brief', 'history',
                      'checkpoint', 'lifecycle', 'coordinate', 'requirement', 'view', 'refresh', 'bd', 'feedback',
                      'create-project', 'project-creations', 'creation-standing'):
            self.assertIn(known, actions)

    def test_every_action_is_refused_for_another_project(self):
        before = tree(self.root)
        for action in sorted(actions_of_the_endpoint() - set(endpoint.SERVICE_ONLY_ACTIONS)) + ['no-such-action']:
            for project in ('beta', 'gamma'):                 # another project of the runtime, and one that is none
                with self.subTest(action=action, project=project):
                    self.refused({'project': project, 'actor': 'alex/s1', 'action': action, 'args': ['list'],
                                  'payload': {}, 'attachments': {}, 'path': 'CURRENT.md'})
        self.assertEqual(tree(self.root), before)

    def test_raw_bd_is_refused_for_another_project_in_every_command(self):
        for command in sorted(endpoint.ALLOWED) + ['init', 'sql', 'export']:
            with self.subTest(command=command):
                self.refused({'project': 'beta', 'actor': 'alex/s1', 'action': 'bd', 'args': [command, 'beta-1']})
                self.refused({'project': 'beta', 'actor': 'alex/s1', 'args': [command]})        # bd is the default action

    def test_the_session_actions_are_refused_for_another_project(self):
        for args in (['register', '--role', 'worker'], ['resume', 'x'], ['show'], ['run', 'start'], ['run', 'status']):
            with self.subTest(args=args):
                self.refused({'project': 'beta', 'actor': '', 'action': 'session', 'args': args})
                self.refused({'project': 'beta', 'actor': 'session-1', 'action': 'session', 'args': args})

    def test_a_request_that_names_no_project_or_not_a_name_is_refused_the_same(self):
        for request in ({'actor': 'a', 'action': 'view'}, {'project': None, 'action': 'view'},
                        {'project': ['alpha'], 'action': 'view'}, {'project': 7}, {'project': ''},
                        {'project': 'alpha/../beta', 'action': 'view'}, {'project': 'ALPHA'}, {'project': 'alpha '},
                        {'project': '../beta'}, {'project': 'beta\x00alpha'}, [], 'alpha', None):
            with self.subTest(request=request):
                self.refused(request)

    def test_the_web_only_actions_are_refused_before_their_name_is_looked_at(self):
        for action in endpoint.SERVICE_ONLY_ACTIONS:
            for project in ('alpha', 'beta', 'Not A Name', None):
                with self.subTest(action=action, project=project):
                    self.refused({'project': project, 'actor': 'a', 'action': action},
                                 said='%s is available only to the web service' % action)

    def test_a_key_bound_to_nothing_reaches_nothing(self):
        self.refused({'project': 'alpha', 'actor': 'a', 'action': 'view'}, key_projects=frozenset())

    def test_a_project_of_the_key_is_served_as_for_an_unbound_caller(self):
        for bound in (frozenset({'alpha'}), frozenset({'beta', 'alpha'})):
            for request in ({'project': 'alpha', 'actor': 'alex/s1', 'action': 'view'},
                            {'project': 'alpha', 'actor': 'alex/s1', 'action': 'view', 'path': 'CURRENT.md'}):
                with self.subTest(bound=sorted(bound)):
                    self.assertEqual(endpoint.execute(self.root, request, key_projects=bound),
                                     endpoint.execute(self.root, request))
        self.assertEqual(endpoint.execute(self.root, {'project': 'alpha', 'actor': 'a', 'action': 'view'},
                                          key_projects=self.bound)['stdout'], 'the view of alpha\n')
        # And its own refusals are its own: an action that does not exist, a view outside the folder.
        for request, said in (({'project': 'alpha', 'actor': 'alex/s1', 'action': 'no-such-action'}, 'Unknown action'),
                              ({'project': 'alpha', 'actor': 'alex/s1', 'action': 'view',
                                'path': '../../beta/views/CURRENT.md'}, 'Invalid view path')):
            with self.assertRaises(ValueError) as refusal:
                endpoint.execute(self.root, request, key_projects=self.bound)
            self.assertEqual(str(refusal.exception), said)

    def test_an_unbound_caller_is_not_looked_at(self):
        self.assertIsNone(endpoint.key_project_refusal({'project': 'beta'}, None))
        self.assertIsNone(endpoint.key_project_refusal('anything at all', None))
        self.assertEqual(endpoint.execute(self.root, {'project': 'beta', 'actor': 'a', 'action': 'view'})['stdout'],
                         'the view of beta\n')


@POSIX
class LaunchedEndpointTests(unittest.TestCase):
    """The endpoint as a program, started as the forced command starts it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha', 'beta')

    def ask(self, request, *flags):
        done = subprocess.run([sys.executable, str(KIT/'endpoint.py'), '--root', str(self.root), *flags],
                              input=json.dumps(request), capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        return json.loads(done.stdout)

    def view(self, project):
        return {'project': project, 'actor': 'alex/s1', 'action': 'view'}

    def test_another_project_is_answered_as_a_project_that_does_not_exist(self):
        nothing = self.ask(self.view('gamma'))                                   # no key at all, no such project
        self.assertEqual(nothing, {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: %s\n' % UNKNOWN})
        self.assertEqual(self.ask(self.view('beta'), '--key-project', 'alpha'), nothing)
        self.assertEqual(self.ask(self.view('gamma'), '--key-project', 'alpha'), nothing)
        self.assertEqual(self.ask(self.view('beta'), '--key-project', 'alpha', '--key-project', 'gamma'), nothing)

    def test_its_own_projects_are_served(self):
        unbound = self.ask(self.view('alpha'))
        self.assertEqual(unbound['stdout'], 'the view of alpha\n')
        self.assertEqual(self.ask(self.view('alpha'), '--key-project', 'alpha'), unbound)
        both = ('--key-project', 'alpha', '--key-project', 'beta')
        self.assertEqual(self.ask(self.view('alpha'), *both), unbound)
        self.assertEqual(self.ask(self.view('beta'), *both)['stdout'], 'the view of beta\n')

    def test_a_flag_that_is_not_a_project_name_and_the_services_flags_are_refused(self):
        for flags, said in ((('--key-project', 'Alpha'), 'Project: 2-24 lowercase letters/digits'),
                            (('--key-project', '../beta'), 'Project: 2-24 lowercase letters/digits'),
                            (('--key-project', 'alpha', '--authority-store', str(self.root/'store.json')),
                             '--key-project is for an SSH key line and cannot be combined with --authority-store')):
            with self.subTest(flags=flags):
                answer = self.ask(self.view('alpha'), *flags)
                self.assertEqual((answer['returncode'], answer['stdout']), (2, ''))
                self.assertIn(said, answer['stderr'])

    def through_the_wrapper(self, request, *projects, command=None):
        """As sshd runs the line: the wrapper with its own arguments, the caller's words in the environment."""
        target = str(KIT/'endpoint.py')
        line = [sys.executable, str(KIT/'ssh_forced_command.py'), '--root', str(self.root), '--endpoint', target]
        for project in projects:
            line.extend(['--project', project])
        return subprocess.run(line, input=json.dumps(request), capture_output=True, text=True, timeout=120,
                              env=dict(os.environ, SSH_ORIGINAL_COMMAND=target if command is None else command))

    def test_through_the_forced_command(self):
        unbound = self.through_the_wrapper(self.view('beta'))
        self.assertEqual(json.loads(unbound.stdout)['stdout'], 'the view of beta\n')        # as today
        own = self.through_the_wrapper(self.view('alpha'), 'alpha')
        self.assertEqual(json.loads(own.stdout)['stdout'], 'the view of alpha\n')
        other = self.through_the_wrapper(self.view('beta'), 'alpha')
        self.assertEqual(json.loads(other.stdout), {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: %s\n' % UNKNOWN})
        target = str(KIT/'endpoint.py')
        # The caller's words select the endpoint and nothing else: they cannot add a project or drop the binding.
        for words in (target + ' --key-project beta', target + ' --project beta', '--project beta ' + target,
                      target + ' --root ' + str(self.root)):
            with self.subTest(words=words):
                refused = self.through_the_wrapper(self.view('beta'), 'alpha', command=words)
                self.assertEqual((refused.returncode, refused.stdout), (2, ''))
                self.assertIn('accepts no arguments', refused.stderr)


class WrapperProjectTests(unittest.TestCase):

    def test_an_unbound_line_starts_the_endpoint_exactly_as_before(self):
        self.assertEqual(forced.endpoint_argv('/usr/bin/python3', '/srv/kit/endpoint.py', '/srv/state'),
                         ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state'])
        self.assertEqual(forced.endpoint_argv('/usr/bin/python3', '/srv/kit/endpoint.py', '/srv/state', []),
                         ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state'])
        self.assertEqual(forced.parse_args(['--root', '/srv/state']).project, [])

    def test_a_bound_line_hands_its_projects_to_the_endpoint_in_order(self):
        args = forced.parse_args(['--root', '/srv/state', '--project', 'beta', '--endpoint', '/srv/kit/endpoint.py',
                                  '--project', 'alpha'])
        self.assertEqual(forced._projects(args.project), ['beta', 'alpha'])
        self.assertEqual(forced.endpoint_argv('/usr/bin/python3', '/srv/kit/endpoint.py', '/srv/state', ['beta', 'alpha']),
                         ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state',
                          '--key-project', 'beta', '--key-project', 'alpha'])

    def launched(self, *line, command='/srv/kit/endpoint.py'):
        """What ``main`` would exec, or the refusal it wrote."""
        said = io.StringIO()
        with mock.patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': command}), \
                mock.patch.object(forced.os, 'execvpe', side_effect=OSError('not run in a test')) as executed, \
                mock.patch.object(sys, 'stderr', said):
            status = forced.main(['--root', '/srv/state', '--endpoint', '/srv/kit/endpoint.py', '--python', '/usr/bin/python3',
                                  *line])
        return (executed.call_args.args[1] if executed.called else None), status, said.getvalue()

    def test_main_execs_the_bound_command(self):
        argv, _, _ = self.launched('--project', 'alpha', '--project', 'beta')
        self.assertEqual(argv, ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state',
                                '--key-project', 'alpha', '--key-project', 'beta'])
        argv, _, _ = self.launched()
        self.assertEqual(argv, ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state'])

    def test_a_project_that_is_not_a_name_stops_the_line_and_nothing_is_run(self):
        for value in ('Alpha', 'a', 'alpha beta', '../beta', '--key-project', 'alpha,beta', '', 'a' * 25, '1alpha'):
            with self.subTest(value=value):
                argv, status, said = self.launched('--project=' + value)
                self.assertEqual((argv, status), (None, 2))
                self.assertIn('--project must be a project name', said)
        argv, status, said = self.launched('--project', 'alpha', '--project', 'alpha')
        self.assertEqual((argv, status), (None, 2))
        self.assertIn('--project names alpha twice', said)

    def test_the_name_rule_is_the_kits(self):
        for name in ('ab', 'alpha', 'a1', 'a' * 24, 'A', 'a', 'alpha-beta', '1a', 'a' * 25, 'alpha\n'):
            kit = True
            try:
                admin.validate_name(name)
            except ValueError:
                kit = False
            self.assertEqual(bool(forced.PROJECT_NAME.fullmatch(name)), kit, name)


class PrintedLineTests(unittest.TestCase):

    def lines(self, **more):
        return admin.authorized_key_lines('/srv/state', '/srv/kit', 'ssh-ed25519', KEY_BODY, 'alex@laptop',
                                          python='/usr/bin/python3', **more)

    def test_without_projects_the_lines_are_what_they_were(self):
        self.assertEqual(self.lines()['contributor'],
                         'command="/usr/bin/python3 -E -s /srv/kit/ssh_forced_command.py --root /srv/state '
                         '--endpoint /srv/kit/endpoint.py",restrict,no-pty,no-port-forwarding,no-agent-forwarding,'
                         'no-X11-forwarding ssh-ed25519 %s alex@laptop' % KEY_BODY)
        self.assertEqual(self.lines(projects=()), self.lines())
        self.assertEqual(self.lines(projects=[]), self.lines())

    def test_the_bound_line_is_exact(self):
        bound = self.lines(projects=['alpha', 'beta'])
        self.assertEqual(bound['contributor'],
                         'command="/usr/bin/python3 -E -s /srv/kit/ssh_forced_command.py --root /srv/state '
                         '--endpoint /srv/kit/endpoint.py --project alpha --project beta",restrict,no-pty,'
                         'no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 %s alex@laptop '
                         'orchestra-projects=alpha,beta' % KEY_BODY)
        self.assertEqual(bound['operator'], 'ssh-ed25519 %s alex@laptop' % KEY_BODY)        # a shell is not bound

    def test_the_wrapper_reads_back_the_projects_the_line_was_printed_with(self):
        import shlex
        line = self.lines(projects=['alpha', 'beta'])['contributor']
        command = shlex.split(line[len('command="'):line.index('",restrict')])
        at = command.index('/srv/kit/ssh_forced_command.py')
        args = forced.parse_args(command[at + 1:])
        self.assertEqual((args.root, args.endpoint, forced._projects(args.project)),
                         ('/srv/state', ['/srv/kit/endpoint.py'], ['alpha', 'beta']))

    def test_only_projects_of_this_runtime_are_bound(self):
        with tempfile.TemporaryDirectory() as base:
            root = runtime(base, 'alpha', 'beta')
            (root/'projects'/'empty').mkdir()                                     # a folder, not a project
            self.assertEqual(admin.key_projects(root, ['beta', 'alpha']), ['beta', 'alpha'])
            self.assertEqual(admin.key_projects(root, None), [])
            self.assertEqual(admin.key_projects(root, []), [])
            for names, said in ((['gamma'], '--project gamma: no such project in this runtime'),
                                (['empty'], '--project empty: no such project in this runtime'),
                                (['alpha', 'alpha'], '--project names alpha twice'),
                                (['Alpha'], 'Project: 2-24 lowercase letters/digits'),
                                (['../alpha'], 'Project: 2-24 lowercase letters/digits')):
                with self.subTest(names=names), self.assertRaises(ValueError) as refused:
                    admin.key_projects(root, names)
                self.assertIn(said, str(refused.exception))


@unittest.skipUnless(os.name == 'posix', 'the printed line needs an absolute Linux path')
class PrintCommandTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha', 'beta')
        self.key = Path(self.tmp.name)/'key.pub'
        self.key.write_text('ssh-ed25519 %s alex@laptop\n' % KEY_BODY, encoding='utf-8')

    def printed(self, *more):
        said = io.StringIO()
        with mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'authorized-keys', '--key-file',
                                             str(self.key), '--python', '/usr/bin/python3', *more]), redirect_stdout(said):
            admin.main()
        return said.getvalue()

    def test_without_the_flag_nothing_of_the_output_changes(self):
        plain = json.loads(self.printed())
        self.assertNotIn('projects', plain)
        self.assertNotIn('--project', plain['contributor'])
        self.assertNotIn('orchestra-projects', plain['contributor'])
        self.assertIn('operator', plain)
        self.assertEqual(len(plain['notes']), 6)
        self.assertNotIn('bound', json.dumps(plain))

    def test_with_the_flag_the_bound_line_alone_is_printed(self):
        for role in ((), ('--role', 'contributor'), ('--role', 'both')):
            with self.subTest(role=role):
                bound = json.loads(self.printed('--project', 'alpha', '--project', 'beta', *role).split('\n}\n')[0] + '\n}')
                self.assertEqual(bound['projects'], ['alpha', 'beta'])
                self.assertIn(' --project alpha --project beta",restrict,', bound['contributor'])
                self.assertTrue(bound['contributor'].endswith(' alex@laptop orchestra-projects=alpha,beta'))
                self.assertNotIn('operator', bound)
                self.assertEqual(len(bound['notes']), 9)

    def test_what_cannot_be_bound_is_refused(self):
        for more, said in ((('--project', 'gamma'), 'no such project in this runtime'),
                           (('--project', 'alpha', '--role', 'operator'), 'cannot be bound to projects'),
                           (('--project', 'alpha', '--project', 'alpha'), 'twice')):
            with self.subTest(more=more), self.assertRaises(ValueError) as refused:
                self.printed(*more)
            self.assertIn(said, str(refused.exception))


def fingerprint(body):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(body)).digest()).decode().rstrip('=')


class ListingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(os.path.realpath(self.tmp.name))
        self.root = runtime(self.base, 'alpha', 'beta')
        self.kit = self.base/'kit'
        self.kit.mkdir()
        for name in ('ssh_forced_command.py', 'endpoint.py'):
            (self.kit/name).write_text('', encoding='utf-8')
        self.old = self.base/'old-kit'
        self.old.mkdir()
        for name in ('ssh_forced_command.py', 'endpoint.py'):
            (self.old/name).write_text('', encoding='utf-8')

    def line(self, kit=None, root=None, projects=(), body=KEY_BODY, comment='alex@laptop', more=''):
        kit = (kit or self.kit).as_posix()
        root = (root or self.root).as_posix()
        bound = ''.join(' --project ' + name for name in projects)
        return ('command="/usr/bin/python3 -E -s %s/ssh_forced_command.py --root %s --endpoint %s/endpoint.py%s%s",'
                'restrict,no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 %s %s'
                % (kit, root, kit, bound, more, body, comment))

    def one(self, text):
        return admin.key_line(text, self.root, self.kit)

    def test_a_blank_line_and_a_comment_are_no_entries(self):
        for text in ('', '   ', '# command="x" ssh-ed25519 AAAA', '\t# note'):
            self.assertIsNone(self.one(text))

    def test_a_bound_line(self):
        entry = self.one(self.line(projects=['alpha', 'beta'], comment='alex@laptop orchestra-projects=alpha,beta'))
        self.assertEqual(entry, {
            'key_type': 'ssh-ed25519', 'fingerprint': fingerprint(KEY_BODY), 'comment': 'alex@laptop orchestra-projects=alpha,beta',
            'kind': 'bound', 'projects': ['alpha', 'beta'], 'principal': None, 'root': self.root.as_posix(),
            'wrapper': self.kit.as_posix() + '/ssh_forced_command.py', 'endpoints': [self.kit.as_posix() + '/endpoint.py'],
            'other_kit': False, 'other_root': False, 'missing': False, 'unknown_arguments': [], 'names_release': False,
            'unknown_projects': []})

    def test_the_binding_is_the_arguments_and_never_the_comment(self):
        entry = self.one(self.line(comment='alex@laptop orchestra-projects=alpha'))
        self.assertEqual((entry['kind'], entry['projects']), ('confined', []))

    def test_a_confined_line_an_unrestricted_one_and_another_command(self):
        self.assertEqual(self.one(self.line())['kind'], 'confined')
        plain = self.one('ssh-ed25519 %s james@desk' % KEY_BODY)
        self.assertEqual(plain, {'key_type': 'ssh-ed25519', 'fingerprint': fingerprint(KEY_BODY), 'comment': 'james@desk',
                                 'kind': 'unrestricted'})
        # Options that do not confine: still the account's shell.
        self.assertEqual(self.one('no-pty,from="10.0.0.0/8,192.168.1.1" ssh-rsa %s ops' % KEY_BODY)['kind'], 'unrestricted')
        self.assertEqual(self.one('environment="A=command=x" ssh-rsa %s ops' % KEY_BODY)['kind'], 'unrestricted')
        other = self.one('command="/usr/bin/rsync --server --sender . /srv/backups",restrict ssh-ed25519 %s backup' % KEY_BODY)
        self.assertEqual((other['kind'], other['comment']), ('other-command', 'backup'))
        self.assertNotIn('projects', other)
        quoted = self.one('command="sh -c \\"echo ssh_forced_command.py is not run here\\"" ssh-ed25519 %s odd' % KEY_BODY)
        self.assertEqual(quoted['kind'], 'other-command')

    def test_a_line_that_cannot_be_read_says_so(self):
        for text in ('command="unclosed ssh-ed25519 %s x' % KEY_BODY, 'ssh-ed25519', 'ssh-ed25519 not-base64!! x',
                     'restrict nonsense %s x' % KEY_BODY, 'ssh-ed25519  ', 'no-pty'):
            with self.subTest(text=text):
                entry = self.one(text)
                self.assertEqual(entry['kind'], 'unreadable')
                self.assertTrue(entry['reason'])

    def test_a_line_that_points_at_another_kit_is_flagged_whatever_its_text_says(self):
        """An earlier release's wrapper binds nothing: the design's Migration, step 6."""
        entry = self.one(self.line(kit=self.old, projects=['alpha']))
        self.assertEqual((entry['kind'], entry['other_kit'], entry['missing']), ('bound', True, False))
        mixed = self.one(self.line(projects=['alpha']).replace('--endpoint %s' % self.kit.as_posix(),
                                                               '--endpoint %s' % self.old.as_posix()))
        self.assertTrue(mixed['other_kit'])                                     # this wrapper, the other kit's endpoint
        gone = self.one(self.line(kit=self.base/'removed', projects=['alpha']))
        self.assertEqual((gone['other_kit'], gone['missing']), (True, True))

    def test_a_line_for_another_runtime_and_unknown_things(self):
        other = self.one(self.line(root=self.base/'elsewhere', projects=['alpha']))
        self.assertTrue(other['other_root'])
        self.assertNotIn('unknown_projects', other)                             # not looked up in a runtime it does not serve
        entry = self.one(self.line(projects=['alpha', 'gamma', 'Not-A-Name']))
        self.assertEqual(entry['unknown_projects'], ['gamma', 'Not-A-Name'])
        entry = self.one(self.line(more=' --authority-store /x --principal person:alex'))
        self.assertEqual((entry['unknown_arguments'], entry['principal']), (['--authority-store', '/x'], 'person:alex'))
        joined = self.one(self.line().replace('--root ', '--root=') + '')
        self.assertFalse(joined['other_root'])

    @unittest.skipUnless(hasattr(os, 'symlink') and os.name == 'posix', 'the installation layout needs real symlinks')
    def test_an_office_installation(self):
        install = self.base/'install'
        for release in ('r1', 'r2'):
            (install/'releases'/release/'kit').mkdir(parents=True)
            for name in ('ssh_forced_command.py', 'endpoint.py'):
                (install/'releases'/release/'kit'/name).write_text('', encoding='utf-8')
        (install/'current').symlink_to('releases/r2')
        here = install/'releases'/'r2'/'kit'
        through = admin.key_line(self.line(kit=install/'current'/'kit', projects=['alpha']), self.root, here)
        self.assertEqual((through['other_kit'], through['names_release']), (False, False))
        pinned = admin.key_line(self.line(kit=here, projects=['alpha']), self.root, here)
        self.assertEqual((pinned['other_kit'], pinned['names_release']), (False, True))      # this kit today, not after an upgrade
        before = admin.key_line(self.line(kit=install/'releases'/'r1'/'kit', projects=['alpha']), self.root, here)
        self.assertEqual((before['other_kit'], before['names_release']), (True, False))      # the release before: it binds nothing

    def test_the_listing_reads_the_file_and_writes_nothing(self):
        file = self.base/'authorized_keys'
        file.write_text('\n'.join([
            '# the installation\'s operator', 'ssh-ed25519 %s james@desk' % KEY_BODY, '',
            self.line(projects=['alpha'], body=OTHER_BODY, comment='worker orchestra-projects=alpha'),
            self.line(kit=self.old, body=OTHER_BODY, comment='from the release before'),
            'command="/usr/bin/rsync --server" ssh-ed25519 %s backup' % KEY_BODY, 'garbage here']) + '\n', encoding='utf-8')
        before = tree(self.base)
        with mock.patch.object(admin, '__file__', str(self.kit/'admin.py')):
            listing = admin.authorized_keys_listing(self.root, str(file))
        self.assertEqual(tree(self.base), before)
        self.assertEqual([(entry['line'], entry['kind']) for entry in listing['lines']],
                         [(2, 'unrestricted'), (4, 'bound'), (5, 'confined'), (6, 'other-command'), (7, 'unreadable')])
        self.assertEqual(listing['summary'], {'unrestricted': 1, 'bound': 1, 'confined': 1, 'other-command': 1, 'unreadable': 1})
        self.assertEqual(listing['attention'], [5, 7])                          # the other kit's line, and the one nobody can read
        self.assertEqual((listing['file'], listing['root'], listing['kit']), (str(file), str(self.root), str(self.kit)))
        self.assertIn('This command reads the file and changes nothing.', listing['notes'])
        with self.assertRaises(ValueError) as missing:
            admin.authorized_keys_listing(self.root, str(self.base/'none'))
        self.assertIn('Cannot read', str(missing.exception))

    def test_the_command_prints_the_listing(self):
        file = self.base/'authorized_keys'
        file.write_text('ssh-ed25519 %s james@desk\n' % KEY_BODY, encoding='utf-8')
        said = io.StringIO()
        with mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'authorized-keys-list', '--file', str(file)]), \
                mock.patch.object(admin, 'root_path', Path), redirect_stdout(said):
            admin.main()
        self.assertEqual(json.loads(said.getvalue())['summary'], {'unrestricted': 1})


if __name__ == '__main__':
    unittest.main()
