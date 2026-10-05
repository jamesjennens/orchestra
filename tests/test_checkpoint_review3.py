"""Conservative timestamp ties, cursor rollback and staged write controls."""
import copy
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from pathlib import Path

import admin
import briefing as b
from requirements import content_hash
from test_briefing import PROJECT,TASK,STAMP,rows,comment,checkpoint,append_checkpoint,save_cp


class CheckpointThirdReviewTests(unittest.TestCase):
    def test_legacy_same_second_later_comment_is_a_direction_in_brief_and_queue(self):
        data=rows();append_checkpoint(data,'legacy',checkpoint(data))
        data[0]['comments'].append(comment('later','Instruction',STAMP,author='coordinator'))
        read=b.brief(data,PROJECT,TASK)
        self.assertEqual(read['directions']['total'],1)
        self.assertEqual(read['newer']['other_count'],1)
        self.assertEqual(b.checkpoint_queue_fields(data,data[0])['unresolved_directions'],1)
        self.assertEqual(b.direction_page(data,PROJECT,TASK)['items'][0]['id'],TASK+'-clater')

    def test_old_cursor_and_first_provenance_cursor_are_current_until_actual_change(self):
        for shape in ('released','first-provenance'):
            with self.subTest(shape=shape):
                data=rows();snap=b.snapshot(data,PROJECT,TASK)
                cursor=b.activity_cursor(snap)
                if shape=='first-provenance':
                    cursor=b.token(dict(v=1,kind='activity',project=PROJECT,task=TASK,sha256=content_hash(snap)))
                append_checkpoint(data,'legacy',checkpoint(data,activity_cursor=cursor))
                self.assertFalse(b.brief(data,PROJECT,TASK)['checkpoint']['newer_activity'])
                self.assertIsNone(b.brief(data,PROJECT,TASK)['newer'])
                self.assertEqual(b.checkpoint_queue_fields(data,data[0])['newer_activity_coverage'],'current')
                data[0]['title']='Actual changed title'
                self.assertTrue(b.brief(data,PROJECT,TASK)['checkpoint']['newer_activity'])

    def test_default_write_cursor_matches_released_snapshot_hash(self):
        data=rows();snap=b.snapshot(data,PROJECT,TASK);snap.pop('entry_digests')
        expected=b.token(dict(v=1,kind='activity',project=PROJECT,task=TASK,sha256=content_hash(snap)))
        written=[]
        b.save_checkpoint(data,PROJECT,TASK,checkpoint(data),'alice/session',
                          lambda args:(written.append(args),json.dumps({'id':'legacy'}))[1])
        payload=json.loads(written[0][3][len(b.PREFIX):])
        self.assertEqual(payload['activity_cursor'],expected)

    def test_off_after_provenance_refuses_new_write_without_hiding_directions(self):
        data=rows();data[0]['comments'].append(comment('direction','Instruction',author='coordinator'))
        save_cp(data,'enabled')
        before=copy.deepcopy(data)
        self.assertEqual(b.direction_page(data,PROJECT,TASK)['total'],1)
        with self.assertRaisesRegex(ValueError,'already has checkpoint provenance'):
            b.save_checkpoint(data,PROJECT,TASK,checkpoint(data),'alice/session',lambda _:self.fail('write'))
        self.assertEqual(data,before)
        self.assertEqual(b.direction_page(data,PROJECT,TASK)['total'],1)

    def test_operator_switch_audits_both_transitions_atomically_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);marker=root/'deployment.private.json'
            marker.write_text(json.dumps(dict(password='test-only',operators=['ops'],unrelated='keep')))
            original=marker.read_bytes()
            with self.assertRaisesRegex(ValueError,'operator allowlist'):
                admin.checkpoint_provenance_switch(root,'on','worker')
            self.assertEqual(marker.read_bytes(),original)
            self.assertFalse(admin.checkpoint_provenance_switch(root,'status','ops')['checkpoint_provenance_writes'])
            self.assertEqual(marker.read_bytes(),original)
            self.assertTrue(admin.checkpoint_provenance_switch(root,'on','ops')['checkpoint_provenance_writes'])
            with redirect_stderr(io.StringIO()) as log:
                self.assertFalse(admin.checkpoint_provenance_switch(root,'off','ops')['checkpoint_provenance_writes'])
            cfg=json.loads(marker.read_text())
            self.assertNotIn('checkpoint_provenance_writes',cfg)
            self.assertEqual(cfg['unrelated'],'keep')
            audit=cfg['checkpoint_provenance_audit']
            self.assertEqual([(x['actor'],x['action'],x['previous'],x['enabled']) for x in audit],
                             [('ops','on',False,True),('ops','off',True,False)])
            self.assertTrue(all(x['at'] for x in audit))
            self.assertIn('older kits',log.getvalue())

    def test_existing_install_without_key_is_default_off(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'deployment.private.json').write_text('{"operators":[]}')
            self.assertFalse(b.checkpoint_writes_enabled(root))

    def test_own_comment_and_checkpoint_dispositions_refuse_without_write(self):
        data=rows();save_cp(data,'cp1')
        for target in ('first','cp1'):
            with self.subTest(target=target):
                digest=b.entry_digests(b.snapshot(data,PROJECT,TASK))[TASK+'-c'+target]
                payload=checkpoint(data,directions=[dict(id=TASK+'-c'+target,digest=digest,state='acknowledged')])
                with self.assertRaisesRegex(ValueError,'ordinary comment'):
                    b.save_checkpoint(data,PROJECT,TASK,payload,'alice/session',lambda _:self.fail('write'),provenance_writes=True)

    def test_dispositions_on_another_actors_checkpoint_or_review_record_refuse_without_write(self):
        # kittrial-5bb.131: the refusal was pinned only for the worker's own records (the
        # author check refuses those first); a record written by ANOTHER actor is refused
        # by its kind. A plain comment by the same other actor stays a valid target.
        from test_briefing import add_review
        data=rows()
        other_checkpoint=checkpoint(data,summary='Bob checkpointed this task')
        data[0]['comments'].append(comment('bobcp',b.PREFIX+json.dumps(other_checkpoint,sort_keys=True,separators=(',',':')),
                                           author='bob/session'))
        add_review(data,dict(schema_version=1,task=TASK,decision='approve',summary='Looks right'),'bobreview',
                   author='bob/session')
        data[0]['comments'].append(comment('bobnote','Please also check the docs',author='bob/session'))
        digests=b.entry_digests(b.snapshot(data,PROJECT,TASK))
        for target in ('bobcp','bobreview'):
            with self.subTest(target=target):
                entry=TASK+'-c'+target
                payload=checkpoint(data,directions=[dict(id=entry,digest=digests[entry],state='acknowledged')])
                with self.assertRaisesRegex(ValueError,'not own activity or a checkpoint/review record'):
                    b.save_checkpoint(data,PROJECT,TASK,payload,'alice/session',lambda _:self.fail('write'),
                                      provenance_writes=True)
        entry=TASK+'-cbobnote'
        payload=checkpoint(data,directions=[dict(id=entry,digest=digests[entry],state='acknowledged')])
        writes=[]
        b.save_checkpoint(data,PROJECT,TASK,payload,'alice/session',
                          lambda args:(writes.append(args),json.dumps({'id':'ok'}))[1],provenance_writes=True)
        self.assertEqual(len(writes),1)

    def test_malformed_switch_is_off_warns_and_plain_legacy_write_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'deployment.private.json').write_text('{"checkpoint_provenance_writes":"on"}')
            with redirect_stderr(io.StringIO()) as log:enabled=b.checkpoint_writes_enabled(root)
            self.assertFalse(enabled);self.assertIn('malformed value as OFF',log.getvalue())
            data=rows();writes=[]
            b.save_checkpoint(data,PROJECT,TASK,checkpoint(data),'alice/session',
                              lambda args:(writes.append(args),json.dumps({'id':'legacy'}))[1],provenance_writes=enabled)
            self.assertEqual(len(writes),1)

    def test_a_checkpoint_stays_current_after_restore_new_under_another_name(self):
        # kittrial-5bb.131: the project name is part of the activity cursor, so after
        # restore-new every checkpoint read STALE although nothing had changed, while the
        # work queue (which reads the cursor's own project) said current. Both now compare
        # the task's content under the project the cursor was taken in.
        for shape in ('legacy','provenance'):
            with self.subTest(shape=shape):
                data=rows()
                if shape=='legacy':append_checkpoint(data,'cp',checkpoint(data))
                else:save_cp(data,'cp')
                read=b.brief(data,'restored-project',TASK)
                self.assertFalse(read['checkpoint']['newer_activity'])
                self.assertIsNone(read['newer'])
                self.assertNotIn('STALE CHECKPOINT',read['next_action'])
                self.assertEqual(b.checkpoint_queue_fields(data,data[0])['newer_activity_coverage'],'current')
                # The cursor written in the new project names it, as before.
                self.assertEqual(b.untoken(read['activity_cursor'])['project'],'restored-project')
                # Real activity after the restore is still newer activity, with true counts.
                data[0]['comments'].append(comment('after','Instruction after the restore',author='coordinator'))
                read=b.brief(data,'restored-project',TASK)
                self.assertTrue(read['checkpoint']['newer_activity'])
                self.assertEqual(read['newer']['other_count'],1)
                self.assertEqual(read['newer']['coverage'],'unknown' if shape=='legacy' else 'snapshot')

    def test_a_renamed_cursor_matches_only_the_same_task_and_content(self):
        data=rows();snap=b.snapshot(data,PROJECT,TASK);cursor=b.activity_cursor(snap)
        self.assertTrue(b.cursor_matches(cursor,b.snapshot(data,'renamed',TASK)))
        other=rows();other[0]['title']='Another title'
        self.assertFalse(b.cursor_matches(cursor,b.snapshot(other,'renamed',TASK)))
        forged=b.token(dict(b.untoken(cursor),task='other-task'))
        self.assertFalse(b.cursor_matches(forged,b.snapshot(data,'renamed',TASK)))
        self.assertFalse(b.cursor_matches('not a cursor!',b.snapshot(data,'renamed',TASK)))


class CheckpointSwitchLockAndAuditTests(unittest.TestCase):
    """kittrial-5bb.131 items 1 and 2: the switch flip holds the deployment lock, and a
    damaged audit is read, kept aside and replaced instead of blocking the switch."""

    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.marker=self.root/'deployment.private.json'
        self.marker.write_text(json.dumps(dict(password='test-only',operators=['ops','ops2'],unrelated='keep')))

    def flip(self,action,actor='ops'):
        with redirect_stderr(io.StringIO()) as log:
            result=admin.checkpoint_provenance_switch(self.root,action,actor)
        return result,log.getvalue()

    @unittest.skipIf(sys.platform=='win32','flock is POSIX-only')
    def test_a_flip_holds_the_deployment_lock_across_the_read_modify_write(self):
        import fcntl
        real=admin.atomic_private_write
        seen=[]

        def probing(path,text):
            if Path(path).name=='deployment.private.json':
                with (self.root/admin.REVIEW_WRITES_LOCK).open('a') as handle:
                    try:
                        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:
                        seen.append(True)
                    else:
                        seen.append(False)
                        fcntl.flock(handle,fcntl.LOCK_UN)
            return real(path,text)

        with patch.object(admin,'atomic_private_write',side_effect=probing):
            self.flip('on')
        # Another file description cannot take the lock while the flip writes. Without
        # the lock `seen` is [False].
        self.assertEqual(seen,[True])

    @unittest.skipIf(sys.platform=='win32','flock is POSIX-only')
    def test_simultaneous_flips_record_every_audit_entry(self):
        # The review measured 18 of 24 entries for twelve rounds of two simultaneous flips.
        errors=[]

        def flip(action,actor,barrier):
            try:
                barrier.wait(5)
                with redirect_stderr(io.StringIO()):
                    admin.checkpoint_provenance_switch(self.root,action,actor)
            except Exception as error:   # reported below, never swallowed
                errors.append(error)

        for _ in range(12):
            barrier=threading.Barrier(2)
            workers=[threading.Thread(target=flip,args=('on','ops',barrier)),
                     threading.Thread(target=flip,args=('off','ops2',barrier))]
            for worker in workers:worker.start()
            for worker in workers:worker.join(10)
        self.assertEqual(errors,[])
        cfg=json.loads(self.marker.read_text())
        self.assertEqual(len(cfg['checkpoint_provenance_audit']),24)
        self.assertEqual(cfg['unrelated'],'keep')

    @unittest.skipIf(sys.platform=='win32','flock is POSIX-only')
    def test_a_review_writes_flip_and_a_checkpoint_flip_do_not_lose_each_other(self):
        # Both switches rewrite deployment.private.json, so they share the lock.
        barrier=threading.Barrier(2)

        def checkpoint_on():
            barrier.wait(5)
            with redirect_stderr(io.StringIO()):
                admin.checkpoint_provenance_switch(self.root,'on','ops')

        def review_on():
            barrier.wait(5)
            admin.review_writes_command(self.root,'ops2','on')

        workers=[threading.Thread(target=checkpoint_on),threading.Thread(target=review_on)]
        for worker in workers:worker.start()
        for worker in workers:worker.join(10)
        cfg=json.loads(self.marker.read_text())
        self.assertIs(cfg.get('checkpoint_provenance_writes'),True)
        self.assertIs(cfg.get('review_workflow_writes'),True)

    def test_a_damaged_audit_reads_empty_with_a_warning_and_the_next_flip_keeps_it_aside(self):
        for damaged in ('hand-edited',{'actor':'ops'},[{'actor':'ops'},'stray']):
            with self.subTest(damaged=damaged):
                cfg=json.loads(self.marker.read_text())
                for key in [key for key in cfg if key.startswith('checkpoint_provenance')]:cfg.pop(key)
                cfg['checkpoint_provenance_audit']=damaged
                self.marker.write_text(json.dumps(cfg))
                status,log=self.flip('status')
                self.assertEqual((status['checkpoint_provenance_writes'],status['audit_records'],
                                  status['audit_readable']),(False,0,False))
                self.assertIn('damaged',log)
                self.assertNotIn('hand-edited',log)          # names the shape, never the content
                result,log=self.flip('on')
                self.assertEqual((result['checkpoint_provenance_writes'],result['audit_records']),(True,1))
                self.assertIn('kept aside',log)
                cfg=json.loads(self.marker.read_text())
                kept=[key for key in cfg if key.startswith('checkpoint_provenance_audit_damaged_')]
                self.assertEqual(len(kept),1)
                self.assertEqual(cfg[kept[0]],damaged)
                self.assertEqual([(x['actor'],x['action']) for x in cfg['checkpoint_provenance_audit']],[('ops','on')])
                self.assertEqual(cfg['unrelated'],'keep')
                status,log=self.flip('status')
                self.assertTrue(status['audit_readable']);self.assertEqual(log,'')

    def test_a_second_damage_is_kept_under_its_own_name(self):
        # Two damages kept in the same second, and a name already taken: nothing that was
        # kept aside is ever overwritten.
        with patch.object(admin,'utc_stamp',return_value='2026-10-05T12:00:00Z'):
            for value in ('first','second'):
                cfg=json.loads(self.marker.read_text())
                cfg['checkpoint_provenance_audit']=value
                if value=='second':cfg['checkpoint_provenance_audit_damaged_20261005T120000Z_2']='occupied'
                self.marker.write_text(json.dumps(cfg))
                self.flip('on' if value=='first' else 'off')
        cfg=json.loads(self.marker.read_text())
        kept={key:cfg[key] for key in cfg if key.startswith('checkpoint_provenance_audit_damaged_')}
        self.assertEqual(kept,{'checkpoint_provenance_audit_damaged_20261005T120000Z':'first',
                               'checkpoint_provenance_audit_damaged_20261005T120000Z_2':'occupied',
                               'checkpoint_provenance_audit_damaged_20261005T120000Z_3':'second'})
