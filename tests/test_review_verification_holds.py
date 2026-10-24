"""Verifier attestations and explicit integration holds (kittrial-5bb.10).

The acceptance criteria this file pins:

* a wrong checksum, a wrong base, or a commit that is not the delivered tip can never be
  recorded as ``verified``;
* only a listed verifier or a listed operator may attest, never the contribution's author,
  the task's assignee or a party of either;
* an inaccessible remote is an explicit ``unknown`` with its reason, never a fabricated
  pass, and ``unknown`` does not by itself hold;
* an APPROVED contribution under an open hold (or a failed attestation) can never appear
  integration-ready, and every gate that decides integration refuses it with a sentence
  naming the hold and who can release it;
* an exact retry and a new contribution revision cannot accidentally clear a hold, while a
  revision under a standing hold stays reviewable and SHOWS the hold;
* a detached-HEAD bundle (a bundle whose only ref is the ``HEAD`` pseudo-ref) and a
  renamed/revised bundle are observed by the kit's own host observation code, and the
  checksum rule follows the DECLARED delivery, not the file name.

The reader tests build the chain through ``review_workflow.execute`` against a stub native
runner, exactly as the other review-workflow tests do. The bundle tests create a real bundle
with ``git`` and then exercise ``review_workflow.read_bundle_observation`` (kit code); the
digest/byte-count half of the observation needs no git at all and is pinned separately.
"""
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coordination
import http_service
import lifecycle
import review_state
import review_workflow as w
import work
from http_auth import HttpError

TASK = 'task-1'
COMMIT_1 = 'a' * 40
COMMIT_2 = 'f' * 40
BASE = 'b' * 40
SHA_1 = 'c' * 64
SHA_2 = 'd' * 64
OPERATOR = 'coordinator'
VERIFIER = 'verifier'
AUTHOR = 'worker'
REVIEWER = 'reviewer'


def row(task=TASK, comments=None, assignee=AUTHOR, status='in_progress'):
    return dict(id=task, title='A task', issue_type='task', status=status,
                assignee=assignee, description='Intent', acceptance_criteria='Acceptance',
                labels=[], comments=list(comments or []))


@contextlib.contextmanager
def scratch():
    """A disposable writable directory.

    Prefers the platform temporary area and falls back to a directory under the checkout
    (a confined sandbox can deny writes in the platform area). ``os.mkdir`` is used rather
    than ``tempfile.mkdtemp`` because the latter's restrictive mode is denied in the
    sandbox; cleanup never fails the test.
    """
    root = None
    for parent in (Path(tempfile.gettempdir()), Path(__file__).resolve().parents[1]):
        candidate = parent / ('scratch-' + secrets.token_hex(6))
        try:
            os.mkdir(candidate)
            probe = candidate / '.probe'
            probe.write_text('probe', encoding='utf-8')
            probe.unlink()
        except OSError:
            shutil.rmtree(candidate, ignore_errors=True)
            continue
        root = candidate
        break
    if root is None:
        raise unittest.SkipTest('no writable scratch directory')
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def chain(task, commit=COMMIT_1, sha256=SHA_1, base=BASE, kind='bundle', approved=True,
          hold=True, hold_actor=OPERATOR, operators=(OPERATOR,), verifiers=(VERIFIER,),
          author=AUTHOR, assignee=AUTHOR):
    """Build a review chain on a fresh native row; returns ``(issue, contribution_id)``.

    A convenience for the lifecycle/merge-slot tests below, which need the same task row
    a review chain and lifecycle evidence both describe.
    """
    issue = row(task, assignee=assignee)
    rows = [issue]
    holder = {'actor': author}

    def run(argv):
        if argv == ['export', '--all']:
            return ''.join(json.dumps(r) + '\n' for r in rows)
        if argv[:3] == ['comments', 'add', task]:
            cid = str(len(issue['comments']) + 1)
            issue['comments'].append(dict(id=cid, text=argv[3], author=holder['actor'],
                                          created_at='2026-09-16T00:00:00Z'))
            return json.dumps({'id': cid})
        raise AssertionError('unexpected native command: ' + repr(argv))

    counter = {'n': 0}

    def payload(operation, **extra):
        counter['n'] += 1
        return dict(schema_version=1, operation=operation, operation_id='%s-%d' % (task, counter['n']),
                    task=task, previous=w.project(issue, operators=list(operators))['latest_comment_id'],
                    **extra)

    def send(p, actor):
        holder['actor'] = actor
        return w.execute(rows, task, actor, p, run, operators=list(operators),
                         verifiers=list(verifiers), review_writes=True)

    delivery = (dict(kind='remote', remote='ssh://git.example/project', branch='contrib/x')
                if kind == 'remote' else
                dict(kind='bundle', path='reviewer:/deliveries/revision.bundle', sha256=sha256))
    contribution = send(payload('contribute', repository='ssh://git.example/project', commit=commit,
                                base_commit=base, delivery=delivery,
                                summary='Implementation and test evidence', supersedes=None), author)['comment_id']
    if approved:
        send(payload('approve', contribution=contribution, summary='Approved'), REVIEWER)
    if hold:
        send(payload('hold', contribution=contribution, reason='Not release-ready'), hold_actor)
    return issue, contribution


class Harness(unittest.TestCase):
    def setUp(self):
        self.issue = row()
        self.rows = [self.issue]
        self.actor = AUTHOR
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

    def send(self, p, actor=None, operators=(OPERATOR,), verifiers=(VERIFIER,),
             review_writes=True, verified_web_authority=False, owners=None):
        # An attestation is a listed verifier's or a listed operator's; a hold is a listed
        # operator's; every other operation defaults to the contributing worker.
        if actor is None:
            actor = (VERIFIER if p.get('operation') == 'attest' else
                     OPERATOR if p.get('operation') in ('hold', 'release-hold') else AUTHOR)
        self.actor = actor
        return w.execute(self.rows, TASK, actor, p, self.run_native, operators=list(operators),
                         verifiers=list(verifiers), review_writes=review_writes,
                         verified_web_authority=verified_web_authority, owners=owners)

    def review_count(self):
        return len([c for c in self.issue['comments'] if c['text'].startswith(w.PREFIX)])

    def stored(self, p, author=AUTHOR, created_at='2026-09-16T00:00:00Z'):
        """Plant one already-written record directly (an older kit's own write)."""
        cid = str(len(self.issue['comments']) + 1)
        self.issue['comments'].append(dict(id=cid, text=w.PREFIX + w.canonical_bytes(p).decode(),
                                           author=author, created_at=created_at))
        return cid

    def approved(self, **contribution_extra):
        """A contributed and independently approved revision."""
        contribution = self.send(self.contribution(**contribution_extra))['comment_id']
        self.send(self.payload('approve', contribution=contribution, summary='Approved'),
                  REVIEWER)
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        return contribution

    def shared(self):
        from review_state import project as reviewed, scopes_for
        return reviewed(self.issue, scopes_for(self.rows, TASK))


class ChainHarness(Harness):
    """An instantiable harness (no test method) for tests that need a chain helper."""

    def runTest(self):
        pass


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
        self.send(self.attest(contribution, sha256=SHA_1, bytes=4096))
        state = w.project(self.issue)
        self.assertEqual(state['verification'], 'verified')
        self.assertTrue(state['attestation']['verified'])
        self.assertEqual(state['attestation']['problems'], [])
        self.assertEqual(state['attestation']['sha256'], SHA_1)
        self.assertEqual(state['attestation']['bytes'], 4096)
        self.assertEqual(state['attestation']['author'], VERIFIER)
        self.assertEqual(state['attestation']['commit'], COMMIT_1)
        # The declared delivery is untouched: the attestation is a SEPARATE record.
        self.assertEqual(state['contribution']['commit'], COMMIT_1)
        self.assertEqual(state['contribution']['delivery']['sha256'], SHA_1)

    def test_a_same_actor_attest_retry_with_a_new_previous_reconciles(self):
        contribution = self.send(self.contribution())['comment_id']
        payload = self.attest(contribution, sha256=SHA_1, bytes=10)
        first = self.send(payload)
        # The host command (`admin.py attest-delivery`) rebuilds `previous` from the
        # chain head, so a rerun names the record the first run wrote. Same actor, same
        # observation: reconcile, do not refuse it as a different payload (review item
        # `an-exact-rerun-and-the-load-rule`).
        retried = self.send(dict(payload, previous=first['comment_id']))
        self.assertTrue(retried['reconciled'])
        self.assertEqual(retried['comment_id'], first['comment_id'])
        self.assertEqual(self.review_count(), 2)          # contribute, one attestation

    def test_an_attest_retry_with_a_different_observation_still_refuses(self):
        # The `previous` relaxation is scoped to the chain-ordering pointer alone: a
        # different recorded observation on the same id is still refused, so nothing
        # else about the "different payload" rule is weakened.
        contribution = self.send(self.contribution())['comment_id']
        payload = self.attest(contribution, sha256=SHA_1, bytes=10)
        first = self.send(payload)
        with self.assertRaisesRegex(ValueError, 'different payload or actor'):
            self.send(dict(payload, previous=first['comment_id'], reason='A different reading'))

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
        # An abbreviated hash is refused at the record's own boundary.
        with self.assertRaisesRegex(ValueError, 'require exact 40/64 hexadecimal commit'):
            self.send(self.attest(contribution, commit='c6b25b2e5f7a'))
        with self.assertRaisesRegex(ValueError, 'require exact 40/64 hexadecimal commit'):
            self.send(self.attest(contribution, commit='c6b25b2'))
        with self.assertRaisesRegex(ValueError, '64-hex'):
            self.send(self.attest(contribution, sha256='abc', bytes=10))
        # A well-formed hash that names nothing is refused by the OBSERVATION, where it can
        # be decided: the commit is not one of the heads the bundle advertises, so the host
        # command can never write it as a verified pass.
        observation = {'path': '/deliveries/rev.bundle', 'bytes': 10, 'sha256': SHA_1,
                       'heads': [{'commit': COMMIT_1, 'ref': 'HEAD'}], 'heads_error': None}
        self.assertIn('is not one of the heads this bundle advertises',
                      w.bundle_commit_problem(observation, 'deadbeef' + '0' * 32))
        self.assertIsNone(w.bundle_commit_problem(observation, COMMIT_1))
        self.assertIn('could not be listed',
                      w.bundle_commit_problem(dict(observation, heads_error='no git'), COMMIT_1))

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
        # An APPROVED contribution with an unknown reading is NOT held: unknown is surfaced
        # and never promoted to a pass, but it does not by itself stop integration.
        self.send(self.payload('approve', contribution=contribution, summary='Approved'), REVIEWER)
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')

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
        # A later, matching attestation from a listed verifier is the remedy.
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

    def test_only_a_listed_verifier_or_operator_may_attest(self):
        contribution = self.send(self.contribution())['comment_id']
        # An unlisted contributor records nothing at all.
        with self.assertRaisesRegex(w.CoordinatorRuleRefusal,
                                    'only a listed verifier or a listed operator'):
            self.send(self.attest(contribution), 'stranger')
        self.assertNotIn('attestation', w.project(self.issue))
        # A listed verifier may.
        self.send(self.attest(contribution, sha256=SHA_1, bytes=4096), VERIFIER)
        self.assertEqual(w.project(self.issue)['verification'], 'verified')
        # A listed operator may, and so may an actor of a listed operator's principal.
        self.send(self.attest(contribution, sha256=SHA_1, bytes=4096, reason='operator spot check'),
                  OPERATOR)
        self.send(self.attest(contribution, sha256=SHA_1, bytes=4096, reason='principal check'),
                  'agent-x', owners={'agent-x': 'principal-1', OPERATOR: 'principal-1'})
        self.assertEqual(w.project(self.issue)['verification'], 'verified')

    def test_the_author_and_assignee_may_not_attest_even_if_listed(self):
        # The contribution's author, even when listed as a verifier.
        contribution = self.send(self.contribution())['comment_id']
        before = self.review_count()
        with self.assertRaisesRegex(w.CoordinatorRuleRefusal, "contribution author's party"):
            self.send(self.attest(contribution), AUTHOR, verifiers=(AUTHOR,))
        # The refusal is the AUTHOR check, not a later failure: nothing was written. A
        # mutant that dropped the party check would write a record here and die on this
        # assertion rather than on an incidental downstream error.
        self.assertEqual(self.review_count(), before)
        self.assertNotIn('attestation', w.project(self.issue))
        # The task's assignee, even when listed as a verifier.
        self.issue['assignee'] = VERIFIER
        with self.assertRaisesRegex(w.CoordinatorRuleRefusal, "assignee's party"):
            self.send(self.attest(contribution), VERIFIER)
        self.assertEqual(self.review_count(), before)
        self.assertNotIn('attestation', w.project(self.issue))

    def test_the_party_rule_holds_with_every_coordinator_rule_off(self):
        # The coordinator rules are all OFF here: send() passes neither independent_approval
        # nor coordinator_writes. "Never the author's or assignee's party" must still hold,
        # because the map is loaded for attest whatever the rules say (review item 2). w1b
        # is a listed verifier adopted into the SAME principal as the author w1.
        contribution = self.send(self.contribution())['comment_id']
        owners = {AUTHOR: 'lane:w1', 'w1b': 'lane:w1', 'w2b': 'lane:w2'}
        before = self.review_count()
        with self.assertRaisesRegex(w.CoordinatorRuleRefusal, "author's party"):
            self.send(self.attest(contribution, sha256=SHA_1, bytes=4096), 'w1b',
                      verifiers=(VERIFIER, 'w1b', 'w2b'), owners=owners)
        self.assertEqual(self.review_count(), before)
        # A listed verifier of ANOTHER principal is accepted, with no "parties could not be
        # compared" warning (the registry was read).
        receipt = self.send(self.attest(contribution, sha256=SHA_1, bytes=4096), 'w2b',
                            verifiers=(VERIFIER, 'w1b', 'w2b'), owners=owners)
        self.assertEqual(w.project(self.issue)['verification'], 'verified')
        self.assertFalse([line for line in receipt.get('warnings') or [] if 'parties' in line])

    def test_an_unreadable_registry_compares_names_and_says_so_in_the_answer(self):
        contribution = self.send(self.contribution())['comment_id']

        class Damaged:
            def __call__(self):
                raise ValueError('the session registry is damaged')

        receipt = self.send(self.attest(contribution, sha256=SHA_1, bytes=4096), VERIFIER,
                            owners=Damaged())
        self.assertEqual(w.project(self.issue)['verification'], 'verified')
        self.assertTrue(any('parties' in line for line in receipt.get('warnings') or []))

    def test_a_failed_attestation_is_cleared_only_by_an_authorized_reading_or_a_new_revision(self):
        contribution = self.approved()
        self.send(self.attest(contribution, verdict='failed', reason='checksum mismatch'), VERIFIER)
        self.assertEqual(w.project(self.issue)['review_state'], 'held')
        # An unlisted contributor cannot clear it (or set anything else).
        with self.assertRaisesRegex(w.CoordinatorRuleRefusal, 'listed verifier or a listed operator'):
            self.send(self.attest(contribution, verdict='unknown', reason='let me pass it'), 'stranger')
        self.assertEqual(w.project(self.issue)['review_state'], 'held')
        # A superseding revision resets the attestation: the new revision needs its own.
        revised = self.send(self.contribution(commit=COMMIT_2, path='/deliveries/revision-2.bundle',
                                              sha256=SHA_2))['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertNotIn('verification', state)
        self.send(self.attest(revised, commit=COMMIT_2, sha256=SHA_2, bytes=20), VERIFIER)
        self.assertEqual(w.project(self.issue)['verification'], 'verified')

    def test_an_unlisted_contributor_cannot_hold_any_contribution(self):
        contribution = self.approved()
        before = self.review_count()
        for operation in ('hold', 'release-hold'):
            payload = (self.hold(contribution) if operation == 'hold'
                       else self.release_hold(contribution, 'any'))
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, 'only a listed operator'):
                    self.send(payload, 'stranger')
        self.assertEqual(self.review_count(), before)
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')


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
        item = work.queue(self.rows, AUTHOR, ['--mine'])['items'][0]
        self.assertEqual(item['review_state'], 'held')
        self.assertTrue(item['assignee_comment_pending'])
        self.assertEqual(item['whose_move'], 'you')
        self.assertEqual(item['next_actor'], 'assignee')
        self.assertIn('Integration hold', item['next'])
        self.assertEqual(item['holds'], [state['holds'][0]['comment_id']])
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
        self.assertEqual(review_state.next_actor('held', AUTHOR), 'coordinator')
        self.assertEqual(review_state.next_actor('held', None), 'coordinator')
        # The author still owes OPEN requested changes, so with any the move is theirs.
        self.assertEqual(review_state.next_actor('held', AUTHOR, pending_items=1), 'assignee')

    def test_state_held_lists_approved_and_held_only(self):
        # kittrial-5bb.272 item 3: `work --state held` finds the approved-and-held
        # revision; a held revision that is NOT approved stays under the state it really
        # shows - awaiting-review here - with the hold beside it, never under `held`.
        approved = self.approved()
        self.send(self.hold(approved))

        def plant(target, payload, author=AUTHOR):
            cid = str(len(target['comments']) + 1)
            target['comments'].append(dict(id=cid, text=w.PREFIX + w.canonical_bytes(payload).decode(),
                                           author=author, created_at='2026-09-16T00:00:00Z'))
            return cid

        # A second task whose current contribution is held while still AWAITING review.
        second = row(task='task-2')
        self.rows.append(second)
        awaiting = plant(second, dict(schema_version=1, operation='contribute', operation_id='op-c2',
                                      task='task-2', previous=None, repository='ssh://git.example/project',
                                      commit=COMMIT_2, base_commit=BASE,
                                      delivery=dict(kind='remote', remote='ssh://git.example/project',
                                                    branch='contrib/y'),
                                      summary='Second task', supersedes=None))
        plant(second, dict(schema_version=1, operation='hold', operation_id='op-h2', task='task-2',
                           previous=awaiting, contribution=awaiting, reason='Held early, still in review'),
              author=OPERATOR)
        self.assertEqual(w.project(second)['review_state'], 'awaiting-review')

        held_rows = work.queue(self.rows, AUTHOR, ['--state', 'held'])['items']
        self.assertEqual([item['task'] for item in held_rows], [TASK])          # only the approved one
        self.assertTrue(held_rows[0]['holds'])
        awaiting_rows = work.queue(self.rows, AUTHOR, ['--state', 'awaiting-review'])['items']
        # The held-not-approved revision is found under awaiting-review, hold beside it.
        self.assertEqual([item['task'] for item in awaiting_rows], ['task-2'])
        self.assertIn('Integration hold', awaiting_rows[0]['next'])

    def test_a_closed_task_that_is_held_is_not_active_work(self):
        # kittrial-5bb.272 item 4, the coordinator's resolution at integration: closure
        # removes the held task from every active queue (`--state held` included); the
        # hold record stays in history and `review` still reports it.
        contribution = self.approved()
        self.send(self.hold(contribution))
        self.assertEqual(w.project(self.issue)['review_state'], 'held')
        self.issue['status'] = 'closed'
        self.assertEqual(work.queue(self.rows, AUTHOR, ['--state', 'held'])['items'], [])
        self.assertEqual(work.queue(self.rows, AUTHOR, ['--mine'])['items'], [])
        self.assertEqual(work.queue(self.rows, REVIEWER, [])['items'], [])
        # The record stays: the review read still says held.
        self.assertEqual(w.project(self.issue)['review_state'], 'held')

    def test_lifecycle_reads_the_chain_through_the_shared_projection(self):
        # kittrial-5bb.272 item 5, corrected by review item 1 (r2): the lifecycle read
        # projects through the SAME operator voids and revert journal as review, brief
        # and work - project(row, operators, journal) - so the two reads cannot disagree
        # about a hold.
        import lifecycle
        from review_workflow import project, integration_hold_sentence
        contribution = self.approved()
        self.send(self.hold(contribution))
        state = lifecycle.review_chain_state(self.issue, (OPERATOR,), None)
        self.assertEqual(state, project(self.issue, (OPERATOR,), None))
        self.assertTrue(integration_hold_sentence(state))
        self.assertEqual(lifecycle.held_issue_sentence(self.issue, (OPERATOR,), None),
                         integration_hold_sentence(project(self.issue, (OPERATOR,), None)))

    def test_a_voided_release_hold_reads_held_again_in_the_lifecycle_facts(self):
        # kittrial-5bb.272 review item 1, the reviewer's scenario: a release-hold the
        # surviving chain no longer links IS voidable, and with the void applied the
        # canonical readers read the hold back in force. The first revision's lifecycle
        # read ignored voids, so it read the forked chain as unparseable and answered
        # None - no hold sentence, no refusal. Now it projects through the same voids
        # and the held task is refused.
        import lifecycle
        import recovery
        from requirements import canonical_bytes
        from review_workflow import integration_hold_sentence
        contribution = self.approved()
        hold_receipt = self.send(self.hold(contribution))
        release_receipt = self.send(self.release_hold(contribution, hold_receipt['comment_id']))
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        # A repair revision whose previous names the HOLD, not the release-hold: the
        # chain now has two children of the hold and is unreadable until an operator
        # reconciles it - exactly the state the dangling-record sentence tells a worker
        # to repair with a revision or an operator to void.
        self.stored(dict(schema_version=1, operation='contribute', operation_id='op-fork',
                         task=TASK, previous=hold_receipt['comment_id'], supersedes=contribution,
                         repository='ssh://git.example/project', commit=COMMIT_2, base_commit=BASE,
                         delivery=dict(kind='bundle', path='reviewer:/deliveries/rev-2.bundle',
                                       sha256=SHA_2),
                         summary='A repair revision recorded at the hold'))
        # Without the void the fork is unreadable, and the FIRST revision's read
        # (records(row, None)) answered None for it: no hold seen, nothing refused.
        self.assertIsNone(lifecycle.review_chain_state(self.issue))
        self.assertEqual(lifecycle.held_tasks(self.rows), {})
        # The operator voids the unlinked release-hold, preserving its exact bytes; the
        # void is planted as the host command writes it (recovery's own prefix, the
        # operator as the native author).
        target = next(comment for comment in self.issue['comments']
                      if comment['id'] == release_receipt['comment_id'])
        void = dict(schema_version=1, operation='void-record', operation_id='void-release-1', task=TASK,
                    target=release_receipt['comment_id'], target_kind='contribution-review',
                    target_sha256=recovery.digest(target['text']),
                    original=target['text'], reason='Reconciling the forked repair',
                    disposition='void', operator=OPERATOR)
        self.issue['comments'].append(dict(id='void-1', text=recovery.PREFIX + canonical_bytes(void).decode(),
                                           author=OPERATOR, created_at='2026-09-16T00:00:00Z'))
        # The lifecycle read now projects through the same voids as review/brief/work:
        # the chain is readable again, the hold is back in force, and the task is held.
        state = lifecycle.review_chain_state(self.issue, (OPERATOR,), None)
        self.assertIsNotNone(state)
        self.assertTrue(integration_hold_sentence(state))
        self.assertIn(TASK, lifecycle.held_tasks(self.rows, (OPERATOR,), None))

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
        # does not, so the new revision is not accidentally integration-ready. The revision
        # is still in the reviewers' queue, WITH the hold shown (item 7).
        revised = self.send(self.contribution(commit=COMMIT_2, path='/deliveries/revision-2.bundle',
                                              sha256=SHA_2))['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual([h['comment_id'] for h in state['holds']], [hold_id])
        # The hold still names the revision it was audited against.
        self.assertEqual(state['holds'][0]['contribution'], contribution)
        item = work.queue(self.rows, REVIEWER, ['--state', 'awaiting-review'])['items'][0]
        self.assertEqual(item['review_state'], 'awaiting-review')
        self.assertEqual(item['holds'], [hold_id])
        # An approval of the new revision does NOT clear it: the state stays held.
        self.send(self.payload('approve', contribution=revised, summary='Approved'), REVIEWER)
        self.assertEqual(w.project(self.issue)['review_state'], 'held')

    def test_a_hold_survives_a_renamed_revised_bundle(self):
        contribution = self.approved()
        hold_id = self.send(self.hold(contribution))['comment_id']
        # The same content delivered under a renamed path with a new declared checksum:
        # the checksum rule follows the DECLARED delivery, never the file name.
        revised = self.send(self.contribution(commit=COMMIT_2, path='/deliveries/renamed-rev2.bundle',
                                              sha256=SHA_2))['comment_id']
        with self.assertRaisesRegex(ValueError, 'not the declared delivery checksum'):
            self.send(self.attest(revised, commit=COMMIT_2, sha256=SHA_1, bytes=10))
        self.send(self.attest(revised, commit=COMMIT_2, sha256=SHA_2, bytes=20))
        state = w.project(self.issue)
        self.assertEqual(state['verification'], 'verified')
        # The hold still stands against the new revision, which stays reviewable.
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual([h['comment_id'] for h in state['holds']], [hold_id])

    def test_only_a_listed_operator_may_hold_or_release(self):
        contribution = self.approved()
        with self.assertRaisesRegex(ValueError, 'only a listed operator'):
            self.send(self.hold(contribution), AUTHOR)
        hold_id = self.send(self.hold(contribution))['comment_id']
        with self.assertRaisesRegex(ValueError, 'only a listed operator'):
            self.send(self.release_hold(contribution, hold_id), AUTHOR)
        self.assertEqual(w.project(self.issue)['review_state'], 'held')
        # An actor of a listed operator's principal may release it.
        self.send(self.release_hold(contribution, hold_id), 'agent-x',
                  owners={'agent-x': 'principal-1', OPERATOR: 'principal-1'})
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        # A verified web authority ALONE cannot: these operations are host-only (item 6).
        second = self.send(self.hold(contribution))['comment_id']
        with self.assertRaisesRegex(ValueError, 'only a listed operator'):
            self.send(self.release_hold(contribution, second), 'web-owner',
                      operators=(), verified_web_authority=True)
        self.assertEqual(w.project(self.issue)['review_state'], 'held')

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
        # `.get` deliberately: a mutant that cleared every hold on a stray release would
        # otherwise die with a KeyError instead of failing this assertion.
        self.assertEqual([h['comment_id'] for h in state.get('holds', [])], [hold_id])
        self.assertEqual(state['review_state'], 'held')
        self.assertEqual(state.get('ignored_hold_releases'), ['4'])
        self.assertTrue(any('naming no open hold' in line for line in state['warnings']))
        # A duplicate release of the SAME hold (already released) is ignored too: the
        # second one cannot silently clear a later hold.
        self.send(self.release_hold(contribution, hold_id))
        later = self.send(self.hold(contribution, reason='A later concern'))['comment_id']
        self.stored(self.release_hold(contribution, hold_id), author=OPERATOR)
        state = w.project(self.issue)
        self.assertEqual([h['comment_id'] for h in state.get('holds', [])], [later])
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
        # The refusal names the boundary that introduced the shape (item 6).
        with self.assertRaisesRegex(ValueError, 'kittrial-5bb.10'):
            self.send(self.attest(contribution, sha256=SHA_1, bytes=10), review_writes=False)
        self.assertEqual(self.review_count(), before)

    def test_changes_requested_and_held_say_both(self):
        contribution = self.approved()
        hold_id = self.send(self.hold(contribution))['comment_id']
        self.send(self.payload('request-changes', contribution=contribution, summary='Please fix',
                               items=[{'id': 'item-1', 'text': 'Fix the thing'}]), REVIEWER)
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'changes-requested')
        self.assertEqual([h['comment_id'] for h in state.get('holds', [])], [hold_id])
        item = work.queue(self.rows, AUTHOR, ['--mine'])['items'][0]
        self.assertEqual(item['review_state'], 'changes-requested')
        self.assertEqual(item['whose_move'], 'you')
        self.assertIn('outstanding review', item['next'])
        self.assertIn('Integration hold', item['next'])

    def test_approve_while_held_is_accepted_and_the_state_stays_held(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.hold(contribution))
        self.send(self.payload('approve', contribution=contribution, summary='Approved'), REVIEWER)
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'held')
        self.assertNotEqual(state['review_state'], 'awaiting-integration')

    def test_the_brief_says_which_reason_the_contribution_is_held(self):
        # Item 5: the held sentence used to say "an integration hold is open, OR verification
        # failed"; the shared projection knows which, so the brief must say it.
        import briefing
        from test_briefing import PROJECT
        contribution = self.approved()
        hold_id = self.send(self.hold(contribution))['comment_id']
        action = briefing.brief(self.rows, PROJECT, TASK)['next_action']
        self.assertIn(hold_id, action)
        self.assertIn('integration hold', action)
        self.assertNotIn('or verification failed', action)
        # A failed verification says so instead, naming the attestation record.
        second = Harness()
        second.setUp()
        failed_contribution = second.approved()
        failed_id = second.send(second.attest(failed_contribution, verdict='failed',
                                              reason='the bundle checksum differs'), VERIFIER)['comment_id']
        failed_action = briefing.brief(second.rows, PROJECT, TASK)['next_action']
        self.assertIn(failed_id, failed_action)
        self.assertIn('FAILED attestation', failed_action)
        self.assertNotIn('integration hold', failed_action)

    def test_the_work_help_says_held_means_approved_and_held(self):
        text = json.dumps(work.help_payload('work'))
        self.assertIn('held means an APPROVED contribution is held', text)
        self.assertIn('unapproved revision under a hold', text)


class IntegrationHoldStopsIntegrationTests(unittest.TestCase):
    """Item 4: a stored hold is honoured whatever the switch says."""

    def merge_run(self, rows):
        calls = []

        def run(argv):
            calls.append(list(argv))
            if argv == ['export', '--all']:
                return ''.join(json.dumps(r) + '\n' for r in rows)
            if argv[:2] == ['merge-slot', 'check']:
                return json.dumps({'available': True, 'holder': None, 'waiters': None})
            if argv[:2] == ['merge-slot', 'acquire']:
                return json.dumps({'acquired': True})
            if argv[:1] == ['show']:
                return json.dumps(rows)
            raise AssertionError('unexpected native command: ' + repr(argv))

        return run, calls

    def test_merge_acquire_is_refused_for_a_held_contribution(self):
        issue, _ = chain('trial-a')
        run, calls = self.merge_run([issue])
        with scratch() as tmp:
            with self.assertRaisesRegex(w.CoordinatorRuleRefusal,
                                        'open integration hold'):
                coordination.apply_native({'operation': 'merge-acquire', 'task': 'trial-a',
                                           'target': 'refs/heads/main'},
                                          OPERATOR, run, Path(tmp), review_writes=True)
        # The refusal happens before the slot is read or reserved.
        self.assertEqual([call for call in calls if call[:2] == ['merge-slot', 'check']], [])
        self.assertEqual([call for call in calls if call[:2] == ['merge-slot', 'acquire']], [])

    def test_merge_acquire_is_refused_by_a_stored_hold_with_the_switch_off(self):
        # The mutant this kills: gating the hold check on `review_writes` again. A hold
        # stored while the switch was on must still refuse the merge slot after the switch
        # is turned off (review item 4: the switch governs WRITES only).
        issue, _ = chain('trial-a')
        run, calls = self.merge_run([issue])
        with scratch() as tmp:
            with self.assertRaisesRegex(w.CoordinatorRuleRefusal, 'open integration hold'):
                coordination.apply_native({'operation': 'merge-acquire', 'task': 'trial-a',
                                           'target': 'refs/heads/main'},
                                          OPERATOR, run, Path(tmp), review_writes=False)
        self.assertEqual([call for call in calls if call[:2] == ['merge-slot', 'check']], [])
        self.assertEqual([call for call in calls if call[:2] == ['merge-slot', 'acquire']], [])

    def test_merge_acquire_is_unchanged_with_the_switch_off_and_nothing_stored(self):
        # With nothing stored, nothing differs from main: the same acquisition succeeds
        # with the switch off, so an installation that configures nothing is unaffected.
        run, calls = self.merge_run([row('trial-a')])
        with scratch() as tmp:
            result = coordination.apply_native({'operation': 'merge-acquire', 'task': 'trial-a',
                                                'target': 'refs/heads/main'},
                                               OPERATOR, run, Path(tmp), review_writes=False)
        self.assertTrue(result['acquired'])
        self.assertEqual([call for call in calls if call[:2] == ['merge-slot', 'acquire']],
                         [['merge-slot', 'acquire', '--holder', OPERATOR, '--json']])

    def test_merge_acquire_is_allowed_once_the_hold_is_released(self):
        issue, contribution = chain('trial-a')
        run, _ = self.merge_run([issue])
        with scratch() as tmp:
            # Release the hold on the chain, then the same acquisition succeeds.
            rows = [issue]
            holder = {'actor': OPERATOR}

            def chain_run(argv):
                if argv == ['export', '--all']:
                    return ''.join(json.dumps(r) + '\n' for r in rows)
                cid = str(len(issue['comments']) + 1)
                issue['comments'].append(dict(id=cid, text=argv[3], author=holder['actor'],
                                              created_at='2026-09-16T00:00:00Z'))
                return json.dumps({'id': cid})

            hold_id = w.project(issue, operators=[OPERATOR])['holds'][0]['comment_id']
            w.execute(rows, 'trial-a', OPERATOR,
                      dict(schema_version=1, operation='release-hold', operation_id='release-1',
                           task='trial-a', previous=w.project(issue)['latest_comment_id'],
                           contribution=contribution, hold=hold_id, reason='Cleared'),
                      chain_run, operators=[OPERATOR], verifiers=[VERIFIER], review_writes=True)
            result = coordination.apply_native({'operation': 'merge-acquire', 'task': 'trial-a',
                                                'target': 'refs/heads/main'},
                                               OPERATOR, run, Path(tmp), review_writes=True)
        self.assertTrue(result['acquired'])

    def test_a_reviewed_or_integrated_fact_is_refused_for_a_held_contribution(self):
        from test_lifecycle_release import (NativeStore, MERGE_A, SOURCE_A, ACTOR,
                                            payload as lifecycle_payload, scope as lifecycle_scope)
        issue, _ = chain('trial-a')
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.rows[0]['comments'] = issue['comments']
        before = len(store.rows)
        for dimension in ('reviewed', 'integrated'):
            with self.subTest(dimension=dimension):
                with self.assertRaisesRegex(ValueError, 'held contribution is not integration-ready'):
                    lifecycle.apply_native(
                        lifecycle_payload('trial-a', dimension, 'passed',
                                          operation='op-' + dimension,
                                          scope_value=lifecycle_scope(source=SOURCE_A,
                                                                      integration=MERGE_A)),
                        ACTOR, store.run)
        self.assertEqual(len(store.rows), before)

    def test_the_release_selection_skips_a_held_task_with_the_reason(self):
        from test_lifecycle_release import NativeStore, RELEASE_COMMIT
        issue, _ = chain('trial-a')
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.rows[0]['comments'] = issue['comments']
        selection = lifecycle.release_selection(
            store.rows,
            {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
             'release_id': 'release-1', 'environment': 'production'},
            lambda commit, release: True)
        self.assertEqual(selection['targets'], [])
        self.assertTrue(any('held from integration' in item['reason']
                            for item in selection['skipped']))
        self.assertTrue(any('open integration hold' in item['reason']
                            for item in selection['skipped']))

    def test_a_release_naming_a_held_task_is_refused(self):
        from test_lifecycle_release import (NativeStore, ACTOR, release_payload, target)
        issue, _ = chain('trial-a')
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.rows[0]['comments'] = issue['comments']
        before = len(store.rows)
        with self.assertRaisesRegex(ValueError, 'Refusing a release that names trial-a'):
            lifecycle.apply_release(release_payload([target('trial-a')]), ACTOR, store.run)
        self.assertEqual(len(store.rows), before)

    def broken_chain(self, task='trial-a', hold=True):
        """A chain that carries a hold AND cannot be parsed (kittrial-5bb.297).

        The chain() helper records contribute and approve (and a hold unless told
        not to); a raw comment with the kit's own prefix but unparsable bytes then
        breaks the read - a damaged write or a hostile edit is how such a history
        comes to be."""
        issue, contribution = chain(task, hold=hold)
        issue['comments'].append(dict(id='broken-1', text=w.PREFIX + '{"broken": not json',
                                      author=AUTHOR, created_at='2026-09-16T00:00:00Z'))
        return issue, contribution

    def test_reviewed_and_integrated_facts_refuse_a_task_whose_chain_cannot_be_read(self):
        # kittrial-5bb.297: the fail-open - an unparseable chain read as unheld - closed
        # on the fact gate: the refusal names the task and why it cannot be read.
        from test_lifecycle_release import (ACTOR, MERGE_A, SOURCE_A, NativeStore,
                                            payload as lifecycle_payload, scope as lifecycle_scope)
        issue, _ = self.broken_chain()
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.rows[0]['comments'] = issue['comments']
        before = len(store.rows)
        for dimension in ('reviewed', 'integrated'):
            with self.subTest(dimension=dimension):
                with self.assertRaisesRegex(ValueError, 'cannot be read'):
                    lifecycle.apply_native(
                        lifecycle_payload('trial-a', dimension, 'passed',
                                          operation='op-unreadable-' + dimension,
                                          scope_value=lifecycle_scope(source=SOURCE_A,
                                                                      integration=MERGE_A)),
                        ACTOR, store.run, operators=[OPERATOR])
        self.assertEqual(len(store.rows), before)

    def test_the_release_selection_skips_a_target_whose_chain_cannot_be_read(self):
        # kittrial-5bb.297: such a target is skipped with the reason, and the skipped
        # list is the release audit's report of it. A task with NO chain at all is
        # never flagged - it has no holds to hide.
        from test_lifecycle_release import NativeStore, RELEASE_COMMIT
        issue, _ = self.broken_chain()
        plain = row('trial-b', assignee=AUTHOR)          # no chain comments at all
        store = NativeStore(tasks=('trial-a', 'trial-b')).seed('trial-a').seed('trial-b')
        store.rows[0]['comments'] = issue['comments']
        store.rows[1]['comments'] = plain['comments']
        self.assertIsNone(lifecycle.unreadable_hold_chain(plain))
        self.assertIsNone(lifecycle.unreadable_hold_chain(None))
        selection = lifecycle.release_selection(
            store.rows,
            {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
             'release_id': 'release-1', 'environment': 'production'},
            lambda commit, release: True, operators=[OPERATOR])
        # The unreadable task is never offered; the plain no-chain task may be.
        self.assertNotIn('trial-a', [item['task'] for item in selection['targets']])
        reasons = [item['reason'] for item in selection['skipped']]
        self.assertTrue(any('unreadable hold chain' in reason and 'trial-a' in reason
                            and 'cannot be read' in reason for reason in reasons), reasons)

    def test_a_release_naming_a_task_whose_chain_cannot_be_read_is_refused(self):
        from test_lifecycle_release import (ACTOR, NativeStore, release_payload, target)
        issue, _ = self.broken_chain()
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.rows[0]['comments'] = issue['comments']
        before = len(store.rows)
        with self.assertRaisesRegex(ValueError,
                                    'Refusing a release that names trial-a.*cannot be read'):
            lifecycle.apply_release(release_payload([target('trial-a')]), ACTOR, store.run,
                                    operators=[OPERATOR])
        self.assertEqual(len(store.rows), before)

    def test_one_operator_void_makes_every_surface_agree(self):
        # kittrial-5bb.297 review item 'operators-on-every-surface': the repair the
        # refusal names must be seen by every surface, so ONE operator void of the
        # broken comment - read through the configured operators - releases them all:
        # the merge slot acquires, the facts are accepted, the release selection no
        # longer skips the task.
        import recovery
        from requirements import canonical_bytes
        from test_lifecycle_release import (ACTOR, MERGE_A, SOURCE_A, NativeStore,
                                            RELEASE_COMMIT, payload as lifecycle_payload,
                                            scope as lifecycle_scope)
        issue, _ = self.broken_chain(hold=False)
        broken = next(comment for comment in issue['comments'] if comment['id'] == 'broken-1')
        void = dict(schema_version=1, operation='void-record', operation_id='void-broken-1',
                    task='trial-a', target='broken-1', target_kind='contribution-review',
                    target_sha256=recovery.digest(broken['text']), original=broken['text'],
                    reason='Repairing the damaged record', disposition='void', operator=OPERATOR)
        issue['comments'].append(dict(id='void-broken', text=recovery.PREFIX + canonical_bytes(void).decode(),
                                      author=OPERATOR, created_at='2026-09-16T00:01:00Z'))
        run, calls = self.merge_run([issue])
        with scratch() as tmp:
            result = coordination.apply_native({'operation': 'merge-acquire', 'task': 'trial-a',
                                                'target': 'refs/heads/main'},
                                               OPERATOR, run, Path(tmp), review_writes=True,
                                               operators=[OPERATOR])
        self.assertTrue(result['acquired'])
        self.assertIsNone(lifecycle.unreadable_hold_chain(issue, [OPERATOR]))
        self.assertEqual(lifecycle.require_unheld_fact(issue, 'integrated', 'trial-a',
                                                      operators=[OPERATOR]), None)
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.rows[0]['comments'] = issue['comments']
        selection = lifecycle.release_selection(
            store.rows,
            {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
             'release_id': 'release-1', 'environment': 'production'},
            lambda commit, release: True, operators=[OPERATOR])
        self.assertNotIn('unreadable hold chain',
                         ' '.join(item['reason'] for item in selection['skipped']))

    def test_merge_acquire_is_refused_for_a_malformed_task_row(self):
        # kittrial-5bb.297 review item 4c: a malformed row used to give issue=None and
        # the hold gate passed; the acquisition is refused until the row is repaired.
        malformed = dict(row('trial-a'), malformed=True, error='JSON nested too deeply')
        base_run, calls = self.merge_run([malformed])

        def run(argv):
            if argv[:1] == ['list']:
                return '[]'                      # the malformed row's classify read
            return base_run(argv)
        with scratch() as tmp:
            with self.assertRaisesRegex(w.CoordinatorRuleRefusal, 'is malformed'):
                coordination.apply_native({'operation': 'merge-acquire', 'task': 'trial-a',
                                           'target': 'refs/heads/main'},
                                          OPERATOR, run, Path(tmp), review_writes=True)
        self.assertEqual([call for call in calls if call[:2] == ['merge-slot', 'acquire']], [])

    def test_merge_acquire_is_refused_for_a_task_whose_chain_cannot_be_read(self):
        # kittrial-5bb.297: the merge slot fails closed too, before the slot is read.
        issue, _ = self.broken_chain()
        run, calls = self.merge_run([issue])
        with scratch() as tmp:
            with self.assertRaisesRegex(w.CoordinatorRuleRefusal, 'cannot be read'):
                coordination.apply_native({'operation': 'merge-acquire', 'task': 'trial-a',
                                           'target': 'refs/heads/main'},
                                          OPERATOR, run, Path(tmp), review_writes=True)
        self.assertEqual([call for call in calls if call[:2] == ['merge-slot', 'check']], [])
        self.assertEqual([call for call in calls if call[:2] == ['merge-slot', 'acquire']], [])


class BundleObservationTests(unittest.TestCase):
    """The kit's host observation of a delivered bundle (item 5)."""

    def test_the_observation_reads_the_file_bytes_and_digest_without_git(self):
        with scratch() as tmp:
            path = Path(tmp) / 'delivery.bundle'
            data = b'# v2 git bundle\n' + b'not a real bundle body, but real bytes'
            path.write_bytes(data)
            observation = w.read_bundle_observation(path)
            self.assertEqual(observation['bytes'], len(data))
            self.assertEqual(observation['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(observation['heads'], [])
            self.assertTrue(observation['heads_error'])       # no git runner was supplied

    def test_a_missing_file_is_refused(self):
        with scratch() as tmp:
            with self.assertRaisesRegex(ValueError, 'not a readable file'):
                w.read_bundle_observation(Path(tmp) / 'nothing.bundle')

    def bounded(self, call, seconds=20):
        """``call()`` on a daemon thread with a wall-clock timeout (review item
        ``a-fifo-hangs``): a fifo opened WITHOUT ``O_NONBLOCK`` blocks for ever, so a
        regression fails by assertion here instead of hanging the module."""
        import threading
        outcome = {}

        def target():
            try:
                outcome['value'] = call()
            except BaseException as error:              # noqa: BLE001 - ferried to the caller
                outcome['error'] = error

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(seconds)
        self.assertFalse(worker.is_alive(), 'the observation blocked: a fifo was opened blocking')
        if 'error' in outcome:
            raise outcome['error']
        return outcome.get('value')

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs are POSIX-only')
    def test_a_fifo_in_place_of_a_bundle_is_refused_without_blocking(self):
        with scratch() as tmp:
            fifo = Path(tmp) / 'planted.bundle'
            os.mkfifo(str(fifo))
            with self.assertRaisesRegex(ValueError, 'not a regular file'):
                self.bounded(lambda: w.read_bundle_observation(fifo))

    def test_a_file_that_is_not_a_git_bundle_is_refused_before_it_is_hashed(self):
        # A mutant that removed the signature check would return the arbitrary file's
        # digest and size, so this asserts the refusal AND that no observation came back
        # (review item 5: the is-a-file/signature checks must die by assertion).
        with scratch() as tmp:
            path = Path(tmp) / 'not-a-bundle.txt'
            path.write_bytes(b'this is a plain text file, not a bundle\n' * 10)
            with self.assertRaisesRegex(ValueError, 'not a git bundle'):
                w.read_bundle_observation(path)
            # A directory is refused as a non-regular file, with the message, rather than
            # raising IsADirectoryError from read_bytes.
            with self.assertRaisesRegex(ValueError, 'not a regular file'):
                w.read_bundle_observation(Path(tmp))
            # A symlink is refused instead of followed, so a verifier cannot point the
            # command at any file the host account can read.
            link = Path(tmp) / 'link.bundle'
            try:
                link.symlink_to(path)
            except (OSError, NotImplementedError):
                pass
            else:
                with self.assertRaisesRegex(ValueError, 'not be a symlink'):
                    w.read_bundle_observation(link)

    def test_a_bundle_over_the_stated_size_limit_is_refused(self):
        with scratch() as tmp:
            path = Path(tmp) / 'big.bundle'
            path.write_bytes(b'# v2 git bundle\n' + b'x')
            import review_workflow
            saved = review_workflow.BUNDLE_MAX_BYTES
            review_workflow.BUNDLE_MAX_BYTES = 4
            self.addCleanup(setattr, review_workflow, 'BUNDLE_MAX_BYTES', saved)
            with self.assertRaisesRegex(ValueError, 'larger than the 4-byte observation limit'):
                w.read_bundle_observation(path)

    def git(self, *args, cwd):
        done = subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True)
        if done.returncode != 0:
            raise AssertionError('git %s failed: %s' % (' '.join(args), done.stderr.strip()))
        return done.stdout

    def listing_git(self, cwd):
        def run(argv):
            return self.git(*argv, cwd=cwd)
        return run

    def make_detached_bundle(self, root):
        repo = root / 'src'
        repo.mkdir()
        self.git('init', '-q', cwd=repo)
        self.git('config', 'user.email', 't@example.invalid', cwd=repo)
        self.git('config', 'user.name', 'Test', cwd=repo)
        (repo / 'a.txt').write_text('one\n', encoding='utf-8')
        self.git('add', 'a.txt', cwd=repo)
        self.git('commit', '-q', '-m', 'one', cwd=repo)
        (repo / 'a.txt').write_text('two\n', encoding='utf-8')
        self.git('commit', '-q', '-am', 'two', cwd=repo)
        self.git('checkout', '-q', '--detach', cwd=repo)
        sha = self.git('rev-parse', 'HEAD', cwd=repo).strip()
        bundle = root / 'detached.bundle'
        self.git('bundle', 'create', str(bundle), 'HEAD', cwd=repo)
        return bundle, sha

    def test_a_detached_head_bundle_is_observed_by_the_kit(self):
        if shutil.which('git') is None:
            self.skipTest('git is not available')
        with scratch() as tmp:
            root = Path(tmp)
            bundle, sha = self.make_detached_bundle(root)
            observation = w.read_bundle_observation(bundle, git=self.listing_git(root))
            self.assertEqual(observation['sha256'], hashlib.sha256(bundle.read_bytes()).hexdigest())
            self.assertEqual(observation['bytes'], bundle.stat().st_size)
            self.assertEqual(observation['heads'], [{'commit': sha, 'ref': 'HEAD'}])
            self.assertIsNone(observation['heads_error'])
            self.assertIsNone(w.bundle_commit_problem(observation, sha))
            # The observation feeds a real attestation: a listed verifier records the
            # observed tip, digest and byte count as verified.
            h = ChainHarness()
            h.setUp()
            contribution = h.send(h.contribution(commit=sha, sha256=observation['sha256']))['comment_id']
            h.send(h.attest(contribution, commit=sha, sha256=observation['sha256'],
                            bytes=observation['bytes']), VERIFIER)
            self.assertEqual(w.project(h.issue)['verification'], 'verified')

    def test_a_renamed_bundle_observes_the_same_bytes(self):
        if shutil.which('git') is None:
            self.skipTest('git is not available')
        with scratch() as tmp:
            root = Path(tmp)
            bundle, sha = self.make_detached_bundle(root)
            renamed = root / 'revision-2-renamed.bundle'
            renamed.write_bytes(bundle.read_bytes())
            first = w.read_bundle_observation(bundle, git=self.listing_git(root))
            again = w.read_bundle_observation(renamed, git=self.listing_git(root))
            self.assertEqual(first['sha256'], again['sha256'])
            self.assertEqual(first['bytes'], again['bytes'])
            # The DECLARED path is a label: the same bytes' digest verifies a revision that
            # declares a renamed path.
            h = ChainHarness()
            h.setUp()
            contribution = h.send(h.contribution(commit=sha, path='/deliveries/renamed.bundle',
                                                 sha256=again['sha256']))['comment_id']
            h.send(h.attest(contribution, commit=sha, sha256=again['sha256'],
                            bytes=again['bytes']), VERIFIER)
            self.assertEqual(w.project(h.issue)['verification'], 'verified')


class WebRouteHostOnlyTests(unittest.TestCase):
    """Item 6: the three operations are host-only, and the web route says so."""

    def command(self, operation, **extra):
        payload = dict(operation=operation, schema_version=1, operation_id='op-1',
                       previous=None, task_id='task-1', contribution='1', reason='because',
                       **extra)
        # An instance without __init__: the review mapping needs only the class field table.
        backend = http_service.EndpointBackend.__new__(http_service.EndpointBackend)
        return backend._command('reviews.add', 'principal', 'proj', payload, 'op-1')

    def test_the_web_route_refuses_attest_hold_and_release_hold(self):
        for operation in ('attest', 'hold', 'release-hold'):
            with self.subTest(operation=operation):
                with self.assertRaises(HttpError) as raised:
                    self.command(operation)
                self.assertIn('host-only', str(raised.exception))
                self.assertIn('admin.py attest-delivery', str(raised.exception))

    def test_the_web_route_still_maps_the_ordinary_operations(self):
        action, _, args, attachments = self.command('approve', summary='ok')
        self.assertEqual(action, 'review')
        body = json.loads(attachments['0']['text'])
        self.assertEqual(body['operation'], 'approve')


if __name__ == '__main__':
    unittest.main()
