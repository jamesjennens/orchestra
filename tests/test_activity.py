import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from activity import FRESHNESS,build_entries,feed,format_text,load_export,main,parse_moment

def record(issue_id,updated_at,comments,title='Task'):
    return {'id':issue_id,'title':title,'status':'open','updated_at':updated_at,'comments':comments}

def comment(comment_id,created_at,text='Body',author='alice'):
    return {'id':comment_id,'created_at':created_at,'author':author,'text':text}

def saved(directory,rows):
    path=Path(directory)/'issues.jsonl'
    path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
    return path

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
        self.assertEqual(entry['issue_id'],'p-one')
        self.assertEqual(entry['issue_title'],'Title one')
        self.assertEqual(entry['author'],'bob')
        self.assertEqual(entry['timestamp'],'2026-09-09T22:00:00Z')
        self.assertEqual(entry['body'],'Body text')

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

if __name__=='__main__':unittest.main()
