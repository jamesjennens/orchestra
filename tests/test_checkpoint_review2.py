"""Rollback staging, bounded windows, direction access and request guards."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import briefing as b
from requirements import canonical_bytes
from test_briefing import PROJECT, TASK, STAMP, rows, comment, checkpoint, append_checkpoint, save_cp


class CheckpointSecondReviewTests(unittest.TestCase):
    def test_every_requested_window_boundary(self):
        for count in (100,101,199,200,201,350,351,499,500,501):
            with self.subTest(count=count):
                values={'e%04d'%n:('%064x'%n) for n in range(count)}
                p=b.bounded_digests(values)
                self.assertEqual(len(p['digests']),min(count,200))
                self.assertEqual(len(p['older']),min(max(count-200,0),300))
                self.assertEqual(p['covered'],count)
                self.assertFalse(set(p['digests'])&set(p['older']))
                self.assertEqual(p['digests'],dict(list(values.items())[-200:]))

    def test_default_writer_is_old_shape_and_readers_accept_enabled_shape(self):
        data=rows();writes=[]
        b.save_checkpoint(data,PROJECT,TASK,checkpoint(data),'alice/session',
                          lambda args:(writes.append(args),json.dumps({'id':'cp1'}))[1])
        p=json.loads(writes[0][3][len(b.PREFIX):])
        self.assertEqual(set(p),set(checkpoint(data)))
        append_checkpoint(data,'cp1',p)
        save_cp(data,'cp2')
        self.assertEqual(b.checkpoints(data[0])[1],[])
        self.assertEqual(b.verify_checkpoint(data,PROJECT,TASK)['coverage'],'verified')

    def test_off_switch_refuses_new_fields_but_retry_of_written_request_works(self):
        data=rows();payload=checkpoint(data,provenance=b.bounded_digests(b.entry_digests(b.snapshot(data,PROJECT,TASK))))
        with self.assertRaisesRegex(ValueError,'off'):
            b.save_checkpoint(data,PROJECT,TASK,payload,'alice/session',lambda _:self.fail('write'))
        save_cp(data,'cp1',provenance=payload['provenance'])
        result=b.save_checkpoint(data,PROJECT,TASK,payload,'alice/session',lambda _:self.fail('retry write'))
        self.assertTrue(result['reconciled'])

    def test_installation_switch_is_default_off_boolean_only_and_cannot_follow_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            self.assertFalse(b.checkpoint_writes_enabled(root))
            marker=root/'deployment.private.json'
            for value in (False,True,'true',1,None):
                marker.write_text(json.dumps({'checkpoint_provenance_writes':value}),encoding='utf-8')
                if type(value) is bool:self.assertEqual(b.checkpoint_writes_enabled(root),value)
                else:
                    with self.assertRaisesRegex(ValueError,'boolean'):b.checkpoint_writes_enabled(root)

    def test_endpoint_default_and_enabled_writer_use_installation_setting(self):
        for enabled in (False,True):
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp);data=rows();writes=[]
                (root/'deployment.private.json').write_text(json.dumps({'checkpoint_provenance_writes':enabled}),encoding='utf-8')
                def run(args):
                    if args[0]=='export':return '\n'.join(json.dumps(r) for r in data)
                    writes.append(args);return json.dumps({'id':'cp1'})
                b.execute(root,root,PROJECT,'alice/session','checkpoint',[TASK,'@attachment:cp'],
                          {'cp':{'flag':'--file','text':json.dumps(checkpoint(data))}},run)
                payload=json.loads(writes[0][3][len(b.PREFIX):])
                self.assertEqual('provenance' in payload,enabled)

    def test_all_aged_directions_have_full_digest_in_paginated_read_and_can_resolve(self):
        data=rows()
        data[0]['comments'] += [comment('a%04d'%n,'Direction',author='reviewer') for n in range(501)]
        save_cp(data,'cp1')
        current=b.bounded_digests(b.entry_digests(b.snapshot(data,PROJECT,TASK)))
        collected=[];offset=0
        while True:
            page=b.direction_page(data,PROJECT,TASK,offset,100)
            collected+=page['items']
            if page['next_offset'] is None:break
            offset=page['next_offset']
        self.assertEqual(len(collected),501)
        earliest=collected[0]
        self.assertNotIn(earliest['id'],current['digests'])
        self.assertEqual(len(earliest['digest']),64)
        save_cp(data,'cp2',directions=[dict(id=earliest['id'],digest=earliest['digest'],state='resolved',note='done',evidence='test')])
        self.assertEqual(b.direction_page(data,PROJECT,TASK)['total'],500)
        self.assertEqual(b.brief(data,PROJECT,TASK)['directions']['total'],500)
        with tempfile.TemporaryDirectory() as temp:
            page=json.loads(b.execute(Path(temp),Path(temp),PROJECT,'alice/session','checkpoint',
                                      [TASK,'--directions','--limit','1','--offset','499'],{},
                                      lambda _: '\n'.join(json.dumps(r) for r in data)))
            self.assertIsNone(page['next_offset']);self.assertEqual(len(page['items']),1)

    def test_legacy_checkpoint_baselines_old_comments_and_says_unknown_once(self):
        data=rows();data[0]['comments'] += [comment(str(n),'Old',author='reviewer') for n in range(300)]
        append_checkpoint(data,'old',checkpoint(data))
        result=b.brief(data,PROJECT,TASK)
        self.assertEqual(result['directions']['total'],0)
        self.assertNotIn('OUTSTANDING',result['next_action'])
        self.assertEqual(sum('Direction baseline:' in x for x in result['warnings']),1)
        data[0]['comments'].append(comment('new','New','2026-09-16T00:00:00Z',author='reviewer'))
        result=b.brief(data,PROJECT,TASK)
        self.assertEqual(result['directions']['total'],1)
        self.assertEqual(result['newer']['other_count'],1)
        save_cp(data,'cp2')
        self.assertEqual(b.brief(data,PROJECT,TASK)['directions']['total'],1)
        self.assertEqual(b.checkpoint_queue_fields(data,data[0])['unresolved_directions'],1)

    def test_unassigned_checkpoint_author_is_own_actor(self):
        data=rows();data[0]['assignee']=None
        save_cp(data,'cp1')
        data[0]['comments'] += [comment('own','Own','2026-09-16T00:00:00Z'),
                               comment('other','New','2026-09-16T00:00:00Z',author='reviewer')]
        result=b.brief(data,PROJECT,TASK)
        self.assertEqual(result['directions']['total'],1)
        self.assertEqual(result['newer']['own_count'],1)
        self.assertEqual(result['newer']['other_count'],1)

    def test_reassignment_excludes_prior_owner_before_checkpoint_but_keeps_later_comments(self):
        data=rows();save_cp(data,'cp1');data[0]['assignee']='bob/session'
        result=b.brief(data,PROJECT,TASK)
        self.assertEqual(result['directions']['total'],0)
        data[0]['comments'].append(comment('old-owner-new','Later instruction','2026-09-16T00:00:00Z'))
        self.assertEqual(b.brief(data,PROJECT,TASK)['directions']['total'],1)

    def test_provenance_is_delta_after_first_record_and_retained_coverage_agrees(self):
        data=rows();data[0]['comments'] += [comment(str(n)) for n in range(300)]
        first=save_cp(data,'cp1');sizes=[len(canonical_bytes(first))]
        for n in range(2,6):
            payload=save_cp(data,'cp'+str(n));sizes.append(len(canonical_bytes(payload)))
            self.assertTrue(payload['provenance']['delta'])
            self.assertLess(sizes[-1],2000)
            self.assertNotIn('carried',payload)
            self.assertEqual(b.verify_checkpoint(data,PROJECT,TASK)['unverified'],0)
        self.assertLess(sum(sizes),sizes[0]+8000)
        data[0]['title']='Edited state'
        self.assertEqual(b.brief(data,PROJECT,TASK)['newer']['unverified_count'],0)

    def test_newest_evidence_wins_after_two_absorbed_edits(self):
        data=rows();save_cp(data,'cp1')
        data[0]['comments'][0]['text']='Second';save_cp(data,'cp2')
        data[0]['comments'][0]['text']='Third';save_cp(data,'cp3')
        result=b.verify_checkpoint(data,PROJECT,TASK)
        self.assertEqual(result['changed'],0)
        self.assertEqual(result['unchanged'],3)

    def test_caller_carried_is_refused_before_native_write(self):
        data=rows();digest=b.entry_digests(b.snapshot(data,PROJECT,TASK))[TASK+'-cfirst']
        payload=checkpoint(data,carried=[dict(id=TASK+'-cfirst',digest=digest,state='resolved',note='fake',evidence='fake')])
        with self.assertRaisesRegex(ValueError,'server-derived'):
            b.save_checkpoint(data,PROJECT,TASK,payload,'alice/session',lambda _:self.fail('write'),provenance_writes=True)

    def test_retry_is_bound_to_native_author_and_cannot_reconcile_others(self):
        data=rows();p=checkpoint(data);append_checkpoint(data,'cp1',p)
        with self.assertRaisesRegex(ValueError,'Stale previous'):
            b.save_checkpoint(data,PROJECT,TASK,p,'other/session',lambda _:self.fail('write'),provenance_writes=True)

    def test_unrelated_entry_id_is_refused_before_write(self):
        data=rows();payload=checkpoint(data,directions=[dict(id='other-task-c1',digest='0'*64,state='acknowledged')])
        with self.assertRaisesRegex(ValueError,'not an entry'):
            b.save_checkpoint(data,PROJECT,TASK,payload,'alice/session',lambda _:self.fail('write'),provenance_writes=True)

    def test_non_assignee_and_unassigned_cannot_record_dispositions(self):
        for assignee in ('alice/session',None):
            data=rows();data[0]['assignee']=assignee
            digest=b.entry_digests(b.snapshot(data,PROJECT,TASK))[TASK+'-cfirst']
            payload=checkpoint(data,directions=[dict(id=TASK+'-cfirst',digest=digest,state='acknowledged')])
            with self.assertRaisesRegex(ValueError,'current task assignee'):
                b.save_checkpoint(data,PROJECT,TASK,payload,'other/session',lambda _:self.fail('write'),provenance_writes=True)

    def test_pages_are_bounded_and_cursor_detects_edits(self):
        data=rows();data[0]['comments'].append(comment('d','Direction',author='reviewer'))
        page=b.direction_page(data,PROJECT,TASK)
        data[0]['comments'][-1]['text']='Edited'
        self.assertNotEqual(b.direction_page(data,PROJECT,TASK)['activity_cursor'],page['activity_cursor'])
        for offset,limit in ((-1,1),(0,0),(0,101),(2,1)):
            with self.assertRaises(ValueError):b.direction_page(data,PROJECT,TASK,offset,limit)
