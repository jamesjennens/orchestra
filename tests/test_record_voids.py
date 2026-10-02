"""Operator voids of reference and capability records, and orphan-anchor release (kittrial-5bb.74).

The .41 design (section 3.7) repairs a malformed catalog record through the existing
operator `void-record`. A void of a keyed record is a `record-void-v1` comment on the
anchor, validated by `recovery.records` (the review voids' own trust rule: an exact
record, its stored native author the payload's operator and on the deployment
allowlist, after its target, the target's bytes preserved). The anchor kind's readers
and writers leave out the comment an applied void names; a void of a record the entry
reads is refused at write and inert on read.

`admin.py anchor-release` closes an anchor that holds no record and frees its key, for
the case where only the lost original payload could have finished it.

Runs over the reference tests' fake bd (exact `list --label`, `show --include-comments`,
comment author = the acting actor).
"""
import contextlib
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin
import capability_records as cr
import capability_verification as cv
import recovery
import reference_records as rr
import reserved_comments
import review_workflow
from requirements import canonical_bytes
from test_capability_records import CapabilityCase, entry as capability_entry
from test_reference_records import OPERATOR, ReferenceCase, acceptance, entry

MALFORMED = 'Kind: reference-entry-v1\n{"not": "valid"}'


def void_payload(task, target, original, kind='reference-entry', operation_id='void-1', operator=OPERATOR,
                 **extra):
    payload = {'schema_version': 1, 'operation': 'void-record', 'operation_id': operation_id, 'task': task,
               'target': target, 'target_kind': kind, 'target_sha256': recovery.digest(original),
               'original': original, 'reason': 'Malformed record; repaired by the operator', 'disposition': 'void',
               'operator': operator}
    payload.update(extra)
    return payload


class VoidCase(ReferenceCase):
    def comment(self, task, prefix):
        return next(comment for comment in self.native.row(task)['comments'] if comment['text'].startswith(prefix))

    def void(self, payload, actor=OPERATOR, operators=(OPERATOR,), kind=rr.KIND):
        """The host route, `admin.py void-record`, as the anchor kind runs it."""
        self.native.actor = actor
        return kind.apply_void(payload, actor, self.native, list(operators))

    def plant_void(self, payload, author=OPERATOR):
        """A void written around the host command, as a raw native comment would be."""
        task = payload['task']
        return self.native.add_comment(task, recovery.PREFIX + canonical_bytes(payload).decode('utf-8'),
                                       author=author)

    def revise_as(self, operators, **extra):
        self.native.actor = 'alice'
        fields = dict(operation='revise', operation_id='alex-ref-2', revision=2, statement='Revised statement.')
        fields.update(extra)
        return rr.apply_native(entry(**fields), 'alice', self.native, self.project, operators=operators)

    def malformed_entry(self):
        """calendar.trading with a valid draft revision 1 and one malformed revision comment."""
        self.propose()
        digest = self.sha(1)
        bad = self.native.add_comment('ref-1', MALFORMED, author='alice')
        return digest, bad


class ReferenceVoidTests(VoidCase):
    def test_a_void_repairs_a_malformed_entry_for_readers_and_the_writer(self):
        digest, bad = self.malformed_entry()
        self.assertEqual(self.get()['state'], 'malformed')
        self.assertIn('1 entry skipped as malformed (ref-1)', rr.read(['list'], self.native, [OPERATOR])['coverage'])
        with self.assertRaisesRegex(ValueError, 'malformed reference revision comment'):
            self.revise_as([OPERATOR], expected_sha256=digest)
        writes = len(self.native.writes())

        result = self.void(void_payload('ref-1', bad['id'], MALFORMED))
        self.assertEqual(result, {'comment_id': 'c-3', 'reconciled': False, 'target': bad['id']})
        self.assertEqual(len(self.native.writes()), writes + 1)
        view = self.get()
        self.assertEqual((view['state'], view['proposed']['revision']), ('draft-only', 1))
        self.assertIn('record-voided', [warning['code'] for warning in view['warnings']])
        listing = rr.read(['list'], self.native, [OPERATOR])
        self.assertEqual((listing['total'], 'malformed' in listing['coverage']), (1, False))
        self.assertEqual(rr.work_attention(rr.read_rows(self.native), OPERATOR, [OPERATOR])['malformed'], 0)
        # Nothing is deleted, and the anchor stays hidden on every surface.
        row = self.native.row('ref-1')
        self.assertEqual([comment['id'] for comment in row['comments']], ['c-1', 'c-2', 'c-3'])
        self.assertEqual(row['comments'][1]['text'], MALFORMED)
        self.assertTrue(reserved_comments.is_record_anchor(row))
        self.assertEqual(reserved_comments.hide_records([row]), [])
        # The writer agrees with the readers once it is given the allowlist, as the endpoint
        # gives it; without it no void applies and the write still fails closed.
        with self.assertRaisesRegex(ValueError, 'malformed reference revision comment'):
            self.revise_as(None, expected_sha256=digest)
        revised = self.revise_as([OPERATOR], expected_sha256=digest)
        self.assertEqual((revised['revision'], self.get()['proposed']['revision']), (2, 2))

    def test_a_void_retry_is_idempotent_and_a_reused_id_or_target_is_refused(self):
        _, bad = self.malformed_entry()
        payload = void_payload('ref-1', bad['id'], MALFORMED)
        first = self.void(payload)
        writes = len(self.native.writes())
        self.assertEqual(self.void(payload), dict(first, reconciled=True))
        with self.assertRaisesRegex(ValueError, 'already used with different payload'):
            self.void(dict(payload, reason='Another reason'))
        with self.assertRaisesRegex(ValueError, 'already targets ' + bad['id']):
            self.void(dict(payload, operation_id='void-2'))
        self.assertEqual(len(self.native.writes()), writes)

    def test_a_void_by_a_non_operator_is_refused_and_a_planted_one_is_ignored(self):
        _, bad = self.malformed_entry()
        writes = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.void(void_payload('ref-1', bad['id'], MALFORMED, operator='alice'), actor='alice')
        with self.assertRaisesRegex(ValueError, 'No operator allowlist is configured'):
            self.void(void_payload('ref-1', bad['id'], MALFORMED), operators=())
        self.assertEqual(len(self.native.writes()), writes)
        # Planted around the host command: naming yourself, or naming an operator you are not.
        self.plant_void(void_payload('ref-1', bad['id'], MALFORMED, operator='alice'), author='alice')
        self.plant_void(void_payload('ref-1', bad['id'], MALFORMED, operation_id='void-2'), author='alice')
        view = self.get()
        self.assertEqual(view['state'], 'malformed')
        self.assertEqual([warning['code'] for warning in view['warnings']].count('void-invalid'), 2)
        # A real operator's void stops applying when that operator is removed (revocation)
        # and applies again when they are re-added.
        self.void(void_payload('ref-1', bad['id'], MALFORMED, operation_id='void-3'))
        self.assertEqual(self.get()['state'], 'draft-only')
        self.assertEqual(self.get(operators=('someone-else',))['state'], 'malformed')
        self.assertEqual(self.get()['state'], 'draft-only')

    def test_a_void_of_a_valid_accepted_revision_is_refused_and_inert(self):
        # The reader must keep showing the accepted revision: a void repairs history, it
        # never withdraws a decision (that is a new revision or a retirement).
        self.propose()
        self.accept(1, self.sha(1))
        accepted = self.comment('ref-1', 'Kind: reference-entry-v1\n{"acceptance_state":"accepted"')
        evidence = self.comment('ref-1', 'Kind: reference-acceptance-v1')
        writes = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'well-formed reference-entry record the entry reads'):
            self.void(void_payload('ref-1', accepted['id'], accepted['text']))
        with self.assertRaisesRegex(ValueError, 'well-formed reference-acceptance record the entry reads'):
            self.void(void_payload('ref-1', evidence['id'], evidence['text'], kind='reference-acceptance',
                                   operation_id='void-2'))
        self.assertEqual(len(self.native.writes()), writes)
        self.plant_void(void_payload('ref-1', accepted['id'], accepted['text']))
        view = self.get()
        self.assertEqual((view['state'], view['record']['revision'], view['acceptance']['operator']),
                         ('accepted', 2, OPERATOR))
        self.assertIn('void-refused', [warning['code'] for warning in view['warnings']])

    def test_a_void_naming_another_kind_or_another_anchor_is_refused(self):
        _, bad = self.malformed_entry()
        self.propose(operation_id='b-1', key='feed.units')
        writes = len(self.native.writes())
        refusals = (
            # another anchor: the target comment is not on the named task
            (void_payload('ref-2', bad['id'], MALFORMED), rr.KIND, 'missing or duplicated'),
            # another kind of the same family: the target is an entry, not acceptance evidence
            (void_payload('ref-1', bad['id'], MALFORMED, kind='reference-acceptance'), rr.KIND,
             'not a reference-acceptance record'),
            # another family: routed to the reference kind, or to the capability kind
            (void_payload('ref-1', bad['id'], MALFORMED, kind='capability-entry'), rr.KIND,
             'not a reference record kind'),
            (void_payload('ref-1', bad['id'], MALFORMED, kind='capability-entry'), cr.KIND,
             'not a capability anchor'),
            # a proposal kind (kittrial-5bb.68) is not a void target yet
            (void_payload('ref-1', bad['id'], MALFORMED, kind='requirement-proposal'), rr.KIND,
             'Unsupported operator void target kind'),
        )
        for payload, kind, message in refusals:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.void(payload, kind=kind)
        # The review owner refuses a reference record as its target too.
        rows = rr.read_rows(self.native)
        with self.assertRaisesRegex(ValueError, 'not a contribution-review record'):
            review_workflow.apply_void(rows, 'ref-1', OPERATOR,
                                       void_payload('ref-1', bad['id'], MALFORMED, kind='contribution-review'),
                                       self.native, operator=True, operators=[OPERATOR])
        self.assertEqual(len(self.native.writes()), writes)
        # Planted, they are ignored: ref-1 stays malformed, ref-2 reads as before.
        self.plant_void(void_payload('ref-2', bad['id'], MALFORMED))
        self.plant_void(void_payload('ref-1', bad['id'], MALFORMED, kind='reference-acceptance'))
        self.plant_void(void_payload('ref-1', bad['id'], MALFORMED, kind='capability-entry', operation_id='v-3'))
        self.assertEqual(self.get()['state'], 'malformed')
        self.assertEqual(self.get('feed.units')['state'], 'draft-only')
        self.assertIn('void-invalid', [warning['code'] for warning in self.get('feed.units')['warnings']])

    def test_one_of_a_conflicting_revision_pair_is_voided_and_the_survivor_is_protected(self):
        self.propose()
        forged = rr.entry_record(dict(entry(statement='A conflicting statement.'), operation='propose'), 1, 'draft')
        planted = self.native.add_comment('ref-1', rr.entry_comment(forged), author='mallory')
        self.assertEqual(self.get()['state'], 'malformed')
        original = self.native.row('ref-1')['comments'][0]
        self.void(void_payload('ref-1', planted['id'], planted['text']))
        view = self.get()
        self.assertEqual((view['state'], view['proposed']['statement']['text']),
                         ('draft-only', entry()['statement']))
        with self.assertRaisesRegex(ValueError, 'well-formed reference-entry record the entry reads'):
            self.void(void_payload('ref-1', original['id'], original['text'], operation_id='void-2'))

    def test_what_a_kit_without_keyed_voids_reads(self):
        # The rollback claim, checked against this kit's code paths with the void support
        # taken away: the anchor stays hidden, the entry reads malformed again (the older
        # keyed reader never looks at a void), and the older review reader lists the void
        # as invalid, never applied. Nothing has to be cleaned up before or after.
        _, bad = self.malformed_entry()
        self.void(void_payload('ref-1', bad['id'], MALFORMED))
        row = self.native.row('ref-1')
        with patch.object(rr.KIND, 'live_row', lambda row, operators: (row, [])):
            self.assertEqual(self.get()['state'], 'malformed')
        with patch.object(recovery, 'KIND_PREFIXES', dict(recovery.REVIEW_KIND_PREFIXES)):
            voids, _, invalid = recovery.records(row, [OPERATOR])
        self.assertEqual((voids, invalid), ([], ['c-3']))
        self.assertTrue(reserved_comments.is_record_anchor(row))
        self.assertEqual(self.get()['state'], 'draft-only')
        # On this kit a keyed void is no part of a review history, so a review read of
        # the anchor neither applies nor reports it (the anchor's kind does).
        self.assertEqual(review_workflow.project(row, operators=[OPERATOR])['warnings'], [])


class CapabilityVoidTests(CapabilityCase):
    KEY = 'review.structured-contribution'

    def void(self, task, comment, kind, operation_id='void-1', operators=(OPERATOR,)):
        self.native.actor = OPERATOR
        return cr.KIND.apply_void(void_payload(task, comment['id'], comment['text'], kind=kind,
                                               operation_id=operation_id), OPERATOR, self.native, list(operators))

    def get(self):
        return self.read('get', self.KEY)

    def verification_body(self, passed=True):
        row, _ = cr.anchor_for(cr.read_rows(self.native), self.KEY)
        record = cr.existing_revisions(row)[1]
        payload = {'schema_version': 1, 'key': self.KEY, 'revision': 1, 'record_sha256': record['sha256'],
                   'commit': 'a' * 40, 'checked_at': '2026-10-01T12:00:00Z', 'source': 'ast',
                   'graph_built_at_commit': None, 'tool': {'name': 'orchestra-capability-check', 'version': '0.1.0'},
                   'results': [{'pointer': pointer, 'resolved': True, 'reason': None}
                               for pointer in record['code'] + record['tests'] + record['anchors']],
                   'passed': passed}
        return cv.build(payload, 'alice', False)[1]

    def test_the_capability_kinds_reuse_the_same_void(self):
        anchor = self.propose()['native_id']
        digest = self.sha(1)
        check_body = self.verification_body()
        bad_entry = self.native.add_comment(anchor, 'Kind: capability-entry-v1\n{}', author='alice')
        bad_alias = self.native.add_comment(anchor, 'Kind: capability-alias-v1\n{}', author='alice')
        bad_check = self.native.add_comment(anchor, 'Kind: capability-verification-v1\n{}', author='alice')
        self.assertEqual(self.get()['state'], 'malformed')
        self.void(anchor, bad_entry, 'capability-entry')
        view = self.get()
        self.assertEqual(view['state'], 'draft-only')
        codes = [warning['code'] for warning in view['warnings']]
        self.assertTrue({'malformed-alias', 'malformed-verification'} <= set(codes))
        self.void(anchor, bad_alias, 'capability-alias', operation_id='void-2')
        self.void(anchor, bad_check, 'capability-verification', operation_id='void-3')
        codes = [warning['code'] for warning in self.get()['warnings']]
        self.assertEqual((codes.count('record-voided'), 'malformed-alias' in codes,
                          'malformed-verification' in codes), (3, False, False))
        # Well-formed alias and verification records are what the entry reads: protected.
        alias = self.alias(self.KEY, 'contribution flow')
        alias_comment = next(c for c in self.native.row(anchor)['comments'] if c['id'] == alias['comment_id'])
        check = self.native.add_comment(anchor, check_body, author='alice')
        for comment, kind, operation_id in ((alias_comment, 'capability-alias', 'void-4'),
                                            (check, 'capability-verification', 'void-5')):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'well-formed %s record' % kind):
                self.void(anchor, comment, kind, operation_id=operation_id)
        # The writer agrees: a contributor revision now passes compare-and-swap.
        self.native.actor = 'alice'
        revised = cr.apply_native(capability_entry(operation='revise', operation_id='cap-2', revision=2,
                                                   expected_sha256=digest, summary='Revised.'),
                                  'alice', self.native, self.project, operators=[OPERATOR])
        self.assertEqual(revised['revision'], 2)
        # A retirement finds a successor whose malformed record was voided.
        rows = cr.read_rows(self.native)
        self.assertNotIn(self.KEY, cr.newest_revisions(rows))
        self.assertIn(self.KEY, cr.newest_revisions(rows, [OPERATOR]))


class AnchorReleaseTests(VoidCase):
    def crash_before_close(self, **extra):
        self.native.close_outcome = 'crash'
        with self.assertRaises(RuntimeError):
            self.propose(**extra)
        self.native.close_outcome = 'ok'

    def release(self, issue_id='ref-1', actor=OPERATOR, operators=(OPERATOR,), kind=rr.KIND):
        self.native.actor = actor
        return kind.release(self.project, issue_id, actor, 'the original payload is lost', self.native,
                            operators=list(operators))

    def receipt(self):
        return json.loads(next((self.project / '.reference-requests').glob('*.json')).read_text(encoding='utf-8'))

    def test_an_open_orphan_is_closed_and_its_key_freed(self):
        self.crash_before_close()
        row = self.native.row('ref-1')
        self.assertEqual((row['status'], reserved_comments.is_record_anchor(row)), ('open', False))
        with self.assertRaisesRegex(ValueError, 'anchor-release'):
            self.propose(operation_id='someone-else')
        result = self.release()
        self.assertEqual((result['closed'], result['labels_removed'][-1], len(result['receipts_released'])),
                         (True, 'reference-key:calendar-trading', 1))
        row = self.native.row('ref-1')
        self.assertEqual(row['status'], 'closed')
        self.assertEqual(row['labels'], ['reference'])
        self.assertTrue(row['comments'][-1]['text'].startswith('Released by operator %s' % OPERATOR))
        self.assertFalse(reserved_comments.is_record_anchor(row))
        receipt = self.receipt()
        self.assertEqual((receipt['status'], receipt['reconciliation']['released_anchor']), ('released', 'ref-1'))
        admin.validate_coordination_files({'.reference-requests/' + 'a' * 64 + '.json': receipt})
        listing = rr.read(['list'], self.native, [OPERATOR])
        self.assertNotIn('incomplete', listing['coverage'])
        # The key is free: another operation proposes it on a new anchor, and so does the
        # original operation ID, now settled as released.
        self.assertEqual(self.propose(operation_id='someone-else')['native_id'], 'ref-2')
        self.assertEqual(self.get()['native_id'], 'ref-2')
        with self.assertRaisesRegex(ValueError, 'it was already released'):
            self.release()

    def test_a_release_is_refused_while_the_anchor_holds_a_record(self):
        self.propose()
        writes = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'still holds a record'):
            self.release()
        self.malformed_entry_on_new_key()   # a malformed record is still a record until it is voided
        with self.assertRaisesRegex(ValueError, 'still holds a record'):
            self.release('ref-2')
        self.assertEqual(len(self.native.writes()), writes)

    def malformed_entry_on_new_key(self):
        """feed.units whose only record comment is malformed (written around the writer)."""
        task = self.native.seed('ref-2', labels=['reference', 'reference:draft', 'reference-key:feed-units'],
                                status='closed')['id']
        bad = self.native.add_comment(task, MALFORMED, author='mallory')
        return task, bad

    def test_an_anchor_whose_every_record_is_voided_is_released_and_stays_hidden(self):
        task, bad = self.malformed_entry_on_new_key()
        self.assertEqual(self.get('feed.units')['state'], 'malformed')
        self.void(void_payload(task, bad['id'], MALFORMED))
        self.assertIn('1 incomplete anchor with no record yet (ref-2)',
                      rr.read(['list'], self.native, [OPERATOR])['coverage'])
        with self.assertRaisesRegex(ValueError, 'release the anchor'):
            self.get('feed.units')
        result = self.release(task)
        self.assertEqual((result['closed'], result['receipts_released']), (False, []))
        row = self.native.row(task)
        self.assertEqual(row['labels'], ['reference'])
        self.assertTrue(reserved_comments.is_record_anchor(row))   # still hidden: its comments are kept
        self.assertNotIn('incomplete', rr.read(['list'], self.native, [OPERATOR])['coverage'])
        self.assertEqual(self.propose(operation_id='f-1', key='feed.units')['native_id'], 'ref-1')

    def test_a_release_needs_a_configured_operator_and_a_matching_kind(self):
        self.crash_before_close()
        for actor, operators, message in (('alice', (OPERATOR,), 'not a server-side configured operator'),
                                          (OPERATOR, (), 'No operator allowlist')):
            with self.subTest(actor=actor), self.assertRaisesRegex(ValueError, message):
                self.release(actor=actor, operators=operators)
        with self.assertRaisesRegex(ValueError, 'not a capability anchor'):
            self.release(kind=cr.KIND)
        with self.assertRaisesRegex(ValueError, 'Unknown reference anchor'):
            self.release('ref-9')
        self.assertEqual(self.native.row('ref-1')['status'], 'open')
        self.assertEqual(self.receipt()['status'], 'pending')

    def test_an_interrupted_release_is_finished_by_a_rerun(self):
        self.crash_before_close()
        calls = []
        original = self.native.__class__.__call__

        def failing(native, args):
            if args[:1] == ['update'] and 'reference-key:calendar-trading' in args:
                calls.append(args)
                raise ValueError('native update failed')
            return original(native, args)

        with patch.object(self.native.__class__, '__call__', failing):
            with self.assertRaisesRegex(ValueError, 'native update failed'):
                self.release()
        self.assertEqual(len(calls), 1)
        self.assertIn('reference-key:calendar-trading', self.native.row('ref-1')['labels'])
        result = self.release()
        self.assertEqual((result['closed'], result['receipts_released'], result['labels_removed']),
                         (False, [], ['reference-key:calendar-trading']))
        notes = [c for c in self.native.row('ref-1')['comments'] if c['text'].startswith('Released by operator')]
        self.assertEqual(len(notes), 1)


class EndpointWriteTests(VoidCase):
    """The endpoint gives contributor writes the deployment allowlist, so they see operator voids."""

    def setUp(self):
        super().setUp()
        from test_reference_wiring import _endpoint_module
        self.endpoint = _endpoint_module(self)
        self.root = self.project / 'runtime'
        self.trial = self.root / 'projects' / 'p'
        (self.trial / '.beads').mkdir(parents=True)
        (self.trial / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': [OPERATOR]}), encoding='utf-8')
        env = patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''})
        env.start()
        self.addCleanup(env.stop)

    def fake_run(self, argv, env, timeout=None):
        command = list(map(str, argv))
        command = command[command.index('--sandbox') + 1:]
        if command[:1] == ['--actor']:
            self.native.actor = command[1]
            command = command[2:]
        try:
            stdout = self.native(command)
        except (ValueError, RuntimeError) as error:
            return types.SimpleNamespace(returncode=1, stdout='', stderr=str(error))
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr='')

    def execute(self, action, args, attachments=None, actor='alice'):
        request = {'project': 'p', 'actor': actor, 'action': action, 'args': args, 'attachments': attachments or {}}
        with patch.object(self.endpoint, 'project_dir', return_value=self.trial), \
                patch.object(self.endpoint, 'environment', return_value={}), \
                patch.object(self.endpoint.native, 'run', side_effect=self.fake_run), \
                patch.object(self.endpoint.fcntl, 'flock', Mock()):
            return self.endpoint.execute(self.root, request)

    def test_ref_revise_through_the_endpoint_sees_the_operator_void(self):
        digest, bad = self.malformed_entry()
        fields = {k: v for k, v in entry(operation_id='alex-ref-2', revision=2, expected_sha256=digest,
                                          statement='Revised statement.').items() if k != 'operation'}
        attachments = {'0': {'flag': '--file', 'text': json.dumps(fields)}}
        with self.assertRaisesRegex(ValueError, 'malformed reference revision comment'):
            self.execute('ref', ['revise', '@attachment:0'], attachments)
        self.void(void_payload('ref-1', bad['id'], MALFORMED))
        written = self.execute('ref', ['revise', '@attachment:0'], attachments)
        self.assertEqual(json.loads(written['stdout'])['revision'], 2, written)
        got = json.loads(self.execute('ref', ['get', 'calendar.trading'])['stdout'])
        self.assertEqual((got['state'], got['proposed']['revision']), ('draft-only', 2))


class AdminCommandTests(VoidCase):
    """`admin.py void-record` with a keyed target kind, and `admin.py anchor-release`."""

    def setUp(self):
        super().setUp()
        self.root = self.project / 'runtime'
        self.trial = self.root / 'projects' / 'trial'
        (self.trial / '.beads').mkdir(parents=True)
        (self.trial / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': [OPERATOR]}), encoding='utf-8')
        for patcher in (patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''}),
                        patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=Mock(), LOCK_EX=2)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_native(self, root, name, args):
        if args[:1] == ['--actor']:
            self.native.actor = args[1]
            args = args[2:]
        return self.native(list(args))

    def admin(self, *argv):
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', side_effect=self.run_native), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        return json.loads(out.getvalue())

    def test_void_record_routes_a_keyed_target_to_its_anchor_kind(self):
        _, bad = self.malformed_entry()
        path = self.root / 'void.json'
        path.write_text(json.dumps(void_payload('ref-1', bad['id'], MALFORMED)), encoding='utf-8')
        self.native.calls = []
        result = self.admin('void-record', 'trial', '--actor', OPERATOR, '--file', str(path))
        self.assertEqual((result['target'], result['reconciled']), (bad['id'], False))
        self.assertNotIn(['export', '--all'], self.native.calls)   # it reads only the anchor
        self.assertEqual(self.get()['state'], 'draft-only')
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.admin('void-record', 'trial', '--actor', 'alice', '--file', str(path))

    def test_anchor_release_is_a_host_command_for_operators(self):
        self.native.close_outcome = 'crash'
        with self.assertRaises(RuntimeError):
            self.propose()
        self.native.close_outcome = 'ok'
        argv = ('anchor-release', 'trial', '--kind', 'reference', '--issue-id', 'ref-1', '--reason', 'payload lost')
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.admin(*argv, '--actor', 'alice')
        result = self.admin(*argv, '--actor', OPERATOR)
        self.assertEqual((result['released'], result['closed']), ('ref-1', True))
        self.assertEqual(self.native.row('ref-1')['labels'], ['reference'])


if __name__ == '__main__':
    unittest.main()
