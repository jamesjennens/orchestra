"""Reviewer real-bd repros for kittrial-5bb.388 r3 (not part of the delivery)."""
import json
import sys
import threading
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_http_review_fixes as fixes
import test_rooms_items_real_bd as base
import rooms

_orig = fixes.create_server


def _ranged(service, backend, host='127.0.0.1', port=0, **kw):
    for candidate in range(29800, 29900):
        try:
            return _orig(service, backend, host='127.0.0.1', port=candidate, **kw)
        except OSError:
            continue
    raise RuntimeError('no free port in 29800-29899')


fixes.create_server = _ranged


class Rev388r3Bd(base.RoomsItemsRealBdTests):
    def with_pointer(self, rid, sequence=1):
        pointer = self.pointer(rid, sequence)
        return [(row.get('id'), row.get('issue_type') or row.get('type'), row.get('title'), row.get('status'))
                for row in self.export_rows() if pointer in (row.get('description') or '')]

    def proposals_with(self, needle):
        answer = self.request('GET', '/v1/projects/pp/proposals', token=self.tokens['alex'])
        return [p for p in (answer.data.get('items') or []) if needle in json.dumps(p)]

    def all_props(self):
        answer = self.request('GET', '/v1/projects/pp/proposals', token=self.tokens['alex'])
        print('  [proposals GET', answer.status, 'total', answer.data.get('total'), 'items', len(answer.data.get('items') or []), str(answer.data.get('error'))[:120], ']')
        return [(p.get('key'), (p.get('title') or {}).get('text'), p.get('state')) for p in (answer.data.get('items') or [])]

    def events(self, rid):
        return [l for l in rooms.load_lines(self.project, rid) if l['kind'] == 'item-event']

    def fail_appends(self, which=(1,)):
        original = rooms.accept_item
        state = {'n': 0}

        def failing(*a, **kw):
            state['n'] += 1
            if state['n'] in which:
                raise OSError('disk full (reviewer injected)')
            return original(*a, **kw)
        rooms.accept_item = failing
        self.addCleanup(setattr, rooms, 'accept_item', original)

    def err(self, answer):
        return (answer.data.get('error') or {}).get('message', '')[:110]

    # ---- B-2 reruns with the archive in a second thread ----
    def test_r9_room_archived_during_requirement_write(self):
        rid = self.room('R9')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r9-item-key').status)
        original = self.backend.owner_requirements
        box = {}

        def archive():
            t0 = time.monotonic()
            box['a'] = self.request('POST', '/v1/projects/pp/rooms/%s/archive' % rid, {},
                                    token=self.tokens['alex'], key='r9-archive')
            box['dt'] = time.monotonic() - t0

        def wrapped(principal, project_id, action, fields, key, task=None):
            if action == 'create' and 't' not in box:
                box['t'] = threading.Thread(target=archive)
                box['t'].start()
                time.sleep(2)
                box['mid'] = rooms.read_room(self.project, rid)['state']
            return original(principal, project_id, action, fields, key, task)
        self.backend.owner_requirements = wrapped
        try:
            answer = self.decide(rid, 'alex', key='r9-accept-alex')
        finally:
            self.backend.owner_requirements = original
        box['t'].join(120)
        rows = self.with_pointer(rid)
        print('\nR9 room state during write', box['mid'], '| archive', box['a'].status, '%.1fs' % box['dt'],
              '| accept', answer.status, (answer.data.get('error') or {}).get('code'),
              '| rows', rows, '| events', len(self.events(rid)),
              '| room now', rooms.read_room(self.project, rid)['state'])
        self.assertEqual('open', box['mid'])
        self.assertEqual(200, answer.status)
        self.assertEqual(2, len(rows))
        self.assertEqual(1, len(self.events(rid)))

    def test_r9b_room_archived_during_contributor_proposal(self):
        rid = self.room('R9b')
        self.assertEqual(201, self.item(rid, key='r9b-item-key').status)
        original = self.backend.invoke
        box = {}

        def archive():
            box['a'] = self.request('POST', '/v1/projects/pp/rooms/%s/archive' % rid, {},
                                    token=self.tokens['alex'], key='r9b-archive')

        def wrapped(route, *a, **kw):
            if route == 'proposals.submit' and 't' not in box:
                box['t'] = threading.Thread(target=archive)
                box['t'].start()
                time.sleep(2)
                box['mid'] = rooms.read_room(self.project, rid)['state']
            return original(route, *a, **kw)
        self.backend.invoke = wrapped
        before = self.all_props()
        try:
            answer = self.decide(rid, 'casey', key='r9b-accept-casey')
        finally:
            self.backend.invoke = original
        box['t'].join(120)
        props = [p for p in self.all_props() if p not in before]
        print('\nR9b room state during proposal', box['mid'], '| archive', box['a'].status,
              '| accept', answer.status, (answer.data.get('error') or {}).get('code'),
              '| new proposals', props, '| events', len(self.events(rid)))
        self.assertEqual(200, answer.status)
        self.assertEqual(1, len(props))
        self.assertEqual(1, len(self.events(rid)))

    # ---- S-1: failed append, amend, accept ----
    def test_r12_failed_append_amend_accept_requirement(self):
        rid = self.room('R12')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r12-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='r12-accept-1')
        rows1 = self.with_pointer(rid)
        am = self.decide(rid, 'alex', operation='amend',
                         body={'title': 'Add TSV export', 'text': 'We need TSV export.'}, key='r12-amend')
        second = self.decide(rid, 'alex', key='r12-accept-2')
        rows2 = self.with_pointer(rid)
        item = rooms.read_item(self.project, rid, 1)
        print('\nR12 first accept', first.status, self.err(first), '| rows after first', rows1)
        print('R12 amend', am.status, '| second accept', second.status, self.err(second),
              (second.data.get('record') or {}).get('id'))
        print('R12 rows after', rows2, '| item', item['state'], item['title'], (item['record'] or {}).get('id'))
        self.assertEqual(2, len(rows2), 'more than one requirement+decision pair carries the pointer')

    def test_r10_retry_after_failed_append_then_amend_decision(self):
        rid = self.room('R10')
        self.assertEqual(201, self.item(rid, kind='decision', title='Use CSV', text='CSV first.',
                                        key='r10-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='r10-accept-1')
        rows1 = self.with_pointer(rid)
        am = self.decide(rid, 'alex', operation='amend', body={'title': 'Use TSV', 'text': 'TSV first.'},
                         key='r10-amend')
        second = self.decide(rid, 'alex', key='r10-accept-2')
        rows2 = self.with_pointer(rid)
        item = rooms.read_item(self.project, rid, 1)
        print('\nR10 first accept', first.status, self.err(first), '| rows after first', rows1)
        print('R10 amend', am.status, '| retry accept', second.status, self.err(second), second.data.get('record'))
        print('R10 rows after retry', rows2, '| item now', item['state'], item['title'], item['record'])
        self.assertEqual(1, len(rows2), 'two decision rows carry the pointer')

    def test_r11_retry_after_failed_append_requirement(self):
        rid = self.room('R11')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r11-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='r11-accept-1')
        rows1 = self.with_pointer(rid)
        second = self.decide(rid, 'alex', key='r11-accept-2')
        rows2 = self.with_pointer(rid)
        print('\nR11 first', first.status, '| rows', rows1, '| retry (fresh key)', second.status,
              second.data.get('record'), '| rows after', rows2)
        self.assertEqual(200, second.status)
        self.assertEqual(2, len(rows2))

    def test_r13_failed_append_then_other_parent(self):
        rid = self.room('R13')
        job2 = json.loads(self.bd('create', 'Second job', '--type', 'epic', '--json').stdout)['id']
        self.assertEqual(201, self.item(rid, parent=self.job, key='r13-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='r13-accept-1')
        second = self.decide(rid, 'alex', key='r13-accept-2', body={'parent': job2})
        rows = self.with_pointer(rid)
        parents = []
        for row_id, kind, _t, _s in rows:
            if kind != 'decision':
                r = self.row(row_id)
                parents.append((row_id, r.get('parent'), [d.get('depends_on_id') for d in (r.get('dependencies') or [])]))
        print('\nR13 first', first.status, '| second (editor picks job2 %s)' % job2, second.status, self.err(second),
              (second.data.get('record') or {}).get('id'), '| rows', rows, '| parents', parents,
              '| item', rooms.read_item(self.project, rid, 1)['state'])

    def test_r14_a_fail_b_fail_a(self):
        rid = self.room('R14')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r14-item-key').status)
        self.fail_appends((1, 2))
        s = [self.decide(rid, 'alex', key='r14-acc-1').status]
        s.append(self.decide(rid, 'alex', operation='amend', body={'title': 'B', 'text': 'B text.'},
                             key='r14-am-1').status)
        s.append(self.decide(rid, 'alex', key='r14-acc-2').status)
        s.append(self.decide(rid, 'alex', operation='amend', body={'title': 'Add export', 'text': 'We need CSV export.'},
                             key='r14-am-2').status)
        last = self.decide(rid, 'alex', key='r14-acc-3')
        s.append(last.status)
        rows = self.with_pointer(rid)
        item = rooms.read_item(self.project, rid, 1)
        print('\nR14 statuses acc/amend/acc/amend/acc', s, self.err(last), '| rows', rows,
              '| item', item['state'], item['title'], (item['record'] or {}).get('id'))

    def test_r15_contributor_failed_append_amend_accept(self):
        rid = self.room('R15')
        self.assertEqual(201, self.item(rid, key='r15-item-key').status)
        before = self.all_props()
        self.fail_appends()
        first = self.decide(rid, 'casey', key='r15-accept-1')
        am = self.decide(rid, 'casey', operation='amend', body={'title': 'Add TSV', 'text': 'We need TSV.'},
                         key='r15-amend')
        second = self.decide(rid, 'casey', key='r15-accept-2')
        props = [p for p in self.all_props() if p not in before]
        print('\nR15 contributor first', first.status, '| amend', am.status, '| second', second.status,
              self.err(second), second.data.get('record'), '| new proposals', props)

    def test_r16_amend_parents(self):
        made = self.bd('create', 'A decision row', '--type', 'decision', '--json')
        decision_row = json.loads(made.stdout)['id']
        task_row = json.loads(self.bd('create', 'Plain task', '--type', 'task', '--json').stdout)['id']
        rid = self.room('R16')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r16-item-key').status)
        out = []
        for label, parent in (('decision-row', decision_row), ('other-project', 'qq-abc'),
                              ('missing', 'pp-zzzz9'), ('plain-task', task_row)):
            a = self.decide(rid, 'alex', operation='amend', key='r16-am-%s' % label,
                            body={'title': 'Add export', 'text': 'We need CSV export.', 'parent': parent})
            out.append((label, a.status, self.err(a)[:60]))
            b = self.decide(rid, 'alex', key='r16-acc-%s' % label, body={'parent': parent})
            out.append((label + ' accept', b.status, self.err(b)[:60]))
            if b.status == 200:
                break
        out.append(('rows', self.with_pointer(rid)))
        for line in out:
            print('R16', line)


    def test_r8b_contributor_and_owner_concurrently_counted(self):
        rid = self.room('R8bc')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r8bc-item-key').status)
        before = self.all_props()
        answers = []
        barrier = threading.Barrier(2)

        def run(who):
            barrier.wait(5)
            answers.append((who, self.decide(rid, who, key='r8bc-accept-' + who)))
        threads = [threading.Thread(target=run, args=(w,)) for w in ('casey', 'alex')]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
        rows = self.with_pointer(rid)
        props = [p for p in self.all_props() if p not in before]
        print('\nR8bc contributor+owner', [(w, a.status, (a.data.get('error') or {}).get('code')) for w, a in answers],
              'rows', rows, 'new proposals', props, 'events', len(self.events(rid)))
        self.assertTrue((len(rows) == 2 and not props) or (not rows and len(props) == 1))
        self.assertEqual(1, len(self.events(rid)))


if __name__ == '__main__':
    unittest.main()
