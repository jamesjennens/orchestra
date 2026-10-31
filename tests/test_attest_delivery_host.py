"""`admin.py attest-delivery` end to end, and the endpoint's party-map wiring (kittrial-5bb.10).

Item 1 of the review: every documented call without `--operation-id` used to die with
`ValueError: Invalid workflow ID`, because the default id was `attest-delivery/HASH` and
`review_workflow.identity` allows no slash. These tests RUN the command itself, as the docs
show it, on a REAL git bundle file: a verified delivery, a wrong checksum, a commit that is
not in the bundle, a detached-HEAD bundle, a renamed copy, an unlisted caller and the
author.

Item 2: the actor -> principal map is loaded for `attest`/`hold`/`release-hold` whatever the
coordinator rules say, on the endpoint route and in the command.

Item 3: the command authorizes before it reads, refuses a symlink / non-regular file / a file
that is not a git bundle (so it cannot put an arbitrary file's digest into a review chain),
bounds `git`, and refuses a remote delivery because it never contacts the remote.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin
import review_workflow as w
import work
from test_review_verification_holds import Harness, scratch

TASK = 'task-1'
BASE = 'b' * 40
OPERATOR = 'coordinator'
VERIFIER = 'verifier'
AUTHOR = 'worker'
W1B = 'w1b'
W2B = 'w2b'
REVIEWER = 'reviewer'

try:
    import endpoint
except ImportError:                                   # fcntl: the endpoint is POSIX only
    endpoint = None

POSIX = unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')


class AttestDeliveryCase(unittest.TestCase):
    """A runtime root, a deployment and a review chain, driven through ``admin.main``."""

    def setUp(self):
        if shutil.which('git') is None:
            self.skipTest('git is not available')
        # A disposable directory that works under a confined sandbox too (the platform temp
        # area is denied there; see the helper).
        self._scratch = scratch()
        self.root = self._scratch.__enter__()
        self.addCleanup(lambda: self._scratch.__exit__(None, None, None))
        self.project = self.root / 'projects' / 'trial'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.configure(operators=[OPERATOR], verifiers=[VERIFIER, W1B, W2B, AUTHOR])
        self.flock = mock.Mock()
        patcher = mock.patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=self.flock,
                                                                              LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, LOCK_SH=1)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('ORCHESTRA_OPERATORS', 'ORCHESTRA_VERIFIERS'):
            saved = mock.patch.dict(os.environ)
            saved.start()
            self.addCleanup(saved.stop)
            os.environ.pop(name, None)
        self.harness = Harness()
        self.harness.setUp()
        self.rows = self.harness.rows
        self.issue = self.harness.issue

    def configure(self, **values):
        (self.root / 'deployment.private.json').write_text(
            json.dumps(dict({'password': 'x', 'unit': 'none', 'review_workflow_writes': True},
                            **values)), encoding='utf-8')

    def registry(self, owners):
        (self.project / '.sessions.json').write_text(
            json.dumps({'schema_version': 1, 'records': {}, 'owners': dict(owners)}), encoding='utf-8')

    def run_native(self, root, name, args):
        actor = None
        if args[:1] == ['--actor']:
            actor = args[1]
            args = args[2:]
        if args == ['export', '--all']:
            return ''.join(json.dumps(row) + '\n' for row in self.rows)
        if args[:3] == ['comments', 'add', TASK]:
            cid = str(len(self.issue['comments']) + 1)
            self.issue['comments'].append(dict(id=cid, text=args[3], author=actor or 'unknown',
                                               created_at='2026-09-16T00:00:00Z'))
            return json.dumps({'id': cid})
        raise AssertionError('unexpected native command: ' + repr(args))

    def cli(self, *argv, failing=False):
        """Run one admin.py command; returns stdout (a refusal propagates to the caller)."""
        with mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                mock.patch.object(admin, 'root_path', return_value=self.root), \
                mock.patch.object(admin, 'run_bd', side_effect=self.run_native), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        return out.getvalue()

    def git(self, *args, cwd):
        done = subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, 'git %s failed: %s' % (' '.join(args), done.stderr))
        return done.stdout

    def make_bundle(self, name='delivery.bundle', detached=False):
        repo = self.root / ('src-' + name.replace('.', '-'))
        repo.mkdir()
        self.git('init', '-q', cwd=repo)
        self.git('config', 'user.email', 't@example.invalid', cwd=repo)
        self.git('config', 'user.name', 'Test', cwd=repo)
        (repo / 'a.txt').write_text('one\n', encoding='utf-8')
        self.git('add', 'a.txt', cwd=repo)
        self.git('commit', '-q', '-m', 'one', cwd=repo)
        sha = self.git('rev-parse', 'HEAD', cwd=repo).strip()
        bundle = self.root / name
        if detached:
            self.git('checkout', '-q', '--detach', cwd=repo)
            self.git('bundle', 'create', str(bundle), 'HEAD', cwd=repo)
        else:
            branch = self.git('rev-parse', '--abbrev-ref', 'HEAD', cwd=repo).strip()
            self.git('bundle', 'create', str(bundle), branch, cwd=repo)
        return bundle, sha

    def contribute(self, commit, sha256, author=AUTHOR, kind='bundle', path='/deliveries/rev.bundle'):
        """Build a contribution declaring ``commit``/``sha256`` as a listed verifier can see it."""
        self.issue['assignee'] = author
        self.harness.actor = author
        return self.harness.send(self.harness.contribution(commit=commit, path=path, sha256=sha256,
                                                           delivery_kind=kind))['comment_id']

    def attest(self, *extra, actor=VERIFIER, bundle, reason='Read the bytes and the heads'):
        return self.cli('attest-delivery', 'trial', '--actor', actor, '--task', TASK,
                        '--bundle', str(bundle), '--reason', reason, *extra)

    @staticmethod
    def run_with_timeout(function, seconds=20, label='call'):
        """Run ``function()`` with a wall-clock timeout.

        A mutant that opens a fifo WITHOUT ``O_NONBLOCK`` must fail this test by
        assertion, not hang the module and every test after it (review item
        ``a-fifo-hangs``). The work runs on a daemon thread, so a blocked call can be
        abandoned without stopping the run.
        """
        import threading
        box = {}

        def target():
            try:
                box['value'] = function()
            except BaseException as error:              # noqa: BLE001 - ferried to the caller
                box['error'] = error

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(seconds)
        if thread.is_alive():
            raise AssertionError('%s did not answer in %ds: a fifo was opened blocking' % (label, seconds))
        if 'error' in box:
            raise box['error']
        return box.get('value')


class DocumentedCommandTests(AttestDeliveryCase):
    """Item 1: the command works as documented, without --operation-id, on a real bundle."""

    def test_a_matching_bundle_is_verified_as_documented(self):
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest)
        result = json.loads(self.attest(bundle=bundle))
        # No --operation-id: the default id is valid and the write went through.
        self.assertEqual(result['observation']['sha256'], digest)
        self.assertEqual(result['observation']['bytes'], bundle.stat().st_size)
        self.assertEqual(result['observation']['heads'][0]['commit'], sha)
        state = w.project(self.issue)
        self.assertEqual(state['verification'], 'verified')
        self.assertEqual(state['attestation']['author'], VERIFIER)
        self.assertEqual(state['attestation']['sha256'], digest)

    def test_a_large_real_bundle_is_verified_across_hash_blocks(self):
        # kittrial-5bb.272 item 2c, the check no review ran: a real bundle well past one
        # hash block (BUNDLE_BLOCK_BYTES is 1 MiB). Random content does not compress, so
        # the bundle really spans several blocks, and the observation must still count and
        # hash exactly what is on disk - counted here independently, block by block.
        repo = self.root / 'src-large'
        repo.mkdir()
        self.git('init', '-q', cwd=repo)
        self.git('config', 'user.email', 't@example.invalid', cwd=repo)
        self.git('config', 'user.name', 'Test', cwd=repo)
        (repo / 'blob.bin').write_bytes(os.urandom(6 * 1024 * 1024 + 4096))
        self.git('add', 'blob.bin', cwd=repo)
        self.git('commit', '-q', '-m', 'large', cwd=repo)
        sha = self.git('rev-parse', 'HEAD', cwd=repo).strip()
        branch = self.git('rev-parse', '--abbrev-ref', 'HEAD', cwd=repo).strip()
        bundle = self.root / 'large.bundle'
        self.git('bundle', 'create', str(bundle), branch, cwd=repo)
        digest = hashlib.sha256()
        size = 0
        with bundle.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
                size += len(chunk)
        self.assertGreaterEqual(size, 3 * w.BUNDLE_BLOCK_BYTES)   # the block path is the only path
        self.contribute(sha, digest.hexdigest())
        result = json.loads(self.attest(bundle=bundle))
        self.assertEqual((result['observation']['bytes'], result['observation']['sha256']),
                         (size, digest.hexdigest()))
        self.assertEqual(result['observation']['heads'][0]['commit'], sha)
        self.assertEqual(w.project(self.issue)['verification'], 'verified')

    def test_an_account_id_is_refused_by_the_verifiers_list_write(self):
        # kittrial-5bb.272 review item 2: pinning an account id on the verifiers list
        # as accepted behavior kept a misconfiguration silent. The operator allowlist
        # has refused these since kittrial-5bb.70; the verifier list refuses them too
        # now. The read path stays exact-match (a hand-planted id authorizes only
        # itself), but the write never creates that state.
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest)
        deployment = self.root / 'deployment.private.json'
        before = deployment.read_text(encoding='utf-8')
        # An exact account id (usr_ + 16 hex, the web service's own shape): refused.
        with self.assertRaisesRegex(ValueError, 'shape of an HTTP account or agent id'):
            self.cli('verifiers', 'add', 'usr_4f7c1d2e3a4b5c6d', '--actor', OPERATOR,
                     '--reason', 'a mistaken listing')
        self.assertEqual(deployment.read_text(encoding='utf-8'), before)
        # An agent label of an account id is refused before that: the identity rule
        # admits no slash, so it never reaches the list write at all.
        with self.assertRaisesRegex(ValueError, 'Invalid verifier identity|shape of an HTTP account or agent id'):
            self.cli('verifiers', 'add', 'usr_4f7c1d2e3a4b5c6d/heron', '--actor', OPERATOR,
                     '--reason', 'a mistaken listing')
        self.assertEqual(deployment.read_text(encoding='utf-8'), before)
        # A near miss is an ordinary name: 15 digits, or not hex at all.
        self.cli('verifiers', 'add', 'usr_fred', '--actor', OPERATOR, '--reason', 'a person named fred')
        self.assertIn('usr_fred', json.loads(deployment.read_text(encoding='utf-8'))['verifiers'])
        # And the read half the reviewer kept: a hand-planted exact id still authorizes
        # only itself - a real actor is refused - so removing it is the only fix.
        self.configure(operators=[OPERATOR], verifiers=['usr_4f7c1d2e3a4b5c6d'])
        with self.assertRaisesRegex(ValueError, 'only a listed verifier or a listed operator'):
            self.attest(actor=VERIFIER, bundle=bundle)

    def test_a_wrong_checksum_cannot_be_recorded_as_verified(self):
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, 'e' * 64)                      # the declaration differs from the file
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'not the declared delivery checksum'):
            self.attest(bundle=bundle)
        self.assertEqual(len(self.issue['comments']), before)
        self.assertNotIn('attestation', w.project(self.issue))
        self.assertNotEqual(digest, 'e' * 64)

    def test_a_commit_that_is_not_in_the_bundle_cannot_be_verified(self):
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest)
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'not one of the heads this bundle advertises'):
            self.attest('--commit', 'deadbeef' + '0' * 32, bundle=bundle)
        self.assertEqual(len(self.issue['comments']), before)

    def test_a_detached_head_bundle_is_verified(self):
        bundle, sha = self.make_bundle(name='detached.bundle', detached=True)
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest)
        result = json.loads(self.attest(bundle=bundle))
        self.assertEqual(result['observation']['heads'], [{'commit': sha, 'ref': 'HEAD'}])
        self.assertEqual(w.project(self.issue)['verification'], 'verified')

    def test_a_renamed_copy_of_the_bundle_is_verified(self):
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        renamed = self.root / 'revision-2-renamed.bundle'
        renamed.write_bytes(bundle.read_bytes())
        self.contribute(sha, digest, path='/deliveries/renamed.bundle')
        self.attest(bundle=renamed)
        self.assertEqual(w.project(self.issue)['verification'], 'verified')

    def test_an_unlisted_caller_is_refused_before_the_file_is_read(self):
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest)
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'only a listed verifier or a listed operator'):
            self.attest(actor='stranger', bundle=bundle)
        self.assertEqual(len(self.issue['comments']), before)

    def test_the_author_is_refused_even_when_listed_as_a_verifier(self):
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        # The contribution's author IS the listed verifier AUTHOR.
        self.contribute(sha, digest, author=AUTHOR)
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, "author's party"):
            self.attest(actor=AUTHOR, bundle=bundle)
        self.assertEqual(len(self.issue['comments']), before)

    def test_a_declared_remote_delivery_cannot_be_verified_here(self):
        bundle, sha = self.make_bundle()
        self.contribute(sha, 'e' * 64, kind='remote')
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'never contacts the remote'):
            self.attest(bundle=bundle)
        self.assertEqual(len(self.issue['comments']), before)


class HostBoundsTests(AttestDeliveryCase):
    """Item 3: the command refuses what it must not read, and bounds git."""

    def test_a_file_that_is_not_a_git_bundle_is_refused(self):
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest)
        text = self.root / 'plain.txt'
        text.write_bytes(b'the digest and size of this file must never enter a review chain\n')
        with self.assertRaisesRegex(ValueError, 'not a git bundle'):
            self.attest(bundle=text)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs are POSIX-only')
    def test_a_fifo_bundle_is_refused_without_blocking_the_command(self):
        # Review item `a-fifo-hangs`: the observation open used O_RDONLY without
        # O_NONBLOCK, so `attest-delivery --bundle <fifo>` never returned. Run the REAL
        # command on a fifo under a wall-clock timeout and require the refusal sentence;
        # the mutant without O_NONBLOCK dies by assertion here instead of hanging.
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest)
        fifo = self.root / 'planted.bundle'
        os.mkfifo(str(fifo))
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            self.run_with_timeout(lambda: self.attest(bundle=fifo), label='attest-delivery on a fifo')
        self.assertEqual(len(self.issue['comments']), before)

    def test_a_symlink_to_a_file_is_refused(self):
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest)
        link = self.root / 'link.bundle'
        try:
            link.symlink_to(bundle)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are unavailable')
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'not be a symlink'):
            self.attest(bundle=link)
        self.assertEqual(len(self.issue['comments']), before)

    def test_the_git_call_is_bounded_and_a_timeout_is_reported(self):
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest)
        with mock.patch.object(subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('git', 60)):
            with self.assertRaisesRegex(ValueError, 'timed out after 60 seconds'):
                self.attest('--commit', sha, bundle=bundle)

    def test_the_party_map_is_read_with_every_coordinator_rule_off(self):
        # w1b is a listed verifier adopted into the SAME principal as the author, and the
        # deployment turns no coordinator rule on. The command must still refuse it.
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest, author=AUTHOR)
        self.registry({AUTHOR: 'lane:w1', W1B: 'lane:w1', W2B: 'lane:w2'})
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, "author's party"):
            self.attest(actor=W1B, bundle=bundle)
        self.assertEqual(len(self.issue['comments']), before)
        # A verifier of another principal is accepted, and the answer carries no
        # "parties could not be compared" warning.
        result = json.loads(self.attest(actor=W2B, bundle=bundle))
        self.assertFalse([line for line in result.get('warnings') or [] if 'parties' in line])
        self.assertEqual(w.project(self.issue)['verification'], 'verified')


class DefaultIdAndRetryTests(AttestDeliveryCase):
    """Item 2(1): the default id names the actor, and a same-actor rerun reconciles.

    The default operation id is derived from the observation, so a rerun by the same
    verifier rebuilds the same id; but the host command derives ``previous`` from the
    chain head, so the rerun's payload names the record the first run wrote. It must
    reconcile to that record, not answer "Operation ID already used with different
    payload or actor". A second listed verifier must get its own id, because the
    default id names the acting verifier.
    """

    def test_an_exact_rerun_by_the_same_verifier_reconciles(self):
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest)
        first = json.loads(self.attest(bundle=bundle))
        before = len(self.issue['comments'])
        second = json.loads(self.attest(bundle=bundle))
        self.assertTrue(second['reconciled'])
        self.assertEqual(second['comment_id'], first['comment_id'])
        self.assertEqual(len(self.issue['comments']), before)
        self.assertEqual(w.project(self.issue)['verification'], 'verified')

    def test_a_second_listed_verifier_gets_its_own_default_id(self):
        bundle, sha = self.make_bundle()
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.contribute(sha, digest)
        first = json.loads(self.attest(actor=VERIFIER, bundle=bundle))
        second = json.loads(self.attest(actor=W2B, bundle=bundle))
        self.assertFalse(second.get('reconciled', False))
        self.assertNotEqual(second['comment_id'], first['comment_id'])
        state = w.project(self.issue)
        self.assertEqual(state['verification'], 'verified')
        self.assertEqual(state['attestation']['author'], W2B)


@POSIX
class EndpointPartyMapTests(unittest.TestCase):
    """Item 2: the endpoint supplies the party map for a review action with all rules off."""

    def setUp(self):
        self._scratch = scratch()
        self.root = self._scratch.__enter__()
        self.addCleanup(lambda: self._scratch.__exit__(None, None, None))
        self.project = self.root / 'projects' / 'alpha'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': [OPERATOR], 'verifiers': [VERIFIER, 'w1b', 'w2b'],
                        'password': 'x', 'review_workflow_writes': True}), encoding='utf-8')
        (self.project / '.sessions.json').write_text(
            json.dumps({'schema_version': 1, 'records': {},
                        'owners': {AUTHOR: 'lane:w1', 'w1b': 'lane:w1', 'w2b': 'lane:w2'}}),
            encoding='utf-8')

    def capture_owners(self, action):
        """Drive one endpoint action with work.execute spied on; return the kwargs it saw."""
        seen = {}

        def spy(path, actor, action, args, attachments, runner, **kwargs):
            seen.update(kwargs)
            return {'comment_id': '0', 'review_state': 'none', 'workflow_state': 'none',
                    'integration': {}}

        request = {'project': 'alpha', 'actor': OPERATOR, 'action': action,
                   'args': [TASK, '@attachment:0'],
                   'attachments': {'0': {'flag': '--file',
                                         'text': json.dumps({'operation': 'attest'})}}}
        with mock.patch.object(work, 'execute', side_effect=spy), \
                mock.patch.object(endpoint, 'guarded_write',
                                  side_effect=lambda root, request, journal, effect, **kw: effect()):
            endpoint.execute(self.root, request)
        return seen

    def test_a_review_action_carries_the_party_map_with_all_rules_off(self):
        seen = self.capture_owners('review')
        self.assertIsNotNone(seen.get('owners'),
                             'the endpoint must supply the party map for a review action with all rules off')
        self.assertEqual(seen['owners'](), {AUTHOR: 'lane:w1', 'w1b': 'lane:w1', 'w2b': 'lane:w2'})

    def test_another_action_carries_no_map_with_all_rules_off(self):
        self.assertIsNone(self.capture_owners('work').get('owners'))

    def test_the_captured_map_refuses_the_author_party_and_accepts_another(self):
        owners = self.capture_owners('review')['owners']
        h = Harness()
        h.setUp()
        contribution = h.send(h.contribution())['comment_id']
        with self.assertRaisesRegex(w.CoordinatorRuleRefusal, "author's party"):
            h.send(h.attest(contribution, sha256='c' * 64, bytes=10), 'w1b',
                   verifiers=(VERIFIER, 'w1b', 'w2b'), owners=owners)
        h.send(h.attest(contribution, sha256='c' * 64, bytes=10), 'w2b',
               verifiers=(VERIFIER, 'w1b', 'w2b'), owners=owners)
        self.assertEqual(w.project(h.issue)['verification'], 'verified')


if __name__ == '__main__':
    unittest.main()
