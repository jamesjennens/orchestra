"""Optional real bd/Dolt and loopback HTTP acceptance, in disposable fixtures only."""
import json
import os
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_bd_label_aliases as fixture
import record_json


class NativeExportLineTests(fixture.RealBdLabelAliasTests):
    """Reuse the pinned server-mode fixture; never an existing project or database."""

    def ep(self, args, actor='victim'):
        result = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': actor,
                                                      'action': 'bd', 'args': args})
        self.assertEqual(0, result['returncode'], result)
        return json.loads(result['stdout'])

    def rows(self):
        result = self.bd('export', '--all')
        self.assertEqual(0, result.returncode, result.stderr)
        return record_json.loads_rows(result.stdout)

    def test_native_unicode_fields_and_shared_views_keep_whole_rows(self):
        import briefing
        import lifecycle
        import work
        healthy = self.ep(['create', 'Healthy neighbor', '--json'])['id']
        for number, separator in enumerate(('\u0085', '\u2028', '\u2029')):
            with self.subTest(separator=repr(separator)):
                text = 'before' + separator + 'after'
                task = self.ep(['create', text, '--description', text, '--labels', 'label' + text,
                                '--assignee', 'victim', '--json'])['id']
                self.ep(['comments', 'add', task, text, '--json'], actor='commenter')
                exported = self.rows()
                selected = next(r for r in exported if r['id'] == task)
                self.assertFalse(selected.get('malformed'))
                self.assertEqual(text, selected['title'])
                self.assertEqual(text, selected['description'])
                self.assertIn('label' + text, selected['labels'])
                self.assertIn(text, [c['text'] for c in selected.get('comments', [])])
                self.assertIn(healthy, {r['id'] for r in exported})
                calls = []
                def run(args):
                    calls.append(args)
                    result = self.bd(*args)
                    self.assertEqual(0, result.returncode, result.stderr)
                    return result.stdout
                intact = briefing.exported_rows(run)
                self.assertEqual(['export'], [a[0] for a in calls])
                self.assertFalse(any(r.get('malformed') for r in intact))
                self.assertEqual(text, briefing.brief(intact, 'pp', task)['title']['text'])
                self.assertEqual(text, next(i for i in work.queue(intact, 'victim', ['--mine'])['items'] if i['task'] == task)['title'])
                self.assertIn(text, json.dumps(briefing.history_page(intact, 'pp', task), ensure_ascii=False))
                self.assertIn(task, {r['id'] for r in lifecycle.project_facts(intact)})
                refresh = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': 'victim',
                                                               'action': 'refresh', 'args': []})
                self.assertEqual(0, refresh['returncode'], refresh)
                view = self.project / 'views' / 'issues.jsonl'
                self.assertIn(task, {r['id'] for r in record_json.loads_rows(view.read_text(encoding='utf-8'))})

    def test_real_http_title_round_trip_and_credential_name_reservation(self):
        import http_service
        import test_http_review_fixes as fixes
        root = self.root
        slot = self.bd('show', 'pp-merge-slot', '--json')
        if slot.returncode:
            made = self.bd('create', 'Merge Slot', '--id', 'pp-merge-slot', '--labels', 'gt:slot', '--json')
            self.assertEqual(0, made.returncode, made.stderr)
        held = self.ep(['create', 'Held name row', '--assignee', 'victim', '--json'])['id']
        self.ep(['comments', 'add', held, 'before\u0085after', '--json'], actor='commenter')
        class HttpHarness(fixes.Harness):
            def make_backend(self):
                return http_service.EndpointBackend(sys.executable, str(KIT / 'endpoint.py'), str(root), service=self.service)
        harness = HttpHarness(methodName='runTest')
        harness.setUp()
        try:
            token = harness.admin_token()
            registered = harness.request('POST', '/v1/projects', {'project_id': 'pp', 'name': 'Unicode rows'}, token=token)
            self.assertEqual(201, registered.status, registered.data)
            for number, separator in enumerate(('\u0085', '\u2028', '\u2029')):
                text = 'title-before' + separator + 'after'
                changed = harness.request('PATCH', '/v1/projects/pp/tasks/' + held, {'title': text}, token=token, key='unicode-title-%d' % number)
                self.assertEqual(200, changed.status, changed.data)
                self.assertEqual(text, next(r for r in self.rows() if r['id'] == held)['title'])
            free = harness.request('POST', '/v1/projects/pp/worker-credentials', {'actor': 'free-line-worker'}, token=token, key='unicode-free-name')
            self.assertEqual(201, free.status, free.data)
            for held_name in ('victim', 'commenter'):
                taken = harness.request('POST', '/v1/projects/pp/worker-credentials', {'actor': held_name}, token=token, key='unicode-held-' + held_name)
                self.assertEqual(422, taken.status, taken.data)
        finally:
            harness._stop_server()
            harness.doCleanups()


def load_tests(loader, tests, pattern):
    # Borrow the fixture, not its unrelated inherited label-alias test cases.
    return unittest.TestSuite(NativeExportLineTests(name) for name in (
        'test_native_unicode_fields_and_shared_views_keep_whole_rows',
        'test_real_http_title_round_trip_and_credential_name_reservation'))
