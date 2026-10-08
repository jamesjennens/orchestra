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
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
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
MINE = 'person:lane-one'
OTHER = 'person:lane-two'
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
    return {str(path.relative_to(folder)): (path.read_bytes() if path.is_file() else None)
            for path in sorted(Path(folder).rglob('*'))}


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

    def test_a_bound_key_acts_only_as_its_principals_actors(self):
        # Its own actor is served, exactly as an unbound caller.
        self.assertEqual(endpoint.execute(self.root, self.view(), key_principal=MINE),
                         endpoint.execute(self.root, self.view()))
        # Another principal's actor, and an actor with no owner at all, are refused before
        # anything is read or run.
        for actor in (self.other['actor'], 'session-'+str(uuid.uuid4()), 'legacy-name', ''):
            with self.subTest(actor=actor):
                self.refused(self.view(actor=actor),
                             'This key is bound to principal %s and may act only as actors that principal '
                             'registered in this project; %s is not one of them' % (MINE, actor or "''"))

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

    def test_a_principal_without_projects_may_name_another_project_but_only_its_actors(self):
        # A principal without --project is not rule 1: another project is not refused as
        # unknown. But it still acts only as this principal's actors, and beta has none.
        self.refused({'project': 'beta', 'actor': self.mine['actor'], 'action': 'view'},
                     'This key is bound to principal %s and may act only as actors that principal '
                     'registered in this project; %s is not one of them' % (MINE, self.mine['actor']))
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
        for flags, said in ((('--key-principal', 'lane-one'), 'must be a principal name of the form person:NAME'),
                            (('--key-principal', 'person:' + 'a'*100), 'must be a principal name'),
                            (('--key-principal', 'person:two words'), 'must be a principal name'),
                            (('--key-principal', MINE, '--authority-store', str(self.root/'store.json')),
                             '--key-principal is for an SSH key line and cannot be combined with --authority-store')):
            with self.subTest(flags=flags):
                answer = self.ask(self.view(self.mine['actor']), *flags)
                self.assertEqual((answer['returncode'], answer['stdout']), (2, ''))
                self.assertIn(said, answer['stderr'])

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
        for value in ('lane-one', 'person:', 'person:two words', 'person:' + 'a'*100, 'Person:lane', 'person:lane\n'):
            with self.subTest(value=value):
                argv, status, said = self.launched('--principal', value)
                self.assertEqual((argv, status), (None, 2))
                self.assertIn('--principal must be a principal name', said)

    def test_the_name_rule_is_the_kits(self):
        for name in ('person:a', MINE, 'person:lane.one', 'person:lane-one', 'person:lane_one',
                     'person:' + 'a'*95, 'lane', 'person:', 'person:two words', 'person:' + 'a'*96,
                     'Person:lane', 'person:lane/one'):
            kit = True
            try:
                sessions.valid_principal(name)
            except ValueError:
                kit = False
            self.assertEqual(bool(forced.PRINCIPAL_NAME.fullmatch(name)), kit, name)


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
        self.assertIsNone(made['principal'])
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

    def test_the_owner_map_is_validated(self):
        actor = record()['actor']
        for owners in ({actor: 'lane-one'}, {actor: 'person:'}, {'bad name': MINE}, {actor: 7}):
            with self.subTest(owners=owners):
                with self.assertRaises(ValueError):
                    sessions.validate({'schema_version': 1, 'records': {}, 'owners': owners})
        # A key need not be a registered session: adoption names legacy actors.
        sessions.validate({'schema_version': 1, 'records': {}, 'owners': {'legacy-name': MINE}})

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

    def adopt(self, **more):
        args = dict(project='alpha', actor='alex/s1', principal=MINE, operator='ops', reason='the lane he works in')
        args.update(more)
        return admin.adopt_actor(self.root, args['project'], args['actor'], args['principal'],
                                 args['operator'], args['reason'])

    def test_an_existing_actor_is_given_to_a_principal_and_audited(self):
        result = self.adopt()
        self.assertEqual((result['changed'], result['previous'], result['principal']), (True, None, MINE))
        self.assertEqual(sessions.owners(self.root/'projects'/'alpha'), {'alex/s1': MINE})
        audit = admin.actor_adoptions(self.root)
        self.assertEqual(len(audit), 1)
        self.assertEqual({k: audit[0][k] for k in ('operator', 'project', 'actor', 'principal', 'previous')},
                         {'operator': 'ops', 'project': 'alpha', 'actor': 'alex/s1', 'principal': MINE,
                          'previous': None})
        self.assertEqual(audit[0]['reason'], 'the lane he works in')
        self.assertEqual(admin.actor_adoptions(self.root, 'beta'), [])

    def test_the_same_principal_again_writes_nothing_and_a_change_records_previous(self):
        self.adopt()
        before = (self.root/'actor-adoptions.audit.json').read_bytes()
        again = self.adopt(reason='said twice')
        self.assertFalse(again['changed'])
        self.assertEqual((self.root/'actor-adoptions.audit.json').read_bytes(), before)
        moved = self.adopt(principal=OTHER, reason='moved lane')
        self.assertEqual((moved['changed'], moved['previous']), (True, MINE))
        self.assertEqual(len(admin.actor_adoptions(self.root)), 2)
        self.assertEqual(admin.actor_adoptions(self.root)[1]['previous'], MINE)

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
                           (dict(principal='lane-one'), 'principal name of the form person:NAME'),
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

    def test_the_adopt_command_writes_and_the_reader_prints_it(self):
        made = json.loads(self.run_admin('adopt-actor', 'alpha', 'alex/s1', '--principal', MINE,
                                         '--actor', 'ops', '--reason', 'the lane he works in'))
        self.assertEqual((made['changed'], made['principal']), (True, MINE))
        listing = json.loads(self.run_admin('actor-adoptions'))
        self.assertEqual([entry['actor'] for entry in listing['entries']], ['alex/s1'])
        self.assertEqual(json.loads(self.run_admin('actor-adoptions', 'alpha'))['entries'],
                         listing['entries'])
        self.assertEqual(json.loads(self.run_admin('actor-adoptions', 'beta'))['entries'], [])


if __name__ == '__main__':
    unittest.main()
