import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requirement_governance as governance
import requirement_http as http
import requirement_owner_records as owner
import requirement_records as records
import reserved_comments
from requirements import canonical_bytes, content_hash
from test_requirement_records import Native

ACCOUNT = 'usr_' + 'a' * 16


class OwnerNative(Native):
    def __init__(self):
        super().__init__()
        self.actor = ACCOUNT
        self.seed('job-1', issue_type='job')

    def __call__(self, args):
        result = super().__call__(args)
        if args[0] == 'create' and '--dry-run' not in args:
            row = self.row(json.loads(result)['id'])
            row['created_by'] = self.actor
            row['issue_type'] = args[args.index('--type') + 1]
        return result


class OwnerEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        governance.initialize(self.path, 'alpha', ACCOUNT, 'creation-alpha')
        self.context = http.OwnerContext('alpha', ACCOUNT, governance.current(self.path, 'alpha'))
        self.native = OwnerNative()

    def create(self):
        return http.apply(self.path, self.context, 'create', {
            'kind': 'requirement', 'parent': 'job-1', 'title': 'One thing',
            'description': 'The owner edits this.'}, 'new-requirement', self.native)

    def accept(self, item, operation='accept-requirement'):
        return http.apply(self.path, self.context, 'accept', {
            'expected_revision': item['revision'], 'expected_sha256': item['sha256']},
            operation, self.native, task=item['id'])

    def test_poisoned_row_is_marked_while_healthy_accepted_content_survives(self):
        first = self.create(); accepted = self.accept(first)
        bad = self.native.seed('broken-1', labels=['requirement'], comments=[
            {'id': 'bad', 'author': 'alice', 'text': 'Kind: requirement-owner-xyz-v1'}])
        # Give it real content before poisoning its separate authority ledger.
        bad['comments'].insert(0, dict(self.native.row(first['id'])['comments'][0]))
        bad['comments'][0]['text'] = records.REVISION_PREFIX + '{broken'
        for mode in ('simple', 'governed'):
            state = governance.current(self.path, 'alpha')
            governance.set_mode(self.path, 'alpha', mode, ACCOUNT, 'mode-'+mode,
                                state['revision'], state['sha256'])
            for command in ('list', 'brd'):
                result = http.read(self.path, 'alpha', [command], self.native)
                self.assertEqual(next(i for i in result['items'] if i['id']==first['id'])['accepted']['sha256'], accepted['sha256'])
                self.assertTrue(next(i for i in result['items'] if i['id']=='broken-1')['unreadable'])
            with self.assertRaisesRegex(ValueError, 'broken-1.*cannot be read'):
                http.read(self.path, 'alpha', ['get', 'broken-1'], self.native)

    def test_whole_owner_family_is_reserved_including_unknown_and_incomplete_spelling(self):
        for suffix in ('acceptance-v1 ', 'acceptance-v1\r{}', 'acceptance-v2\n{}', 'xyz-v1', ''):
            body = 'Kind: requirement-owner-'+suffix
            with self.subTest(body=body):
                self.assertIsNotNone(reserved_comments.reserved_match(body))
                self.assertIsNotNone(reserved_comments.reserved_match('\ufeff'+body))
                with self.assertRaises(ValueError):
                    reserved_comments.check_comment_body(body, 'positional', actor='alice', task='req-1')

    def test_accepted_text_and_decisions_are_immutable_while_pending_edit_is_separate(self):
        first = self.create(); accepted = self.accept(first)
        evidence = owner.existing_acceptances(self.native.row(first['id']))[2]
        self.native.row(evidence['decision']['decision_id'])['title'] = 'Contributor retitled this'
        self.native.seed('unrelated-decision', title='Forged owner direction', issue_type='decision')
        http.apply(self.path, self.context, 'revise', {
            'expected_revision': accepted['revision'], 'expected_sha256': accepted['sha256'],
            'title': 'Pending title', 'description': 'Pending body'}, 'pending-edit', self.native, first['id'])
        result = http.read(self.path, 'alpha', ['brd'], self.native)
        self.assertEqual(result['items'][0]['accepted']['description'], 'The owner edits this.')
        self.assertEqual(result['items'][0]['pending_draft']['description'], 'Pending body')
        self.assertEqual(result['decisions'], [{'id': evidence['decision']['decision_id'],
            'requirement_id': first['id'], 'title': 'Accepted One thing', 'revision': 2}])
        self.assertNotIn('questions', result)

    def test_accepting_latest_accepted_content_is_noop(self):
        first = self.create(); accepted = self.accept(first); before = len(self.native.writes())
        repeated = self.accept(accepted, 'already-accepted')
        self.assertEqual(repeated['sha256'], accepted['sha256'])
        self.assertEqual(len(self.native.writes()), before)

    def test_cli_requirements_reads_route_to_requirements_action(self):
        import client
        import io
        configuration = self.path/'synthetic-client.json'; configuration.write_text('{}')
        for command in (['list'], ['get','req-1'], ['brd'], ['governance'], ['snapshot']):
            with patch.object(sys, 'argv', ['client.py','--config',str(configuration),
                    '--project','alpha','--actor','reader','--','requirements',*command]), \
                    patch.object(client, 'request', return_value={'returncode':0,'stdout':'{}','stderr':''}) as request, \
                    patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(client.main(), 0)
                self.assertEqual(request.call_args.args[3], command)
                self.assertEqual(request.call_args.args[4], 'requirements')

    def test_nel_create_and_revision_are_preserved_and_do_not_allocate_orphan(self):
        created = http.apply(self.path, self.context, 'create', {
            'kind': 'requirement', 'parent': 'job-1', 'title': 'NEL\u0085title',
            'description': 'Before\u0085after'}, 'nel-create', self.native)
        self.accept(created, 'nel-accept')
        result = http.read(self.path, 'alpha', ['get', created['id']], self.native)
        self.assertEqual(result['accepted']['description'], 'Before\u0085after')
        self.assertEqual(sum('requirement' in r['labels'] for r in self.native.rows), 1)

    def test_unreadable_anchor_stays_visible_by_filtered_membership_without_fields(self):
        first = self.create(); self.accept(first)
        healthy = copy.deepcopy(self.native.rows)
        marker = {'id': 'deep-row', 'malformed': True}
        def membership(args):
            self.assertNotIn('--type', args, 'Requirements use native task labels, not unsupported issue types')
            if args[0] == 'list':
                return json.dumps([{'id': 'deep-row'}] if '--label' in args and args[args.index('--label')+1]=='requirement' else [])
            return self.native(args)
        with patch.object(records, 'read_rows', return_value=healthy+[marker]):
            listing = http.read(self.path, 'alpha', ['list'], membership)
            self.assertEqual(len(listing['items']), 2)
            self.assertTrue(next(i for i in listing['items'] if i['id']=='deep-row')['unreadable'])
            with self.assertRaisesRegex(ValueError, 'deep-row.*cannot be read'):
                http.read(self.path, 'alpha', ['get', 'deep-row'], membership)

    def test_allocated_create_retry_finishes_same_row_after_unconfirmed_comment(self):
        self.native.fail_comment_prefix = records.REVISION_PREFIX
        with self.assertRaises(ValueError): self.create()
        allocated = next(r['id'] for r in self.native.rows if 'requirement' in r['labels'])
        listing = http.read(self.path, 'alpha', ['list'], self.native)
        self.assertTrue(listing['items'][0]['unreadable'])
        self.native.fail_comment_prefix = None
        recovered = self.create()
        self.assertEqual(recovered['id'], allocated)
        self.assertEqual(sum('requirement' in r['labels'] for r in self.native.rows), 1)

    def test_restored_current_source_mode_allows_destination_acceptance_and_further_restore(self):
        first = self.create(); self.accept(first)
        files = governance.restored_files(governance.snapshot(self.path, 'alpha'), 'alpha', 'beta')
        beta = self.path/'beta'; beta.mkdir()
        for name, value in files.items(): (beta/name).write_text(json.dumps(value))
        context = http.OwnerContext('beta', ACCOUNT, governance.current(beta, 'beta'))
        created = http.apply(beta, context, 'create', {'kind':'requirement', 'parent':'job-1',
            'title':'Restored project content', 'description':'Accepted without changing its source mode.'},
            'beta-create', self.native)
        accepted = http.apply(beta, context, 'accept', {'expected_revision':created['revision'],
            'expected_sha256':created['sha256']}, 'beta-accept', self.native, created['id'])
        self.assertEqual(http.read(beta, 'beta', ['get', created['id']], self.native)['accepted']['sha256'], accepted['sha256'])
        files = governance.restored_files(governance.snapshot(beta, 'beta'), 'beta', 'gamma')
        evidence = owner.existing_acceptances(self.native.row(created['id']))[2]
        governance.validate_evidence_files(files, 'gamma', evidence)
        files = governance.restored_files(files, 'gamma', 'delta')
        governance.validate_evidence_files(files, 'delta', evidence)

    def test_owner_no_operator_enrollment_writes_decision_then_evidence_then_revision(self):
        item = self.create()
        before = len(self.native.calls)
        accepted = self.accept(item)
        row = self.native.row(item['id'])
        self.assertEqual(accepted['revision'], 2)
        self.assertEqual(accepted['acceptance_state'], 'accepted')
        evidence = owner.existing_acceptances(row)[2]
        self.assertEqual(evidence['account_id'], ACCOUNT)
        decision = self.native.row(evidence['decision']['decision_id'])
        self.assertEqual(decision['issue_type'], 'decision')
        calls = self.native.calls[before:]
        writes = [call for call in calls if '--dry-run' not in call
                  and (call[0] in ('create', 'update') or call[:2] == ['comments', 'add'])]
        self.assertEqual(writes[0][0], 'create')
        self.assertTrue(writes[1][3].startswith(owner.ACCEPTANCE_PREFIX))
        self.assertTrue(writes[2][3].startswith(records.REVISION_PREFIX))
        self.assertFalse(records._host_acceptances(row))  # no host evidence fabricated
        self.assertEqual(records.existing_acceptances(row)[2], evidence)
        writes_before = len(self.native.writes())
        self.accept(item)
        self.assertEqual(len(self.native.writes()), writes_before)

    def test_edit_accepted_is_new_draft_and_preserves_historical_evidence(self):
        first = self.create(); accepted = self.accept(first)
        revised = http.apply(self.path, self.context, 'revise', {
            'expected_revision': accepted['revision'], 'expected_sha256': accepted['sha256'],
            'title': 'Changed thing', 'description': 'A new draft.'},
            'edit-requirement', self.native, task=first['id'])
        self.assertEqual(revised['acceptance_state'], 'draft')
        self.assertEqual(revised['revision'], 3)
        self.assertEqual(set(records.existing_revisions(self.native.row(first['id']))), {1, 2, 3})
        self.assertEqual(set(owner.existing_acceptances(self.native.row(first['id']))), {2})
        row = self.native.row(first['id'])
        self.assertTrue(records.resolved_acceptance(row, records.existing_revisions(row)[2]))
        self.assertFalse(records.resolved_acceptance(row, records.existing_revisions(row)[3]))
        view = http.read(self.path, 'alpha', ['get', first['id']], self.native)
        for revision in view['history']:
            self.assertEqual(content_hash(revision), revision['sha256'])

    def test_missing_owner_evidence_never_uses_an_accepted_revision_as_authority(self):
        first = self.create(); self.accept(first)
        row = self.native.row(first['id'])
        row['comments'] = [c for c in row['comments'] if not c['text'].startswith(owner.ACCEPTANCE_PREFIX)]
        self.assertFalse(records.resolved_acceptance(row, records.existing_revisions(row)[2]))
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            http.read(self.path, 'alpha', ['get', first['id']], self.native)

    def test_historical_owner_acceptance_survives_mode_change_but_not_wrong_project_or_chain(self):
        first = self.create(); self.accept(first)
        governance.set_mode(self.path, 'alpha', 'governed', ACCOUNT, 'switch-mode',
                            self.context.governance['revision'], self.context.governance['sha256'])
        view = http.read(self.path, 'alpha', ['get', first['id']], self.native)
        self.assertEqual(view['current']['acceptance_state'], 'accepted')
        row = self.native.row(first['id'])
        comment = next(c for c in row['comments'] if c['text'].startswith(owner.ACCEPTANCE_PREFIX))
        original = owner.parse(comment['text'])
        for changed in (dict(original, project='other'),
                        dict(original, governance=dict(original['governance'], sha256='0' * 64))):
            changed['sha256'] = content_hash(changed)
            comment['text'] = owner.body(changed)
            with self.assertRaises(ValueError):
                http.read(self.path, 'alpha', ['get', first['id']], self.native)
        comment['text'] = owner.body(original)

    def test_refused_stale_mode_and_cas_make_no_native_writes(self):
        first = self.create(); accepted = self.accept(first)
        before = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.accept(first, 'stale-click')
        governance.set_mode(self.path, 'alpha', 'governed', ACCOUNT, 'governed',
                            self.context.governance['revision'], self.context.governance['sha256'])
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.accept(accepted, 'mode-changed')
        self.assertEqual(len(self.native.writes()), before)

    def test_shared_export_publisher_proposal_and_historical_exact_refs(self):
        from export_requirements import adapt, selection_from_manifest
        from publish_brd import publish
        from proposal_records import accepting_revision
        from test_requirement_workflow import baseline, exported, seal
        first = self.create(); accepted = self.accept(first)
        row = self.native.row(first['id']); revisions = records.existing_revisions(row)
        self.assertEqual(accepting_revision(row, revisions, revisions[1]), 2)
        manifest = baseline()
        manifest['canonical_project'] = 'alpha'; manifest['job'] = 'job-1'
        manifest['requirements'][0] = revisions[2]
        seal(manifest)
        rows = [r for r in exported(manifest) if r['id'] != row['id']] + [row]
        history = governance.snapshot(self.path, 'alpha')
        with self.assertRaisesRegex(ValueError, 'governance snapshot'):
            adapt(rows, selection_from_manifest(manifest))
        result = adapt(rows, selection_from_manifest(manifest), governance=history)
        self.assertEqual(result['manifest'], manifest)
        self.assertEqual(result['provenance']['requirements_governance'], history)
        wrong_project = dict(selection_from_manifest(manifest), canonical_project='other')
        with self.assertRaisesRegex(ValueError, 'different project'):
            adapt(rows, wrong_project, governance=history)
        restored = governance.restored_files(history, 'alpha', 'restored')
        restored_selection = dict(selection_from_manifest(manifest), canonical_project='restored')
        self.assertEqual(adapt(rows, restored_selection, governance=restored)['manifest']['requirements'][0],
                         revisions[2])
        evidence = owner.existing_acceptances(row)[2]
        publication = records.publication_acceptance(dict(
            evidence['decision'], record_sha256=evidence['record_sha256']), manifest)
        self.assertEqual(publication['owners'], [ACCOUNT])
        with tempfile.TemporaryDirectory() as folder:
            self.assertTrue(publish(result['manifest'], folder).is_dir())
        changed = http.apply(self.path, self.context, 'revise', {
            'expected_revision': accepted['revision'], 'expected_sha256': accepted['sha256'],
            'description': 'Different draft.'}, 'shared-reader-edit', self.native, first['id'])
        revisions = records.existing_revisions(row)
        self.assertIsNone(accepting_revision(row, revisions, revisions[1]))
        self.assertTrue(records.resolved_acceptance(row, revisions[2]))
        self.assertFalse(records.resolved_acceptance(row, revisions[changed['revision']]))
        self.assertEqual(adapt(rows, selection_from_manifest(manifest), governance=history)['manifest'], manifest)
        forged = copy.deepcopy(rows)
        own_row = next(r for r in forged if r['id'] == row['id'])
        own_row['comments'] = [c for c in own_row['comments'] if not c['text'].startswith(owner.ACCEPTANCE_PREFIX)]
        with self.assertRaisesRegex(ValueError, 'acceptance evidence'):
            adapt(forged, selection_from_manifest(manifest), governance=history)

    def test_partial_evidence_write_retry_finishes_one_decision_and_exact_revision(self):
        item = self.create()
        self.native.fail_comment_prefix = records.REVISION_PREFIX
        with self.assertRaises(ValueError):
            self.accept(item)
        row = self.native.row(item['id'])
        self.assertEqual(set(owner.existing_acceptances(row)), {2})
        self.assertEqual(set(records.existing_revisions(row)), {1})
        self.native.fail_comment_prefix = None
        self.accept(item)
        self.assertEqual(set(records.existing_revisions(row)), {1, 2})
        self.assertEqual(sum(row['issue_type'] == 'decision' for row in self.native.rows), 1)
        self.assertEqual(sum(c['text'].startswith(owner.ACCEPTANCE_PREFIX)
                             for c in row['comments']), 1)

    def test_schema_hash_author_binding_and_raw_guard(self):
        item = self.create(); self.accept(item)
        row = self.native.row(item['id'])
        evidence = owner.existing_acceptances(row)[2]
        for change in ({'account_id': 'agent_' + 'a' * 16}, {'source': {'room': 'invented'}},
                       {'record_sha256': 'bad'}, {'extra': 'field'}, {'revision': True},
                       {'decision': dict(evidence['decision'], owners=['alice'])}):
            bad = dict(evidence, **change); bad['sha256'] = content_hash(bad)
            self.assertIsNone(owner.parse(owner.ACCEPTANCE_PREFIX + canonical_bytes(bad).decode()))
        forged = copy.deepcopy(row)
        for comment in forged['comments']:
            if comment['text'].startswith(owner.ACCEPTANCE_PREFIX):
                comment['author'] = 'alice'
        with self.assertRaisesRegex(ValueError, 'author'):
            owner.existing_acceptances(forged)
        for prefix in (owner.ACCEPTANCE_PREFIX, owner.STATE_PREFIX):
            with self.assertRaises(ValueError):
                reserved_comments.check_comment_body(prefix + '{}', 'positional', actor=ACCOUNT, task=item['id'])

    def test_caller_authority_fields_are_never_used(self):
        for field in ('account_id', 'governance', 'decision', 'actor', 'acceptance_state', 'operator'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'Unsupported'):
                http.apply(self.path, self.context, 'create', {
                    'kind': 'requirement', 'parent': 'job-1', 'title': 'A', 'description': 'B',
                    field: ACCOUNT}, 'bad-' + field, self.native)
        self.assertEqual(self.native.writes(), [])


if __name__ == '__main__':
    unittest.main()
