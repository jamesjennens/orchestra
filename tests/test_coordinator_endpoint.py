"""A confined coordinator runs its acceptance commands through the endpoint (kittrial-5bb.195).

Slice 3 of docs/COORDINATORS_PER_PROJECT_DESIGN.md: for an actor a bound key's principal owns
AND that is on this installation's operator list (or, for ``capability-verify``, its verifiers
list), in a project the key may name, the endpoint answers the host commands ``admin.py`` would
otherwise have to run - guidance set/clear/status, accepting reference and capability records,
proposal review and decision, the operator's handoff and ``set-onboarding``. Voiding,
reverting, reconciling, backups, retiring and the rollout switches are deliberately absent
(James's answer to question 5, 2026-10-07).

The tests that matter are ``test_nothing_configured_cannot_reach_the_surface``: with no
``--key-principal`` every subcommand is refused before a lock, a native read or a write, so an
installation that configures nothing behaves exactly as today; and
``test_a_listed_actor_of_the_principal_is_served``: a bound, listed actor's command is carried
out as the host command would carry it out.
"""
import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import sessions

try:
    import endpoint
except ImportError:                                   # fcntl: the endpoint is POSIX only
    endpoint = None

POSIX = unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
PRINCIPAL = 'lane:lane-one'
OTHER = 'lane:lane-two'
ACTOR = 'coordinator'
HELPER = 'helper'
VERIFIER = 'verifier'
GOOD_GUIDANCE = 'Follow the standing guidance for this project.\n'
KEY_BODY = base64.b64encode(b'orchestra-coordinator-synthetic-key').decode()


def runtime(base, *projects):
    """A runtime root with initialized-looking projects, each with one view to read."""
    root = Path(base)/'rt'
    for name in projects:
        (root/'projects'/name/'.beads').mkdir(parents=True)
        (root/'projects'/name/'.beads'/'metadata.json').write_text('{}', encoding='utf-8')
        (root/'projects'/name/'views').mkdir()
        (root/'projects'/name/'views'/'CURRENT.md').write_text('the view of %s\n' % name, encoding='utf-8')
    return root


def registry(path, owners):
    """The project's session registry, exactly as rule 2 writes the ``owners`` map."""
    data = {'schema_version': 1, 'records': {}, 'owners': dict(owners)}
    (Path(path)/'.sessions.json').write_text(json.dumps(data), encoding='utf-8')


def deployment(root, operators=(ACTOR,), verifiers=()):
    (root/'deployment.private.json').write_text(
        json.dumps({'operators': list(operators), 'verifiers': list(verifiers), 'password': 'x'}),
        encoding='utf-8')


def request(subcommand, project='alpha', actor=ACTOR, text=None, payload=None):
    """One request as the client transports it: the payload rides as a ``--file`` attachment."""
    built = {'project': project, 'actor': actor, 'action': 'coordinator', 'args': [subcommand]}
    body = text if text is not None else (json.dumps(payload) if payload is not None else None)
    if body is not None:
        built['args'] += ['--file', '@attachment:0']
        built['attachments'] = {'0': {'flag': '--file', 'text': body}}
    return built


def file_tree(folder):
    """Every file below ``folder``; the coordination lock holds no state and is never backed up."""
    return {str(path.relative_to(folder)): (path.read_bytes() if path.is_file() else None)
            for path in sorted(Path(folder).rglob('*'))
            if path.name not in ('.coordination.lock', '.review-writes.lock')}


@POSIX
class CoordinatorGateTests(unittest.TestCase):
    """The refusal that makes the whole surface unreachable without a bound key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(self.tmp.name, 'alpha', 'beta')
        deployment(self.root)
        registry(self.root/'projects'/'alpha', {ACTOR: PRINCIPAL, HELPER: PRINCIPAL, VERIFIER: PRINCIPAL})
        registry(self.root/'projects'/'beta', {ACTOR: PRINCIPAL})

    def refused(self, built, said=None, **authority):
        """The request must raise, and must not reach a program, a native read or a lock."""
        reached = AssertionError('the request reached a program or a lock')
        before = file_tree(self.root)
        with mock.patch.object(subprocess, 'run', side_effect=reached), \
                mock.patch.object(endpoint.native, 'run', side_effect=reached), \
                mock.patch.object(endpoint.fcntl, 'flock', side_effect=reached):
            with self.assertRaises(ValueError) as refusal:
                endpoint.execute(self.root, built, **authority)
        self.assertEqual(file_tree(self.root), before)
        if said is not None:
            self.assertIn(said, str(refusal.exception))
        return str(refusal.exception)

    def test_nothing_configured_cannot_reach_the_surface(self):
        # No --key-principal (the ordinary host loop, or a key that binds nothing): every
        # subcommand is refused by the first line of the gate, before the deployment file is
        # even read, let alone a project file or a bd call.
        for subcommand in endpoint.COORDINATOR_COMMANDS:
            with self.subTest(subcommand=subcommand):
                self.refused(request(subcommand), 'bound to a principal',
                             key_principal=None, key_projects=['alpha'])

    def test_a_bound_key_without_a_listed_actor_is_refused(self):
        # Rule 2 lets the key act as this actor; the operator allowlist still does not.
        for subcommand in endpoint.COORDINATOR_COMMANDS:
            with self.subTest(subcommand=subcommand):
                self.refused(request(subcommand, actor=HELPER), 'not a server-side configured operator',
                             key_principal=PRINCIPAL, key_projects=['alpha'])

    def test_an_actor_of_another_principal_is_refused_by_rule_2(self):
        registry(self.root/'projects'/'alpha', {ACTOR: OTHER})
        self.refused(request('guidance-status'), 'may act only as actors that principal registered',
                     key_principal=PRINCIPAL, key_projects=['alpha'])

    def test_a_project_the_key_may_not_name_is_refused_by_rule_1(self):
        # alpha is initialized and the actor is owned there, but the key names only beta.
        self.refused(request('guidance-status', project='alpha'), 'Unknown/uninitialized project',
                     key_principal=PRINCIPAL, key_projects=['beta'])
        # And a bound key may not reach the web-only actions through this surface either.
        self.refused({'project': 'alpha', 'actor': ACTOR, 'action': 'coordinator',
                      'args': ['creation-standing']}, 'Unknown coordinator command',
                     key_principal=PRINCIPAL, key_projects=['alpha'])

    def test_the_commands_that_stay_with_the_installation_operator_are_absent(self):
        # James's answer to question 5: these are not reachable through this surface, for
        # anybody, however listed. They stay admin.py host commands.
        for name in ('void-record', 'revert-record', 'reconcile-request', 'reference-reconcile',
                     'capability-retire', 'capability-alias-propose', 'capability-alias-reject',
                     'proposal-settings', 'anchor-release', 'retire-project', 'remove-creation',
                     'backup', 'restore-new', 'review-writes', 'checkpoint-provenance-writes',
                     'operators', 'verifiers', 'set-guidance', 'proposal-review-extra'):
            with self.subTest(name=name):
                self.refused(request(name), 'Unknown coordinator command',
                             key_principal=PRINCIPAL, key_projects=['alpha'])

    def test_a_listed_actor_of_the_principal_is_served(self):
        # The positive control for the gate: the same request that is refused above reaches
        # the guidance reader when the actor is bound, listed and in its own project.
        answer = endpoint.execute(self.root, request('guidance-status'),
                                  key_principal=PRINCIPAL, key_projects=['alpha'])
        self.assertEqual(answer['returncode'], 0)
        self.assertEqual(json.loads(answer['stdout'])['schema_version'], 1)

    def test_capability_verify_admits_a_verifier_and_the_others_do_not(self):
        deployment(self.root, operators=('somebody-else',), verifiers=(VERIFIER,))
        self.refused(request('guidance-status', actor=VERIFIER), 'not a server-side configured operator',
                     key_principal=PRINCIPAL, key_projects=['alpha'])
        # capability-verify passes the gate for a verifier; it then fails on the payload, and
        # that refusal is not the allowlist one.
        said = self.refused(request('capability-verify', actor=VERIFIER,
                                    payload={'schema_version': 1, 'items': []}),
                            key_principal=PRINCIPAL, key_projects=['alpha'])
        self.assertNotIn('allowlist', said)

    def test_a_malformed_request_for_the_action_is_refused(self):
        for args in ([], 'guidance-status', [7], ['guidance-status', '--file']):
            with self.subTest(args=args):
                self.refused({'project': 'alpha', 'actor': ACTOR, 'action': 'coordinator', 'args': args},
                             key_principal=PRINCIPAL, key_projects=['alpha'])


@POSIX
class GuidanceAndOnboardingTests(unittest.TestCase):
    """Two families that write project files and no bd row, end to end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(self.tmp.name, 'alpha')
        deployment(self.root)
        registry(self.root/'projects'/'alpha', {ACTOR: PRINCIPAL})
        self.project = self.root/'projects'/'alpha'

    def ask(self, subcommand, text=None, payload=None, actor=ACTOR):
        return endpoint.execute(self.root, request(subcommand, actor=actor, text=text, payload=payload),
                                key_principal=PRINCIPAL, key_projects=['alpha'])

    def test_guidance_set_status_clear_round_trip(self):
        installed = json.loads(self.ask('guidance-set', text=GOOD_GUIDANCE)['stdout'])
        self.assertTrue(installed['changed'])
        self.assertTrue((self.project/'GUIDANCE.md').is_file())
        status = json.loads(self.ask('guidance-status')['stdout'])
        self.assertEqual(status['version'], installed['version'])
        self.assertEqual(status['set_by'], ACTOR)
        # The host form: a bound, listed actor sees the text, which the self-declared
        # `guidance status` action withholds.
        self.assertEqual(status['text'], GOOD_GUIDANCE)
        cleared = json.loads(self.ask('guidance-clear')['stdout'])
        self.assertIn('GUIDANCE.md', cleared['removed'])
        self.assertFalse((self.project/'GUIDANCE.md').exists())

    def test_a_write_answer_carries_the_servers_time_and_a_read_does_not(self):
        self.assertIn('server_time', self.ask('guidance-set', text=GOOD_GUIDANCE))
        self.assertNotIn('server_time', self.ask('guidance-status'))

    def test_set_onboarding_writes_the_operators_document(self):
        result = json.loads(self.ask('set-onboarding', text='Project onboarding for alpha.\n')['stdout'])
        self.assertEqual((result['state'], result['source']), ('set', 'operator'))
        self.assertEqual((self.project/'ONBOARDING.md').read_text(encoding='utf-8'),
                         'Project onboarding for alpha.\n')


@POSIX
class DispatchedToTheHostRoutesTests(unittest.TestCase):
    """Each family reaches the same library entry point, with the same authority, as admin.py.

    The library calls are replaced so the test pins the routing and the locking, which is what
    the endpoint adds; the library behaviour itself is covered by its own tests and by the
    host commands that call it identically. The two file-writing families above run for real.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(self.tmp.name, 'alpha')
        deployment(self.root, operators=(ACTOR,), verifiers=(VERIFIER,))
        registry(self.root/'projects'/'alpha', {ACTOR: PRINCIPAL, VERIFIER: PRINCIPAL})

    def execute(self, subcommand, payload=None, text=None, actor=ACTOR):
        return endpoint.execute(self.root, request(subcommand, actor=actor, payload=payload, text=text),
                                key_principal=PRINCIPAL, key_projects=['alpha'])

    def test_reference_apply_takes_the_operators_single_route(self):
        import reference_records
        seen = {}

        def apply_native(payload, actor, run, project, operator=False, operators=None):
            seen.update(payload=payload, actor=actor, project=project, operator=operator, operators=operators)
            return {'operation': 'accept', 'key': 'r-1'}

        with mock.patch.object(reference_records, 'apply_native', side_effect=apply_native):
            answer = self.execute('reference-apply', payload={'schema_version': 1, 'key': 'r-1'})
        self.assertEqual(answer['returncode'], 0)
        self.assertEqual((seen['operator'], seen['actor']), (True, ACTOR))
        self.assertEqual(seen['operators'], {ACTOR})
        self.assertEqual(seen['project'], self.root/'projects'/'alpha')
        # admin.py sets `operation: accept` for a direct record; so must the endpoint.
        self.assertEqual(seen['payload']['operation'], 'accept')

    def test_a_reference_batch_holds_no_lock_around_the_batch(self):
        import reference_records
        seen = {}

        def apply_batch(payload, actor, run, project, operators=None, lock=None):
            seen.update(payload=payload, operators=operators, lock=lock)
            return {'complete': True}

        with mock.patch.object(reference_records, 'apply_batch', side_effect=apply_batch), \
                mock.patch.object(endpoint.fcntl, 'flock') as flock:
            answer = self.execute('reference-apply',
                                  payload={'schema_version': 1, 'items': [{'key': 'r-1'}]})
            # The batch takes the lock itself, once per item (kittrial-5bb.67 review 01a0fc55).
            self.assertEqual(flock.call_count, 0)
        self.assertEqual(answer['returncode'], 0)
        self.assertTrue(callable(seen['lock']))
        self.assertEqual(seen['operators'], {ACTOR})

    def test_capability_apply_takes_the_operators_route(self):
        import capability_records
        seen = {}

        def apply_native(payload, actor, run, project, operator=False, operators=None):
            seen.update(operator=operator, operators=operators, payload=payload)
            return {'operation': 'accept', 'key': 'c-1'}

        with mock.patch.object(capability_records, 'apply_native', side_effect=apply_native):
            answer = self.execute('capability-apply', payload={'schema_version': 1, 'key': 'c-1'})
        self.assertEqual(answer['returncode'], 0)
        self.assertEqual((seen['operator'], seen['operators']), (True, {ACTOR}))

    def test_capability_verify_reaches_the_verification_route(self):
        import capability_verification
        seen = {}

        def verify_batch(payload, actor, run, operators=None, verifiers=None, journal=None, lock=None):
            seen.update(actor=actor, operators=operators, verifiers=verifiers, journal=journal, lock=lock)
            return {'items': [], 'recorded': 0, 'refused': 0}

        with mock.patch.object(capability_verification, 'verify_batch', side_effect=verify_batch):
            answer = self.execute('capability-verify',
                                  payload={'schema_version': 1, 'items': [{'key': 'c-1'}]},
                                  actor=VERIFIER)
        self.assertEqual(answer['returncode'], 0)
        self.assertEqual(seen['actor'], VERIFIER)
        self.assertEqual(seen['verifiers'], {VERIFIER})
        self.assertEqual(seen['journal'], self.root/'projects'/'alpha')
        self.assertTrue(callable(seen['lock']))

    def test_proposal_review_and_decide_keep_their_two_roles(self):
        import proposal_records
        routes = []

        def dispose(payload, actor, run, project, operators=None, route='review', http=None):
            routes.append(route)
            return {'state': 'under-review'}

        with mock.patch.object(proposal_records, 'dispose', side_effect=dispose):
            self.execute('proposal-review', payload={'schema_version': 1})
            self.execute('proposal-decide', payload={'schema_version': 1})
        self.assertEqual(routes, ['review', 'decide'])

    def test_handoff_uses_the_operators_transfer(self):
        import handoff
        seen = {}

        def execute(path, actor, p, run, operator=False, recovery=None):
            seen.update(path=path, actor=actor, payload=p, operator=operator)
            return {'reconciled': False, 'task': p.get('task')}

        with mock.patch.object(handoff, 'execute', side_effect=execute):
            answer = self.execute('handoff', payload={'operation_id': 'handoff-1', 'task': 't-1'})
        self.assertEqual(answer['returncode'], 0)
        self.assertEqual((seen['operator'], seen['actor']), (True, ACTOR))
        self.assertEqual(seen['path'], self.root/'projects'/'alpha')


class PrintedCoordinatorLineTests(unittest.TestCase):
    """The client reaches the action by name, so a coordinator never hand-crafts JSON."""

    def test_the_client_dispatches_the_coordinator_action(self):
        import client
        source = (KIT/'client.py').read_text(encoding='utf-8')
        marker = "args[:1] in (['brief']"
        line = next(line for line in source.splitlines() if marker in line)
        self.assertIn("['coordinator']", line)


if __name__ == '__main__':
    unittest.main()
