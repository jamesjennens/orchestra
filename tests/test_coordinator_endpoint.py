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
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import client
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


def request(subcommand, project='alpha', actor=ACTOR, text=None, payload=None, **extra):
    """One request as the shipped client transports it.

    ``client.py`` ``_attachments`` replaces ``--file FILE`` by ONE token ``@attachment:N``
    with the flag kept on the attachment (review of 958e883, item 2), so that is the default
    here. ``two_token=True`` builds the older two-token shape, which is still accepted.
    """
    built = {'project': project, 'actor': actor, 'action': 'coordinator', 'args': [subcommand]}
    body = text if text is not None else (json.dumps(payload) if payload is not None else None)
    if body is not None:
        built['args'] += ['--file', '@attachment:0'] if extra.pop('two_token', False) else ['@attachment:0']
        built['attachments'] = {'0': {'flag': '--file', 'text': body}}
    built.update(extra)
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

    def test_set_onboarding_replaces_a_damaged_previous_document(self):
        # kittrial-5bb.238 item 1 (review of 6f3007a, finding 1): the previous document is read
        # to compute `changed`, and a file that is not UTF-8 answered a bare UnicodeDecodeError,
        # rc 2, nothing written - so the route could not replace a damaged document, which the
        # host command can. An unreadable previous text is treated as changed and replaced, and
        # the answer says so in a sentence.
        (self.project/'ONBOARDING.md').write_bytes(b'\xff\xfe\x00damaged')
        answer = self.ask('set-onboarding', text='Fresh onboarding for alpha.\n')
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        result = json.loads(answer['stdout'])
        self.assertIs(result['changed'], True)
        self.assertEqual((self.project/'ONBOARDING.md').read_bytes(),
                         b'Fresh onboarding for alpha.\n')
        self.assertIn('could not be read', answer['stderr'])
        self.assertNotIn('UnicodeDecodeError', answer['stderr'])
        # The next set of the same text is not changed, and says nothing about the old damage.
        again = self.ask('set-onboarding', text='Fresh onboarding for alpha.\n')
        self.assertEqual(again['returncode'], 0, again['stderr'])
        self.assertIs(json.loads(again['stdout'])['changed'], False)
        self.assertNotIn('could not be read', again['stderr'])

    def test_guidance_clear_with_nothing_set_answers_changed_false_and_writes_nothing(self):
        # Review of 958e883, item 3a: this used to answer rc 124 "outcome unknown" through an
        # AttributeError (version_of(None)), and a caller with no shell cannot reconcile that.
        before = file_tree(self.project)
        answer = self.ask('guidance-clear')
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        result = json.loads(answer['stdout'])
        self.assertIs(result['changed'], False)
        self.assertEqual(result['removed'], [])
        self.assertEqual(file_tree(self.project), before)
        self.assertFalse((self.project/'.guidance-clear.json').exists())
        self.assertNotIn('server_time', answer)

    def test_a_second_clear_writes_nothing_and_appends_no_history(self):
        self.assertIs(json.loads(self.ask('guidance-set', text=GOOD_GUIDANCE)['stdout'])['changed'], True)
        first = json.loads(self.ask('guidance-clear')['stdout'])
        self.assertIs(first['changed'], True)
        before = file_tree(self.project)
        again = json.loads(self.ask('guidance-clear')['stdout'])
        self.assertIs(again['changed'], False)
        self.assertEqual(again['removed'], [])
        self.assertEqual(file_tree(self.project), before)

    def test_set_onboarding_records_who_set_it_and_never_reads_a_path_in_the_text(self):
        # Item 3b: the route must not probe (which resolved and read every absolute *.py path
        # the text named and revealed the resolved symlink target), and it must record WHO set
        # the text, as guidance records set_by.
        secret = self.root/'wrapper-endpoint.py'
        secret.write_text("raise SystemExit('This deployment serves only other projects')\n",
                          encoding='utf-8')
        alias = self.root/'alias-endpoint.py'
        try:
            alias.symlink_to(secret)
        except OSError:
            alias = secret
        text = 'Canonical endpoint: %s\nalias: %s\n' % (secret, alias)
        answer = self.ask('set-onboarding', text=text)
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        result = json.loads(answer['stdout'])
        self.assertEqual(result['set_by'], ACTOR)
        self.assertIs(result['changed'], True)
        self.assertNotIn('WARNING', answer['stderr'])
        self.assertNotIn('serves only', answer['stderr'] + answer['stdout'])
        self.assertNotIn(str(secret.resolve()), answer['stdout'] + answer['stderr'])
        self.assertEqual((self.project/'ONBOARDING.md').read_text(encoding='utf-8'), text)
        self.assertIs(json.loads(self.ask('set-onboarding', text=text)['stdout'])['changed'], False)

    def test_a_non_object_attachment_and_a_non_json_payload_answer_one_sentence(self):
        # Item 3c: a bare AttributeError / JSONDecodeError before.
        broken = request('guidance-set')
        broken['args'] = ['guidance-set', '@attachment:0']
        broken['attachments'] = ['not', 'an', 'object']
        with self.assertRaises(ValueError) as refusal:
            endpoint.execute(self.root, broken, key_principal=PRINCIPAL, key_projects=['alpha'])
        self.assertIn('Invalid attachment', str(refusal.exception))
        with self.assertRaises(ValueError) as refusal:
            self.ask('reference-apply', text='{not json')
        self.assertIn('not valid JSON', str(refusal.exception))
        self.assertNotIn('JSONDecodeError', str(refusal.exception))

    def test_the_two_token_shape_a_hand_built_request_uses_is_still_accepted(self):
        # The older `--file @attachment:N` form stays readable for a hand-built request.
        answer = endpoint.execute(self.root, request('guidance-set', text=GOOD_GUIDANCE, two_token=True),
                                  key_principal=PRINCIPAL, key_projects=['alpha'])
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        self.assertTrue((self.project/'GUIDANCE.md').is_file())

    def test_an_attachment_carried_under_another_flag_is_refused(self):
        # The attachment's own flag must be a file flag: a payload smuggled as --body-file or
        # with no flag is refused (mutant M19, "the attachment flag is not checked").
        for flag in ('--body-file', '--design-file', '--force', ''):
            with self.subTest(flag=flag):
                broken = request('guidance-set')
                broken['args'] = ['guidance-set', '@attachment:0']
                broken['attachments'] = {'0': {'flag': flag, 'text': GOOD_GUIDANCE}}
                with self.assertRaises(ValueError) as refusal:
                    endpoint.execute(self.root, broken, key_principal=PRINCIPAL, key_projects=['alpha'])
                self.assertIn('Invalid attachment', str(refusal.exception))
        self.assertFalse((self.project/'GUIDANCE.md').exists())


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

    def test_a_verify_whose_every_item_was_already_recorded_carries_no_server_time(self):
        # kittrial-5bb.238 item 4 (review of 6f3007a, finding 4): the rule is that an answer
        # carries the server's time when the call WROTE. A retry whose every item was already
        # recorded wrote nothing, so it carries none, like the reconciled and already-accepted
        # no-ops.
        import capability_verification

        def verify_batch(payload, actor, run, operators=None, verifiers=None, journal=None, lock=None):
            return {'items': [{'key': 'c-1', 'result': 'already-recorded'},
                              {'key': 'c-2', 'result': 'already-recorded'}],
                    'recorded': 2, 'refused': 0}

        with mock.patch.object(capability_verification, 'verify_batch', side_effect=verify_batch):
            answer = self.execute('capability-verify',
                                  payload={'schema_version': 1, 'items': [{'key': 'c-1'}]},
                                  actor=VERIFIER)
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        self.assertNotIn('server_time', answer)
        # One item really recorded: the call wrote, so it carries the time.
        def mixed(payload, actor, run, operators=None, verifiers=None, journal=None, lock=None):
            return {'items': [{'key': 'c-1', 'result': 'recorded'},
                              {'key': 'c-2', 'result': 'already-recorded'}],
                    'recorded': 2, 'refused': 0}

        with mock.patch.object(capability_verification, 'verify_batch', side_effect=mixed):
            answer = self.execute('capability-verify',
                                  payload={'schema_version': 1, 'items': [{'key': 'c-1'}]},
                                  actor=VERIFIER)
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        self.assertIn('server_time', answer)

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


@POSIX
class ApplyOperationAllowlistTests(unittest.TestCase):
    """Only the payload operations this surface is meant to carry pass (review of 958e883, item 1).

    `coordinator capability-apply` with ``operation: retire`` reached
    ``apply_native(operator=True)`` and superseded an accepted capability. The check is now on
    the payload, before the library, for the single form and the ``items`` batch alike.
    ``accept`` is the only operation left (owner decision of 2026-10-09 on kittrial-5bb.238):
    ``draft`` is refused with its own sentence, because through this route a new record is
    proposed first and then accepted.
    """

    #: Every operation the route must refuse, including '' (a present but empty value).
    REFUSED = ('retire', 'propose', 'revise', 'void', 'revert', 'reconcile', 'settings',
               'incorporated', 'acceptance', '', 'ACCEPT', 'Draft')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(self.tmp.name, 'alpha')
        deployment(self.root)
        registry(self.root/'projects'/'alpha', {ACTOR: PRINCIPAL})

    def execute(self, subcommand, payload):
        return endpoint.execute(self.root, request(subcommand, payload=payload),
                                key_principal=PRINCIPAL, key_projects=['alpha'])

    def refused(self, subcommand, payload):
        import capability_records
        import reference_records
        called = AssertionError('the library was called for a refused operation')
        before = file_tree(self.root)
        with mock.patch.object(capability_records, 'apply_native', side_effect=called), \
                mock.patch.object(capability_records, 'apply_batch', side_effect=called), \
                mock.patch.object(reference_records, 'apply_native', side_effect=called), \
                mock.patch.object(reference_records, 'apply_batch', side_effect=called):
            with self.assertRaises(ValueError) as refusal:
                self.execute(subcommand, payload)
        self.assertEqual(file_tree(self.root), before)
        return str(refusal.exception)

    def test_capability_apply_refuses_retire_and_every_other_operation(self):
        single = {'schema_version': 1, 'operation': 'retire', 'key': 'c.s1.old', 'revision': 2,
                  'record_sha256': 'a'*64, 'successor': 'c.s1.new', 'acceptance_state': 'superseded',
                  'acceptance': {'decision_id': 'decision-1', 'owners': ['account:u-1'],
                                 'approvers': ['account:u-1'], 'policy': 'any-owner',
                                 'evidence': 'decision-1'}}
        said = self.refused('capability-apply', single)
        self.assertIn('carries only accept', said)
        self.assertIn('retire', said)
        for operation in self.REFUSED:
            with self.subTest(operation=operation):
                self.assertIn('carries only accept',
                              self.refused('capability-apply', dict(single, operation=operation)))

    def test_reference_apply_refuses_every_operation_it_does_not_carry(self):
        for operation in self.REFUSED:
            with self.subTest(operation=operation):
                self.refused('reference-apply', {'schema_version': 1, 'operation': operation,
                                                 'key': 'r-1'})

    def test_draft_is_refused_single_with_the_propose_first_sentence(self):
        # The owner decision (2026-10-09, comment 01a1215a): the direct accepted revision 1 is
        # NOT carried by this route. The sentence names the two ways a record is made and says
        # nothing was written; it must never read as one of the other refusals.
        for command in ('capability-apply', 'reference-apply'):
            with self.subTest(command=command):
                said = self.refused(command, {'schema_version': 1, 'operation': 'draft',
                                              'key': 'c-1'})
                self.assertIn('proposed first', said)
                self.assertIn('capability propose / ref propose', said)
                self.assertIn('installation operator on the host', said)
                self.assertIn('operation draft', said)
                self.assertIn('Nothing was written', said)
                self.assertNotIn('carries only accept', said)

    def test_a_draft_beside_a_valid_item_refuses_the_whole_batch(self):
        # The owner decision: draft anywhere in a batch refuses the batch, whichever place it
        # holds, so a valid item beside it is NOT applied (nothing is written at all). A batch
        # item carries no `operation` of its own, so the valid item is a bare item.
        for command in ('capability-apply', 'reference-apply'):
            for index in (0, 1):
                with self.subTest(command=command, index=index):
                    items = [{'key': 'c-1'}, {'key': 'c-2'}]
                    items[index] = {'key': 'c-%d' % (index + 1), 'operation': 'draft'}
                    said = self.refused(command, {'schema_version': 1, 'operation_id': 'batch-draft',
                                                  'items': items})
                    self.assertIn('items[%d]' % index, said)
                    self.assertIn('proposed first', said)

    def test_an_items_batch_refuses_a_stray_operation_field(self):
        batch = {'schema_version': 1, 'operation_id': 'batch-1', 'items': [{'key': 'c-1'}],
                 'acceptance_state': 'accepted', 'acceptance': {}}
        for command in ('capability-apply', 'reference-apply'):
            with self.subTest(command=command):
                said = self.refused(command, dict(batch, items=[{'key': 'c-1', 'operation': 'retire'}]))
                self.assertIn('items[0]', said)
                self.assertIn('retire', said)
                self.refused(command, dict(batch, operation='retire'))
                # An item that repeats the batch's own acceptance is refused too: the field
                # belongs to the batch, not to an item (the behaviour on main, kept).
                self.assertIn('no operation of its own',
                              self.refused(command, dict(batch, items=[{'key': 'c-1',
                                                                        'operation': 'accept'}])))

    def test_accept_reaches_the_library(self):
        import capability_records
        import reference_records
        seen = []

        def apply_native(payload, actor, run, project, operator=False, operators=None):
            seen.append(payload.get('operation'))
            return {'state': 'accepted'}

        for command, module in (('capability-apply', capability_records),
                                ('reference-apply', reference_records)):
            with self.subTest(command=command):
                with mock.patch.object(module, 'apply_native', side_effect=apply_native):
                    answer = self.execute(command, {'schema_version': 1, 'operation': 'accept',
                                                    'key': 'k-1'})
                self.assertEqual(answer['returncode'], 0, answer['stderr'])
        self.assertEqual(seen, ['accept', 'accept'])


@POSIX
class MutantCoverageTests(unittest.TestCase):
    """The five mutants that survived the rev-1 review (out/mutants.log M9/M20/M14/M15/M11)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(self.tmp.name, 'alpha', 'beta')
        deployment(self.root, operators=(ACTOR,), verifiers=(VERIFIER,))
        registry(self.root/'projects'/'alpha', {ACTOR: PRINCIPAL, HELPER: PRINCIPAL, VERIFIER: PRINCIPAL})
        registry(self.root/'projects'/'beta', {ACTOR: PRINCIPAL})

    def refused(self, built, said=None, **authority):
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

    def test_m9_the_verifiers_list_admits_only_capability_verify(self):
        deployment(self.root, operators=('somebody-else',), verifiers=(VERIFIER,))
        for subcommand in ('guidance-set', 'set-onboarding', 'handoff', 'guidance-status',
                           'guidance-clear', 'reference-apply', 'capability-apply',
                           'proposal-review', 'proposal-decide'):
            with self.subTest(subcommand=subcommand):
                self.refused(request(subcommand, actor=VERIFIER, text='x'),
                             'not a server-side configured operator',
                             key_principal=PRINCIPAL, key_projects=['alpha'])
        said = self.refused(request('capability-verify', actor=VERIFIER,
                                    payload={'schema_version': 1, 'items': []}),
                            key_principal=PRINCIPAL, key_projects=['alpha'])
        self.assertNotIn('not a server-side configured operator', said)

    def test_m20_the_operator_list_is_never_taken_from_the_request(self):
        deployment(self.root, operators=('somebody-else',))
        built = request('guidance-status', actor=HELPER)
        built['operators'] = [HELPER]
        built['operator'] = HELPER
        built['owners'] = [HELPER]
        self.refused(built, 'not a server-side configured operator',
                     key_principal=PRINCIPAL, key_projects=['alpha'])

    def test_m14_rule_2_holds_for_guidance_set_too(self):
        registry(self.root/'projects'/'alpha', {ACTOR: OTHER})
        self.refused(request('guidance-set', text=GOOD_GUIDANCE),
                     'may act only as actors that principal registered',
                     key_principal=PRINCIPAL, key_projects=['alpha'])

    def test_m15_rule_1_holds_for_handoff_too(self):
        self.refused(request('handoff', project='alpha', payload={'operation_id': 'h-1'}),
                     'Unknown/uninitialized project',
                     key_principal=PRINCIPAL, key_projects=['beta'])

    def test_m11_the_server_never_reads_a_path_named_in_the_request(self):
        canary = self.root/'serves-only.py'
        canary.write_text("raise SystemExit('This deployment serves only other projects')\n",
                          encoding='utf-8')
        for args in (['guidance-set', str(canary)], ['guidance-set', '--file', str(canary)],
                     ['set-onboarding', str(canary)]):
            with self.subTest(args=args):
                built = {'project': 'alpha', 'actor': ACTOR, 'action': 'coordinator', 'args': args}
                before = file_tree(self.root)
                with mock.patch.object(endpoint.native, 'run',
                                       side_effect=AssertionError('native ran')):
                    with self.assertRaises(ValueError) as refusal:
                        endpoint.execute(self.root, built, key_principal=PRINCIPAL,
                                         key_projects=['alpha'])
                self.assertIn('Invalid attachment', str(refusal.exception))
                self.assertEqual(file_tree(self.root), before)
        self.assertFalse((self.root/'projects'/'alpha'/'GUIDANCE.md').exists())
        self.assertFalse((self.root/'projects'/'alpha'/'ONBOARDING.md').exists())


@POSIX
class RoundTwoMutantCoverageTests(unittest.TestCase):
    """The four mutants the second review of kittrial-5bb.195 left alive (README, finding 2).

    N2 the operation allowlist is checked on the FIRST batch item only; N6 ``set_by`` is taken
    from the request; N14 ``guidance-status`` takes no lock; N19 an attachment the token did not
    name is used (the first one wins). One test each, through the real endpoint.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(self.tmp.name, 'alpha')
        deployment(self.root)
        registry(self.root/'projects'/'alpha', {ACTOR: PRINCIPAL})
        self.project = self.root/'projects'/'alpha'

    def ask(self, built):
        return endpoint.execute(self.root, built, key_principal=PRINCIPAL, key_projects=['alpha'])

    def refused_before_the_library(self, subcommand, payload):
        import capability_records
        import reference_records
        called = AssertionError('the library was called for a refused operation')
        before = file_tree(self.root)
        with mock.patch.object(capability_records, 'apply_native', side_effect=called), \
                mock.patch.object(capability_records, 'apply_batch', side_effect=called), \
                mock.patch.object(reference_records, 'apply_native', side_effect=called), \
                mock.patch.object(reference_records, 'apply_batch', side_effect=called):
            with self.assertRaises(ValueError) as refusal:
                self.ask(request(subcommand, payload=payload))
        self.assertEqual(file_tree(self.root), before)
        return str(refusal.exception)

    def test_n2_the_allowlist_is_checked_on_every_batch_item_not_only_the_first(self):
        # The only rev-2 batch test had one item, so a check that stopped after items[0]
        # survived. Item 0 is a plain acceptance and item 1 carries the operation.
        batch = {'schema_version': 1, 'operation_id': 'batch-n2', 'items': [],
                 'acceptance_state': 'accepted', 'acceptance': {}}
        for command in ('capability-apply', 'reference-apply'):
            for operation in ('retire', 'draft'):
                with self.subTest(command=command, operation=operation):
                    items = [{'key': 'c-1'}, {'key': 'c-2', 'operation': operation}]
                    said = self.refused_before_the_library(command, dict(batch, items=items))
                    self.assertIn('items[1]', said)
                    self.assertIn(operation, said)
        # And a batch whose first item carries it is still refused at items[0].
        said = self.refused_before_the_library(
            'capability-apply', dict(batch, items=[{'key': 'c-1', 'operation': 'retire'},
                                                   {'key': 'c-2'}]))
        self.assertIn('items[0]', said)

    def test_n6_set_by_is_the_servers_actor_and_never_the_requests(self):
        built = request('set-onboarding', text='Onboarding for alpha.\n')
        built['set_by'] = 'somebody-else'
        built['set_at'] = '1999-01-01T00:00:00Z'
        built['actor_field'] = 'somebody-else'
        built['attachments']['0']['set_by'] = 'somebody-else'
        answer = self.ask(built)
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        result = json.loads(answer['stdout'])
        self.assertEqual(result['set_by'], ACTOR)
        self.assertNotIn('somebody-else', json.dumps(result))
        self.assertEqual((self.project/'ONBOARDING.md').read_text(encoding='utf-8'),
                         'Onboarding for alpha.\n')

    def test_n14_guidance_status_takes_the_project_coordination_lock(self):
        # The host `admin.py guidance-status` takes the lock so the answer is one snapshot,
        # not a read racing a set; the route must take it too.
        before = file_tree(self.project)
        with mock.patch.object(endpoint.fcntl, 'flock') as flock:
            answer = self.ask(request('guidance-status'))
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        self.assertEqual(json.loads(answer['stdout'])['schema_version'], 1)
        self.assertEqual(flock.call_count, 1)
        self.assertEqual(flock.call_args.args[1], endpoint.fcntl.LOCK_EX)
        self.assertEqual(file_tree(self.project), before)

    def test_n19_only_the_attachment_the_token_names_is_used(self):
        decoy = 'Text the token does not name.\n'
        named = 'Text the token names.\n'
        built = request('guidance-set')
        built['args'] = ['guidance-set', '@attachment:1']
        built['attachments'] = {'0': {'flag': '--file', 'text': decoy},
                                '1': {'flag': '--file', 'text': named}}
        answer = self.ask(built)
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        self.assertEqual((self.project/'GUIDANCE.md').read_text(encoding='utf-8'), named)
        # The other token picks the other attachment: the token is the only key used.
        built['args'] = ['guidance-set', '@attachment:0']
        answer = self.ask(built)
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        self.assertEqual((self.project/'GUIDANCE.md').read_text(encoding='utf-8'), decoy)


@POSIX
class ClientWireShapeTests(unittest.TestCase):
    """What the shipped client sends reaches every family (review of 958e883, item 2).

    ``client._wire`` builds the request the way the CLI does: ``--file FILE`` becomes ONE
    ``@attachment:N`` token. Each family is driven through it into ``endpoint.execute`` with
    the library replaced, so the payload the endpoint decodes is pinned.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(self.tmp.name, 'alpha')
        deployment(self.root, operators=(ACTOR,), verifiers=(VERIFIER,))
        registry(self.root/'projects'/'alpha', {ACTOR: PRINCIPAL, VERIFIER: PRINCIPAL})

    def wire(self, subcommand, body, actor=ACTOR):
        payload = self.root/('payload-%s.txt' % subcommand)
        payload.write_text(body, encoding='utf-8')
        return client._wire('alpha', actor, [subcommand, '--file', str(payload)],
                            'coordinator', None)

    def carry(self, wire, **authority):
        return endpoint.execute(self.root, json.loads(wire), key_principal=PRINCIPAL,
                                key_projects=['alpha'], **authority)

    def test_the_client_wire_shape_reaches_every_family(self):
        import capability_records
        import capability_verification
        import handoff
        import proposal_records
        import reference_records
        seen = {}

        def ref_native(payload, actor, run, project, operator=False, operators=None):
            seen['reference-apply'] = payload
            return {'state': 'accepted'}

        def cap_native(payload, actor, run, project, operator=False, operators=None):
            seen['capability-apply'] = payload
            return {'state': 'accepted'}

        def verify(payload, actor, run, operators=None, verifiers=None, journal=None, lock=None):
            seen['capability-verify'] = payload
            return {'items': [], 'recorded': 0, 'refused': 0}

        def dispose(payload, actor, run, project, operators=None, route='review', http=None):
            seen['proposal-' + route] = payload
            return {'state': 'under-review'}

        def handed(path, actor, payload, run, operator=False, recovery=None):
            seen['handoff'] = payload
            return {'reconciled': False}

        with mock.patch.object(reference_records, 'apply_native', side_effect=ref_native), \
                mock.patch.object(capability_records, 'apply_native', side_effect=cap_native), \
                mock.patch.object(capability_verification, 'verify_batch', side_effect=verify), \
                mock.patch.object(proposal_records, 'dispose', side_effect=dispose), \
                mock.patch.object(handoff, 'execute', side_effect=handed):
            self.assertEqual(self.carry(self.wire('guidance-set', GOOD_GUIDANCE))['returncode'], 0)
            self.assertEqual((self.root/'projects'/'alpha'/'GUIDANCE.md').read_text(encoding='utf-8'),
                             GOOD_GUIDANCE)
            self.assertEqual(self.carry(self.wire('set-onboarding', 'Onboarding.\n'))['returncode'], 0)
            bodies = {
                'reference-apply': {'schema_version': 1, 'operation': 'accept', 'key': 'r-1'},
                'capability-apply': {'schema_version': 1, 'operation': 'accept', 'key': 'c-1'},
                'capability-verify': {'schema_version': 1, 'items': [{'key': 'c-1'}]},
                'proposal-review': {'schema_version': 1, 'key': 'p-1'},
                'proposal-decide': {'schema_version': 1, 'key': 'p-1'},
                'handoff': {'operation_id': 'h-1', 'task': 't-1'},
            }
            for subcommand, body in bodies.items():
                with self.subTest(subcommand=subcommand):
                    actor = VERIFIER if subcommand == 'capability-verify' else ACTOR
                    answer = self.carry(self.wire(subcommand, json.dumps(body), actor=actor))
                    self.assertEqual(answer['returncode'], 0, answer['stderr'])
        self.assertEqual(seen['reference-apply']['key'], 'r-1')
        self.assertEqual(seen['capability-apply']['key'], 'c-1')
        self.assertEqual(seen['capability-verify']['items'], [{'key': 'c-1'}])
        self.assertEqual(seen['proposal-review']['key'], 'p-1')
        self.assertEqual(seen['proposal-decide']['key'], 'p-1')
        self.assertEqual(seen['handoff']['task'], 't-1')


@POSIX
@unittest.skipUnless(os.name == 'posix', 'the wrapper replaces itself with os.execvpe')
class ClientThroughTheForcedCommandTests(unittest.TestCase):
    """``client._wire`` -> the real ``ssh_forced_command.py`` -> the real ``endpoint.py``.

    One test per family, the method of the kittrial-5bb.194 reviews: the wrapper launches the
    endpoint with the fixed root, the key's projects and its principal, and the endpoint is the
    shipped one, so a wire shape the client never sends would be refused here.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(self.tmp.name, 'alpha')
        deployment(self.root, operators=(ACTOR,), verifiers=(VERIFIER,))
        registry(self.root/'projects'/'alpha', {ACTOR: PRINCIPAL, VERIFIER: PRINCIPAL})

    def wire(self, subcommand, body, actor=ACTOR):
        payload = self.root/('payload-%s.txt' % subcommand)
        payload.write_text(body, encoding='utf-8')
        return client._wire('alpha', actor, [subcommand, '--file', str(payload)],
                            'coordinator', None)

    def through(self, wire):
        target = str(KIT/'endpoint.py')
        line = [sys.executable, str(KIT/'ssh_forced_command.py'), '--root', str(self.root),
                '--endpoint', target, '--python', sys.executable,
                '--project', 'alpha', '--principal', PRINCIPAL]
        return subprocess.run(line, input=wire, capture_output=True, text=True, encoding='utf-8',
                              env=dict(os.environ, SSH_ORIGINAL_COMMAND=target), timeout=240)

    def envelope(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_the_text_families_are_carried_out_end_to_end(self):
        answer = self.envelope(self.through(self.wire('guidance-set', GOOD_GUIDANCE)))
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        self.assertEqual((self.root/'projects'/'alpha'/'GUIDANCE.md').read_text(encoding='utf-8'),
                         GOOD_GUIDANCE)
        answer = self.envelope(self.through(self.wire('set-onboarding', 'Onboarding.\n')))
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        self.assertEqual(json.loads(answer['stdout'])['set_by'], ACTOR)

    def test_every_json_family_gets_past_the_attachment_shape(self):
        # Before the fix each of these answered rc 2 "Use --file with a local JSON or text
        # file; the server reads no file path" through the shipped client. The library then
        # refuses the deliberately incomplete payload, but never about the attachment shape.
        for subcommand in ('reference-apply', 'capability-apply', 'capability-verify',
                           'proposal-review', 'proposal-decide', 'handoff'):
            with self.subTest(subcommand=subcommand):
                actor = VERIFIER if subcommand == 'capability-verify' else ACTOR
                answer = self.envelope(self.through(
                    self.wire(subcommand, json.dumps({'schema_version': 1}), actor=actor)))
                said = (answer.get('stderr') or '') + (answer.get('stdout') or '')
                self.assertNotIn('Use --file with a local JSON or text file', said)
                self.assertNotIn('Invalid attachment', said)

    def test_a_draft_is_refused_through_the_real_wrapper(self):
        # The owner decision of 2026-10-09, on the path a confined coordinator really uses:
        # the shipped client's wire into the real ssh_forced_command wrapper and the shipped
        # endpoint. Single, then inside a batch (second item), and nothing is written.
        for subcommand in ('capability-apply', 'reference-apply'):
            with self.subTest(subcommand=subcommand):
                request = self.wire(subcommand, json.dumps({'schema_version': 1,
                                                            'operation': 'draft', 'key': 'c-1'}))
                before = file_tree(self.root)
                answer = self.envelope(self.through(request))
                self.assertEqual(answer['returncode'], 2, answer)
                self.assertIn('proposed first', answer['stderr'])
                self.assertIn('installation operator on the host', answer['stderr'])
                self.assertIn('Nothing was written', answer['stderr'])
                self.assertEqual(file_tree(self.root), before)
        request = self.wire('reference-apply', json.dumps(
            {'schema_version': 1, 'operation_id': 'batch-draft',
             'items': [{'key': 'r-1'}, {'key': 'r-2', 'operation': 'draft'}]}))
        before = file_tree(self.root)
        answer = self.envelope(self.through(request))
        self.assertEqual(answer['returncode'], 2, answer)
        self.assertIn('items[1]', answer['stderr'])
        self.assertEqual(file_tree(self.root), before)


if __name__ == '__main__':
    unittest.main()
