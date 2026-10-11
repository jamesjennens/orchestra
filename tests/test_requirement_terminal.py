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
        self.assertNotIn(item['id'], http.read(self.path, 'alpha', ['brd'], self.native)['nonterminal_ids'])
        writes = len(self.native.writes())
        self.assertEqual(self.act(item)['state'], result['state']); self.assertEqual(len(self.native.writes()), writes)
        for action, body in [('accept', dict(expected_revision=2, expected_sha256=item['sha256'])),
                             ('revise', dict(expected_revision=2, expected_sha256=item['sha256'], title='Undo'))]:
            with self.assertRaisesRegex(ValueError, 'terminal'):
                http.apply(self.path, self.context, action, body, 'undo-'+action, self.native, item['id'])
        with self.assertRaisesRegex(ValueError, 'different terminal'):
            self.act(item, operation='other-terminal')
        self.assertEqual(len(self.native.writes()), writes)

    def recovery(self, item):
        return self.read(item)['pending_transition']

    def recover(self, item, action, saved, context=None):
        body=dict(original_operation_id=saved['operation_id'], expected_receipt_sha256=saved['expected_receipt_sha256'])
        if action=='abandon': body['reason']='No state was written; retain the attempted intent.'
        return http.apply(self.path, context or self.context, action, body, 'recovery-'+action, self.native, item['id'])

    def test_second_owner_finishes_saved_reason_after_reload_and_departure(self):
        item=self.accepted(); self.native.fail_comment_prefix=owner.REASON_PREFIX
        with self.assertRaisesRegex(ValueError,'native comment'): self.act(item)
        saved=self.recovery(item); self.assertTrue(saved['can_finish']); self.assertFalse(saved['can_abandon'])
        self.assertEqual(saved['state']['account_id'],ACCOUNT)
        self.native.fail_comment_prefix=None; second='usr_'+'b'*16; self.native.actor=second
        context=http.OwnerContext('alpha',second,governance.current(self.path,'alpha'))
        before=len(self.native.writes()); completed=self.recover(item,'finish',saved,context)
        self.assertEqual(completed['reason'],saved['reason']); self.assertEqual(completed['state'],saved['state'])
        self.assertEqual(len(self.native.writes()),before+2)
        again=self.recover(item,'finish',saved,context); self.assertEqual(again['state'],completed['state'])
        self.assertEqual(len(self.native.writes()),before+2)
        comments=[c for c in self.native.row(item['id'])['comments'] if c['text'].startswith((owner.STATE_PREFIX,owner.REASON_PREFIX))]
        self.assertEqual([c['author'] for c in comments],[ACCOUNT,ACCOUNT])
        receipt=terminal.read_receipt(terminal.receipt_path(self.path,saved['operation_id']))
        current=dict(operation_id=saved['operation_id'],expected_receipt_sha256=terminal.receipt_sha256(receipt))
        with self.assertRaisesRegex(ValueError,'already written'): self.recover(item,'abandon',current)

    def test_abandon_keeps_disposition_and_allows_new_transition_but_not_old_key(self):
        item=self.accepted(); self.native.fail_comment_prefix=owner.STATE_PREFIX
        with self.assertRaises(ValueError): self.act(item)
        saved=self.recovery(item); self.assertTrue(saved['can_abandon']); self.assertTrue(saved['can_finish'])
        before=len(self.native.writes()); released=self.recover(item,'abandon',saved)
        self.assertEqual(released['disposition'],'released'); self.assertEqual(len(self.native.writes()),before)
        prior=terminal.read_receipt(terminal.receipt_path(self.path,saved['operation_id']))
        self.assertEqual(prior['status'],'released'); self.assertEqual(prior['owner_state'],saved['state'])
        self.assertEqual(prior['terminal_disposition']['prior_receipt_sha256'],saved['expected_receipt_sha256'])
        self.assertEqual(self.read(item)['requirement_state'],'active'); self.assertIsNone(self.recovery(item))
        self.assertTrue(self.recover(item,'abandon',saved)['reconciled'])
        with self.assertRaisesRegex(ValueError,'abandoned'): self.act(item)
        self.native.fail_comment_prefix=None; self.assertEqual(self.act(item,operation='new-terminal')['requirement_state'],'withdrawn')

    def test_abandon_refuses_any_state_even_when_successor_is_no_longer_eligible(self):
        for prefix in (owner.STATE_PREFIX,owner.REASON_PREFIX):
            with self.subTest(prefix=prefix):
                self.setUp(); item=self.accepted(); target=self.accepted('replacement')
                self.native.fail_comment_prefix=prefix
                with self.assertRaises(ValueError): self.act(item,'supersede',successor={k:target[k] for k in ('id','revision','sha256')})
                self.native.fail_comment_prefix=None; self.act(target,operation='target-terminal')
                saved=self.recovery(item); self.assertFalse(saved['can_finish'])
                self.assertEqual(saved['can_abandon'],prefix==owner.STATE_PREFIX)
                with self.assertRaisesRegex(ValueError,'successor.*eligible'): self.recover(item,'finish',saved)
                if prefix==owner.STATE_PREFIX: self.recover(item,'abandon',saved)
                else:
                    with self.assertRaisesRegex(ValueError,'already written'): self.recover(item,'abandon',saved)

    def test_abandon_requires_complete_native_absence_and_refuses_unreadable_export(self):
        item=self.accepted(); self.native.fail_comment_prefix=owner.STATE_PREFIX
        with self.assertRaises(ValueError): self.act(item)
        saved=self.recovery(item); self.native.rows.append(dict(id='unreadable',malformed=True,labels=[],comments=[]))
        before=len(self.native.writes())
        with self.assertRaisesRegex(ValueError,'absence'): self.recover(item,'abandon',saved)
        self.assertEqual(len(self.native.writes()),before)

    def test_reason_bounds_stored_bytes_and_pending_edit_advice(self):
        item=self.accepted()
        for reason in ('z'*1001,'z'+' '*5000,' '*1000):
            with self.subTest(length=len(reason)),self.assertRaisesRegex(ValueError,'1000'):
                self.act(item,reason=reason)
        self.assertFalse(terminal.receipt_path(self.path,'terminal-first').exists())
        state,reason=owner.terminal_evidence('alpha',records.existing_revisions(self.native.row(item['id']))[2],
            ACCOUNT,'length-proof',self.context.governance,'withdrawn',None,'z','now')
        reason['reason']='z'*1001; reason['sha256']=content_hash(reason)
        with self.assertRaisesRegex(ValueError,'1000'): owner.validate_reason(reason)
        changed=http.apply(self.path,self.context,'revise',dict(expected_revision=2,expected_sha256=item['sha256'],title='Pending'),
            'edit-pending',self.native,item['id'])
        with self.assertRaisesRegex(ValueError,'accept the pending edit first'): self.act(changed)

    def test_damaged_receipts_cost_their_item_or_file_and_not_a_healthy_requirement(self):
        item=self.accepted(); healthy=self.accepted('healthy'); self.native.fail_comment_prefix=owner.STATE_PREFIX
        with self.assertRaises(ValueError): self.act(item)
        self.native.fail_comment_prefix=None; path=terminal.receipt_path(self.path,'terminal-first'); original=path.read_bytes()
        for shape in ('invalid-json','array','empty','identity','hash','binding'):
            with self.subTest(shape=shape):
                path.write_bytes(original)
                if shape=='invalid-json': path.write_text('{bad')
                elif shape=='array': path.write_text('[]')
                elif shape=='empty': path.write_text('{}')
                else:
                    saved=json.loads(original)
                    if shape=='identity': saved['id']=healthy['id']
                    elif shape=='hash': saved['sha256']='0'*64
                    else: saved['owner_state_reason']['reason']='Altered'
                    path.write_text(json.dumps(saved))
                target=healthy if shape!='identity' else item
                self.assertEqual(self.read(target)['requirement_state'],'active')
                pending,damaged=terminal.scan(self.path,'alpha'); self.assertEqual(pending,[]); self.assertEqual(len(damaged),1)
                self.assertNotIn('pending',damaged[0]['message'])
                result=http.apply(self.path,self.context,'revise',dict(expected_revision=target['revision'],expected_sha256=target['sha256'],title='Still editable'),
                    'unrelated-edit-'+shape,self.native,target['id'])
                result=http.apply(self.path,self.context,'accept',dict(expected_revision=result['revision'],expected_sha256=result['sha256']),
                    'unrelated-accept-'+shape,self.native,target['id'])
                if target is healthy: healthy.update(result)
                else: item.update(result)

    def test_contributor_cannot_revise_terminal_requirement(self):
        item=self.accepted(); self.act(item); before=len(self.native.writes())
        with self.assertRaisesRegex(ValueError,'terminal'):
            records.apply_native(dict(schema_version=1,operation_id='contributor-terminal-edit',operation='revise',
                kind='requirement',task=item['id'],key='first',title='Changed',description='Changed',revision=3,acceptance_state='draft'),
                'contributor',self.native,self.path)
        self.assertEqual(len(self.native.writes()),before)

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
        active = self.accepted('active-target')
        self.act(second, operation='withdraw-second')
        for target in (first, second, narrative, dict(first,id='missing'), dict(active,sha256='0'*64),dict(active,revision=1)):
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
        comment=next(c for c in row['comments'] if c['text'].startswith(owner.REASON_PREFIX))
        retained=comment['text'];comment['text']=owner.REASON_PREFIX+'{bad'
        unknown=adapt(rows,selection_from_manifest(manifest),governance=governance.snapshot(self.path,'alpha'))
        self.assertEqual(unknown['manifest'],before['manifest'])
        self.assertEqual(unknown['provenance']['requirement_terminal_states'][item['id']]['requirement_state'],'unknown')
        comment['text']=retained
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

    def test_operator_finds_receipt_and_restored_owner_can_finish_or_abandon(self):
        item=self.accepted();self.native.fail_comment_prefix=owner.REASON_PREFIX
        with self.assertRaises(ValueError):self.act(item)
        saved=self.recovery(item);self.native.fail_comment_prefix=None
        result=records.reconcile(self.path,saved['operation_id'],'operator','Finish saved reason.','complete',self.native)
        self.assertEqual(result['state'],saved['state']);self.assertEqual(result['reason'],saved['reason'])
        empty=self.accepted('restore-empty');self.native.fail_comment_prefix=owner.STATE_PREFIX
        with self.assertRaises(ValueError):self.act(empty,operation='restore-pending')
        self.native.fail_comment_prefix=None
        receipts={http.JOURNAL+'/'+p.name:json.loads(p.read_text()) for p in (self.path/http.JOURNAL).glob('*.json')}
        restored=governance.restored_files(governance.snapshot(self.path,'alpha'),'alpha','beta')
        beta=self.path.parent/'beta';beta.mkdir()
        for name,value in {**restored,**receipts}.items():
            target=beta/name;target.parent.mkdir(exist_ok=True);target.write_bytes(canonical_bytes(value))
        pending=http.read(beta,'beta',['get',empty['id']],self.native)['pending_transition']
        context=http.OwnerContext('beta','usr_'+'b'*16,governance.current(beta,'beta'))
        fields=dict(original_operation_id=pending['operation_id'],expected_receipt_sha256=pending['expected_receipt_sha256'],reason='Abandon the untouched restored attempt.')
        released=http.apply(beta,context,'abandon',fields,'restore-abandon',self.native,empty['id'])
        self.assertEqual(released['disposition'],'released')
        self.assertEqual(terminal.read_receipt(terminal.receipt_path(beta,pending['operation_id']))['terminal_disposition']['project'],'alpha')

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
