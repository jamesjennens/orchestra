"""Capability verification, slice 1b (kittrial-5bb.69; docs/CAPABILITY_INDEX_DESIGN.md section 5).

The record and its two write routes, the trust rules readers apply, the integrated-
commit test (with reverts and retractions read by the kit's own readers), native
order versus `checked_at`, the caps, the read cost and `views/CAPABILITIES.md`.

The fake bd is the capability fake plus what pinned bd 1.2.2 does for the narrow
lifecycle reads, as verified on a disposable database: `list --all --desc-contains X`
returns the rows (lifecycle event rows included, with their `dependencies`) whose
description contains X, and `list --all --parent T` returns T's children.
"""
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import capability_records as cr
import capability_verification as cv
import render
import test_follow_on_contributions as follow
from test_capability_records import CapabilityNative, entry
from test_reference_records import OPERATOR, TODAY, acceptance

KEY = 'review.structured-contribution'
VERIFIER = 'ci-host'
OPS = [OPERATOR, follow.OPERATOR]
INTEGRATED = follow.MERGE_1
OTHER = '1' * 40
PREFIX = cv.PREFIX


class VerifyNative(CapabilityNative):
    def __call__(self, args):
        if args[0] == 'list' and ('--desc-contains' in args or '--parent' in args):
            self.calls.append(list(args))
            rows = self.rows
            if '--desc-contains' in args:
                text = args[args.index('--desc-contains') + 1].lower()
                rows = [row for row in rows if text in (row.get('description') or '').lower()]
            if '--parent' in args:
                parent = args[args.index('--parent') + 1]
                rows = [row for row in rows if any(d.get('depends_on_id') == parent and d.get('type') == 'parent-child'
                                                   for d in row.get('dependencies') or [])]
            return json.dumps([self._bare(row) for row in rows])
        return super().__call__(args)


class VerificationCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.native = VerifyNative()
        clock = patch('time.gmtime', return_value=TODAY)
        clock.start()
        self.addCleanup(clock.stop)
        # The kit's own lifecycle/revert fixture: task-1, contribution 1 integrated at MERGE_1.
        self.fixture = follow.IntegrationRevertTests('test_void_record_retracts_a_host_issued_revert')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.contribution = self.fixture.integration_case()
        self.journal = self.fixture.journal
        self.capability_rows = self.native.rows
        self.propose()

    def sync(self):
        """The native project holds the capability anchors and the lifecycle task with its events."""
        for row in self.fixture.rows:
            row.setdefault('labels', [])
            row.setdefault('comments', [])
        self.native.rows = self.capability_rows + [row for row in self.fixture.rows
                                                   if row not in self.capability_rows]

    def run_native(self, args):
        self.sync()
        result = self.native(args)
        self.capability_rows = [row for row in self.native.rows if row not in self.fixture.rows]
        return result

    def propose(self, actor='alice', **extra):
        self.native.actor = actor
        return cr.apply_native(entry(**extra), actor, self.run_native, self.project)

    def accept(self, key=KEY, revision=1, operation_id='accept-1'):
        self.native.actor = OPERATOR
        row, _ = cr.anchor_for(cr.read_rows(self.run_native), key)
        sha = cr.existing_revisions(row)[revision]['sha256']
        return cr.apply_native({'schema_version': 1, 'operation_id': operation_id, 'operation': 'accept',
                                'key': key, 'revision': revision, 'record_sha256': sha,
                                'acceptance_state': 'accepted', 'acceptance': acceptance()},
                               OPERATOR, self.run_native, self.project, operator=True, operators=OPS)

    def current(self, key=KEY):
        view = self.get(key)
        return view['record'] or view['proposed']

    def payload(self, key=KEY, commit=OTHER, missing=(), unknown=(), checked_at='2026-10-01T12:00:00Z', **extra):
        row, _ = cr.anchor_for(cr.read_rows(self.run_native), key)
        revisions = cr.existing_revisions(row)
        accepted = [n for n, r in revisions.items() if r['acceptance_state'] != 'draft']
        record = revisions[max(accepted)] if accepted else revisions[max(revisions)]
        results = []
        for pointer in record['code'] + record['tests'] + record['anchors']:
            resolved = False if pointer in missing else (None if pointer in unknown else True)
            results.append({'pointer': pointer, 'resolved': resolved,
                            'reason': None if resolved else ('symbol-missing' if resolved is False
                                                             else 'unsupported-file-type')})
        payload = {'schema_version': 1, 'key': key, 'revision': record['revision'], 'record_sha256': record['sha256'],
                   'commit': commit, 'checked_at': checked_at, 'source': 'ast', 'graph_built_at_commit': None,
                   'tool': {'name': 'orchestra-capability-check', 'version': '0.1.0'}, 'results': results,
                   'passed': all(item['resolved'] is True for item in results)}
        payload.update(extra)
        return payload

    def verify(self, actor='alice', operator=False, verifiers=(), **options):
        self.native.actor = actor
        return cv.verify(self.payload(**options), actor, self.run_native, operators=OPS, verifiers=list(verifiers),
                         journal=self.journal, operator=operator)

    def get(self, key=KEY, verifiers=()):
        return cr.read(['get', key], self.run_native, OPS, verifiers=list(verifiers), journal=self.journal)

    def state(self, key=KEY, verifiers=()):
        return self.get(key, verifiers)['verification']

    def anchor(self, key=KEY):
        return cr.anchor_for(cr.read_rows(self.run_native), key)[0]['id']

    def plant(self, payload, actor, says_verified, author):
        """A record written around the operations, as a raw native comment would be."""
        _, body = cv.build(payload, actor, says_verified)
        self.sync()
        self.native.add_comment(self.anchor(payload['key']), body, author=author)


class RecordTests(VerificationCase):
    def test_the_record_is_closed_and_written_by_the_operation(self):
        result = self.verify()
        self.assertEqual((result['passed'], result['identity'], result['reconciled']), (True, 'unverified', False))
        comment = self.native.row(self.anchor())['comments'][-1]
        self.assertTrue(comment['text'].startswith(PREFIX))
        record = cv.parse(comment['text'])
        self.assertEqual(set(record), set(cv.FIELDS))
        self.assertEqual(record['submitter'], {'actor': 'alice', 'person': None, 'identity': 'unverified',
                                               'submitted_by_agent': False})
        self.assertEqual((record['key'], record['revision'], record['commit']), (KEY, 1, OTHER))

    def test_every_malformed_payload_is_refused_before_any_write(self):
        good = self.payload()
        before = len(self.native.writes())
        cases = (
            (dict(good, commit='abc123'), 'full 40 or 64 character'),
            (dict(good, commit=OTHER.upper().replace('1', 'A')), 'full 40 or 64 character'),
            (dict(good, checked_at='yesterday'), 'checked_at'),
            (dict(good, source='guess'), 'source must be'),
            (dict(good, tool={'name': 'x'}), 'tool must be'),
            (dict(good, passed=False), 'passed must be true exactly'),
            (dict(good, submitter={'identity': 'verified'}), 'unknown field'),
            (dict(good, sha256='0' * 64), 'unknown field'),
            (dict(good, results=good['results'][:-1]), 'exactly the pointers'),
            (dict(good, results=good['results'] + [{'pointer': 'extra.py::f', 'resolved': True, 'reason': None}]),
             'exactly the pointers'),
            (dict(good, results=[dict(good['results'][0], reason='free text, not a code')] + good['results'][1:]),
             'carries no reason'),
            (dict(good, revision=2), 'does not match the current revision'),
            (dict(good, record_sha256='f' * 64), 'does not match the current revision'),
            (dict(good, key='no.such'), 'Unknown|no revision record|not found'),
            ({name: value for name, value in good.items() if name != 'tool'}, 'missing tool'),
        )
        for payload, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.native.actor = 'alice'
                cv.verify(payload, 'alice', self.run_native, operators=OPS, journal=self.journal)
        self.assertEqual(len(self.native.writes()), before)

    def test_a_check_with_only_unknown_pointers_is_not_recorded_and_a_missing_one_fails(self):
        with self.assertRaisesRegex(ValueError, 'neither a pass nor a failure'):
            self.verify(unknown=('review_workflow.py::execute',))
        failed = self.verify(missing=('review_workflow.py::execute',), unknown=('tests/test_review_workflow.py',))
        self.assertFalse(failed['passed'])
        self.propose(operation_id='bare', key='bare.thing', name='Bare', aliases=[], code=[], tests=[], anchors=[])
        with self.assertRaisesRegex(ValueError, 'nothing to verify'):
            self.verify(key='bare.thing')

    def test_idempotent_per_key_revision_commit_actor_and_route(self):
        first = self.verify()
        again = self.verify(checked_at='2026-10-02T09:00:00Z')
        self.assertEqual((again['reconciled'], again['comment_id']), (True, first['comment_id']))
        with self.assertRaisesRegex(ValueError, 'already recorded a different result'):
            self.verify(missing=('review_workflow.py::execute',))
        # Another author at the same commit is not blocked, and neither is a new commit.
        self.assertFalse(self.verify(actor='bob')['reconciled'])
        self.assertFalse(self.verify(commit='2' * 40)['reconciled'])
        # A contributor who names the operator as its actor does not pre-empt the operator's
        # own record on the host route.
        self.assertEqual(self.verify(actor=OPERATOR)['identity'], 'unverified')
        trusted = self.verify(actor=OPERATOR, operator=True)
        self.assertEqual((trusted['identity'], trusted['reconciled']), ('verified', False))

    def test_a_malformed_verification_record_fails_only_itself(self):
        self.verify()
        self.sync()
        self.native.add_comment(self.anchor(), PREFIX + '{"schema_version": 1}', author='mallory')
        view = self.get()
        self.assertEqual(view['verification']['state'], 'reported')
        self.assertIn('malformed-verification', [warning['code'] for warning in view['warnings']])


class TrustTests(VerificationCase):
    def test_the_endpoint_route_is_never_verified_whatever_actor_is_declared(self):
        self.assertEqual(self.state()['state'], 'unverified')
        result = self.verify(actor=OPERATOR, commit=INTEGRATED)
        self.assertEqual(result['identity'], 'unverified')
        block = self.state()
        self.assertEqual((block['state'], block['verified_at']), ('reported', None))
        self.assertEqual(block['report']['submitter'], {'person': None, 'identity': 'unverified'})

    def test_only_a_listed_actor_writes_verified_on_the_host_route(self):
        for actor in ('mallory', VERIFIER):
            with self.assertRaisesRegex(ValueError, 'not on the deployment operator allowlist or the verifiers'):
                self.verify(actor=actor, operator=True)
        self.assertEqual(self.verify(actor=VERIFIER, operator=True, verifiers=[VERIFIER],
                                     commit=INTEGRATED)['identity'], 'verified')
        block = self.state(verifiers=[VERIFIER])
        self.assertEqual(block['state'], 'verified')
        self.assertEqual(block['verified_at'], {'commit': INTEGRATED, 'checked_at': '2026-10-01T12:00:00Z',
                                                'person': 'operator:' + VERIFIER, 'integrated': True})
        # Revoked: the same record reads `reported`; re-listing restores it.
        self.assertEqual(self.state(verifiers=[])['state'], 'reported')
        self.assertEqual(self.state(verifiers=[VERIFIER])['state'], 'verified')

    def test_a_reader_needs_the_record_to_say_verified_and_its_author_to_be_that_listed_actor(self):
        cases = (('says so, wrong author', OPERATOR, True, 'mallory', 'reported'),
                 ('right author, does not say so', OPERATOR, False, OPERATOR, 'reported'),
                 ('says so, author not listed', 'mallory', True, 'mallory', 'reported'),
                 ('both hold', OPERATOR, True, OPERATOR, 'verified'))
        for index, (label, actor, says, author, expected) in enumerate(cases):
            self.plant(self.payload(commit='%040x' % (index + 2)), actor, says, author)
            self.assertEqual(self.state()['state'], expected, label)

    def test_a_trusted_pass_at_a_commit_that_is_not_integrated_reads_verified_and_says_so(self):
        self.verify(actor=OPERATOR, operator=True, commit=OTHER)
        block = self.state()
        self.assertEqual((block['state'], block['verified_at']['integrated']), ('verified', False))


class DriftTests(VerificationCase):
    MISSING = ('review_workflow.py::execute',)

    def test_drift_is_raised_by_anyone_and_cleared_only_by_a_trusted_pass_at_an_integrated_commit(self):
        self.verify(actor='bob', missing=self.MISSING)
        block = self.state()
        self.assertEqual((block['state'], block['open_failing']), ('drifted', 1))
        self.assertEqual(block['drift']['missing'], list(self.MISSING))
        # An untrusted pass, even at the integrated commit, clears nothing.
        self.verify(actor='carol', commit=INTEGRATED)
        self.assertEqual(self.state()['state'], 'drifted')
        # A trusted pass at a commit that is NOT integrated clears nothing either.
        self.verify(actor=OPERATOR, operator=True, commit=OTHER)
        self.assertEqual(self.state()['state'], 'drifted')
        # A trusted pass at the integrated commit clears it.
        self.verify(actor=OPERATOR, operator=True, commit=INTEGRATED)
        block = self.state()
        self.assertEqual((block['state'], block['open_failing'], block['verified_at']['integrated']),
                         ('verified', 0, True))
        # A later failing report, from anyone, drifts it again.
        self.verify(actor='dave', missing=self.MISSING, commit='3' * 40)
        self.assertEqual(self.state()['state'], 'drifted')

    def test_later_means_native_order_never_checked_at(self):
        # The trusted pass is written FIRST but stamped far in the future; the failing
        # report comes after it in native order with an earlier stamp. It still drifts.
        self.verify(actor=OPERATOR, operator=True, commit=INTEGRATED, checked_at='2099-01-01T00:00:00Z')
        self.assertEqual(self.state()['state'], 'verified')
        self.verify(actor='bob', missing=self.MISSING, checked_at='2020-01-01T00:00:00Z')
        self.assertEqual(self.state()['state'], 'drifted')

    def test_a_reverted_integration_does_not_clear_drift_and_a_retraction_restores_it(self):
        self.verify(actor='bob', missing=self.MISSING)
        self.verify(actor=OPERATOR, operator=True, commit=INTEGRATED)
        self.assertEqual(self.state()['state'], 'verified')
        receipt = self.fixture.operator_revert(follow.revert_payload(contribution=self.contribution))
        self.assertEqual(cv.integrated_commits(self.fixture.rows, OPS, self.journal), set())
        block = self.state()
        self.assertEqual(block['state'], 'drifted')
        self.fixture.retract(receipt['comment_id'])
        self.assertEqual(cv.integrated_commits(self.fixture.rows, OPS, self.journal), {INTEGRATED})
        self.assertEqual(self.state()['state'], 'verified')
        # A revert nobody on the allowlist authored is not honoured: the integration stands.
        self.assertEqual(cv.integrated_commits(self.fixture.rows, [], self.journal), {INTEGRATED})

    def test_a_trusted_failure_drifts_and_a_new_revision_starts_again(self):
        self.verify(actor=OPERATOR, operator=True, commit=INTEGRATED, missing=self.MISSING)
        self.assertEqual(self.state()['state'], 'drifted')
        self.native.actor = 'alice'
        sha = self.current()['sha256']
        cr.apply_native(entry(operation='revise', operation_id='rev-2', revision=2, expected_sha256=sha,
                              code=['review_workflow.py::run']), 'alice', self.run_native, self.project)
        block = self.state()
        self.assertEqual((block['state'], block['revision'], block['records']), ('superseded-revision', 2, 0))
        with self.assertRaisesRegex(ValueError, 'does not match the current revision'):
            self.native.actor = 'bob'
            cv.verify(dict(self.payload(), revision=1, record_sha256=sha), 'bob', self.run_native, operators=OPS)

    def test_an_accepted_revision_is_the_one_verified_while_a_newer_draft_waits(self):
        self.accept()
        accepted = self.current()
        self.assertEqual(accepted['revision'], 2)
        self.verify(actor=OPERATOR, operator=True, commit=INTEGRATED)
        self.native.actor = 'alice'
        cr.apply_native(entry(operation='revise', operation_id='rev-3', revision=3,
                              expected_sha256=accepted['sha256'], code=['review_workflow.py::run']),
                        'alice', self.run_native, self.project)
        block = self.state()
        self.assertEqual((block['state'], block['revision']), ('verified', 2))


class CapTests(VerificationCase):
    MISSING = ('review_workflow.py::execute',)

    def more(self, count):
        for index in range(count):
            self.propose(operation_id='more-%d' % index, key='more.k%d' % index, name='More %d' % index, aliases=[])

    def test_untrusted_records_are_capped_per_revision_and_trusted_ones_are_not(self):
        with patch.object(cv, 'CAP_UNTRUSTED_REVISION', 2):
            self.verify(actor='a1')
            self.verify(actor='a2')
            with self.assertRaisesRegex(ValueError, 'already has 2 untrusted verification records'):
                self.verify(actor='a3')
            self.assertEqual(self.verify(actor=OPERATOR, operator=True)['identity'], 'verified')

    def test_open_failing_reports_from_unverified_submitters_share_one_project_pool(self):
        self.more(2)
        with patch.object(cv, 'CAP_UNVERIFIED_FAILING', 2):
            self.verify(actor='bob', missing=self.MISSING)
            self.verify(actor='carol', key='more.k0', missing=self.MISSING)
            with self.assertRaisesRegex(ValueError, 'shared pool of open failing reports .* is full \\(2 per'):
                self.verify(actor='dave', key='more.k1', missing=self.MISSING)
            # A passing report is not a failing one, and a trusted failure is never capped.
            self.verify(actor='dave', key='more.k1')
            self.verify(actor=OPERATOR, operator=True, key='more.k1', missing=self.MISSING, commit='4' * 40)
            # A trusted pass at an integrated commit clears one, which frees the pool.
            self.verify(actor=OPERATOR, operator=True, commit=INTEGRATED)
            self.assertEqual(self.state()['state'], 'verified')
            self.verify(actor='erin', key='more.k1', missing=self.MISSING, commit='5' * 40)

    def test_the_host_batch_reports_each_item_and_holds_the_lock_once_per_item(self):
        self.more(2)
        items = [self.payload(commit=INTEGRATED), self.payload(key='more.k0', commit=INTEGRATED),
                 dict(self.payload(key='more.k1', commit=INTEGRATED), record_sha256='f' * 64)]
        holds = []

        @contextlib.contextmanager
        def lock():
            holds.append(len(self.native.writes()))
            yield

        with self.assertRaisesRegex(ValueError, 'not on the deployment operator allowlist or the verifiers'):
            cv.verify_batch({'schema_version': 1, 'items': items}, 'mallory', self.run_native, operators=OPS)
        with self.assertRaisesRegex(ValueError, 'must not repeat'):
            cv.verify_batch({'schema_version': 1, 'items': items[:1] * 2}, OPERATOR, self.run_native, operators=OPS)
        self.native.actor = OPERATOR
        done = cv.verify_batch({'schema_version': 1, 'items': items}, OPERATOR, self.run_native, operators=OPS,
                               journal=self.journal, lock=lock)
        self.assertEqual([item['result'] for item in done['items']], ['recorded', 'recorded', 'refused'])
        self.assertEqual((done['recorded'], done['refused'], len(holds)), (2, 1, 3))
        self.assertIn('does not match the current revision', done['items'][2]['reason'])
        again = cv.verify_batch({'schema_version': 1, 'items': items[:2]}, OPERATOR, self.run_native, operators=OPS)
        self.assertEqual([item['result'] for item in again['items']], ['already-recorded'] * 2)
        # One payload on its own is accepted too.
        single = cv.verify_batch(self.payload(commit='6' * 40), OPERATOR, self.run_native, operators=OPS)
        self.assertEqual(single['items'][0]['result'], 'recorded')


class ReadCostTests(VerificationCase):
    def reads(self):
        return [call[0] + (':' + next((flag[2:] for flag in ('--desc-contains', '--parent') if flag in call), '')
                           if call[0] == 'list' and ('--desc-contains' in call or '--parent' in call) else '')
                for call in self.native.calls if call[0] in ('list', 'show', 'export')]

    def test_integration_is_read_only_for_a_trusted_pass_and_then_narrowly(self):
        self.verify(actor='bob')
        self.native.calls = []
        self.assertEqual(self.state()['state'], 'reported')
        self.assertEqual(self.reads(), ['list', 'show', 'list'])          # exactly the slice 1a read
        self.verify(actor=OPERATOR, operator=True, commit=INTEGRATED)
        self.native.calls = []
        self.assertEqual(self.state()['state'], 'verified')
        # One list finds the lifecycle events naming the commit, one lists that task's
        # events, one show reads the task: no export.
        self.assertEqual(self.reads(), ['list', 'show', 'list', 'list:desc-contains', 'list:parent', 'show'])
        payload = self.payload(commit='7' * 40)
        self.native.calls = []
        self.native.actor = 'carol'                     # a passing report reads only its own key
        cv.verify(payload, 'carol', self.run_native, operators=OPS, journal=self.journal)
        self.assertEqual(self.reads(), ['list', 'show'])

    def test_the_integrated_set_is_computed_at_most_once_per_call(self):
        integrated = cv.Integrated(self.run_native, OPS, self.journal)
        self.native.calls = []
        self.assertTrue(integrated(INTEGRATED))
        self.assertTrue(integrated(INTEGRATED.upper()))                   # memoised, case-insensitive
        self.assertEqual(self.reads(), ['list:desc-contains', 'list:parent', 'show'])
        self.native.calls = []
        for index in range(8):
            self.assertFalse(integrated('%040x' % (index + 10)))
        # Narrow reads for the first commits, then ONE export answers every later one.
        self.assertEqual(self.reads().count('export'), 1)
        self.assertEqual(self.reads().count('list:desc-contains'), cv.NARROW_COMMITS - 1)
        self.native.calls = []
        self.assertTrue(integrated(INTEGRATED))
        self.assertFalse(integrated('9' * 40))
        self.assertEqual(self.reads(), [])

    def test_the_narrow_read_and_the_export_agree(self):
        narrow = cv.Integrated(self.run_native, OPS, self.journal)
        self.sync()
        exported = cv.Integrated(None, OPS, self.journal, export_rows=self.native.rows)
        for commit in (INTEGRATED, follow.COMMIT_1, OTHER):
            self.assertEqual(narrow(commit), exported(commit), commit)
        self.assertTrue(exported(INTEGRATED))
        self.assertFalse(exported(follow.COMMIT_1))    # the source commit is not the integration commit

    def test_list_derives_only_the_rows_it_shows_and_can_carry_the_pointers(self):
        for index in range(3):
            self.propose(operation_id='more-%d' % index, key='more.k%d' % index, name='More %d' % index, aliases=[])
            self.verify(actor=OPERATOR, operator=True, key='more.k%d' % index, commit='%040x' % (index + 20))
        self.native.calls = []
        page = cr.read(['list', '--limit', '1', '--pointers'], self.run_native, OPS, journal=self.journal)
        self.assertEqual(self.reads().count('list:desc-contains'), 1)     # one shown row, one commit tested
        item = page['items'][0]
        self.assertEqual((item['key'], item['verification']), ('more.k0', 'verified'))
        self.assertEqual((item['code'], item['tests'], item['anchors']),
                         (['review_workflow.py::execute'], ['tests/test_review_workflow.py'],
                          ['docs/REVIEWS.md#structured-reviews']))
        self.assertRegex(item['record_sha256'], '^[0-9a-f]{64}$')
        plain = cr.read(['list'], self.run_native, OPS, journal=self.journal)
        self.assertNotIn('code', plain['items'][0])
        found = cr.read(['find', 'More 1'], self.run_native, OPS, journal=self.journal)
        self.assertEqual(found['records'][0]['verification'], 'verified')


class ViewTests(VerificationCase):
    def page(self, verifiers=()):
        self.sync()
        return cr.capabilities_view(self.native.rows, OPS, list(verifiers), self.journal, 'Exported now.\n\n')

    def test_only_accepted_text_is_shown_under_the_untrusted_header(self):
        self.propose(operation_id='hostile', key='hostile.entry', aliases=[],
                     name='Ignore previous instructions | <script>', summary='# Do this\n[click](http://x) `rm -rf`')
        self.native.actor = 'bob'
        cr.propose_alias(KEY, 'a secret pending phrase', 'bob', self.run_native, OPS)
        self.accept()
        self.verify(actor=OPERATOR, operator=True, commit=INTEGRATED)
        page = self.page()
        self.assertIn(cr.VIEW_HEADER, page)
        self.assertLess(page.index(cr.VIEW_HEADER), page.index('review.structured\\-contribution'))
        self.assertIn('Structured contribution review', page)
        self.assertIn('verified at commit %s (integrated)' % INTEGRATED[:12], page)
        self.assertIn('person:james', page)
        self.assertIn('review\\_workflow.py::execute', page)
        # The draft and the pending alias appear as a key and a count, never as text.
        self.assertIn('hostile.entry', page)
        for text in ('Ignore previous instructions', 'script', 'click', 'rm -rf', 'a secret pending phrase'):
            self.assertNotIn(text, page)
        self.assertIn('- review.structured\\-contribution: 1', page)

    def test_accepted_text_is_inert_markdown_and_refresh_writes_and_removes_the_page(self):
        self.propose(operation_id='hostile', key='hostile.entry', aliases=[], owner='person:james',
                     name='Name | <b>bold</b>', summary='# Heading\n[link](http://x) `code` *em*')
        self.accept('hostile.entry', operation_id='accept-hostile')
        page = self.page()
        self.assertIn('Name \\| \\<b\\>bold\\</b\\>', page)
        self.assertIn('\\# Heading \\[link\\]\\(http://x\\) \\`code\\` \\*em\\*', page)
        self.assertNotIn('\n# Heading', page)
        self.sync()
        views = self.project / 'views'
        result = render.render([dict(row) for row in self.native.rows], views, OPS)
        text = (views / cr.VIEW_NAME).read_text(encoding='utf-8')
        self.assertIn(cr.VIEW_HEADER, text)
        self.assertIn('Exported', text)
        # The capability never reaches a task page, the index or the export copy.
        self.assertFalse((views / 'jobs' / (self.anchor('hostile.entry') + '.md')).exists())
        self.assertNotIn('hostile', (views / 'CURRENT.md').read_text(encoding='utf-8'))
        self.assertNotIn('hostile', (views / 'issues.jsonl').read_text(encoding='utf-8'))
        self.assertIn('issues', result)
        # A project with no capability has no page, and a stale one is removed.
        render.render([dict(row) for row in self.fixture.rows], views, OPS)
        self.assertFalse((views / cr.VIEW_NAME).exists())


if __name__ == '__main__':
    unittest.main()
