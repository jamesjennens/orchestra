"""Verifier attestations and explicit integration holds (kittrial-5bb.10).

The acceptance criteria this file pins:

* a wrong checksum, a wrong base, or a commit that is not the delivered tip can never be
  recorded as ``verified``;
* an inaccessible remote is an explicit ``unknown`` with its reason, never a fabricated
  pass;
* a held contribution can never appear integration-ready (``review_state == 'held'``,
  never ``awaiting-integration``, in ``review``, ``brief`` and ``work`` alike);
* an exact retry and a new contribution revision cannot accidentally clear a hold;
* a detached-HEAD bundle (a bundle whose only ref is the ``HEAD`` pseudo-ref) and a
  renamed/revised bundle import and verify through the documented commands, and the
  checksum rule follows the DECLARED delivery, not the file name.

The reader tests build the chain through ``review_workflow.execute`` against a stub native
runner, exactly as the other review-workflow tests do. The bundle tests use real ``git`` in
a scratch directory and skip when git is unavailable.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle
import review_state
import review_workflow as w
import work

TASK = 'task-1'
COMMIT_1 = 'a' * 40
COMMIT_2 = 'f' * 40
BASE = 'b' * 40
SHA_1 = 'c' * 64
SHA_2 = 'd' * 64
OPERATOR = 'coordinator'


def row(comments=None, assignee='worker', status='in_progress'):
    return dict(id=TASK, title='A task', issue_type='task', status=status,
                assignee=assignee, description='Intent', acceptance_criteria='Acceptance',
                labels=[], comments=list(comments or []))


class Harness(unittest.TestCase):
    def setUp(self):
        self.issue = row()
        self.rows = [self.issue]
        self.actor = 'worker'
        self.count = 0

    def payload(self, op, **extra):
        self.count += 1
        return dict(schema_version=1, operation=op, operation_id='op-' + str(self.count),
                    task=TASK, previous=w.project(self.issue)['latest_comment_id'], **extra)

    def contribution(self, commit=COMMIT_1, path='/deliveries/revision-1.bundle',
                     sha256=SHA_1, delivery_kind='bundle', **extra):
        prior = w.project(self.issue)['contribution']
        if delivery_kind == 'remote':
            delivery = dict(kind='remote', remote='ssh://git.example/project', branch='contrib/x')
        else:
            delivery = dict(kind='bundle', path='reviewer:' + path, sha256=sha256)
        p = dict(repository='ssh://git.example/project', commit=commit, base_commit=BASE,
                 delivery=delivery, summary='Implementation and test evidence',
                 supersedes=prior['comment_id'] if prior else None)
        p.update(extra)
        return self.payload('contribute', **p)

    def attest(self, contribution, verdict='verified', commit=COMMIT_1, base_commit=BASE,
               method='bundle-sha256 + git rev-parse of the delivered tip', reason='Checked',
               **extra):
        return self.payload('attest', contribution=contribution, verdict=verdict,
                            commit=commit, base_commit=base_commit, method=method,
                            reason=reason, **extra)

    def hold(self, contribution, reason='Checksum reviewed by hand; not release-ready'):
        return self.payload('hold', contribution=contribution, reason=reason)

    def release_hold(self, contribution, hold_id, reason='Hold cleared after re-verification'):
        return self.payload('release-hold', contribution=contribution, hold=hold_id,
                            reason=reason)

    def run_native(self, args):
        if args == ['export', '--all']:
            return ''.join(json.dumps(r) + '\n' for r in self.rows)
        self.assertEqual(args[:3], ['comments', 'add', TASK])
        cid = str(len(self.issue['comments']) + 1)
        self.issue['comments'].append(dict(id=cid, text=args[3], author=self.actor,
                                           created_at='2026-09-16T00:00:00Z'))
        return json.dumps({'id': cid})

    def send(self, p, actor=None, operators=(OPERATOR,), review_writes=True,
             verified_web_authority=False):
        # An integration hold is a coordinator control, so those two operations default to
        # the configured operator; every other operation defaults to the contributing worker.
        if actor is None:
            actor = OPERATOR if p.get('operation') in ('hold', 'release-hold') else 'worker'
        self.actor = actor
        return w.execute(self.rows, TASK, actor, p, self.run_native, operators=list(operators),
                         review_writes=review_writes,
                         verified_web_authority=verified_web_authority)

    def review_count(self):
        return len([c for c in self.issue['comments'] if c['text'].startswith(w.PREFIX)])

    def stored(self, p, author='worker', created_at='2026-09-16T00:00:00Z'):
        """Plant one already-written record directly (an older kit's own write)."""
        cid = str(len(self.issue['comments']) + 1)
        self.issue['comments'].append(dict(id=cid, text=w.PREFIX + w.canonical_bytes(p).decode(),
                                           author=author, created_at=created_at))
        return cid

    def approved(self, **contribution_extra):
        """A contributed and independently approved revision."""
        contribution = self.send(self.contribution(**contribution_extra))['comment_id']
        self.send(self.payload('approve', contribution=contribution, summary='Approved'),
                  'reviewer')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        return contribution

    def shared(self):
        from review_state import project as reviewed, scopes_for
        return reviewed(self.issue, scopes_for(self.rows, TASK))


class VerifiedAttestationTests(Harness):
    def test_a_chain_without_the_new_records_adds_no_keys(self):
        # A chain written before these operations existed must read exactly as it did
        # before them, so the additive keys are absent rather than null/empty.
        self.send(self.contribution())
        state = w.project(self.issue)
        for key in ('attestation', 'attestations', 'verification', 'holds', 'hold_releases',
                    'ignored_hold_releases'):
            with self.subTest(key=key):
                self.assertNotIn(key, state)

    def test_a_matching_bundle_observation_is_recorded_as_verified(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.attest(contribution, sha256=SHA_1, bytes=4096), OPERATOR)
        state = w.project(self.issue)
        self.assertEqual(state['verification'], 'verified')
        self.assertTrue(state['attestation']['verified'])
        self.assertEqual(state['attestation']['problems'], [])
        self.assertEqual(state['attestation']['sha256'], SHA_1)
        self.assertEqual(state['attestation']['bytes'], 4096)
        self.assertEqual(state['attestation']['author'], OPERATOR)
        self.assertEqual(state['attestation']['commit'], COMMIT_1)
        # The declared delivery is untouched: the attestation is a SEPARATE record.
        self.assertEqual(state['contribution']['commit'], COMMIT_1)
        self.assertEqual(state['contribution']['delivery']['sha256'], SHA_1)

    def test_a_wrong_checksum_can_never_be_recorded_as_verified(self):
        contribution = self.send(self.contribution())['comment_id']
        before = self.review_count()
        with self.assertRaisesRegex(ValueError, 'not the declared delivery checksum'):
            self.send(self.attest(contribution, sha256='e' * 64, bytes=4096))
        self.assertEqual(self.review_count(), before)      # nothing was written
        # A bundle observation with no byte count is not a verification either.
        with self.assertRaisesRegex(ValueError, 'delivered byte count'):
            self.send(self.attest(contribution, sha256=SHA_1))

    def test_a_commit_that_is_not_the_delivered_tip_can_never_be_verified(self):
        contribution = self.send(self.contribution())['comment_id']
        with self.assertRaisesRegex(ValueError, 'not the declared delivered commit'):
            self.send(self.attest(contribution, commit=COMMIT_2, sha256=SHA_1, bytes=10))
        # A wrong base is refused too: the attestation names both ends of the delivery.
        with self.assertRaisesRegex(ValueError, 'not the declared base commit'):
            self.send(self.attest(contribution, base_commit='9' * 40, sha256=SHA_1, bytes=10))

    def test_a_nonexistent_or_abbreviated_commit_fails_validation(self):
        contribution = self.send(self.contribution())['comment_id']
        # The reported defect: an old record cited `c6b25b2e5f7a` (not a valid object)
        # while a 7-character prefix comparison matched.
        with self.assertRaisesRegex(ValueError, 'require exact 40/64 hexadecimal commit'):
            self.send(self.attest(contribution, commit='c6b25b2e5f7a'))
        with self.assertRaisesRegex(ValueError, 'require exact 40/64 hexadecimal commit'):
            self.send(self.attest(contribution, commit='c6b25b2'))
        with self.assertRaisesRegex(ValueError, '64-hex'):
            self.send(self.attest(contribution, sha256='abc', bytes=10))

    def test_a_remote_delivery_verifies_on_its_delivered_tip(self):
        contribution = self.send(self.contribution(delivery_kind='remote'))['comment_id']
        self.send(self.attest(contribution, method='git ls-remote of the delivered branch'))
        self.assertEqual(w.project(self.issue)['verification'], 'verified')
        self.assertIsNone(w.project(self.issue)['attestation']['sha256'])

    def test_an_unreachable_remote_is_an_explicit_unknown(self):
        contribution = self.send(self.contribution(delivery_kind='remote'))['comment_id']
        self.send(self.attest(contribution, verdict='unknown',
                              method='git ls-remote of the delivered branch',
                              reason='remote credentials unavailable: the branch could not be read'))
        state = w.project(self.issue)
        self.assertEqual(state['verification'], 'unknown')
        self.assertFalse(state['attestation']['verified'])
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertTrue(any('explicit UNKNOWN' in line for line in state['warnings']))
        # It is never quietly promoted: a later reader still sees the unknown.
        self.assertEqual(w.project(self.issue)['verification'], 'unknown')

    def test_unknown_and_failed_both_need_a_reason(self):
        contribution = self.send(self.contribution())['comment_id']
        base = dict(schema_version=1, operation='attest', operation_id='x-1', task=TASK,
                    previous=None, contribution=contribution, verdict='unknown',
                    commit=COMMIT_1, base_commit=BASE, method='probe')
        with self.assertRaises(ValueError):
            w.validate(base, TASK)
        w.validate(dict(base, reason='unreachable'), TASK)

    def test_a_failed_verification_holds_the_contribution(self):
        contribution = self.approved()
        self.send(self.attest(contribution, verdict='failed',
                              reason='the delivered bundle checksum differs from the declared one'))
        state = w.project(self.issue)
        self.assertEqual(state['verification'], 'failed')
        self.assertEqual(state['review_state'], 'held')
        self.assertNotEqual(state['review_state'], 'awaiting-integration')
        self.assertTrue(any('FAILED' in line for line in state['warnings']))
        # A later, matching attestation is the remedy: the newest reading wins.
        self.send(self.attest(contribution, sha256=SHA_1, bytes=4096,
                              reason='re-checked after the delivery was corrected'))
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')

    def test_a_demoted_stored_attestation_reads_as_unverified_never_as_verified(self):
        contribution = self.send(self.contribution())['comment_id']
        # An attestation written by an older/looser kit whose observation does not match.
        self.stored(self.attest(contribution, sha256='e' * 64, bytes=10), author=OPERATOR)
        state = w.project(self.issue)
        self.assertEqual(state['verification'], 'unverified')
        self.assertFalse(state['attestation']['verified'])
        self.assertTrue(state['attestation']['problems'])
        self.assertTrue(any('read as unverified' in line for line in state['warnings']))


class IntegrationHoldTests(Harness):
    def test_a_held_contribution_never_reads_awaiting_integration(self):
        contribution = self.approved()
        receipt = self.send(self.hold(contribution))
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'held')
        # The write receipt reports the same effective state as the reads.
        self.assertEqual(receipt['review_state'], 'held')
        self.assertEqual(len(state['holds']), 1)
        self.assertEqual(state['holds'][0]['contribution'], contribution)
        self.assertEqual(state['holds'][0]['author'], OPERATOR)
        self.assertTrue(any('Integration hold(s) in force' in line for line in state['warnings']))
        # The shared effective projection, the brief and the work queue all agree.
        self.assertEqual(self.shared()['review_state'], 'held')
        import briefing
        from test_briefing import PROJECT
        self.assertEqual(briefing.brief(self.rows, PROJECT, TASK)['review']['review_state'], 'held')
        item = work.queue(self.rows, 'worker', ['--mine'])['items'][0]
        self.assertEqual(item['review_state'], 'held')
        self.assertEqual(item['whose_move'], 'coordinator')
        self.assertEqual(item['next_actor'], 'coordinator')
        self.assertNotEqual(item['review_state'], 'awaiting-integration')

    def test_a_hold_beats_recorded_integration_evidence(self):
        # Even with a trusted passing integration scope for the contribution's commit, a
        # held contribution is never shown integrated: the hold is not cleared by evidence.
        contribution = self.approved()
        self.send(self.hold(contribution))
        raw = w.project(self.issue)
        scopes = [dict(scope=dict(source_commit=COMMIT_1, integration_commit='d' * 40,
                                  release_id='release-1', environment='production'),
                       integrated=dict(value='passed'), order=1, scope_token='tok-1')]
        effective = review_state.effective(raw, scopes)
        self.assertEqual(effective['integration']['fact'], 'passed')
        self.assertEqual(effective['review_state'], 'held')
        self.assertNotEqual(effective['review_state'], 'integrated')

    def test_the_held_state_is_a_filterable_work_state(self):
        contribution = self.approved()
        self.send(self.hold(contribution))
        self.assertIn('held', work.WORK_STATES)
        self.assertEqual(review_state.next_actor('held', 'worker'), 'coordinator')
        self.assertEqual(review_state.next_actor('held', None), 'coordinator')

    def test_a_hold_survives_an_exact_retry(self):
        contribution = self.approved()
        payload = self.hold(contribution)
        first = self.send(payload)
        retried = self.send(dict(payload))
        self.assertTrue(retried['reconciled'])
        self.assertEqual(retried['comment_id'], first['comment_id'])
        self.assertEqual(self.review_count(), 3)          # contribute, approve, hold
        self.assertEqual(w.project(self.issue)['review_state'], 'held')

    def test_a_hold_survives_a_new_contribution_revision(self):
        contribution = self.approved()
        hold_id = self.send(self.hold(contribution))['comment_id']
        # A new revision supersedes the held one. A withdrawal/approval resets; the HOLD
        # does not, so the new revision is not accidentally integration-ready.
        revised = self.send(self.contribution(commit=COMMIT_2, path='/deliveries/revision-2.bundle',
                                              sha256=SHA_2))['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'held')
        self.assertEqual([h['comment_id'] for h in state['holds']], [hold_id])
        # The hold still names the revision it was audited against.
        self.assertEqual(state['holds'][0]['contribution'], contribution)
        self.send(self.payload('approve', contribution=revised, summary='Approved'), 'reviewer2')
        self.assertEqual(w.project(self.issue)['review_state'], 'held')

    def test_a_hold_survives_a_renamed_revised_bundle(self):
        contribution = self.approved()
        self.send(self.hold(contribution))
        # The same content delivered under a renamed path with a new declared checksum:
        # the checksum rule follows the DECLARED delivery, never the file name.
        revised = self.send(self.contribution(commit=COMMIT_2, path='/deliveries/renamed-rev2.bundle',
                                              sha256=SHA_2))['comment_id']
        with self.assertRaisesRegex(ValueError, 'not the declared delivery checksum'):
            self.send(self.attest(revised, commit=COMMIT_2, sha256=SHA_1, bytes=10))
        self.send(self.attest(revised, commit=COMMIT_2, sha256=SHA_2, bytes=20))
        state = w.project(self.issue)
        self.assertEqual(state['verification'], 'verified')
        self.assertEqual(state['review_state'], 'held')    # the hold still stands

    def test_only_a_configured_coordinator_may_hold_or_release(self):
        contribution = self.approved()
        with self.assertRaisesRegex(ValueError, 'configured coordinator'):
            self.send(self.hold(contribution), 'worker')
        hold_id = self.send(self.hold(contribution))['comment_id']
        with self.assertRaisesRegex(ValueError, 'configured coordinator'):
            self.send(self.release_hold(contribution, hold_id), 'worker')
        self.assertEqual(w.project(self.issue)['review_state'], 'held')
        # A verified web authority is the web route's equivalent of a listed operator.
        self.send(self.release_hold(contribution, hold_id), 'web-owner',
                  operators=(), verified_web_authority=True)
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')

    def test_release_hold_is_audited_and_clears_exactly_one_open_hold(self):
        contribution = self.approved()
        first = self.send(self.hold(contribution, reason='First concern'))['comment_id']
        second = self.send(self.hold(contribution, reason='Second concern'))['comment_id']
        self.assertEqual(len(w.project(self.issue)['holds']), 2)
        released = self.send(self.release_hold(contribution, first, reason='First cleared'))
        state = w.project(self.issue)
        self.assertEqual([h['comment_id'] for h in state['holds']], [second])
        self.assertEqual(state['review_state'], 'held')
        self.assertEqual(state['hold_releases'], [dict(
            comment_id=released['comment_id'], hold=first, contribution=contribution,
            reason='First cleared', author=OPERATOR, timestamp='2026-09-16T00:00:00Z')])
        # Releasing the same hold twice is refused before any write, so a retry cannot
        # clear a DIFFERENT hold that happens to be open.
        before = self.review_count()
        with self.assertRaisesRegex(ValueError, 'must name an OPEN hold'):
            self.send(self.release_hold(contribution, first, reason='Again'))
        self.assertEqual(self.review_count(), before)
        with self.assertRaisesRegex(ValueError, 'must name an OPEN hold'):
            self.send(self.release_hold(contribution, 'no-such-hold', reason='Never existed'))

    def test_a_stray_release_record_on_the_read_path_changes_nothing(self):
        contribution = self.approved()
        hold_id = self.send(self.hold(contribution))['comment_id']
        # A release record planted for a hold that does not exist (or is not this task's).
        self.stored(self.release_hold(contribution, 'ghost-hold'), author=OPERATOR)
        state = w.project(self.issue)
        self.assertEqual([h['comment_id'] for h in state['holds']], [hold_id])
        self.assertEqual(state['review_state'], 'held')
        self.assertEqual(state['ignored_hold_releases'], ['4'])
        self.assertTrue(any('naming no open hold' in line for line in state['warnings']))
        # A duplicate release of the SAME hold (already released) is ignored too: the
        # second one cannot silently clear a later hold.
        self.send(self.release_hold(contribution, hold_id))
        later = self.send(self.hold(contribution, reason='A later concern'))['comment_id']
        self.stored(self.release_hold(contribution, hold_id), author=OPERATOR)
        state = w.project(self.issue)
        self.assertEqual([h['comment_id'] for h in state['holds']], [later])
        self.assertEqual(state['review_state'], 'held')

    def test_the_new_shapes_are_refused_with_the_write_switch_off(self):
        contribution = self.send(self.contribution())['comment_id']
        before = self.review_count()
        for payload in (self.attest(contribution, sha256=SHA_1, bytes=10),
                        self.hold(contribution),
                        self.release_hold(contribution, 'any')):
            with self.subTest(operation=payload['operation']):
                with self.assertRaisesRegex(ValueError, 'review_workflow_writes off'):
                    self.send(payload, operators=(OPERATOR,), review_writes=False)
        self.assertEqual(self.review_count(), before)


class DetachedHeadBundleTests(unittest.TestCase):
    """The documented import path for a bundle with no branch ref (kittrial-5bb.10).

    ``git bundle create FILE HEAD`` from a detached HEAD records the ``HEAD`` pseudo-ref
    and no branch, so a plain ``git clone`` lands detached and a plain ``git fetch`` leaves
    only ``FETCH_HEAD``. The guide therefore documents the explicit
    ``git fetch BUNDLE SHA:refs/heads/NAME`` form, and the renamed-bundle rule: the file
    name is irrelevant, the bytes' SHA-256 is the identity.
    """

    def setUp(self):
        if shutil.which('git') is None:
            self.skipTest('git is not available')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'src'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 't@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.repo / 'a.txt').write_text('one\n', encoding='utf-8')
        self.git('add', 'a.txt')
        self.git('commit', '-q', '-m', 'one')
        (self.repo / 'a.txt').write_text('two\n', encoding='utf-8')
        self.git('commit', '-q', '-am', 'two')
        self.git('checkout', '-q', '--detach')
        self.sha = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args, cwd=None):
        done = subprocess.run(['git', *args], cwd=str(cwd or self.repo), capture_output=True,
                              text=True)
        if done.returncode != 0:
            raise AssertionError('git %s failed: %s' % (' '.join(args), done.stderr.strip()))
        return done.stdout

    def test_a_detached_head_bundle_records_only_the_head_pseudo_ref(self):
        bundle = Path(self.temp.name) / 'detached.bundle'
        self.git('bundle', 'create', str(bundle), 'HEAD')
        heads = self.git('bundle', 'list-heads', str(bundle))
        self.assertEqual(heads.split(), [self.sha, 'HEAD'])
        # A raw sha cannot even be bundled without a ref, so HEAD is the documented form.
        with self.assertRaises(AssertionError):
            self.git('bundle', 'create', str(bundle) + '.raw', self.sha)

    def test_the_documented_explicit_fetch_materialises_the_delivered_commit(self):
        bundle = Path(self.temp.name) / 'detached.bundle'
        self.git('bundle', 'create', str(bundle), 'HEAD')
        imported = Path(self.temp.name) / 'imported'
        imported.mkdir()
        self.git('init', '-q', cwd=imported)
        self.git('fetch', str(bundle), '%s:refs/heads/contribution' % self.sha, cwd=imported)
        self.assertEqual(self.git('rev-parse', 'refs/heads/contribution', cwd=imported).strip(),
                         self.sha)
        # A default fetch from a HEAD-only bundle leaves only FETCH_HEAD: that is why the
        # guide names the explicit refspec rather than a bare `git fetch`.
        plain = Path(self.temp.name) / 'plain'
        plain.mkdir()
        self.git('init', '-q', cwd=plain)
        self.git('fetch', str(bundle), cwd=plain)
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)', cwd=plain).strip(), '')
        self.assertEqual(self.git('rev-parse', 'FETCH_HEAD', cwd=plain).strip(), self.sha)

    def test_a_renamed_revised_bundle_verifies_by_its_bytes(self):
        import hashlib
        first = Path(self.temp.name) / 'revision-1.bundle'
        self.git('bundle', 'create', str(first), 'HEAD')
        renamed = Path(self.temp.name) / 'revision-2-renamed.bundle'
        renamed.write_bytes(first.read_bytes())
        self.assertEqual(hashlib.sha256(renamed.read_bytes()).hexdigest(),
                         hashlib.sha256(first.read_bytes()).hexdigest())
        self.git('bundle', 'verify', str(renamed))
        imported = Path(self.temp.name) / 'renamed'
        imported.mkdir()
        self.git('init', '-q', cwd=imported)
        self.git('fetch', str(renamed), '%s:refs/heads/contribution' % self.sha, cwd=imported)
        self.assertEqual(self.git('rev-parse', 'refs/heads/contribution', cwd=imported).strip(),
                         self.sha)


class LifecycleScopeCommitTests(unittest.TestCase):
    """The lifecycle scope write rule (kittrial-5bb.10): exact 40/64-hex commits only."""

    def scope(self, source='a' * 40, integration=''):
        return {'source_commit': source, 'integration_commit': integration,
                'release_id': '', 'environment': ''}

    def test_a_nonempty_scope_commit_must_be_an_exact_full_hash(self):
        lifecycle.validate_scope(self.scope())
        lifecycle.validate_scope(self.scope(integration='f' * 64))
        for value in ('c6b25b2e5f7a', 'c6b25b2', 'source-a', 'merge-a', 'zz' * 20):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'exact 40/64 hexadecimal commit'):
                    lifecycle.validate_scope(self.scope(source=value))

    def test_a_scope_with_no_commit_is_still_valid(self):
        # A scope that identifies its work by release id/environment carries no commit;
        # the commit rule only constrains a non-empty one.
        lifecycle.validate_scope({'source_commit': '', 'integration_commit': '',
                                  'release_id': 'release-1', 'environment': 'prod'})

    def test_a_stored_legacy_scope_still_reads(self):
        # The published contract predates the rule; the stored-record reader tolerates a
        # legacy scope so an installation's existing history keeps its meaning.
        payload = dict(schema_version=1, operation_id='op-1', task='trial-task',
                       dimension='implemented', value='passed',
                       scope=self.scope(source='source-a'),
                       evidence=['commit:source-a'], provenance='performed', actor='alice')
        lifecycle.validate_stored_payload(payload)
        with self.assertRaisesRegex(ValueError, 'exact 40/64 hexadecimal commit'):
            lifecycle.validate_payload(payload)
