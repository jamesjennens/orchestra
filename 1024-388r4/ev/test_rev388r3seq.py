"""Reviewer repros for kittrial-5bb.388 r2 (not part of the delivery). Stub backend."""
import fcntl
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rooms
import test_http_agents as agents
import test_rooms_items_http as base

_orig_create = agents.create_server


def _ranged_create(service, backend, host='127.0.0.1', port=0, **kw):
    # Reviewer brief: listen on 127.0.0.1, a port in 29800-29899 only.
    for candidate in range(29800, 29900):
        try:
            return _orig_create(service, backend, host='127.0.0.1', port=candidate, **kw)
        except OSError:
            continue
    raise RuntimeError('no free port in 29800-29899')


agents.create_server = _ranged_create


class Rev388r3Seq(base.RoomsItemsHttpTests):
    def lines(self, rid=None):
        return rooms.load_lines(Path(self.rooms_root) / self.pid, rid or self.room['id'])

    def counts(self, tag):
        c = (len(self.backend.requirements), len(self.backend.proposals), len(self.backend.decisions))
        print('%s requirements/proposals/decisions = %s' % (tag, c))
        return c

    # ---- B1 reruns ----
    def test_f1_accept_after_accept(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.assertEqual(200, self.accept(self.tokens['owner'], key='k-a-rev388key').status)
        second = self.accept(self.tokens['coord'], key='k-b-rev388key')
        print('\nF1 second accept status', second.status, second.data.get('error', {}).get('code'))
        self.assertEqual((1, 0, 1), self.counts('F1'))
        self.assertEqual(409, second.status)

    def test_f1b_accept_rejected_item(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.assertEqual(200, self.decision(self.tokens['owner']).status)
        refused = self.accept(self.tokens['owner'], key='k-after-reject-rev388key')
        print('\nF1b accept-after-reject', refused.status)
        self.assertEqual((0, 0, 0), self.counts('F1b'))

    def test_f1c_contributor_accept_of_rejected_item(self):
        self.post_item(self.tokens['worker'])
        self.assertEqual(200, self.decision(self.tokens['owner']).status)
        refused = self.accept(self.tokens['worker'], key='k-w-rev388key')
        print('\nF1c', refused.status)
        self.assertEqual((0, 0, 0), self.counts('F1c'))

    def test_f1d_accept_in_archived_room(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.assertEqual(200, self.request('POST', self.path('/archive'), {},
                                           token=self.tokens['owner']).status)
        refused = self.accept(self.tokens['owner'], key='late-rev388key')
        print('\nF1d', refused.status)
        self.assertEqual((0, 0, 0), self.counts('F1d'))

    def test_f1e_moderated_item_accept(self):
        self.post_item(self.tokens['worker'], text='SECRET item text')
        self.assertEqual(201, self.request('POST', self.path('/messages/1/moderate'),
                                           {'reason': 'private'}, token=self.tokens['coord'],
                                           key='mod-rev388key').status)
        refused = self.accept(self.tokens['worker'], key='k-mod-rev388key')
        print('\nF1e', refused.status)
        self.assertEqual((0, 0, 0), self.counts('F1e'))

    def test_f1f_superseded_item_accept(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.assertEqual(201, self.post_item(self.tokens['worker'], key='i2-rev388key', supersedes=1,
                                             parent='job_0001').status)
        refused = self.accept(self.tokens['owner'], key='k-sup-rev388key')
        print('\nF1f superseded accept', refused.status)
        self.assertEqual((0, 0, 0), self.counts('F1f'))

    # ---- B2 rerun: contributor accept arrives while the owner's record write is running ----
    def test_f2_concurrent_accept_one_record(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        original = self.backend.owner_requirements
        box = {}

        def worker():
            box['w'] = self.accept(self.tokens['worker'], key='k-worker-rev388key')

        def slow(principal, project_id, action, fields, key, task=None):
            if action == 'create' and 't' not in box:
                box['t'] = threading.Thread(target=worker)
                box['t'].start()
                time.sleep(1.5)  # the worker request is now waiting on the item lock
            return original(principal, project_id, action, fields, key, task)
        self.backend.owner_requirements = slow
        t0 = time.monotonic()
        owner = self.accept(self.tokens['owner'], key='k-owner-rev388key')
        box['t'].join(90)
        print('\nF2 owner', owner.status, 'worker', box['w'].status, box['w'].data.get('error', {}).get('code'),
              'elapsed %.1fs' % (time.monotonic() - t0))
        events = [l for l in self.lines() if l['kind'] == 'item-event']
        print('F2 events', [(e['event'], e['record']['kind']) for e in events])
        self.assertEqual((1, 0, 1), self.counts('F2'))
        self.assertEqual(1, len(events))

    # ---- lock timeout behaviour: hold the item lock from outside, then accept ----
    def test_l1_item_lock_held_elsewhere(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        lock_path = Path(self.rooms_root) / self.pid / '.item-decisions.lock'
        other = Path(self.rooms_root) / self.other_pid / '.item-decisions.lock'
        print('\nL1 lock path exists', lock_path.exists(), lock_path, 'other project lock path', other)
        import http.client as hc
        orig_conn = hc.HTTPConnection

        class Slow(orig_conn):
            def __init__(self, host, port=None, timeout=None, **kw):
                super().__init__(host, port, timeout=120, **kw)
        hc.HTTPConnection = Slow
        self.addCleanup(setattr, hc, 'HTTPConnection', orig_conn)
        handle = open(lock_path, 'a+b')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            t0 = time.monotonic()
            answer = self.accept(self.tokens['owner'], key='k-lock-rev388key')
            print('L1 accept while lock held elsewhere', answer.status, answer.data.get('error'),
                  'after %.1fs' % (time.monotonic() - t0))
            # a message post (room lock only) is not blocked by the item lock
            t0 = time.monotonic()
            msg = self.request('POST', self.path('/messages'), {'text': 'hi'}, token=self.tokens['worker'],
                               key='k-msg-rev388key')
            print('L1 message post while item lock held', msg.status, 'after %.1fs' % (time.monotonic() - t0))
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self.counts('L1')
        retry = self.accept(self.tokens['owner'], key='k-lock2-rev388key')
        print('L1 accept after release', retry.status)

    # ---- B3 reruns and extensions ----
    def test_f3_viewer_amends_question(self):
        self.post_item(self.tokens['worker'], kind='question', title='Delimiter?', text='Comma?')
        amended = self.request('POST', self.path('/items/1/amend'),
                               {'title': 'Viewer wrote this', 'text': 'Viewer text'},
                               token=self.tokens['viewer'], key='v-amend-rev388key')
        print('\nF3 viewer amend', amended.status, amended.data.get('error', {}).get('message'))
        self.assertEqual(403, amended.status)

    def test_f3b_viewer_rejects_question(self):
        self.post_item(self.tokens['worker'], kind='question', title='Delimiter?', text='Comma?')
        rejected = self.request('POST', self.path('/items/1/reject'), {'reason': 'viewer says no'},
                                token=self.tokens['viewer'], key='v-rej-rev388key')
        print('\nF3b viewer reject', rejected.status)
        self.assertEqual(403, rejected.status)

    def test_f3c_viewer_answers_question(self):
        self.post_item(self.tokens['worker'], kind='question', title='Delimiter?', text='Comma?')
        answered = self.request('POST', self.path('/items/1/answer'), {'text': 'Viewer answer'},
                                token=self.tokens['viewer'], key='v-ans-rev388key')
        print('\nF3c viewer answer', answered.status, answered.data.get('state'), answered.data.get('text'))

    def test_f3d_contributor_amends_owners_requirement(self):
        self.post_item(self.tokens['owner'], title='Owner req', text='Owner text', parent='job_0001')
        amended = self.request('POST', self.path('/items/1/amend'), {'title': 'W', 'text': 'W text'},
                               token=self.tokens['worker'], key='w-amend-rev388key')
        rejected = self.request('POST', self.path('/items/1/reject'), {'reason': 'w no'},
                                token=self.tokens['worker'], key='w-rej-rev388key')
        print('\nF3d contributor on owner item: amend', amended.status, 'reject', rejected.status)
        self.assertEqual(403, amended.status)
        self.assertEqual(403, rejected.status)

    def test_f3e_contributor_amends_other_contributors_item(self):
        # second contributor
        wid = self.create_account(self.admin, 'worker2', 'worker2-password-1')
        put = self.request('PUT', '/v1/projects/%s/members/%s' % (self.pid, wid), {'role': 'contributor'},
                           token=self.admin)
        self.assertIn(put.status, (200, 201))
        w2 = self.login('worker2', 'worker2-password-1')[0]
        self.post_item(self.tokens['worker'], title='W1 req', text='W1 text')
        self.post_item(self.tokens['worker'], kind='decision', title='W1 dec', text='W1 dec text', key='i2-rev388key')
        out = []
        for seq in (1, 2):
            a = self.request('POST', self.path('/items/%d/amend' % seq), {'title': 'W2', 'text': 'W2'},
                             token=w2, key='w2-am-%d-rev388key' % seq)
            r = self.request('POST', self.path('/items/%d/reject' % seq), {'reason': 'w2'},
                             token=w2, key='w2-rj-%d-rev388key' % seq)
            out.append((seq, a.status, r.status))
        print('\nF3e worker2 on worker items (seq, amend, reject):', out)
        self.assertTrue(all(a == 403 and r == 403 for _s, a, r in out))

    def test_f3f_agent_credential(self):
        self.post_item(self.tokens['worker'], kind='question', title='Q', text='Q?')
        created = self.request('POST', '/v1/agents', {
            'name': 'Kestrel', 'working_directory': '/tmp/kestrel', 'projects': [self.pid]},
            token=self.tokens['owner'])
        if created.status != 201:
            print('\nF3f could not create agent', created.status, created.data)
            self.skipTest('no agent')
        agent_id = created.data.get('agent', {}).get('id') or created.data.get('id')
        secret = created.data['credential']['secret']
        grant = self.request('PUT', '/v1/projects/%s/agents/%s/coordinator' % (self.pid, agent_id), {},
                             token=self.tokens['owner'], key='grant-rev388key')
        res = {}
        for route, body in (('/items', {'item_kind': 'question', 'title': 'A', 'text': 'A'}),
                            ('/items/1/answer', {'text': 'agent'}),
                            ('/items/1/amend', {'title': 'agent', 'text': 'agent'}),
                            ('/items/1/reject', {'reason': 'agent'}),
                            ('/items/1/accept', {}),
                            ('/messages/1/moderate', {'reason': 'agent'})):
            r = self.request('POST', self.path(route), body, token=secret, key='ag-%d-rev388key' % len(res))
            res[route] = r.status
        print('\nF3f grant', grant.status, 'agent statuses', res)
        self.assertTrue(all(v in (401, 403) for v in res.values()), res)

    # ---- B4 rerun, every surface ----
    def test_f4_moderated_item_every_surface(self):
        self.post_item(self.tokens['worker'], title='SECRET-ORIG-TITLE', text='SECRET-ORIG-TEXT', parent='job_0001')
        self.assertEqual(200, self.request('POST', self.path('/items/1/amend'),
                                           {'title': 'SECRET-AMENDED-TITLE', 'text': 'SECRET-AMENDED-TEXT'},
                                           token=self.tokens['owner'], key='am-rev388key').status)
        q = self.post_item(self.tokens['worker'], kind='question', title='SECRET-Q-TITLE', text='SECRET-Q-TEXT',
                           key='i2-rev388key').data['sequence']
        self.assertEqual(200, self.request('POST', self.path('/items/%d/answer' % q), {'text': 'SECRET-ANSWER'},
                                           token=self.tokens['viewer'], key='ans-rev388key').status)
        r3 = self.post_item(self.tokens['worker'], title='SECRET-R3-TITLE', text='SECRET-R3-TEXT',
                            key='i3-rev388key').data['sequence']
        self.assertEqual(200, self.request('POST', self.path('/items/%d/reject' % r3), {'reason': 'SECRET-REASON'},
                                           token=self.tokens['owner'], key='rej-rev388key').status)
        r4 = self.post_item(self.tokens['worker'], title='SECRET-R4-TITLE', text='SECRET-R4-TEXT', key='i4-rev388key',
                            parent='job_0001').data['sequence']
        acc = self.accept(self.tokens['owner'], sequence=r4, key='acc4-rev388key', body={'note': 'SECRET-NOTE'})
        self.assertEqual(200, acc.status, acc.data)
        r5p = self.post_item(self.tokens['worker'], title='SECRET-R5-TITLE', text='SECRET-R5-TEXT', key='i5-rev388key',
                             supersedes=1, parent='SECRET-PARENT')
        self.assertEqual(201, r5p.status, r5p.data)
        r5 = r5p.data['sequence']
        for seq in (1, q, r3, r4, r5):
            m = self.request('POST', self.path('/messages/%d/moderate' % seq), {'reason': 'private'},
                             token=self.tokens['coord'], key='mod%d-rev388key' % seq)
            self.assertEqual(201, m.status, m.data)
        surfaces = {
            'read': json.dumps(self.read(self.tokens['viewer']).data),
            'read_since0_limit1': json.dumps(self.request('GET', self.path('') + '?since=0&limit=1',
                                                          token=self.tokens['viewer']).data),
            'export_md': json.dumps(self.request('GET', self.path('/export') + '?format=markdown',
                                                 token=self.tokens['viewer']).data),
            'export_jsonl': json.dumps(self.request('GET', self.path('/export') + '?format=jsonl',
                                                    token=self.tokens['viewer']).data),
            'list': json.dumps(self.request('GET', '/v1/projects/%s/rooms' % self.pid,
                                            token=self.tokens['viewer']).data),
            'index.json': (Path(self.rooms_root) / self.pid / rooms.FOLDER / rooms.INDEX).read_text(),
        }
        leaks = {}
        for name, blob in surfaces.items():
            found = sorted({w for w in ('SECRET-ORIG-TITLE', 'SECRET-ORIG-TEXT', 'SECRET-AMENDED-TITLE',
                                        'SECRET-AMENDED-TEXT', 'SECRET-Q-TITLE', 'SECRET-Q-TEXT',
                                        'SECRET-ANSWER', 'SECRET-R3-TITLE', 'SECRET-REASON',
                                        'SECRET-R4-TITLE', 'SECRET-NOTE', 'SECRET-R5-TITLE',
                                        'SECRET-PARENT') if w in blob})
            leaks[name] = found
        print('\nF4 leaks per surface:', json.dumps(leaks))
        # record pointer of moderated accepted item
        for it in self.items():
            print('F4 item', it['sequence'], it['state'], 'record', it.get('record'), 'superseded_by',
                  it.get('superseded_by'), 'supersedes', it.get('supersedes'), 'parent', it.get('parent'),
                  'history', len(it.get('history') or []))
        self.assertFalse(any(leaks.values()), leaks)

    # ---- S1 rerun ----
    def test_f5_archived_project_item_events(self):
        self.post_item(self.tokens['worker'], kind='question', title='Q', text='Q?')
        self.post_item(self.tokens['worker'], title='R', text='R', key='i2-rev388key', parent='job_0001')
        self.service.state['projects'][self.pid]['archived'] = True
        self.store.save()
        res = {
            'answer': self.request('POST', self.path('/items/1/answer'), {'text': 'late'},
                                   token=self.tokens['viewer'], key='ap-ans-rev388key').status,
            'accept': self.accept(self.tokens['owner'], sequence=2, key='ap-acc-rev388key').status,
            'post': self.post_item(self.tokens['worker'], key='ap-post-rev388key').status,
            'moderate': self.request('POST', self.path('/messages/1/moderate'), {'reason': 'x'},
                                     token=self.tokens['coord'], key='ap-mod-rev388key').status,
            'amend': self.request('POST', self.path('/items/2/amend'), {'title': 'x', 'text': 'y'},
                                  token=self.tokens['owner'], key='ap-am-rev388key').status,
        }
        print('\nF5 archived project', res)
        self.counts('F5')
        self.assertTrue(all(v == 409 for v in res.values()), res)

    # ---- N2 rerun ----
    def test_f6_outsider_probe(self):
        self.post_item(self.tokens['worker'], kind='question', title='Q', text='Q?')
        out = []
        for seq, route, body in ((1, 'answer', {'text': 'x'}), (9, 'answer', {'text': 'x'}),
                                 (1, 'reject', {'reason': 'x'}), (9, 'reject', {'reason': 'x'}),
                                 (1, 'accept', {}), (9, 'accept', {}), (1, 'amend', {'title': 'x', 'text': 'y'})):
            r = self.request('POST', self.path('/items/%d/%s' % (seq, route)), body, token=self.tokens['outsider'],
                             key='o-%d-%s-rev388key' % (seq, route))
            out.append((seq, route, r.status, r.data.get('error', {}).get('message')))
        print('\nF6 outsider', out)
        self.assertFalse(any('No such item' in (m or '') for *_x, m in out))

    # ---- F7 note rerun ----
    def test_f7_hand_edited_cache_served(self):
        self.post_item(self.tokens['worker'])
        index_path = Path(self.rooms_root) / self.pid / rooms.FOLDER / rooms.INDEX
        index = json.loads(index_path.read_text(encoding='utf-8'))
        index['rooms'][self.room['id']]['items'][0]['state'] = 'accepted'
        index['rooms'][self.room['id']]['items'][0]['text'] = 'FORGED'
        index_path.write_text(json.dumps(index), encoding='utf-8')
        served = self.items()
        print('\nF7 served', served[0]['state'], served[0]['text'])
        # does any decision read the cache? an accept after the forgery:
        acc = self.accept(self.tokens['owner'], key='f7-acc-rev388key', body={'parent': 'job_0001'})
        print('F7 accept after forged cache', acc.status, acc.data.get('error', {}).get('code'))

    # ---- NEW: second amend of the same item ----
    def test_d1_second_amend(self):
        self.post_item(self.tokens['worker'], title='T0', text='X0', parent='job_0001')
        first = self.request('POST', self.path('/items/1/amend'), {'title': 'T1', 'text': 'X1'},
                             token=self.tokens['worker'], key='am1-rev388key')
        second = self.request('POST', self.path('/items/1/amend'), {'title': 'T2', 'text': 'X2'},
                              token=self.tokens['owner'], key='am2-rev388key')
        third = self.request('POST', self.path('/items/1/amend'), {'title': 'T1', 'text': 'X1'},
                             token=self.tokens['owner'], key='am3-rev388key')
        cur = self.items()[0]
        print('\nD1 first amend', first.status, '| second amend (different text)', second.status,
              second.data.get('error', {}).get('code'), second.data.get('error', {}).get('message'),
              '| third amend (same text as first)', third.status, third.data.get('replayed'),
              '| current', cur['state'], cur['title'], cur['text'])
        print('D1 r1 operation ids would have been', rooms.item_operation(self.room['id'], 1, 0, 'amend'),
              rooms.item_operation(self.room['id'], 1, 1, 'amend'))
        self.assertEqual(200, second.status)

    # ---- NEW: room archived while the owner's record write runs (room archive does not take the item lock) ----
    def _r2only_d2(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        original = self.backend.owner_requirements
        box = {}

        def slow(principal, project_id, action, fields, key, task=None):
            if action == 'create' and 'a' not in box:
                t0 = time.monotonic()
                box['a'] = self.request('POST', self.path('/archive'), {}, token=self.tokens['coord'],
                                        key='d2-arch-rev388key')
                box['dt'] = time.monotonic() - t0
            return original(principal, project_id, action, fields, key, task)
        self.backend.owner_requirements = slow
        owner = self.accept(self.tokens['owner'], key='d2-owner-rev388key')
        events = [l for l in self.lines() if l['kind'] == 'item-event']
        print('\nD2 archive during write', box['a'].status, '(took %.2fs)' % box['dt'],
              '| owner accept', owner.status, owner.data.get('error', {}).get('code'),
              '| item events', len(events), '| item state', self.items()[0]['state'])
        self.assertEqual((0, 0, 0), self.counts('D2'))


if __name__ == '__main__':
    unittest.main()
