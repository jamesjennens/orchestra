"""Regression coverage for continuing the unintegrated checkpoint feature."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import briefing as b
from requirements import canonical_bytes
from test_briefing import TASK, PROJECT, rows, comment, checkpoint, save_cp, append_checkpoint


class CheckpointRevisionTests(unittest.TestCase):
    def verify(self, data):
        with tempfile.TemporaryDirectory() as temp:
            return json.loads(b.execute(Path(temp), Path(temp), PROJECT, 'alice/session',
                                       'checkpoint', [TASK, '--verify'], {},
                                       lambda args: '\n'.join(json.dumps(r) for r in data)))

    def resolve(self, data, cid, ids):
        digests=b.entry_digests(b.snapshot(data, PROJECT, TASK))
        return save_cp(data, cid, directions=[dict(id=TASK+'-c'+key, state='resolved',
                      digest=digests[TASK+'-c'+key], note='Implemented', evidence='tested revision') for key in ids])

    def test_small_new_id_and_edited_retired_direction_remain_resolvable_at_cap(self):
        data=rows()
        data[0]['comments'] += [comment(str(n), 'Please implement '+str(n), author='reviewer') for n in range(100,200)]
        self.resolve(data, 'cp1', [str(n) for n in range(100,200)])
        data[0]['comments'].append(comment('1', 'New direction', author='reviewer'))
        latest=self.resolve(data, 'cp2', ['1'])
        self.assertIn(TASK+'-c1', b.direction_index(latest))
        self.assertNotIn(TASK+'-c100', b.direction_index(latest))
        self.assertEqual(b.brief(data, PROJECT, TASK)['directions']['total'],0)
        target=next(c for c in data[0]['comments'] if c['id']=='100')
        target['text']='Edited retired direction'
        self.assertEqual(b.brief(data, PROJECT, TASK)['directions']['items'][0]['entry_id'],TASK+'-c100')
        latest=self.resolve(data,'cp3',['100'])
        self.assertIn(TASK+'-c100',b.direction_index(latest))
        self.assertNotIn(TASK+'-c101',b.direction_index(latest))
        self.assertEqual(b.brief(data,PROJECT,TASK)['directions']['total'],0)
        self.assertEqual(b.checkpoints(data[0])[1],[])

    def test_retirement_follows_explicit_chain_age_instead_of_ids(self):
        data=rows()
        data[0]['comments'] += [comment(key, 'Direction', author='reviewer') for key in ('z','a','b','c')]
        with patch.object(b,'DIRECTIONS_MAX',3):
            self.resolve(data,'cp1',['z'])
            self.resolve(data,'cp2',['a','b'])
            latest=self.resolve(data,'cp3',['c'])
            self.assertNotIn(TASK+'-cz',b.direction_index(latest))
            self.assertNotIn('carried',latest)  # completed dispositions stay in history, not repeated
            self.assertIn(TASK+'-ca',b.chain_dispositions(b.checkpoint_history(data[0])))
            self.assertEqual(b.brief(data,PROJECT,TASK)['directions']['total'],0)

    def test_latest_truncated_evidence_overrides_old_exact_and_old_truncated(self):
        data=rows()
        data[0]['comments'][0]['id']='a'
        save_cp(data,'cp1')  # a starts in the exact window
        data[0]['comments'] += [comment('z%04d'%n) for n in range(250)]
        data[0]['comments'][0]['text']='First edit absorbed by cp2'
        save_cp(data,'cp2')  # a is now in the truncated map
        self.assertEqual(self.verify(data)['changed'],0)
        data[0]['comments'][0]['text']='Second edit absorbed by cp3'
        save_cp(data,'cp3')
        # Native export order is deliberately unrelated to linked chain order.
        data[0]['comments'].reverse()
        self.assertEqual([cid for cid,_ in b.checkpoint_history(data[0])],['cp1','cp2','cp3'])
        result=self.verify(data)
        self.assertEqual(result['changed'],0)
        self.assertEqual(result['unchanged'],253)
        self.assertIsNone(b.brief(data,PROJECT,TASK)['newer'])
        next(c for c in data[0]['comments'] if c['id']=='a')['text']='Unincorporated third edit'
        self.assertEqual(self.verify(data)['changed_entry_ids'],[TASK+'-ca'])
        self.assertEqual(b.brief(data,PROJECT,TASK)['newer']['changed_or_late_count'],1)

    def test_latest_legacy_record_cannot_overclaim_using_older_evidence(self):
        data=rows()
        save_cp(data,'cp1')
        data[0]['comments'].append(comment('late','Late direction',author='reviewer'))
        append_checkpoint(data,'cp2',checkpoint(data))
        result=self.verify(data)
        self.assertEqual(result['coverage'],'unknown')
        self.assertEqual(result['verified_entries'],0)
        self.assertEqual(result['fresh'],0)
        self.assertEqual(result['unverified'],3)
        data[0]['title']='Activity changed'
        self.assertEqual(b.brief(data,PROJECT,TASK)['newer']['coverage'],'unknown')
        save_cp(data,'cp3')
        self.assertEqual(self.verify(data)['coverage'],'verified')

    def test_reads_parse_normalize_and_snapshot_only_once(self):
        data=rows();template=checkpoint(data)
        # A large payload per record makes repeated parsing materially expensive.
        prov=b.bounded_digests({TASK+'-ce%04d'%n:'a'*64 for n in range(500)})
        previous=None
        for n in range(128):
            payload=dict(template,previous=previous,provenance=prov)
            previous='cp%04d'%n
            append_checkpoint(data,previous,payload)
        data[0]['comments'].reverse()
        for read in (lambda:b.brief(data,PROJECT,TASK),lambda:self.verify(data)):
            with self.subTest(read=read), patch.object(b.record_json,'loads',wraps=b.record_json.loads) as parse, \
                 patch.object(b,'normalize_provenance',wraps=b.normalize_provenance) as normalize, \
                 patch.object(b,'snapshot',wraps=b.snapshot) as snap:
                read()
                # Validation also decodes each small cursor once; only record
                # payload parses count toward the large-record regression.
                self.assertEqual(sum(isinstance(c.args[0],str) and '"acceptance"' in c.args[0]
                                     for c in parse.call_args_list),128)
                self.assertEqual(normalize.call_count,128)
                self.assertEqual(snap.call_count,1)

    def test_brief_reuses_the_excluded_snapshot_cursor(self):
        data=rows();save_cp(data,'cp1')
        with patch.object(b,'activity_cursor',wraps=b.activity_cursor) as cursor:
            b.brief(data,PROJECT,TASK)
            self.assertEqual(cursor.call_count,2)  # whole snapshot and excluded receipt

    def test_all_checkpoint_reads_use_guarded_export_decoding(self):
        data = rows()
        deep = '{"id":"bad-row","unknown":' + ('['*3000) + '0' + (']'*3000) + '}'
        export_text = json.dumps(data[0]) + '\n' + deep + '\n'
        run = lambda argv: export_text
        # Per-row guarded decoding: reading a valid task succeeds even when another row in the export is over 64 levels
        for args in ([TASK, '--verify'], [TASK, '--provenance']):
            with self.subTest(args=args):
                res = b.execute(Path('.'), Path('.'), PROJECT, 'alice/session', 'checkpoint', args, {}, run)
                self.assertIn(TASK, res)
        # Reading the malformed task refuses per-row
        for args in (['bad-row', '--verify'], ['bad-row', '--provenance']):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, 'malformed'):
                b.execute(Path('.'), Path('.'), PROJECT, 'alice/session', 'checkpoint', args, {}, run)
        # And brief and history also succeed on the good task and refuse on the bad task
        brief_res = b.execute(Path('.'), Path('.'), PROJECT, 'alice/session', 'brief', [TASK, '--json'], {}, run)
        self.assertIn(TASK, brief_res)
        with self.assertRaisesRegex(ValueError, 'malformed'):
            b.execute(Path('.'), Path('.'), PROJECT, 'alice/session', 'brief', ['bad-row', '--json'], {}, run)

    def test_altered_older_map_is_rejected_before_native_write(self):
        data=rows()
        data[0]['comments'] += [comment('z%04d'%n) for n in range(250)]
        supplied=b.bounded_digests(b.entry_digests(b.snapshot(data,PROJECT,TASK)))
        supplied['older'][next(iter(supplied['older']))]='b'*16
        calls=[]
        with self.assertRaisesRegex(ValueError,'does not match'):
            b.save_checkpoint(data,PROJECT,TASK,checkpoint(data,provenance=supplied),'alice/session',calls.append, provenance_writes=True)
        self.assertEqual(calls,[])

    def test_deep_json_and_raw_carried_forgery_are_not_accepted(self):
        data=rows();payload=checkpoint(data)
        raw=canonical_bytes(payload).decode()
        data[0]['comments'].append(comment('bad',b.PREFIX+'['*65+'0'+']'*65))
        self.assertEqual(b.checkpoints(data[0])[1],['bad'])
        from reserved_comments import check_comment_body
        # Reserved raw comments must go through the validating checkpoint route.
        with self.assertRaises(ValueError):
            check_comment_body(b.PREFIX+raw,'positional',actor='alice/session',task=TASK)

    def test_checkpoint_read_flags_reject_extra_arguments(self):
        for flag in ('--verify','--provenance'):
            with self.subTest(flag=flag),self.assertRaises(ValueError):
                b.execute(Path('.'),Path('.'),PROJECT,'alice/session','checkpoint',
                          [TASK,flag,'unexpected'],{},lambda args:self.fail('native read'))

    def test_queue_current_coverage_and_read_cost_match_brief(self):
        import work
        data=rows();save_cp(data,'cp1')
        with patch.object(b,'checkpoint_state',wraps=b.checkpoint_state) as state, \
             patch.object(b,'snapshot',wraps=b.snapshot) as snap:
            item=work.queue(data,'alice/session',['--mine'])['items'][0]
            self.assertEqual(state.call_count,1)
            self.assertEqual(snap.call_count,1)
        self.assertEqual(item['newer_activity_coverage'],'current')
        self.assertEqual(item['newer_activity_by_others'],0)
        data[0]['comments'].append(comment('late','Please revise',author='reviewer'))
        item=work.queue(data,'alice/session',['--mine'])['items'][0]
        newer=b.brief(data,PROJECT,TASK)['newer']
        self.assertEqual(item['newer_activity_by_others'],newer['other_count'])
        self.assertEqual(item['newer_activity_own'],newer['own_count'])
        self.assertEqual(item['unresolved_directions'],1)

    def test_queue_expensive_checkpoint_reads_are_limited_to_displayed_page(self):
        import work
        data=[]
        for n in range(12):
            row=rows()[0];row['id']='task-%02d'%n;data.append(row)
        with patch.object(b,'checkpoint_queue_fields',wraps=b.checkpoint_queue_fields) as fields, \
             patch.object(b,'normalize_provenance',wraps=b.normalize_provenance) as normalize:
            result=work.queue(data,'alice/session',['--mine','--limit','2'])
            self.assertEqual(result['total'],12)
            self.assertEqual(fields.call_count,2)
            self.assertEqual(normalize.call_count,0)  # no provenance work on undisplayed rows


if __name__=='__main__':unittest.main()
