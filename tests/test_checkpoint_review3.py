"""Conservative timestamp ties, cursor rollback and staged write controls."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
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

    def test_malformed_switch_is_off_warns_and_plain_legacy_write_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'deployment.private.json').write_text('{"checkpoint_provenance_writes":"on"}')
            with redirect_stderr(io.StringIO()) as log:enabled=b.checkpoint_writes_enabled(root)
            self.assertFalse(enabled);self.assertIn('malformed value as OFF',log.getvalue())
            data=rows();writes=[]
            b.save_checkpoint(data,PROJECT,TASK,checkpoint(data),'alice/session',
                              lambda args:(writes.append(args),json.dumps({'id':'legacy'}))[1],provenance_writes=enabled)
            self.assertEqual(len(writes),1)

    def test_restore_new_scope_requires_fresh_checkpoint_not_false_entry_counts(self):
        data=rows();append_checkpoint(data,'legacy',checkpoint(data))
        read=b.brief(data,'restored-project',TASK)
        self.assertTrue(read['checkpoint']['newer_activity'])
        self.assertEqual(read['newer']['coverage'],'unknown')
        self.assertNotEqual(read['activity_cursor'],read['checkpoint']['incorporated_activity_cursor'])
