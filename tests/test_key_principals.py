"""An SSH key names its principal; actors belong to principals (kittrial-5bb.194).

Rule 2 of docs/COORDINATORS_PER_PROJECT_DESIGN.md. The forced command of an authorized_keys
line may carry ``--principal NAME``; the endpoint is then started with ``--key-principal
NAME`` and refuses every request whose actor the project's session registry does not give to
that principal. A session registration is the exception: it makes the new actor that
principal's. The registry gains an ``owners`` map (actor -> principal); the host command
``admin.py adopt-actor`` gives an existing actor to a principal and records it in an audit.

The test that matters is ``test_a_bound_key_acts_only_as_its_principals_actors``: an actor
another principal owns, and an actor with no principal at all, are both refused before any
action runs, and ``test_a_registration_under_a_bound_key_is_that_principals``.

An installation that configures nothing must behave exactly as today: with no ``--principal``
no owner is read, and a registry written without one carries no ``owners`` key, so an older
kit still reads it (the downgrade limit).
"""
import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import admin
import sessions
import ssh_forced_command as forced

try:
    import endpoint
except ImportError:                                   # fcntl: the endpoint is POSIX only
    endpoint = None

POSIX = unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
KEY_BODY = base64.b64encode(b'orchestra-principal-synthetic-key').decode()
MINE = 'lane:lane-one'
OTHER = 'lane:lane-two'
PERSON = 'person:lane-one'                            # a different principal from MINE, same name
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


def record(name='worker'):
    """One session record, as sessions.execute writes it."""
    request_id = str(uuid.uuid4())
    return {'request_id': request_id, 'actor': 'session-'+str(uuid.uuid4()), 'name': name,
            'created_at': '2026-01-01T00:00:00+00:00'}


def write_registry(path, records=(), owners=None, damaged=None):
    """Write a project registry; ``damaged`` writes raw bytes instead."""
    file = Path(path)/'.sessions.json'
    if damaged is not None:
        file.write_text(damaged, encoding='utf-8')
        return None
    data = {'schema_version': 1, 'records': {r['request_id']: r for r in records}}
    if owners is not None:
        data['owners'] = owners
    file.write_text(json.dumps(data), encoding='utf-8')
    return data


def deployment(root, operators_list=('ops',)):
    (root/'deployment.private.json').write_text(json.dumps({'operators': list(operators_list), 'password': 'x'}),
                                                encoding='utf-8')


def tree(folder):
    # The deployment lock (.review-writes.lock) and the project coordination lock hold no
    # state and are never backed up; taking a lock creates an empty file, so a "nothing was
    # written" comparison ignores them (adopt-actor takes the deployment lock, kittrial-5bb.223
    # finding 1; endpoint tests exclude .coordination.lock the same way).
    return {str(path.relative_to(folder)): (path.read_bytes() if path.is_file() else None)
            for path in sorted(Path(folder).rglob('*'))
            if path.name not in ('.review-writes.lock', '.coordination.lock')}


def actions_of_the_endpoint():
    """Every action name ``endpoint.execute`` compares the request's action with, read from its
    source, exactly as tests/test_key_projects.py does for rule 1. An action added to
    ``execute`` above the principal gate is then exercised by the test below by itself
    (kittrial-5bb.194 review, mutant N1)."""
    source = (KIT/'endpoint.py').read_text(encoding='utf-8')
    body = source[source.index('\ndef execute('):source.index('\ndef main(')]
    names = set(re.findall(r"action'?\)?\s*[!=]=\s*'([a-z-]+)'", body))
    for group in re.findall(r"action in \(([^)]*)\)", body):
        names.update(re.findall(r"'([a-z-]+)'", group))
    return names


@POSIX
class BoundPrincipalEndpointTests(unittest.TestCase):
    """endpoint.execute, with a key bound to a principal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(self.tmp.name, 'alpha', 'beta')
        self.mine = record('mine')
        self.other = record('other')
        write_registry(self.root/'projects'/'alpha', [self.mine, self.other],
                       owners={self.mine['actor']: MINE, self.other['actor']: OTHER})

    def refused(self, request, said):
        reached = AssertionError('the request reached a program or a lock')
        with mock.patch.object(subprocess, 'run', side_effect=reached), \
                mock.patch.object(endpoint.native, 'run', side_effect=reached), \
                mock.patch.object(endpoint.fcntl, 'flock', side_effect=reached):
            with self.assertRaises(ValueError) as refusal:
                endpoint.execute(self.root, request, key_principal=MINE)
        self.assertEqual(str(refusal.exception), said)

    def view(self, project='alpha', actor=None):
        return {'project': project, 'actor': self.mine['actor'] if actor is None else actor, 'action': 'view'}

    def sentence(self, actor):
        return ('This key is bound to principal %s and may act only as actors that principal '
                'registered in this project; %s is not one of them'
                % (MINE, actor if isinstance(actor, str) and actor else repr(actor)))

    def test_a_bound_key_acts_only_as_its_principals_actors(self):
        # Its own actor is served, exactly as an unbound caller.
        self.assertEqual(endpoint.execute(self.root, self.view(), key_principal=MINE),
                         endpoint.execute(self.root, self.view()))
        # Another principal's actor, and an actor with no owner at all, are refused before
        # anything is read or run.
        for actor in (self.other['actor'], 'session-'+str(uuid.uuid4()), 'legacy-name', ''):
            with self.subTest(actor=actor):
                self.refused(self.view(actor=actor), self.sentence(actor))

    def test_a_non_text_actor_is_refused(self):
        # E5: the gate itself refuses an actor that is not text, before any later shape check.
        for actor in (None, 7, True, ['session-'+str(uuid.uuid4())], {'actor': 'x'}):
            with self.subTest(actor=actor):
                self.refused({'project': 'alpha', 'actor': actor, 'action': 'view'}, self.sentence(actor))

    def test_an_actor_whose_case_differs_is_a_different_actor(self):
        # E11: names are compared exactly; 'Session-...' is nobody's.
        upper = self.mine['actor'][0].upper()+self.mine['actor'][1:]
        self.assertEqual(upper, 'S'+self.mine['actor'][1:])
        self.refused(self.view(actor=upper), self.sentence(upper))

    def test_every_session_operation_is_gated_not_only_register(self):
        # E3 and E13: resume, run and show are served only for the key's own actor; nothing is
        # written for another actor and the registry bytes are unchanged.
        registry = self.root/'projects'/'alpha'/'.sessions.json'
        before = registry.read_bytes()
        cases = [['resume', '--request-id', str(uuid.uuid4())],
                 ['run', 'start', '--run-id', str(uuid.uuid4()), '--event-id', str(uuid.uuid4()), '--task', 'x'],
                 ['run', 'status', '--run-id', str(uuid.uuid4())],
                 ['run', 'heartbeat', '--run-id', str(uuid.uuid4()), '--event-id', str(uuid.uuid4())],
                 ['show', 'legacy-name']]
        for actor in (self.other['actor'], 'legacy-name'):
            for args in cases:
                with self.subTest(actor=actor, args=args[:2]):
                    self.refused({'project': 'alpha', 'actor': actor, 'action': 'session', 'args': args},
                                 self.sentence(actor))
        self.assertEqual(registry.read_bytes(), before)

    def test_only_the_first_argument_register_is_exempt(self):
        # E4: 'register' anywhere but the first argument is not the exemption.
        for args in (['run', 'start', '--run-id', str(uuid.uuid4()), '--event-id', str(uuid.uuid4()),
                      '--task', 'register'],
                     ['show', 'register'],
                     ['register-extra', '--name', 'x']):
            with self.subTest(args=args[:2]):
                actor = self.other['actor']
                self.refused({'project': 'alpha', 'actor': actor, 'action': 'session', 'args': args},
                             self.sentence(actor))

    def test_every_action_the_endpoint_answers_is_gated(self):
        # N1: every action name read out of ``execute``'s own source is refused for an actor
        # this key does not own, before it is answered; an action added above the gate fails
        # here. The web-only actions name no project, so the gate refuses them structurally.
        actions = actions_of_the_endpoint()
        self.assertIn('session', actions)
        self.assertIn('view', actions)
        reached = AssertionError('the request reached a program or a lock')
        for action in sorted(actions)+['no-such-action']:
            with self.subTest(action=action):
                request = {'project': 'alpha', 'actor': 'legacy-name', 'action': action,
                           'args': ['list'], 'payload': {}, 'attachments': {}, 'path': 'CURRENT.md'}
                with mock.patch.object(subprocess, 'run', side_effect=reached), \
                        mock.patch.object(endpoint.native, 'run', side_effect=reached), \
                        mock.patch.object(endpoint.fcntl, 'flock', side_effect=reached):
                    with self.assertRaises(ValueError):
                        endpoint.execute(self.root, request, key_principal=MINE)

    def test_a_bound_key_is_refused_the_web_only_actions_structurally(self):
        # G1: for a key bound to a principal, the three web-only actions are refused above the
        # gate, before the project or the action handler is reached. The handlers refuse a
        # non-web caller too, so they are made to fail loudly here: only the structural lines
        # can answer with this exact sentence.
        reached = AssertionError('the request reached the action or its project')
        import project_creation
        with mock.patch.object(endpoint, 'project_dir', side_effect=reached), \
                mock.patch.object(project_creation, 'create_action', side_effect=reached), \
                mock.patch.object(project_creation, 'list_action', side_effect=reached), \
                mock.patch.object(project_creation, 'standing_action', side_effect=reached):
            for action in endpoint.SERVICE_ONLY_ACTIONS:
                with self.subTest(action=action), self.assertRaises(ValueError) as refusal:
                    endpoint.execute(self.root, {'project': 'alpha', 'actor': self.mine['actor'],
                                                 'action': action}, key_principal=MINE)
                self.assertEqual(str(refusal.exception),
                                 '%s is available only to the web service' % action)

    def test_a_replayed_registration_leaves_the_owner_as_it_was(self):
        # S3: replaying somebody's registration under a bound key reconciles; it never takes
        # the actor over and never writes the registry.
        registry = self.root/'projects'/'alpha'/'.sessions.json'
        before = registry.read_bytes()
        request = {'project': 'alpha', 'actor': '', 'action': 'session',
                   'args': ['register', '--name', self.other['name'], '--request-id', self.other['request_id']]}
        with mock.patch.object(endpoint.fcntl, 'flock'):
            answer = endpoint.execute(self.root, request, key_principal=MINE)
        result = json.loads(answer['stdout'])
        self.assertTrue(result['reconciled'])
        self.assertEqual(result['principal'], OTHER)
        self.assertEqual(sessions.owners(self.root/'projects'/'alpha')[self.other['actor']], OTHER)
        self.assertEqual(registry.read_bytes(), before)

    def test_a_registration_under_a_bound_key_takes_the_allocated_actor(self):
        # S6: the new actor is recorded under the key's principal even if its name already
        # carried a stale owner. The public path cannot collide, so the uuid is pinned.
        fixed = uuid.uuid4()
        actor = 'session-'+str(fixed)
        write_registry(self.root/'projects'/'alpha', [self.mine],
                       owners={self.mine['actor']: MINE, actor: OTHER})
        empty = subprocess.CompletedProcess([], 0, '', '')
        request = {'project': 'alpha', 'actor': '', 'action': 'session',
                   'args': ['register', '--name', 'new-worker', '--request-id', str(uuid.uuid4())]}
        with mock.patch.object(sessions.uuid, 'uuid4', return_value=fixed), \
                mock.patch.object(endpoint.native, 'run', return_value=empty), \
                mock.patch.object(endpoint.fcntl, 'flock'), \
                mock.patch.object(endpoint, 'environment', return_value={}):
            answer = endpoint.execute(self.root, request, key_principal=MINE)
        result = json.loads(answer['stdout'])
        self.assertEqual(result['session']['actor'], actor)
        self.assertEqual(sessions.owners(self.root/'projects'/'alpha')[actor], MINE)

    def test_an_unbound_caller_is_not_looked_at_even_with_an_owners_map(self):
        # No principal named: the owners map exists but is not consulted (today's behaviour).
        self.assertEqual(endpoint.execute(self.root, self.view(actor='legacy-name'))['stdout'],
                         'the view of alpha\n')
        self.assertEqual(endpoint.execute(self.root, self.view(actor=self.other['actor']))['stdout'],
                         'the view of alpha\n')

    def test_the_exception_is_a_session_registration_which_records_the_new_actor(self):
        request = {'project': 'alpha', 'actor': '', 'action': 'session',
                   'args': ['register', '--name', 'new-worker', '--request-id', str(uuid.uuid4())]}
        empty = subprocess.CompletedProcess([], 0, '', '')
        with mock.patch.object(endpoint.native, 'run', return_value=empty), \
                mock.patch.object(endpoint, 'environment', return_value={}):
            answer = endpoint.execute(self.root, request, key_principal=MINE)
        self.assertEqual(answer['returncode'], 0)
        result = json.loads(answer['stdout'])
        self.assertEqual(result['principal'], MINE)
        self.assertEqual(sessions.owners(self.root/'projects'/'alpha')[result['session']['actor']], MINE)
        # And from then on the new actor is served under the same key.
        self.assertEqual(endpoint.execute(self.root, self.view(actor=result['session']['actor']),
                                          key_principal=MINE)['stdout'], 'the view of alpha\n')

    def test_session_show_reports_the_principal(self):
        answer = endpoint.execute(self.root, {'project': 'alpha', 'actor': self.mine['actor'], 'action': 'session',
                                              'args': ['show', self.mine['actor']]}, key_principal=MINE)
        self.assertEqual(json.loads(answer['stdout'])['principal'], MINE)

    def test_a_registry_that_cannot_be_read_is_a_refusal_not_an_unowned_actor(self):
        write_registry(self.root/'projects'/'alpha', damaged=json.dumps({'schema_version': 1, 'records': []}))
        with self.assertRaises(ValueError) as refusal:
            endpoint.execute(self.root, self.view(), key_principal=MINE)
        self.assertEqual(str(refusal.exception), 'Invalid session registry')
        # A registry that is not JSON at all (or is unreadable) is one plain sentence, never
        # a bare JSONDecodeError or a PermissionError with a path (review, item 6).
        write_registry(self.root/'projects'/'alpha', damaged='{not json')
        with self.assertRaises(ValueError) as refusal:
            endpoint.execute(self.root, self.view(), key_principal=MINE)
        self.assertEqual(str(refusal.exception),
                         'The session registry cannot be read: it is damaged, unreadable or not JSON; '
                         'nothing was changed')
        self.assertNotIn('JSONDecodeError', str(refusal.exception))
        self.assertNotIn('.sessions.json', str(refusal.exception))

    def test_a_principal_without_projects_may_name_another_project_but_only_its_actors(self):
        # A principal without --project is not rule 1: another project is not refused as
        # unknown. But it still acts only as this principal's actors, and beta has none.
        self.refused({'project': 'beta', 'actor': self.mine['actor'], 'action': 'view'},
                     self.sentence(self.mine['actor']))
        write_registry(self.root/'projects'/'beta', [], owners={self.mine['actor']: MINE})
        self.assertEqual(endpoint.execute(self.root, {'project': 'beta', 'actor': self.mine['actor'],
                                                      'action': 'view'}, key_principal=MINE)['stdout'],
                         'the view of beta\n')


@POSIX
class LaunchedPrincipalEndpointTests(unittest.TestCase):
    """The endpoint as a program, and through the forced command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha', 'beta')
        self.mine = record('mine')
        write_registry(self.root/'projects'/'alpha', [self.mine], owners={self.mine['actor']: MINE})

    def ask(self, request, *flags):
        done = subprocess.run([sys.executable, str(KIT/'endpoint.py'), '--root', str(self.root), *flags],
                              input=json.dumps(request), capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        return json.loads(done.stdout)

    def view(self, actor):
        return {'project': 'alpha', 'actor': actor, 'action': 'view'}

    def test_a_bound_key_is_served_as_its_actor_and_refused_as_another(self):
        self.assertEqual(self.ask(self.view(self.mine['actor']), '--key-principal', MINE)['stdout'],
                         'the view of alpha\n')
        refused = self.ask(self.view('legacy-name'), '--key-principal', MINE)
        self.assertEqual((refused['returncode'], refused['stdout']), (2, ''))
        self.assertIn('may act only as actors that principal registered', refused['stderr'])

    def test_the_principal_flags_are_refused_with_the_service_or_out_of_shape(self):
        for flags, said in ((('--key-principal', 'lane-one'), 'must be a principal name of the form lane:NAME or person:NAME'),
                            (('--key-principal', 'person:' + 'a'*100), 'must be a principal name'),
                            (('--key-principal', 'person:two words'), 'must be a principal name'),
                            (('--key-principal', 'lane:two words'), 'must be a principal name'),
                            (('--key-principal', MINE, '--authority-store', str(self.root/'store.json')),
                             '--key-principal is for an SSH key line and cannot be combined with --authority-store')):
            with self.subTest(flags=flags):
                answer = self.ask(self.view(self.mine['actor']), *flags)
                self.assertEqual((answer['returncode'], answer['stdout']), (2, ''))
                self.assertIn(said, answer['stderr'])

    def test_the_endpoint_refuses_a_repeated_or_abbreviated_key_principal(self):
        # Finding 7: only the wrapper passes --key-principal, but the endpoint's own flag must
        # not take the last of two, and must not accept an abbreviation.
        repeated = self.ask(self.view(self.mine['actor']), '--key-principal', MINE, '--key-principal', OTHER)
        self.assertEqual((repeated['returncode'], repeated['stdout']), (2, ''))
        self.assertIn('--key-principal names', repeated['stderr'])
        done = subprocess.run([sys.executable, str(KIT/'endpoint.py'), '--root', str(self.root),
                               '--key-princ', MINE],
                              input=json.dumps(self.view(self.mine['actor'])), capture_output=True, text=True,
                              timeout=120)
        self.assertEqual((done.returncode, done.stdout), (2, ''))
        self.assertIn('unrecognized arguments', done.stderr)

    def test_lane_and_person_are_both_accepted_and_are_different_principals(self):
        # The coordinator decision: a lane is spelled lane:NAME as well as person:NAME, the two
        # prefixes are different principals, and nothing reads the proposal settings' person: map.
        self.assertEqual(sessions.valid_principal('lane:lane-one'), 'lane:lane-one')
        self.assertEqual(sessions.valid_principal('person:lane-one'), 'person:lane-one')
        self.assertNotEqual(PERSON, MINE)
        write_registry(self.root/'projects'/'alpha', [self.mine], owners={self.mine['actor']: PERSON})
        refused = self.ask(self.view(self.mine['actor']), '--key-principal', MINE)
        self.assertEqual((refused['returncode'], refused['stdout']), (2, ''))
        self.assertIn('may act only as actors that principal registered', refused['stderr'])
        self.assertEqual(self.ask(self.view(self.mine['actor']), '--key-principal', PERSON)['stdout'],
                         'the view of alpha\n')

    def through_the_wrapper(self, request, *args, command=None):
        target = str(KIT/'endpoint.py')
        line = [sys.executable, str(KIT/'ssh_forced_command.py'), '--root', str(self.root), '--endpoint', target]
        line.extend(args)
        return subprocess.run(line, input=json.dumps(request), capture_output=True, text=True, timeout=120,
                              env=dict(os.environ, SSH_ORIGINAL_COMMAND=target if command is None else command))

    def test_through_the_forced_command(self):
        own = self.through_the_wrapper(self.view(self.mine['actor']), '--principal', MINE)
        self.assertEqual(json.loads(own.stdout)['stdout'], 'the view of alpha\n')
        other = self.through_the_wrapper(self.view('legacy-name'), '--principal', MINE)
        self.assertEqual(json.loads(other.stdout)['returncode'], 2)
        self.assertIn('may act only as actors', json.loads(other.stdout)['stderr'])
        # The caller's words cannot add or drop the principal.
        target = str(KIT/'endpoint.py')
        for words in (target + ' --key-principal ' + OTHER, target + ' --principal ' + OTHER):
            with self.subTest(words=words):
                refused = self.through_the_wrapper(self.view(self.mine['actor']), '--principal', MINE, command=words)
                self.assertEqual((refused.returncode, refused.stdout), (2, ''))
                self.assertIn('accepts no arguments', refused.stderr)


class WrapperPrincipalTests(unittest.TestCase):

    def test_an_unbound_line_starts_the_endpoint_exactly_as_before(self):
        self.assertEqual(forced.endpoint_argv('/usr/bin/python3', '/srv/kit/endpoint.py', '/srv/state'),
                         ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state'])
        self.assertEqual(forced.endpoint_argv('/usr/bin/python3', '/srv/kit/endpoint.py', '/srv/state', [], None),
                         ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state'])
        self.assertIsNone(forced.parse_args(['--root', '/srv/state']).principal)
        self.assertIsNone(forced._principal(None))

    def test_a_bound_line_hands_its_principal_and_projects_to_the_endpoint_in_order(self):
        args = forced.parse_args(['--root', '/srv/state', '--principal', MINE, '--project', 'beta',
                                  '--endpoint', '/srv/kit/endpoint.py', '--project', 'alpha'])
        self.assertEqual(forced._principal(args.principal), MINE)
        self.assertEqual(forced._projects(args.project), ['beta', 'alpha'])
        self.assertEqual(forced.endpoint_argv('/usr/bin/python3', '/srv/kit/endpoint.py', '/srv/state',
                                              ['beta', 'alpha'], MINE),
                         ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state',
                          '--key-project', 'beta', '--key-project', 'alpha', '--key-principal', MINE])

    def launched(self, *line, command='/srv/kit/endpoint.py'):
        said = io.StringIO()
        with mock.patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': command}), \
                mock.patch.object(forced.os, 'execvpe', side_effect=OSError('not run in a test')) as executed, \
                mock.patch.object(sys, 'stderr', said):
            status = forced.main(['--root', '/srv/state', '--endpoint', '/srv/kit/endpoint.py',
                                  '--python', '/usr/bin/python3', *line])
        return (executed.call_args.args[1] if executed.called else None), status, said.getvalue()

    def test_main_execs_the_bound_command(self):
        argv, _, _ = self.launched('--principal', MINE, '--project', 'alpha')
        self.assertEqual(argv, ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state',
                                '--key-project', 'alpha', '--key-principal', MINE])
        argv, _, _ = self.launched('--principal', MINE)
        self.assertEqual(argv, ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state',
                                '--key-principal', MINE])

    def test_a_principal_that_is_not_a_name_stops_the_line_and_nothing_is_run(self):
        for value in ('lane-one', 'person:', 'person:two words', 'person:'+'a'*100, 'Person:lane', 'person:lane\n',
                      'lane:', 'lane:two words', 'lane:'+'a'*100):
            with self.subTest(value=value):
                argv, status, said = self.launched('--principal', value)
                self.assertEqual((argv, status), (None, 2))
                self.assertIn('--principal must be a principal name', said)

    def test_the_wrapper_refuses_a_repeated_principal(self):
        # Finding 3: a line with --principal twice is served as the last one and listed as the
        # first; the wrapper refuses it, as it refuses a project named twice.
        argv, status, said = self.launched('--principal', MINE, '--principal', OTHER)
        self.assertEqual((argv, status), (None, 2))
        self.assertIn('--principal names', said)
        self.assertIn('twice', said)
        self.assertIn(MINE, said)
        self.assertIn(OTHER, said)
        args = forced.parse_args(['--root', '/srv/state', '--principal', MINE, '--principal', OTHER])
        with self.assertRaises(ValueError) as refusal:
            forced._principal(args.principal)
        self.assertIn('twice', str(refusal.exception))

    def test_the_wrapper_binds_nothing_from_the_environment(self):
        # W4: a principal in the environment (ORCHESTRA_PRINCIPAL or any other name) with no
        # --principal on the line binds nothing: the line's own arguments are the only input.
        for name in ('ORCHESTRA_PRINCIPAL', 'ORCHESTRA_KEY_PRINCIPAL', 'KEY_PRINCIPAL', 'LC_PRINCIPAL'):
            with self.subTest(name=name):
                said = io.StringIO()
                with mock.patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': '/srv/kit/endpoint.py', name: OTHER}), \
                        mock.patch.object(forced.os, 'execvpe', side_effect=OSError('not run in a test')) as executed, \
                        mock.patch.object(sys, 'stderr', said):
                    status = forced.main(['--root', '/srv/state', '--endpoint', '/srv/kit/endpoint.py',
                                          '--python', '/usr/bin/python3'])
                argv = executed.call_args.args[1] if executed.called else None
                self.assertEqual(argv, ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state'])
                self.assertNotIn('--key-principal', argv)
                self.assertEqual(status, 2)                 # execvpe refused in the test, not the wrapper

    def test_the_name_rule_is_the_kits(self):
        for name in ('person:a', 'person:lane.one', 'person:lane-one', 'person:lane_one',
                     'person:' + 'a'*95, 'lane:a', 'lane:lane.one', 'lane:lane-one', 'lane:lane_one',
                     'lane:' + 'a'*95, 'person:lane/one', 'lane:lane/one',
                     'lane', 'person:', 'lane:', 'person:two words', 'lane:two words',
                     'person:' + 'a'*96, 'lane:' + 'a'*96, 'Person:lane', 'Lane:lane', 'person:Lane-P'):
            kit = True
            try:
                sessions.valid_principal(name)
            except ValueError:
                kit = False
            self.assertEqual(bool(forced.PRINCIPAL_NAME.fullmatch(name)), kit, name)
        # The two prefixes are different principals even under the same name.
        self.assertNotEqual(MINE, PERSON)
        sessions.valid_principal(MINE)
        sessions.valid_principal(PERSON)


class RegistryOwnerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.export = mock.Mock(return_value='')

    def register(self, name='worker', principal=None):
        return sessions.execute(self.path, 'example',
                                ['register', '--name', name, '--request-id', str(uuid.uuid4())],
                                self.export, principal=principal)

    def test_an_unconfigured_installation_writes_no_owners_map(self):
        made = self.register()
        # No owners map at all: the answer is exactly the one this kit gave before rule 2,
        # with no `principal` key (review item 5c).
        self.assertNotIn('principal', made)
        data = json.loads((self.path/'.sessions.json').read_text())
        self.assertNotIn('owners', data)
        # The keys an older kit's validator allows are exactly these, so it still reads it.
        self.assertEqual(set(data) - {'schema_version', 'records', 'resumes', 'runs'}, set())

    def test_a_registration_under_a_principal_writes_the_owner_map(self):
        made = self.register(principal=MINE)
        self.assertEqual(made['principal'], MINE)
        data = json.loads((self.path/'.sessions.json').read_text())
        self.assertEqual(data['owners'], {made['session']['actor']: MINE})
        # And a later registration keeps it.
        second = self.register(name='second', principal=MINE)
        self.assertEqual(sessions.owners(self.path)[second['session']['actor']], MINE)
        self.assertEqual(len(sessions.owners(self.path)), 2)

    def test_session_show_reports_the_principal_and_an_unowned_actor_none(self):
        owned = self.register(principal=MINE)['session']
        write_registry(self.path, [owned], owners={owned['actor']: MINE})
        shown = sessions.execute(self.path, 'example', ['show', owned['actor']], self.export)
        self.assertEqual(shown['principal'], MINE)
        self.assertEqual(shown['session'], owned)
        # A registry with an owners map but no entry for this actor: `principal` is null. A
        # registry with no map at all omits the key (item 5c).
        other = record('other')['actor']
        write_registry(self.path, [{'request_id': str(uuid.uuid4()), 'actor': other, 'name': 'other',
                                    'created_at': '2026-01-01T00:00:00+00:00'}], owners={owned['actor']: MINE})
        self.assertIsNone(sessions.execute(self.path, 'example', ['show', other], self.export)['principal'])
        write_registry(self.path, [{'request_id': str(uuid.uuid4()), 'actor': other, 'name': 'other',
                                    'created_at': '2026-01-01T00:00:00+00:00'}])
        self.assertNotIn('principal', sessions.execute(self.path, 'example', ['show', other], self.export))

    def test_the_owner_map_is_validated(self):
        actor = record()['actor']
        for owners in ({actor: 'lane-one'}, {actor: 'person:'}, {'bad name': MINE}, {actor: 7}):
            with self.subTest(owners=owners):
                with self.assertRaises(ValueError):
                    sessions.validate({'schema_version': 1, 'records': {}, 'owners': owners})
        # A key need not be a registered session: adoption names legacy actors.
        sessions.validate({'schema_version': 1, 'records': {}, 'owners': {'legacy-name': MINE}})

    def test_the_principal_prefixes_are_a_closed_set(self):
        # L7: only lane:NAME and person:NAME are principals; any other prefix word is refused.
        for good in ('lane:lane-one', 'person:lane-one', 'lane:a', 'person:' + 'a'*95):
            with self.subTest(good=good):
                self.assertEqual(sessions.valid_principal(good), good)
        for bad in ('team:x', 'Lane:x', 'LANE:x', 'Person:x', 'lane:', 'person:', 'lane', 'person',
                    ':x', 'lane:person:x', 'person:lane:x', 'lane:x:y', 'lane:-x', 'lane:two words',
                    '', None, 7, 'lane:x\n'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sessions.valid_principal(bad)

    def test_a_reconciled_registration_without_an_owners_map_omits_the_principal(self):
        # P3: a registry with no owners map (an installation that configures nothing) answers a
        # replay exactly as before rule 2, with no `principal` key.
        made = self.register(principal=MINE)
        write_registry(self.path, [made['session']])                  # no owners map at all
        again = sessions.execute(self.path, 'example',
                                 ['register', '--name', made['session']['name'],
                                  '--request-id', made['session']['request_id']], self.export)
        self.assertTrue(again['reconciled'])
        self.assertNotIn('principal', again)

    def test_the_downgrade_limit_is_the_owners_key(self):
        """An older kit's validator refuses a registry that has the owners map."""
        def old_kit_reads(data):
            allowed = {'schema_version', 'records', 'resumes', 'runs'}
            return isinstance(data, dict) and not (set(data) - allowed)
        made = self.register(principal=MINE)
        data = json.loads((self.path/'.sessions.json').read_text())
        self.assertTrue(old_kit_reads({'schema_version': 1, 'records': {}}))         # unconfigured: readable
        self.assertFalse(old_kit_reads(data))                                        # adopted: refused
        self.assertIn(made['session']['actor'], data['owners'])


class AdoptionTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(self.tmp.name, 'alpha', 'beta')
        deployment(self.root)
        # The tracker names the legacy actors these tests adopt; `adopt-actor` reads it to
        # refuse a name that appears nowhere in the project (review, finding 10).
        rows = [{'id': 't-%d' % index, 'actor': name}
                for index, name in enumerate(('alex/s1', 'coordinator'))]
        self.tracker = mock.patch.object(admin, 'run_bd',
                                         return_value='\n'.join(json.dumps(row) for row in rows) + '\n')
        self.tracker.start()
        self.addCleanup(self.tracker.stop)

    def adopt(self, **more):
        args = dict(project='alpha', actor='alex/s1', principal=MINE, operator='ops', reason='the lane he works in',
                    from_principal=None)
        args.update(more)
        return admin.adopt_actor(self.root, args['project'], args['actor'], args['principal'],
                                 args['operator'], args['reason'], from_principal=args['from_principal'])

    def test_an_existing_actor_is_given_to_a_principal_and_audited(self):
        result = self.adopt()
        self.assertEqual((result['changed'], result['previous'], result['principal'], result['moved']),
                         (True, None, MINE, False))
        self.assertEqual(sessions.owners(self.root/'projects'/'alpha'), {'alex/s1': MINE})
        audit = admin.actor_adoptions(self.root)
        self.assertEqual(len(audit), 1)
        self.assertEqual({k: audit[0][k] for k in ('operator', 'project', 'actor', 'principal', 'previous')},
                         {'operator': 'ops', 'project': 'alpha', 'actor': 'alex/s1', 'principal': MINE,
                          'previous': None})
        self.assertEqual(audit[0]['reason'], 'the lane he works in')
        self.assertFalse(audit[0]['moved'])                           # the first owner is not a move
        self.assertEqual(admin.actor_adoptions(self.root, 'beta'), [])

    def test_the_same_principal_again_writes_nothing_and_a_move_needs_from(self):
        self.adopt()
        before = (self.root/'actor-adoptions.audit.json').read_bytes()
        again = self.adopt(reason='said twice')
        self.assertFalse(again['changed'])
        self.assertEqual((self.root/'actor-adoptions.audit.json').read_bytes(), before)
        # Moving another principal's actor with the plain command is refused (item 2).
        untouched = tree(self.root)
        with self.assertRaises(ValueError) as refusal:
            self.adopt(principal=OTHER, reason='moved lane')
        self.assertIn('Refusing to move', str(refusal.exception))
        self.assertIn('--from %s' % MINE, str(refusal.exception))
        self.assertEqual(tree(self.root), untouched)                  # nothing written
        # --from naming the wrong owner is refused too.
        with self.assertRaises(ValueError) as refusal:
            self.adopt(principal=OTHER, reason='moved lane', from_principal=OTHER)
        self.assertIn('--from names %s' % OTHER, str(refusal.exception))
        self.assertEqual(tree(self.root), untouched)
        # --from naming the owner it has now moves it, and the audit records the move.
        moved = self.adopt(principal=OTHER, reason='moved lane', from_principal=MINE)
        self.assertEqual((moved['changed'], moved['previous'], moved['moved']), (True, MINE, True))
        self.assertEqual(len(admin.actor_adoptions(self.root)), 2)
        self.assertEqual(admin.actor_adoptions(self.root)[1]['previous'], MINE)
        self.assertTrue(admin.actor_adoptions(self.root)[1]['moved'])

    def test_from_on_an_actor_with_no_owner_is_refused(self):
        # F3: --from names the owner a move starts from; on an actor with no owner at all it
        # must not be accepted as a first adoption.
        untouched = tree(self.root)
        with self.assertRaises(ValueError) as refusal:
            self.adopt(from_principal=OTHER)
        self.assertIn('does not give alex/s1 to it', str(refusal.exception))
        self.assertEqual(tree(self.root), untouched)

    def test_the_audit_entry_is_written_before_the_registry(self):
        # F5: a kill between the two writes leaves the audit holding the adoption, never the
        # registry holding an owner the audit does not name.
        def killed(path, data):
            raise OSError('killed before the registry write')
        with mock.patch('coordination.atomic', side_effect=killed):
            with self.assertRaises(OSError):
                self.adopt()
        self.assertEqual([entry['actor'] for entry in admin.actor_adoptions(self.root)], ['alex/s1'])
        self.assertEqual(sessions.owners(self.root/'projects'/'alpha'), {})   # registry not written

    def test_the_operator_name_rule_keeps_the_two_prefixes_apart(self):
        # L5: lane:NAME and person:NAME are different principals in the operator-name rule too.
        deployment(self.root, ('ops', 'coordinator'))
        write_registry(self.root/'projects'/'beta', [], owners={'coordinator': PERSON})
        before = tree(self.root)
        with self.assertRaises(ValueError) as refusal:
            self.adopt(actor='coordinator', principal=MINE)
        self.assertIn('already gives it to %s' % PERSON, str(refusal.exception))
        self.assertEqual(tree(self.root), before)

    def test_moving_between_the_two_prefixes_is_a_move(self):
        # L6: person:NAME to lane:NAME (the same suffix) is a move, not a no-op.
        self.adopt(principal=PERSON)
        before = tree(self.root)
        with self.assertRaises(ValueError) as refusal:
            self.adopt(principal=MINE)
        self.assertIn('Refusing to move', str(refusal.exception))
        self.assertEqual(tree(self.root), before)
        moved = self.adopt(principal=MINE, from_principal=PERSON)
        self.assertEqual((moved['changed'], moved['moved'], moved['previous']), (True, True, PERSON))

    def test_a_name_that_appears_nowhere_in_the_project_is_refused(self):
        before = tree(self.root)
        with self.assertRaises(ValueError) as refusal:
            self.adopt(actor='ghost-actor')
        self.assertIn('no session registration, owner entry or tracker row', str(refusal.exception))
        self.assertEqual(tree(self.root), before)
        # A registered session actor is enough, with no tracker row.
        known = record('known')
        write_registry(self.root/'projects'/'alpha', [known])
        self.assertTrue(self.adopt(actor=known['actor'])['changed'])
        # A tracker that cannot be read is a refusal, never "the name is unknown".
        write_registry(self.root/'projects'/'alpha')                  # clear the owner entry
        with mock.patch.object(admin, 'run_bd', side_effect=OSError('no bd')):
            with self.assertRaises(ValueError) as refusal:
                self.adopt(actor='ghost-actor')
            self.assertIn('Cannot read the tracker', str(refusal.exception))

    def test_deep_tracker_rows_read_to_750_and_any_unreadable_row_refuses(self):
        """kittrial-5bb.221 revision 2: project_actor_names read the export with a bare
        json.loads per line. Rows nested to 750 levels read normally now (a name held by
        one is adoptable), and one unreadable row refuses every adoption in the project as
        a tracker that could not be read: the row may be the one that names the actor."""
        deep65 = ('{"id": "t-65", "actor": "mid-author", "metadata": '
                  + '{"a":' * 64 + '1' + '}' * 64 + '}')
        deeper = ('{"id": "t-deeper", "actor": "deep-author", "metadata": '
                  + '{"a":' * 750 + '1' + '}' * 750 + '}')
        healthy = json.dumps({'id': 't-0', 'actor': 'alex/s1'})
        with mock.patch.object(admin, 'run_bd', return_value='\n'.join((healthy, deep65)) + '\n'):
            # A name held only by a row nested 65 levels parses and is adoptable.
            self.assertTrue(self.adopt(actor='mid-author')['changed'])
        write_registry(self.root/'projects'/'alpha')                  # clear owner entries between halves
        with mock.patch.object(admin, 'run_bd', return_value='\n'.join((healthy, deep65, deeper)) + '\n'):
            before = tree(self.root)
            # One unreadable row: every adoption in the project is refused as unreadable,
            # for the name the deep row held and for a healthy one alike.
            for actor in ('deep-author', 'alex/s1', 'ghost-actor'):
                with self.assertRaises(ValueError) as refusal:
                    self.adopt(actor=actor)
                self.assertIn('Cannot read the tracker', str(refusal.exception))
                self.assertIn('unreadable row', str(refusal.exception))
            self.assertEqual(tree(self.root), before)
        # Without the unreadable row the healthy names are adoptable again.
        write_registry(self.root/'projects'/'alpha')
        with mock.patch.object(admin, 'run_bd', return_value=healthy + '\n'):
            self.assertTrue(self.adopt(actor='alex/s1')['changed'])

    def test_an_operator_listed_name_owned_elsewhere_by_another_principal_is_refused(self):
        # An operator identity cannot contain a slash (recovery.identity), so the name that
        # can also be on the operator list is a plain one.
        deployment(self.root, ('ops', 'coordinator'))
        write_registry(self.root/'projects'/'beta', [], owners={'coordinator': OTHER})
        before = tree(self.root)
        with self.assertRaises(ValueError) as refusal:
            self.adopt(actor='coordinator')
        self.assertIn('is on the operator allowlist and project beta already gives it to %s' % OTHER,
                      str(refusal.exception))
        self.assertEqual(tree(self.root), before)                     # nothing written
        # The same principal is not a conflict.
        write_registry(self.root/'projects'/'beta', [], owners={'coordinator': MINE})
        self.assertTrue(self.adopt(actor='coordinator')['changed'])

    def test_a_name_not_on_the_operator_list_may_differ_between_projects(self):
        write_registry(self.root/'projects'/'beta', [], owners={'alex/s1': OTHER})
        self.assertTrue(self.adopt()['changed'])                      # no operator list entry, no conflict

    def test_the_operator_and_the_reason_are_required(self):
        for more, said in ((dict(operator='nobody'), 'not a server-side configured operator'),
                           (dict(reason='   '), 'A reason is required'),
                           (dict(reason='x'*401), 'at most 400 characters'),
                           (dict(principal='lane-one'), 'principal name of the form lane:NAME or person:NAME'),
                           (dict(actor='bad name'), 'must be a short actor name')):
            with self.subTest(more=more), self.assertRaises(ValueError) as refusal:
                self.adopt(**more)
            self.assertIn(said, str(refusal.exception))
        self.assertFalse((self.root/'projects'/'alpha'/'.sessions.json').exists())

    def test_a_damaged_audit_refuses_before_the_registry_is_touched(self):
        (self.root/'actor-adoptions.audit.json').write_text('{not json', encoding='utf-8')
        with self.assertRaises(ValueError) as refusal:
            self.adopt()
        self.assertIn('actor-adoptions audit', str(refusal.exception))
        self.assertFalse((self.root/'projects'/'alpha'/'.sessions.json').exists())

    def test_an_audit_of_the_wrong_shape_is_a_refusal_not_an_empty_history(self):
        # A8: not merely invalid JSON but a JSON document of the wrong shape must refuse.
        for record in ({'schema_version': 1, 'entries': [{'bogus': 1}]},
                       {'schema_version': 2, 'entries': []},
                       {'schema_version': 1, 'entries': 'nope'},
                       {'schema_version': 1}):
            with self.subTest(record=record):
                (self.root/'actor-adoptions.audit.json').write_text(json.dumps(record), encoding='utf-8')
                with self.assertRaises(ValueError) as refusal:
                    admin.actor_adoptions(self.root)
                self.assertIn('not the history this kit writes', str(refusal.exception))
                with self.assertRaises(ValueError):
                    self.adopt()

    def test_the_audit_keeps_the_documented_newest_entries(self):
        # A10: the limit is a documented 200 and the oldest entries are dropped.
        self.assertEqual(admin.ACTOR_ADOPTIONS_MAX, 200)
        entries = [{'at': '2026-01-01T00:00:00Z', 'operator': 'ops', 'project': 'alpha',
                    'actor': 'actor-%03d' % index, 'principal': MINE, 'previous': None, 'reason': 'x'}
                   for index in range(admin.ACTOR_ADOPTIONS_MAX)]
        (self.root/'actor-adoptions.audit.json').write_text(
            json.dumps({'schema_version': admin.ACTOR_ADOPTIONS_SCHEMA, 'entries': entries}), encoding='utf-8')
        self.assertTrue(self.adopt()['changed'])
        kept = admin.actor_adoptions(self.root)
        self.assertEqual(len(kept), admin.ACTOR_ADOPTIONS_MAX)
        self.assertEqual(kept[0]['actor'], 'actor-001')               # the oldest was dropped
        self.assertEqual(kept[-1]['actor'], 'alex/s1')

    def test_adopt_actor_takes_the_project_lock(self):
        # A9: the registry write is under the project's coordination lock. Pinned at the
        # source, as slice 1 pins the action walk, because a source-level assertion is the
        # honest way to hold a lock that a test cannot take without deadlocking.
        source = (KIT/'admin.py').read_text(encoding='utf-8')
        body = source[source.index('\ndef adopt_actor('):source.index('\ndef credential_actors(')]
        self.assertIn('fcntl.flock(lock', body)
        self.assertIn(".coordination.lock", body)

    def test_the_same_name_check_and_both_writes_are_under_the_deployment_lock(self):
        # Finding 1: the operator-name check must run under the deployment lock the audit write
        # takes, and the registry write must stay inside it, so two adopt-actor commands for one
        # operator-listed name in two projects serialize instead of both exiting 0. Pinned at
        # the source, as the coordination-lock test above is, because the race cannot be held
        # without deadlocking the test.
        source = (KIT/'admin.py').read_text(encoding='utf-8')
        body = source[source.index('\ndef adopt_actor('):source.index('\ndef credential_actors(')]
        lock = body.index('with deployment_config_lock(root):')
        self.assertLess(lock, body.index('principal_conflicts(root,actor,principal,project)'))
        self.assertLess(lock, body.index('actor_adoptions(root)'))
        self.assertLess(lock, body.index("atomic(path/'.sessions.json',data)"))
        self.assertLess(body.index('atomic_private_write(root/ACTOR_ADOPTIONS_AUDIT'),
                        body.index("atomic(path/'.sessions.json',data)"))

    def test_a_non_project_is_refused(self):
        with self.assertRaises(ValueError) as refusal:
            self.adopt(project='gamma')
        self.assertIn('Unknown/uninitialized project', str(refusal.exception))


class PrintedPrincipalLineTests(unittest.TestCase):

    def lines(self, **more):
        return admin.authorized_key_lines('/srv/state', '/srv/kit', 'ssh-ed25519', KEY_BODY, 'alex@laptop',
                                          python='/usr/bin/python3', **more)

    def test_without_a_principal_the_line_is_what_it_was(self):
        self.assertEqual(self.lines(projects=['alpha']), self.lines(projects=['alpha'], principal=None))
        self.assertNotIn('--principal', self.lines(projects=['alpha'])['contributor'])

    def test_the_principal_is_on_the_command_and_repeated_in_the_comment(self):
        bound = self.lines(projects=['alpha'], principal=MINE)
        self.assertIn(' --project alpha --principal %s",restrict,' % MINE, bound['contributor'])
        self.assertTrue(bound['contributor'].endswith(' alex@laptop orchestra-projects=alpha orchestra-principal=%s'
                                                      % MINE))
        alone = self.lines(principal=MINE)
        self.assertIn(' --principal %s",restrict,' % MINE, alone['contributor'])
        self.assertTrue(alone['contributor'].endswith(' alex@laptop orchestra-principal=%s' % MINE))

    def test_the_wrapper_reads_back_what_the_line_was_printed_with(self):
        import shlex
        line = self.lines(projects=['alpha'], principal=MINE)['contributor']
        command = shlex.split(line[len('command="'):line.index('",restrict')])
        at = command.index('/srv/kit/ssh_forced_command.py')
        args = forced.parse_args(command[at + 1:])
        self.assertEqual((forced._projects(args.project), forced._principal(args.principal)), (['alpha'], MINE))

    def test_an_operator_key_cannot_be_bound_and_the_principal_is_checked(self):
        with self.assertRaises(ValueError) as refused:
            admin.authorized_keys('/srv/state', '/srv/kit/key.pub', role='operator', principal=MINE)
        self.assertIn('unrestricted operator', str(refused.exception))
        self.assertIsNone(admin.key_principal(None))
        self.assertEqual(admin.key_principal(MINE), MINE)
        with self.assertRaises(ValueError):
            admin.key_principal('lane-one')


@unittest.skipUnless(os.name == 'posix', 'the printed line needs an absolute Linux path')
class PrintAndAdoptCommandTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha')
        deployment(self.root)
        self.key = Path(self.tmp.name)/'key.pub'
        self.key.write_text('ssh-ed25519 %s alex@laptop\n' % KEY_BODY, encoding='utf-8')
        tracker = mock.patch.object(admin, 'run_bd',
                                    return_value=json.dumps({'id': 't-1', 'actor': 'alex/s1'})+'\n')
        tracker.start()
        self.addCleanup(tracker.stop)

    def run_admin(self, *args):
        said = io.StringIO()
        with mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *args]), redirect_stdout(said):
            admin.main()
        return said.getvalue()

    def test_the_printed_payload_names_the_principal(self):
        plain = json.loads(self.run_admin('authorized-keys', '--key-file', str(self.key),
                                          '--python', '/usr/bin/python3'))
        self.assertNotIn('principal', plain)
        bound = json.loads(self.run_admin('authorized-keys', '--key-file', str(self.key),
                                          '--python', '/usr/bin/python3', '--principal', MINE))
        self.assertEqual(bound['principal'], MINE)
        self.assertIn(' --principal %s",restrict,' % MINE, bound['contributor'])
        self.assertNotIn('operator', bound)
        # A lane spelled lane:NAME is accepted exactly like person:NAME.
        lane = json.loads(self.run_admin('authorized-keys', '--key-file', str(self.key),
                                         '--python', '/usr/bin/python3', '--principal', 'lane:orc-coord'))
        self.assertEqual(lane['principal'], 'lane:orc-coord')

    def test_the_printed_payload_notes_first_call_and_pre_registration_check(self):
        bound = json.loads(self.run_admin('authorized-keys', '--key-file', str(self.key),
                                          '--python', '/usr/bin/python3', '--principal', MINE))
        notes = ' '.join(bound['notes'])
        self.assertIn('first kit call must be session register', notes)
        self.assertIn('No other command (such as docs or ready) can precede registration', notes)
        self.assertIn('admin.py --root RUNTIME authorized-keys-list', notes)
        self.assertIn('ssh HOST exit or ssh -T HOST', notes)
        self.assertIn('without registering (or without making an actor)', notes)

        # Pin the exact refusal sentences quoted in docs/OPERATIONS.md against ssh_forced_command
        # With no command (tokens = []):
        _, reason_no_cmd = forced.select_endpoint([], ['/path/to/endpoint.py'])
        no_cmd_sentence = forced.REFUSAL + reason_no_cmd
        self.assertEqual(
            no_cmd_sentence,
            'ssh_forced_command: no endpoint selected: this key runs only the configured endpoint; '
            'a client with "forced_command": true sends its path')

        # With a command other than configured endpoint (e.g. 'exit'):
        _, reason_exit = forced.select_endpoint(['exit'], ['/path/to/endpoint.py'])
        exit_sentence = forced.REFUSAL + reason_exit
        self.assertEqual(exit_sentence, "ssh_forced_command: this key may not run 'exit'")

    def test_a_repeated_principal_is_refused(self):
        # Finding 3: `authorized-keys --principal A --principal B` used to print B silently.
        self.assertEqual(admin.key_principal(MINE), MINE)
        with self.assertRaises(ValueError) as refusal:
            admin.key_principal([MINE, OTHER])
        self.assertIn('--principal names %s' % MINE, str(refusal.exception))
        self.assertIn(OTHER, str(refusal.exception))
        with self.assertRaises(ValueError):
            admin.authorized_keys(self.root, str(self.key), 'contributor', '/usr/bin/python3', None, None,
                                  [MINE, OTHER])

    def test_the_listing_names_both_bindings_and_flags_a_bad_principal(self):
        good = admin.authorized_key_lines('/srv/state', '/srv/kit', 'ssh-ed25519', KEY_BODY, 'alex@laptop',
                                          python='/usr/bin/python3', principal=MINE)['contributor']
        entry = admin.key_line(good, self.root, Path('/srv/kit'))
        self.assertEqual((entry['kind'], entry['principal']), ('bound', MINE))
        self.assertFalse(entry['principal_repeated'])
        self.assertFalse(entry['principal_ill_formed'])
        repeated_line = good.replace('--principal %s' % MINE, '--principal %s --principal %s' % (MINE, OTHER))
        repeated = admin.key_line(repeated_line, self.root, Path('/srv/kit'))
        self.assertTrue(repeated['principal_repeated'])
        self.assertFalse(repeated['principal_ill_formed'])
        ill_line = good.replace('--principal %s' % MINE, '--principal lane-p')
        ill = admin.key_line(ill_line, self.root, Path('/srv/kit'))
        self.assertTrue(ill['principal_ill_formed'])
        self.assertEqual(ill['principal'], 'lane-p')
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text('\n'.join([good, repeated_line, ill_line])+'\n', encoding='utf-8')
        listing = admin.authorized_keys_listing(self.root, str(file))
        # Both kinds of binding are counted: `bound` for all three, `principal-bound` named.
        self.assertEqual(listing['summary'].get('bound'), 3)
        self.assertEqual(listing['summary'].get('principal-bound'), 3)
        self.assertIn(2, listing['attention'])                        # the repeated principal
        self.assertIn(3, listing['attention'])                        # the ill-formed one

    def test_the_listing_flags_bad_lines_that_are_otherwise_clean(self):
        # R3, R4, finding 6: the same flags on lines that are otherwise in order, so they are
        # under attention for the right reason (the older test's /srv/kit paths are under
        # attention anyway and hid the mutant).
        kit = Path(admin.__file__).resolve().parent

        def line(**more):
            return admin.authorized_key_lines(str(self.root), str(kit), 'ssh-ed25519', KEY_BODY,
                                              'alex@laptop', python='/usr/bin/python3', **more)['contributor']

        good = line(principal=MINE)
        repeated_principal = line(principal=MINE).replace(
            '--principal %s' % MINE, '--principal %s --principal %s' % (MINE, OTHER))
        ill_principal = line(principal='lane-p')
        repeated_project = line(projects=['alpha']).replace('--project alpha', '--project alpha --project alpha')
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text('\n'.join([good, repeated_principal, ill_principal, repeated_project])+'\n', encoding='utf-8')
        listing = admin.authorized_keys_listing(self.root, str(file))
        self.assertEqual([entry['kind'] for entry in listing['lines']], ['bound']*4)
        self.assertEqual(listing['attention'], [2, 3, 4])
        self.assertTrue(listing['lines'][1]['principal_repeated'])
        self.assertFalse(listing['lines'][1]['principal_ill_formed'])
        self.assertTrue(listing['lines'][2]['principal_ill_formed'])
        self.assertTrue(listing['lines'][3]['project_repeated'])
        # Every named principal is counted; a token that is a flag is not a principal at all.
        self.assertEqual(listing['summary']['principal-bound'], 3)

    def test_a_repeated_adopt_flag_is_refused_and_an_abbreviation_is_not_a_flag(self):
        # Finding 4: argparse used to keep the last of a repeated --from/--principal/--actor/
        # --reason and accepted an abbreviation; both are now refused.
        cases = (
            ['adopt-actor', 'alpha', 'alex/s1', '--principal', MINE, '--principal', OTHER,
             '--actor', 'ops', '--reason', 'x'],
            ['adopt-actor', 'alpha', 'alex/s1', '--principal', MINE,
             '--from', MINE, '--from', OTHER, '--actor', 'ops', '--reason', 'x'],
            ['adopt-actor', 'alpha', 'alex/s1', '--principal', MINE,
             '--actor', 'ops', '--actor', 'ops2', '--reason', 'x'],
            ['adopt-actor', 'alpha', 'alex/s1', '--principal', MINE,
             '--actor', 'ops', '--reason', 'x', '--reason', 'y'],
        )
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaises(ValueError) as refusal:
                self.run_admin(*argv)
            self.assertIn('given more than once', str(refusal.exception))
        said = io.StringIO()
        with redirect_stderr(said), self.assertRaises(SystemExit):
            self.run_admin('adopt-actor', 'alpha', 'alex/s1', '--princ', MINE, '--actor', 'ops', '--reason', 'x')
        self.assertIn('--principal', said.getvalue())

    def test_a_flag_swallowed_as_a_principal_value_is_not_counted(self):
        # Finding 6: `--principal --project pa` used to make the listing report `--project` as
        # the principal and count it as principal-bound; it is now unknown arguments only.
        kit = Path(admin.__file__).resolve().parent
        good = admin.authorized_key_lines(str(self.root), str(kit), 'ssh-ed25519', KEY_BODY, 'alex@laptop',
                                          python='/usr/bin/python3', principal=MINE)['contributor']
        line = good.replace(' --principal %s"' % MINE, ' --principal --project"')
        entry = admin.key_line(line, self.root, kit)
        self.assertIsNone(entry['principal'])
        self.assertFalse(entry.get('principal_ill_formed'))
        self.assertIn('--principal', entry['unknown_arguments'])
        self.assertIn('--project', entry['unknown_arguments'])
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text(line+'\n', encoding='utf-8')
        listing = admin.authorized_keys_listing(self.root, str(file))
        self.assertEqual(listing['attention'], [1])
        self.assertNotIn('principal-bound', listing['summary'])

    def test_the_adopt_command_writes_and_the_reader_prints_it(self):
        made = json.loads(self.run_admin('adopt-actor', 'alpha', 'alex/s1', '--principal', MINE,
                                         '--actor', 'ops', '--reason', 'the lane he works in'))
        self.assertEqual((made['changed'], made['principal'], made['moved']), (True, MINE, False))
        listing = json.loads(self.run_admin('actor-adoptions'))
        self.assertEqual([entry['actor'] for entry in listing['entries']], ['alex/s1'])
        self.assertEqual(json.loads(self.run_admin('actor-adoptions', 'alpha'))['entries'],
                         listing['entries'])
        self.assertEqual(json.loads(self.run_admin('actor-adoptions', 'beta'))['entries'], [])
        # A move needs --from; with it the entry is marked a move.
        with self.assertRaises(ValueError):
            self.run_admin('adopt-actor', 'alpha', 'alex/s1', '--principal', OTHER,
                           '--actor', 'ops', '--reason', 'moved')
        moved = json.loads(self.run_admin('adopt-actor', 'alpha', 'alex/s1', '--principal', OTHER,
                                          '--from', MINE, '--actor', 'ops', '--reason', 'moved'))
        self.assertEqual((moved['changed'], moved['previous'], moved['moved']), (True, MINE, True))


if __name__ == '__main__':
    unittest.main()
