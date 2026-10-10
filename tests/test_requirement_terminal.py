"""Terminal states retain historical acceptance and recover exact native effects."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import admin
import requirement_governance as governance
import requirement_http as http
import requirement_owner_records as owner
import requirement_records as records
import requirement_terminal as terminal
from requirements import canonical_bytes, content_hash
from test_requirement_owner_records import OwnerNative, ACCOUNT


class TerminalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'alpha'; self.path.mkdir()
        governance.initialize(self.path, 'alpha', ACCOUNT, 'creation-alpha')
        self.context = http.OwnerContext('alpha', ACCOUNT, governance.current(self.path, 'alpha'))
        self.native = OwnerNative()

    def accepted(self, key='first', kind='requirement'):
        draft = http.apply(self.path, self.context, 'create', dict(kind=kind, parent='job-1',
            title=key, description='Immutable '+key), 'create-'+key, self.native)
        return http.apply(self.path, self.context, 'accept', dict(expected_revision=draft['revision'],
            expected_sha256=draft['sha256']), 'accept-'+key, self.native, draft['id'])

    @staticmethod
    def expected(item):
        return dict(expected_revision=item['revision'], expected_sha256=item['sha256'],
                    expected_state_sha256=None, reason='No longer needed. <script>reason</script>')

    def act(self, item, action='withdraw', operation='terminal-first', **extra):
        return http.apply(self.path, self.context, action, dict(self.expected(item), **extra),
                          operation, self.native, item['id'])

    def read(self, item):
        return http.read(self.path, 'alpha', ['get', item['id']], self.native)

    def test_withdraw_preserves_every_revision_acceptance_and_never_reactivates(self):
        item = self.accepted(); row = self.native.row(item['id'])
        original = copy.deepcopy(records.existing_revisions(row)); acceptances = copy.deepcopy(records.existing_acceptances(row))
        result = self.act(item); self.assertEqual(result['requirement_state'], 'withdrawn')
        self.assertEqual(records.existing_revisions(row), original)
        self.assertEqual(records.existing_acceptances(row), acceptances)
        self.assertTrue(records.resolved_acceptance(row, original[2]))
        view = self.read(item); self.assertEqual(view['accepted']['sha256'], item['sha256'])
        self.assertEqual(view['requirement_state'], 'withdrawn'); self.assertEqual(len(view['history']), 2)
        self.assertNotIn(item['id'], http.read(self.path, 'alpha', ['brd'], self.native)['active_ids'])
        writes = len(self.native.writes())
        self.assertEqual(self.act(item)['state'], result['state']); self.assertEqual(len(self.native.writes()), writes)
        for action, body in [('accept', dict(expected_revision=2, expected_sha256=item['sha256'])),
                             ('revise', dict(expected_revision=2, expected_sha256=item['sha256'], title='Undo'))]:
            with self.assertRaisesRegex(ValueError, 'terminal'):
                http.apply(self.path, self.context, action, body, 'undo-'+action, self.native, item['id'])
        with self.assertRaisesRegex(ValueError, 'different terminal'):
            self.act(item, operation='other-terminal')
        self.assertEqual(len(self.native.writes()), writes)

    def test_supersession_is_exact_and_reverse_is_derived_not_two_writes(self):
        first = self.accepted(); successor = self.accepted('second')
        target_before = copy.deepcopy(self.native.row(successor['id']))
        reference = {key:successor[key] for key in ('id','revision','sha256')}
        result = self.act(first, 'supersede', successor=reference)
        self.assertEqual(result['state']['superseded_by'], reference)
        self.assertEqual(self.native.row(successor['id']), target_before)
        self.assertEqual(self.read(successor)['supersedes'], [{key:first[key] for key in ('id','revision','sha256')}])
        self.act(successor, operation='withdraw-successor')
        self.assertEqual(self.read(first)['state']['superseded_by'], reference)
        writes = len(self.native.writes())
        self.assertEqual(self.act(first, 'supersede', successor=reference)['state'], result['state'])
        self.assertEqual(len(self.native.writes()), writes)

    def test_stale_self_draft_narrative_and_terminal_successors_refuse_without_effects(self):
        first = self.accepted(); second = self.accepted('second'); narrative = self.accepted('narrative','brd-section')
        self.act(second, operation='withdraw-second')
        for target in (first, second, narrative, dict(first,id='missing'), dict(second,sha256='0'*64)):
            before = copy.deepcopy(self.native.rows); writes = len(self.native.writes())
            with self.assertRaises(ValueError):
                self.act(first, 'supersede', 'bad-'+target['id'], successor={key:target[key] for key in ('id','revision','sha256')})
            self.assertEqual(self.native.rows, before); self.assertEqual(len(self.native.writes()), writes)
        changed = http.apply(self.path, self.context, 'revise', dict(expected_revision=2,
            expected_sha256=first['sha256'], description='Pending change'), 'pending-edit', self.native, first['id'])
        with self.assertRaisesRegex(ValueError,'pending draft'):
            self.act(changed)

    def test_lost_comment_answers_reconcile_without_duplicate_events(self):
        for prefix in (owner.STATE_PREFIX, owner.REASON_PREFIX):
            with self.subTest(prefix=prefix):
                self.setUp(); item = self.accepted(); original = self.native.__call__
                def lose(args):
                    result = original(args)
                    if args[:2] == ['comments','add'] and args[3].startswith(prefix):
                        raise RuntimeError('Answer lost after native commit')
                    return result
                with self.assertRaisesRegex(RuntimeError,'Answer lost'):
                    http.apply(self.path, self.context, 'withdraw', self.expected(item), 'lost', lose, item['id'])
                self.assertEqual(self.read(item)['requirement_state'],'unknown')
                result = self.act(item,operation='lost')
                row = self.native.row(item['id'])
                self.assertEqual(result['requirement_state'],'withdrawn')
                self.assertEqual(sum(c['text'].startswith(owner.STATE_PREFIX) for c in row['comments']),1)
                self.assertEqual(sum(c['text'].startswith(owner.REASON_PREFIX) for c in row['comments']),1)
                self.assertEqual(self.read(item)['requirement_state'],'withdrawn')

    def test_pending_before_first_native_effect_blocks_edits_and_is_visible_unknown(self):
        item = self.accepted(); self.native.fail_comment_prefix=owner.STATE_PREFIX
        with self.assertRaisesRegex(ValueError,'native comment'):
            self.act(item)
        self.assertEqual(self.read(item)['requirement_state'],'unknown')
        before = len(self.native.writes())
        with self.assertRaisesRegex(ValueError,'pending'):
            http.apply(self.path,self.context,'revise',dict(expected_revision=2,
                expected_sha256=item['sha256'],title='Racing edit'),'racing-edit',self.native,item['id'])
        for operator in (False, True):
            with self.assertRaisesRegex(ValueError,'pending'):
                records.apply_native(dict(schema_version=1,operation_id='native-race',operation='revise',
                    kind='requirement',task=item['id'],key='first',title='Racing native edit',
                    description='Not applied',revision=3,acceptance_state='draft'),
                    'op',self.native,self.path,operator=operator,operators=['op'])
        self.assertEqual(len(self.native.writes()),before)
        second=self.accepted('second')
        reference={key:item[key] for key in ('id','revision','sha256')}
        with self.assertRaisesRegex(ValueError,'pending transition'):
            self.act(second,'supersede','pending-target',successor=reference)
        self.assertFalse(any(c['text'].startswith(owner.STATE_PREFIX) for r in self.native.rows for c in r['comments']))
        self.native.fail_comment_prefix=None; self.act(item)
        self.assertEqual(self.read(item)['requirement_state'],'withdrawn')

    def test_author_reason_chain_label_and_governance_damage_never_promote_active(self):
        item = self.accepted(); self.act(item); row = self.native.row(item['id']); healthy=copy.deepcopy(row)
        for damage in ('author','reason','second','labels','governance','malformed-state','malformed-reason','unsupported-state'):
            row.clear(); row.update(copy.deepcopy(healthy))
            state_comment = next(c for c in row['comments'] if c['text'].startswith(owner.STATE_PREFIX))
            if damage=='author': state_comment['author']='contributor'
            elif damage=='reason': row['comments']=[c for c in row['comments'] if not c['text'].startswith(owner.REASON_PREFIX)]
            elif damage=='labels': row['comments']=[c for c in row['comments'] if not c['text'].startswith((owner.STATE_PREFIX,owner.REASON_PREFIX))]
            elif damage=='malformed-state': state_comment['text']=owner.STATE_PREFIX+'{bad'
            elif damage=='unsupported-state': state_comment['text']=state_comment['text'].replace('state-v1','state-v2',1)
            elif damage=='malformed-reason':
                next(c for c in row['comments'] if c['text'].startswith(owner.REASON_PREFIX))['text']=owner.REASON_PREFIX+'{bad'
            else:
                state=owner.parse(state_comment['text'],state=True)
                if damage=='second': state['operation_id']='second-state'
                else: state['governance']['sha256']='0'*64
                state['sha256']=content_hash(state)
                if damage=='second': row['comments'].append(dict(state_comment,id='duplicate',text=owner.body(state,state=True)))
                else: state_comment['text']=owner.body(state,state=True)
            view=self.read(item); self.assertEqual(view['requirement_state'],'unknown',damage)
            self.assertEqual(view['accepted']['sha256'],item['sha256'])
        row.clear(); row.update(healthy)

    def test_unusable_terminal_receipt_is_bounded_unknown_and_never_writes(self):
        item=self.accepted(); self.act(item); path=terminal.receipt_path(self.path,'terminal-first')
        original=path.read_bytes(); writes=len(self.native.writes())
        for contents in ('{bad', '[]', '{}'):
            path.write_text(contents)
            self.assertIn(self.read(item)['requirement_state'],('unknown','withdrawn'))
            with self.assertRaisesRegex(ValueError,'receipt.*verified') as caught:
                self.act(item)
            self.assertNotIn(str(self.path),str(caught.exception))
            self.assertEqual(len(self.native.writes()),writes)
        path.write_bytes(original); path.unlink(); path.mkdir()
        with self.assertRaisesRegex(ValueError,'Unusable.*receipt'):
            self.act(item)
        self.assertEqual(len(self.native.writes()),writes)

    def test_shared_export_proposal_and_capability_consumers_preserve_exact_history(self):
        from export_requirements import adapt, selection_from_manifest
        from proposal_records import accepting_revision
        from capability_records import check_requirements
        from test_requirement_workflow import baseline, exported, seal
        item=self.accepted(); row=self.native.row(item['id']); revisions=records.existing_revisions(row)
        def native_with_show(args):
            if args[0]=='show':
                return json.dumps([self.native.row(rid) for rid in args[1:] if not rid.startswith('--')])
            return self.native(args)
        key=revisions[2]['key']; check_requirements([{'key':key}],native_with_show)
        self.assertEqual(accepting_revision(row,revisions,revisions[1]),2)
        manifest=baseline(); manifest['canonical_project']='alpha'; manifest['job']='job-1'
        manifest['requirements'][0]=revisions[2]; seal(manifest)
        rows=[r for r in exported(manifest) if r['id']!=row['id']]+[row]
        before=adapt(rows,selection_from_manifest(manifest),governance=governance.snapshot(self.path,'alpha'))
        terminal_result=self.act(item)
        after=adapt(rows,selection_from_manifest(manifest),governance=governance.snapshot(self.path,'alpha'))
        self.assertEqual(after['manifest'],before['manifest'])
        self.assertEqual(after['provenance']['requirement_terminal_states'][item['id']]['state'],terminal_result['state'])
        self.assertIsNone(accepting_revision(row,revisions,revisions[1]))
        with self.assertRaisesRegex(ValueError,'Unknown requirement link'):
            check_requirements([{'key':key}],native_with_show)
        check_requirements([{'key':key,'revision':2}],native_with_show)

    def test_raw_state_reason_and_terminal_labels_are_reserved(self):
        import reserved_comments
        for text in (owner.STATE_PREFIX+'{}',owner.REASON_PREFIX+'{}','\ufeff'+owner.REASON_PREFIX+'{}'):
            with self.assertRaises(ValueError):
                reserved_comments.check_comment_body(text,'positional',actor='worker',task='req-1')
        for label in owner.TERMINAL_LABELS:
            self.assertIsNotNone(reserved_comments.reserved_label(label))

    def test_backup_restored_lineage_keeps_terminal_history_and_receipt_hashes(self):
        item=self.accepted(); result=self.act(item)
        receipts={'.requirement-owner-requests/'+p.name:json.loads(p.read_text()) for p in (self.path/http.JOURNAL).glob('*.json')}
        admin.validate_coordination_files(receipts)
        restored=governance.restored_files(governance.snapshot(self.path,'alpha'),'alpha','beta')
        beta=self.path.parent/'beta'; beta.mkdir()
        for name,value in {**restored,**receipts}.items():
            path=beta/name; path.parent.mkdir(exist_ok=True); path.write_bytes(canonical_bytes(value))
        view=http.read(beta,'beta',['get',item['id']],self.native)
        self.assertEqual(view['state'],result['state']); self.assertEqual(view['requirement_state'],'withdrawn')
        mode=governance.current(beta,'beta'); governance.set_mode(beta,'beta','governed',ACCOUNT,'mode',mode['revision'],mode['sha256'])
        self.assertEqual(http.read(beta,'beta',['get',item['id']],self.native)['accepted']['sha256'],item['sha256'])


if __name__ == '__main__':
    unittest.main()
