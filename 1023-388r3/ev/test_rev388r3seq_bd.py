"""Reviewer real-bd repros for kittrial-5bb.388 r2 (not part of the delivery)."""
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
import http_authority
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


class Rev388r3SeqBd(base.RoomsItemsRealBdTests):
    def with_pointer(self, rid, sequence=1):
        pointer = self.pointer(rid, sequence)
        return [(row.get('id'), row.get('issue_type') or row.get('type'), row.get('title'), row.get('status'))
                for row in self.export_rows() if pointer in (row.get('description') or '')]

    def proposals_with(self, needle):
        answer = self.request('GET', '/v1/projects/pp/proposals', token=self.tokens['alex'])
        return [p for p in (answer.data.get('items') or []) if needle in json.dumps(p)]

    def principal(self, who):
        return self.service.authenticate(self.tokens[who])

    def set_role(self, who, role):
        admin = self.admin_token()
        r = self.request('PUT', '/v1/projects/pp/members/%s' % self.ids[who], {'role': role}, token=admin)
        self.assertIn(r.status, (200, 201), r.data)

    def events(self, rid):
        return [l for l in rooms.load_lines(self.project, rid) if l['kind'] == 'item-event']

    def test_r1_accept_after_accept(self):
        rid = self.room('R1')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r1-item-key').status)
        first = self.decide(rid, 'alex', key='r1-accept-alex')
        self.assertEqual(200, first.status, first.data)
        self.set_role('drew', 'coordinator')
        try:
            second = self.decide(rid, 'drew', key='r1-accept-drew')
        finally:
            self.set_role('drew', 'contributor')
        rows = self.with_pointer(rid)
        print('\nR1 second accept', second.status, second.data.get('error', {}).get('code'))
        print('R1 rows carrying the pointer:', rows, 'events', len(self.events(rid)))
        self.assertEqual(2, len(rows), 'one requirement + one decision only')

    def test_r2_contributor_accept_of_moderated_item(self):
        rid = self.room('R2')
        self.assertEqual(201, self.item(rid, text='SECRET R2 text', key='r2-item-key').status)
        mod = self.request('POST', '/v1/projects/pp/rooms/%s/messages/1/moderate' % rid,
                           {'reason': 'private'}, token=self.tokens['alex'], key='r2-moderate')
        self.assertEqual(201, mod.status, mod.data)
        before = len(self.proposals_with(''))
        answer = self.decide(rid, 'casey', key='r2-accept-casey')
        after = len(self.proposals_with(''))
        print('\nR2 contributor accept of moderated item', answer.status, answer.data.get('error', {}).get('code'),
              'proposals before/after', before, after, 'pointer proposals', len(self.proposals_with(self.pointer(rid))),
              'rows', self.with_pointer(rid))
        self.assertEqual(before, after)

    def test_r3_decision_in_archived_room(self):
        rid = self.room('R3')
        self.assertEqual(201, self.item(rid, kind='decision', title='Use CSV', text='CSV first.',
                                        key='r3-item-key').status)
        arch = self.request('POST', '/v1/projects/pp/rooms/%s/archive' % rid, {}, token=self.tokens['alex'])
        self.assertEqual(200, arch.status, arch.data)
        answer = self.decide(rid, 'alex', key='r3-accept-alex')
        rows = self.with_pointer(rid)
        print('\nR3 archived-room decision accept', answer.status, answer.data.get('error', {}).get('code'), 'rows:', rows)
        self.assertEqual([], rows)

    def test_r5_parent_variants(self):
        made = self.bd('create', 'A decision row', '--type', 'decision', '--json')
        self.assertEqual(0, made.returncode, made.stderr)
        decision_row = json.loads(made.stdout)['id']
        closed = json.loads(self.bd('create', 'Closed epic', '--type', 'epic', '--json').stdout)['id']
        c = self.bd('close', closed)
        print('\nR5 closed epic', closed, 'close rc', c.returncode)
        task_row = json.loads(self.bd('create', 'Plain task', '--type', 'task', '--json').stdout)['id']
        out = []
        for label, parent in (('decision-row', decision_row), ('closed-epic', closed),
                              ('other-project', 'qq-abc'), ('missing', 'pp-zzzz9'), ('plain-task', task_row),
                              ('self-job', self.job)):
            rid = self.room('R5 ' + label)
            posted = self.item(rid, parent=parent, key='r5-item-%s' % label)
            if posted.status != 201:
                out.append((label, 'post', posted.status))
                continue
            accepted = self.decide(rid, 'alex', key='r5-accept-%s' % label)
            parent_seen = None
            if accepted.status == 200:
                row = self.row(accepted.data['record']['id'])
                parent_seen = row.get('parent') or [d.get('depends_on_id') or d.get('id') for d in (row.get('dependencies') or [])][:2]
            out.append((label, parent, accepted.status, (accepted.data.get('error') or {}).get('message', '')[:80],
                        parent_seen, len(self.with_pointer(rid))))
        for line in out:
            print('R5', line)
        # a page/HTTP check: the 422 has a message the page can show
        self.assertTrue(all(o[2] != 200 for o in out if o[0] in ('decision-row', 'other-project', 'missing')), out)

    def test_r8_two_owner_accepts_concurrently(self):
        rid = self.room('R8')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r8-item-key').status)
        self.set_role('drew', 'coordinator')
        answers = []
        try:
            barrier = threading.Barrier(2)

            def run(who):
                barrier.wait(5)
                answers.append((who, self.decide(rid, who, key='r8-accept-' + who)))
            threads = [threading.Thread(target=run, args=(w,)) for w in ('alex', 'drew')]
            t0 = time.monotonic()
            for t in threads:
                t.start()
            for t in threads:
                t.join(180)
        finally:
            self.set_role('drew', 'contributor')
        rows = self.with_pointer(rid)
        print('\nR8 two owner accepts', [(w, a.status, a.data.get('error', {}).get('code')) for w, a in answers],
              'elapsed %.1fs' % (time.monotonic() - t0), 'rows', rows, 'events', len(self.events(rid)))
        self.assertEqual(2, len(rows))
        self.assertEqual(1, len(self.events(rid)))

    def test_r8b_contributor_and_owner_concurrently(self):
        rid = self.room('R8b')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r8b-item-key').status)
        answers = []
        barrier = threading.Barrier(2)

        def run(who):
            barrier.wait(5)
            answers.append((who, self.decide(rid, who, key='r8b-accept-' + who)))
        threads = [threading.Thread(target=run, args=(w,)) for w in ('casey', 'alex')]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
        rows = self.with_pointer(rid)
        props = self.proposals_with(self.pointer(rid))
        print('\nR8b contributor+owner', [(w, a.status, a.data.get('error', {}).get('code')) for w, a in answers],
              'rows', rows, 'proposals', len(props), 'events', len(self.events(rid)))
        self.assertTrue((len(rows) == 2 and not props) or (not rows and len(props) == 1))

    def _r2only_r9(self):
        rid = self.room('R9')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r9-item-key').status)
        original = self.backend.owner_requirements
        box = {}

        def wrapped(principal, project_id, action, fields, key, task=None):
            if action == 'create' and 'a' not in box:
                box['a'] = self.request('POST', '/v1/projects/pp/rooms/%s/archive' % rid, {},
                                        token=self.tokens['alex'], key='r9-archive')
            return original(principal, project_id, action, fields, key, task)
        self.backend.owner_requirements = wrapped
        try:
            answer = self.decide(rid, 'alex', key='r9-accept-alex')
        finally:
            self.backend.owner_requirements = original
        rows = self.with_pointer(rid)
        print('\nR9 archive during write', box['a'].status, '| accept', answer.status,
              answer.data.get('error', {}).get('code'), '| rows', rows, '| events', len(self.events(rid)))
        self.assertEqual([], rows)

    def _r2only_r9b(self):
        rid = self.room('R9b')
        self.assertEqual(201, self.item(rid, key='r9b-item-key').status)
        original = self.backend.invoke
        box = {}

        def wrapped(route, *a, **kw):
            if route == 'proposals.submit' and 'a' not in box:
                box['a'] = self.request('POST', '/v1/projects/pp/rooms/%s/archive' % rid, {},
                                        token=self.tokens['alex'], key='r9b-archive')
            return original(route, *a, **kw)
        self.backend.invoke = wrapped
        before_all = self.request('GET', '/v1/projects/pp/proposals', token=self.tokens['alex']).data
        try:
            answer = self.decide(rid, 'casey', key='r9b-accept-casey')
        finally:
            self.backend.invoke = original
        props = self.proposals_with(self.pointer(rid))
        after_all = self.request('GET', '/v1/projects/pp/proposals', token=self.tokens['alex']).data
        print('\nR9b proposals total before/after', len(before_all.get('items') or []),
              len(after_all.get('items') or []), 'keys', list(after_all)[:8],
              'last item', json.dumps((after_all.get('items') or [None])[-1])[:700])
        print('\nR9b archive during proposal', box.get('a') and box['a'].status, '| accept', answer.status,
              answer.data.get('error', {}).get('code'), '| proposals with pointer', len(props))
        self.assertEqual(0, len(props))

    def test_r10_retry_after_failed_append_then_amend(self):
        # A decision accept whose room append fails after the native decision exists; the item
        # is then amended and accepted again. Which decision does the retry attach to?
        rid = self.room('R10')
        self.assertEqual(201, self.item(rid, kind='decision', title='Use CSV', text='CSV first.',
                                        key='r10-item-key').status)
        original = rooms.accept_item
        state = {'n': 0}

        def failing(*a, **kw):
            state['n'] += 1
            if state['n'] == 1:
                raise OSError('disk full (reviewer injected)')
            return original(*a, **kw)
        rooms.accept_item = failing
        try:
            first = self.decide(rid, 'alex', key='r10-accept-1')
            rows1 = self.with_pointer(rid)
            am = self.decide(rid, 'alex', operation='amend', body={'title': 'Use TSV', 'text': 'TSV first.'},
                             key='r10-amend')
            second = self.decide(rid, 'alex', key='r10-accept-2')
        finally:
            rooms.accept_item = original
        rows2 = self.with_pointer(rid)
        item = rooms.read_item(self.project, rid, 1)
        print('\nR10 first accept', first.status, (first.data.get('error') or {}).get('message', '')[:100],
              '| rows after first', rows1)
        print('R10 amend', am.status, '| retry accept', second.status,
              (second.data.get('error') or {}).get('message', '')[:120], second.data.get('record'))
        print('R10 rows after retry', rows2, '| item now', item['state'], item['title'], item['record'])

    def test_r11_retry_after_failed_append_requirement(self):
        rid = self.room('R11')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r11-item-key').status)
        original = rooms.accept_item
        state = {'n': 0}

        def failing(*a, **kw):
            state['n'] += 1
            if state['n'] == 1:
                raise OSError('disk full (reviewer injected)')
            return original(*a, **kw)
        rooms.accept_item = failing
        try:
            first = self.decide(rid, 'alex', key='r11-accept-1')
            rows1 = self.with_pointer(rid)
            second = self.decide(rid, 'alex', key='r11-accept-2')
        finally:
            rooms.accept_item = original
        rows2 = self.with_pointer(rid)
        print('\nR11 first', first.status, '| rows', rows1, '| retry (fresh key)', second.status,
              second.data.get('record'), '| rows after', rows2)
        self.assertEqual(2, len(rows2))


if __name__ == '__main__':
    unittest.main()
