import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from activity import COVERAGE_NOTE,CURSOR_NOTE,FRESHNESS,build_entries,content_hash,feed,format_text,load_cursor,load_export,main,next_cursor,parse_moment,write_cursor

SCOPE='kittrial:issues.jsonl'

def record(issue_id,updated_at,comments,title='Task'):
    return {'id':issue_id,'title':title,'status':'open','updated_at':updated_at,'comments':comments}

def comment(comment_id,created_at,text='Body',author='alice'):
    return {'id':comment_id,'created_at':created_at,'author':author,'text':text}

def event(event_id,parent,created_at,description='Set implemented to passed',title='State change: implemented -> passed',author='actor'):
    return {'_type':'issue','id':event_id,'issue_type':'event','title':title,'description':description,'created_at':created_at,'created_by':author,
            'dependencies':[{'depends_on_id':parent,'type':'parent-child'}]}

def saved(directory,rows):
    path=Path(directory)/'issues.jsonl'
    path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
    return path

def saved_as(directory,rows):
    Path(directory).mkdir(parents=True,exist_ok=True)
    return saved(directory,rows)

def run(argv):
    out=io.StringIO();errors=io.StringIO()
    with contextlib.redirect_stdout(out),contextlib.redirect_stderr(errors):
        code=main(argv)
    return code,out.getvalue(),errors.getvalue()

LIFECYCLE='Set implemented to passed\n\nReason: review ready\nKind: lifecycle-v1\n{"implemented":{"value":"passed"}}'

class ActivityTests(unittest.TestCase):
    def test_comment_newer_than_unchanged_updated_at_is_reported(self):
        rows=[record('p-task','2026-09-01T00:00:00Z',[comment('one','2026-09-10T12:00:00Z','Late finding')])]
        entries=build_entries(rows,parse_moment('2026-09-05T00:00:00Z'))
        self.assertEqual([e['entry_id'] for e in entries],['p-task-cone'])
        self.assertEqual(entries[0]['timestamp'],'2026-09-10T12:00:00Z')
        self.assertEqual(entries[0]['body'],'Late finding')

    def test_since_boundary_is_inclusive_across_offsets(self):
        rows=[record('p-task','2026-09-01T00:00:00Z',[comment('one','2026-09-10T12:00:00Z')])]
        self.assertEqual(len(build_entries(rows,parse_moment('2026-09-10T14:00:00+02:00'))),1)
        self.assertEqual(build_entries(rows,parse_moment('2026-09-10T14:00:01+02:00')),[])
        self.assertEqual(len(build_entries(rows,parse_moment('2026-09-10T09:00:00-03:00'))),1)
        self.assertEqual(build_entries(rows,parse_moment('2026-09-10T09:00:01-03:00')),[])

    def test_equal_timestamps_are_kept_and_ordered(self):
        rows=[record('p-b','2026-09-01T00:00:00Z',[comment('two','2026-09-10T00:00:00Z'),comment('one','2026-09-10T00:00:00Z')]),
              record('p-a','2026-09-01T00:00:00Z',[comment('10','2026-09-10T00:00:00Z')])]
        self.assertEqual([e['entry_id'] for e in build_entries(rows)],['p-a-c10','p-b-cone','p-b-ctwo'])

    def test_empty_and_missing_comments_produce_no_entries(self):
        rows=[record('p-empty','2026-09-01T00:00:00Z',[]),{'id':'p-none','title':'No comments field','status':'open'}]
        self.assertEqual(build_entries(rows),[])

    def test_malformed_export_line_fails_clearly(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'issues.jsonl'
            path.write_text('{"id":"p-ok","title":"T","comments":[]}\nnot json\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'line 2'):
                load_export(path)

    def test_non_issue_json_export_fails_clearly(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'issues.jsonl'
            path.write_text('[{"id":"p-ok"}]\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'not a JSON issue object'):
                load_export(path)
            path.write_text('{"title":"no id"}\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'no string issue id'):
                load_export(path)

    def test_missing_export_file_fails_clearly(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,'cannot read export'):
                load_export(Path(d)/'absent.jsonl')

    def test_naive_or_malformed_since_is_rejected(self):
        for value in ('2026-09-10T12:00:00','2026-09-10','yesterday',''):
            with self.assertRaisesRegex(ValueError,'--since'):
                parse_moment(value,'--since')

    def test_entry_fields_and_ids(self):
        rows=[record('p-one','2026-09-01T00:00:00Z',[comment('7','2026-09-10T00:00:00+02:00','Body text','bob')],'Title one')]
        entry=build_entries(rows)[0]
        self.assertEqual(entry['entry_id'],'p-one-c7')
        self.assertEqual(entry['kind'],'comment')
        self.assertEqual(entry['issue_id'],'p-one')
        self.assertEqual(entry['issue_title'],'Title one')
        self.assertEqual(entry['author'],'bob')
        self.assertEqual(entry['timestamp'],'2026-09-09T22:00:00Z')
        self.assertEqual(entry['body'],'Body text')
        self.assertNotIn('revision',entry)

    def test_feed_reports_export_freshness_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            path=saved(d,[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z')])])
            result=feed(load_export(path),parse_moment('2026-09-10T00:00:00Z'),path)
            self.assertEqual(result['count'],1)
            self.assertTrue(result['since_inclusive'])
            self.assertEqual(result['since'],'2026-09-10T00:00:00Z')
            self.assertEqual(result['export']['note'],FRESHNESS)
            self.assertIn('not an authoritative',result['export']['note'])
            self.assertTrue(result['export']['modified'].endswith('Z'))

    def test_cli_json_feed_and_error_exit(self):
        with tempfile.TemporaryDirectory() as d:
            path=saved(d,[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z')])])
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['--export',str(path),'--since','2026-09-10T00:00:00Z','--json']),0)
            self.assertEqual(json.loads(out.getvalue())['entries'][0]['entry_id'],'p-one-cone')
            errors=io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(main(['--export',str(path),'--since','2026-09-10T00:00:00']),2)
            self.assertIn('timezone offset',errors.getvalue())

    def test_text_feed_keeps_same_time_entries(self):
        rows=[record('p-b','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z','first'),comment('two','2026-09-10T00:00:00Z','second')])]
        text=format_text(feed(rows))
        self.assertEqual(text.count('p-b-c'),2)
        self.assertIn('first',text)
        self.assertIn('second',text)
        self.assertIn('inclusive',text)

    def test_native_event_entry_uses_native_identity_and_parent(self):
        rows=[record('p-task','2026-09-01T00:00:00Z',[],'Task title'),event('p-task.1','p-task','2026-09-15T10:08:43Z',LIFECYCLE)]
        entry=build_entries(rows)[0]
        self.assertEqual(entry['entry_id'],'p-task.1')
        self.assertEqual(entry['kind'],'event')
        self.assertEqual(entry['issue_id'],'p-task')
        self.assertEqual(entry['issue_title'],'Task title')
        self.assertEqual(entry['author'],'actor')
        self.assertEqual(entry['timestamp'],'2026-09-15T10:08:43Z')
        self.assertEqual(entry['event_title'],'State change: implemented -> passed')
        self.assertEqual(entry['body'],LIFECYCLE)
        self.assertIn('Kind: lifecycle-v1',entry['body'])

    def test_event_description_is_carried_verbatim_as_text(self):
        rows=[event('p-task.2','p-task','2026-09-15T10:08:43Z',LIFECYCLE)]
        raw=rows[0]['description']
        self.assertEqual(build_entries(rows)[0]['body'],raw)
        self.assertTrue(build_entries(rows)[0]['body'].startswith('Set implemented to passed'))
        self.assertEqual(build_entries(rows)[0]['body'].count('Kind: lifecycle-v1'),1)
        self.assertNotIn('implemented',build_entries(rows)[0])

    def test_event_without_parent_dependency_still_appears(self):
        row=event('p-task.9','p-task','2026-09-15T00:00:00Z');row['dependencies']=[]
        entry=build_entries([row])[0]
        self.assertEqual(entry['entry_id'],'p-task.9')
        self.assertEqual(entry['issue_id'],'p-task.9')
        self.assertEqual(entry['issue_title'],'')

    def test_event_and_comments_are_ordered_together(self):
        rows=[record('p-task','2026-09-01T00:00:00Z',[comment('one','2026-09-15T10:00:00Z')]),
              event('p-task.1','p-task','2026-09-15T10:00:00Z'),
              event('p-task.2','p-task','2026-09-16T00:00:00Z',title='State change: tested -> passed')]
        self.assertEqual([e['entry_id'] for e in build_entries(rows)],['p-task-cone','p-task.1','p-task.2'])
        self.assertEqual([e['kind'] for e in build_entries(rows)],['comment','event','event'])

    def test_comments_on_events_are_included_and_resumed_by_cursor(self):
        row=event('p-task.1','p-task','2026-09-15T10:00:00Z')
        initial=feed([row],seen={},scope=SCOPE)
        seen=next_cursor({},initial['entries'])
        row['comments']=[comment('correction','2026-09-16T10:00:00Z','Previous verification was incomplete')]
        result=feed([row],seen=seen,scope=SCOPE)
        self.assertEqual(result['coverage']['comment_entries'],1)
        self.assertEqual(result['coverage']['event_entries'],1)
        self.assertEqual(result['count'],1)
        entry=result['entries'][0]
        self.assertEqual(entry['entry_id'],'p-task.1-ccorrection')
        self.assertEqual(entry['issue_id'],'p-task.1')
        self.assertEqual(entry['body'],'Previous verification was incomplete')
        self.assertEqual(entry['kind'],'comment')
        self.assertFalse(entry['revision'])
        self.assertEqual(feed([row],seen=next_cursor(seen,result['entries']))['count'],0)

    def test_event_without_timezone_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'p-task.2 created_at'):
            build_entries([event('p-task.2','p-task','2026-09-15T10:08:43')])

    def test_coverage_is_explicit_and_not_a_complete_audit(self):
        rows=[record('p-task','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z')]),event('p-task.1','p-task','2026-09-10T00:00:00Z')]
        coverage=feed(rows)['coverage']
        self.assertEqual(coverage['sources'],['comments','native-state-events'])
        self.assertFalse(coverage['complete'])
        self.assertEqual(coverage['note'],COVERAGE_NOTE)
        self.assertIn('not a complete audit',coverage['note'])
        self.assertEqual((coverage['comment_entries'],coverage['event_entries']),(1,1))
        self.assertIn(COVERAGE_NOTE,format_text(feed(rows)))

    def test_text_feed_marks_native_events(self):
        text=format_text(feed([event('p-task.1','p-task','2026-09-15T10:08:43Z','Set implemented to passed')]))
        self.assertIn('p-task.1 [native event]',text)
        self.assertIn('Set implemented to passed',text)

    def test_no_cursor_read_has_no_cursor_block(self):
        rows=[record('p-b','2026-09-01T00:00:00Z',[comment('two','2026-09-10T00:00:00Z'),comment('one','2026-09-10T00:00:00Z')])]
        result=feed(rows,parse_moment('2026-09-10T00:00:00Z'))
        self.assertNotIn('cursor',result)
        self.assertEqual([e['entry_id'] for e in result['entries']],['p-b-cone','p-b-ctwo'])

    def test_cursor_round_trip_delivers_each_entry_once(self):
        with tempfile.TemporaryDirectory() as d:
            path=saved(d,[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z')]),event('p-one.1','p-one','2026-09-10T00:00:00Z')])
            cursor=Path(d)/'cursor.json'
            code,first,err=run(['--export',str(path),'--scope',SCOPE,'--cursor-out',str(cursor),'--json'])
            self.assertEqual((code,err),(0,''))
            payload=json.loads(first)
            self.assertEqual(payload['count'],2)
            self.assertEqual((payload['cursor']['new'],payload['cursor']['revisions'],payload['cursor']['suppressed']),(2,0,0))
            self.assertEqual(payload['cursor']['scope'],SCOPE)
            self.assertIn(CURSOR_NOTE,payload['cursor']['note'])
            code,second,err=run(['--export',str(path),'--scope',SCOPE,'--cursor',str(cursor),'--json'])
            resumed=json.loads(second)
            self.assertEqual(resumed['entries'],[])
            self.assertEqual((resumed['cursor']['new'],resumed['cursor']['suppressed']),(0,2))
            self.assertEqual(json.loads(cursor.read_text(encoding='utf-8'))['seen']['p-one.1'],content_hash(payload['entries'][1]))

    def test_late_and_tied_entries_survive_a_cursor_read(self):
        with tempfile.TemporaryDirectory() as d:
            first=saved(d,[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z','Original')])])
            cursor=Path(d)/'cursor.json'
            self.assertEqual(run(['--export',str(first),'--scope',SCOPE,'--cursor-out',str(cursor),'--json'])[0],0)
            rows=[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z','Original'),comment('two','2026-09-10T00:00:00Z','Tie')]),
                  event('p-one.4','p-one','2026-09-05T00:00:00Z')]
            later=saved_as(Path(d)/'later',rows)
            payload=json.loads(run(['--export',str(later),'--scope',SCOPE,'--cursor',str(cursor),'--json'])[1])
            self.assertEqual([e['entry_id'] for e in payload['entries']],['p-one.4','p-one-ctwo'])
            self.assertEqual([e['revision'] for e in payload['entries']],[False,False])

    def test_edited_entry_is_delivered_as_a_revision(self):
        with tempfile.TemporaryDirectory() as d:
            original=saved(d,[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z','First text')])])
            cursor=Path(d)/'cursor.json'
            self.assertEqual(run(['--export',str(original),'--scope',SCOPE,'--cursor-out',str(cursor),'--json'])[0],0)
            edited=saved_as(Path(d)/'edited',[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z','Corrected text')])])
            payload=json.loads(run(['--export',str(edited),'--scope',SCOPE,'--cursor',str(cursor),'--cursor-out',str(cursor),'--json'])[1])
            self.assertEqual(payload['count'],1)
            self.assertTrue(payload['entries'][0]['revision'])
            self.assertEqual((payload['cursor']['new'],payload['cursor']['revisions']),(0,1))
            self.assertEqual(json.loads(cursor.read_text(encoding='utf-8'))['seen']['p-one-cone'],content_hash(payload['entries'][0]))

    def test_ids_absent_from_a_partial_export_are_retained(self):
        with tempfile.TemporaryDirectory() as d:
            cursor=Path(d)/'cursor.json'
            write_cursor(cursor,SCOPE,{'p-gone-c1':'a'*64})
            self.assertEqual(load_cursor(cursor,SCOPE),{'p-gone-c1':'a'*64})
            path=saved(d,[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z')])])
            payload=json.loads(run(['--export',str(path),'--scope',SCOPE,'--cursor',str(cursor),'--cursor-out',str(cursor),'--json'])[1])
            self.assertEqual((payload['cursor']['seen_before'],payload['cursor']['seen']),(1,2))
            self.assertEqual(json.loads(cursor.read_text(encoding='utf-8'))['seen']['p-gone-c1'],'a'*64)

    def test_cursor_growth_is_linear_in_seen_identities(self):
        with tempfile.TemporaryDirectory() as d:
            path=saved(d,[record('p-a','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z')]),event('p-a.1','p-a','2026-09-10T00:00:00Z')])
            cursor=Path(d)/'cursor.json'
            payload=json.loads(run(['--export',str(path),'--scope',SCOPE,'--cursor-out',str(cursor),'--json'])[1])
            stored=json.loads(cursor.read_text(encoding='utf-8'))
            self.assertEqual(sorted(stored['seen']),['p-a-cone','p-a.1'])
            self.assertEqual(len(stored['seen']),len(payload['entries']))
            self.assertIn('linearly',stored['note'])

    def test_cursor_scope_mismatch_and_malformed_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            cursor=Path(d)/'cursor.json'
            write_cursor(cursor,SCOPE,{})
            with self.assertRaisesRegex(ValueError,'does not match --scope'):
                load_cursor(cursor,'other:source')
            cursor.write_text('not json',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'not valid JSON'):
                load_cursor(cursor,SCOPE)
            cursor.write_text(json.dumps(['nope']),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'not a JSON object'):
                load_cursor(cursor,SCOPE)
            cursor.write_text(json.dumps({'version':99,'scope':SCOPE,'seen':{}}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'unsupported version'):
                load_cursor(cursor,SCOPE)
            cursor.write_text(json.dumps({'version':1,'seen':{}}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'has no scope'):
                load_cursor(cursor,SCOPE)
            cursor.write_text(json.dumps({'version':1,'scope':SCOPE,'seen':{'p-one-c1':''}}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'malformed entry identity'):
                load_cursor(cursor,SCOPE)
            cursor.write_text(json.dumps({'version':1,'scope':SCOPE}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'no seen identity map'):
                load_cursor(cursor,SCOPE)
            with self.assertRaisesRegex(ValueError,'cannot read cursor'):
                load_cursor(Path(d)/'absent.json',SCOPE)

    def test_cli_rejects_bad_cursor_and_flag_combinations(self):
        with tempfile.TemporaryDirectory() as d:
            path=saved(d,[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z')])])
            cursor=Path(d)/'cursor.json'
            code,out,err=run(['--export',str(path),'--cursor',str(cursor)])
            self.assertEqual(code,2);self.assertIn('--scope is required',err)
            code,out,err=run(['--export',str(path),'--scope',SCOPE])
            self.assertEqual(code,2);self.assertIn('--scope only applies',err)
            code,out,err=run(['--export',str(path),'--scope',SCOPE,'--cursor-out',str(cursor),'--since','2026-09-10T00:00:00Z'])
            self.assertEqual(code,2);self.assertIn('not combined',err)
            self.assertFalse(cursor.exists())
            write_cursor(cursor,'other:source',{})
            code,out,err=run(['--export',str(path),'--scope',SCOPE,'--cursor',str(cursor)])
            self.assertEqual(code,2);self.assertIn('does not match --scope',err)
            cursor.write_text('{broken',encoding='utf-8')
            code,out,err=run(['--export',str(path),'--scope',SCOPE,'--cursor',str(cursor),'--cursor-out',str(cursor)])
            self.assertEqual(code,2);self.assertIn('not valid JSON',err)
            self.assertEqual(cursor.read_text(encoding='utf-8'),'{broken')

    def test_cursor_is_written_only_after_a_successful_feed(self):
        with tempfile.TemporaryDirectory() as d:
            broken=Path(d)/'issues.jsonl';broken.write_text('{"id":"p-ok"}\n{oops\n',encoding='utf-8')
            cursor=Path(d)/'cursor.json'
            code,out,err=run(['--export',str(broken),'--scope',SCOPE,'--cursor-out',str(cursor)])
            self.assertEqual(code,2)
            self.assertIn('line 2',err)
            self.assertFalse(cursor.exists())
            self.assertEqual(sorted(Path(d).glob('*.tmp')),[])
            path=saved_as(Path(d)/'good',[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z')])])
            code,text,err=run(['--export',str(path),'--scope',SCOPE,'--cursor-out',str(cursor)])
            self.assertEqual((code,err),(0,''))
            self.assertIn('Cursor: scope '+SCOPE,text)
            self.assertTrue(cursor.is_file())
            self.assertEqual(sorted(Path(d).glob('*.tmp')),[])
            self.assertEqual(json.loads(cursor.read_text(encoding='utf-8'))['scope'],SCOPE)

    def test_failed_stdout_flush_does_not_advance_cursor(self):
        class FailedFlush(io.StringIO):
            def flush(self):raise OSError('output flush failed')
        with tempfile.TemporaryDirectory() as d:
            path=saved(d,[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z')])])
            cursor=Path(d)/'cursor.json'
            for existing in (False,True):
                with self.subTest(existing=existing):
                    args=['--export',str(path),'--scope',SCOPE,'--cursor-out',str(cursor),'--json']
                    before=None
                    if existing:
                        write_cursor(cursor,SCOPE,{'prior-id':'a'*64})
                        before=cursor.read_bytes()
                        args+=['--cursor',str(cursor)]
                    output=FailedFlush();error=io.StringIO()
                    with contextlib.redirect_stdout(output),contextlib.redirect_stderr(error):
                        code=main(args)
                    self.assertEqual(code,2)
                    self.assertIn('output flush failed',error.getvalue())
                    self.assertEqual(cursor.read_bytes() if cursor.exists() else None,before)
                    self.assertFalse(list(Path(d).glob('*.tmp')))

    def test_next_cursor_keeps_absent_and_updates_delivered(self):
        rows=[record('p-one','2026-09-01T00:00:00Z',[comment('one','2026-09-10T00:00:00Z','First')])]
        entries=build_entries(rows)
        updated=next_cursor({'p-gone-c1':'b'*64},entries)
        self.assertEqual(updated['p-gone-c1'],'b'*64)
        self.assertEqual(updated['p-one-cone'],content_hash(entries[0]))
        rows[0]['comments'][0]['text']='Second'
        self.assertNotEqual(next_cursor(updated,build_entries(rows))['p-one-cone'],updated['p-one-cone'])

if __name__=='__main__':unittest.main()
