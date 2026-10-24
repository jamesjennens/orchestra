"""Reviewer repros for kittrial-5bb.388 r1 (not part of the delivery)."""
import json
import socket
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rooms
import test_http_agents as agents
import test_rooms_items_http as base

_orig_create = agents.create_server


def _ranged_create(service, backend, host='127.0.0.1', port=0, **kw):
    # Reviewer brief: listen on 127.0.0.1, a port in 29600-29699 only.
    for candidate in range(29600, 29700):
        try:
            return _orig_create(service, backend, host='127.0.0.1', port=candidate, **kw)
        except OSError:
            continue
    raise RuntimeError('no free port in 29600-29699')


agents.create_server = _ranged_create


class Rev388(base.RoomsItemsHttpTests):
    def lines(self):
        return rooms.load_lines(Path(self.rooms_root) / self.pid, self.room['id'])

    # F1: a second accept (fresh key) after the item is accepted creates a second record.
    def test_f1_accept_after_accept_creates_orphan_requirement(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.assertEqual(200, self.accept(self.tokens['owner'], key='k-a-rev388key').status)
        third = self.accept(self.tokens['coord'], key='k-b-rev388key')
        print('\nF1 second accept status', third.status, third.data.get('error', {}).get('code'))
        print('F1 requirements in tracker:', sorted(self.backend.requirements))
        print('F1 owner_requirements calls:', self.backend.calls)
        self.assertEqual(1, len(self.backend.requirements), 'orphan requirement created')

    # F1b: accepting a rejected / superseded item still creates the record first.
    def test_f1b_accept_rejected_item_creates_record(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.assertEqual(200, self.decision(self.tokens['owner']).status)
        refused = self.accept(self.tokens['owner'], key='k-after-reject-rev388key')
        print('\nF1b accept-after-reject', refused.status, 'requirements:', sorted(self.backend.requirements))
        self.assertEqual({}, self.backend.requirements)

    def test_f1c_contributor_accept_of_rejected_item_creates_proposal(self):
        self.post_item(self.tokens['worker'])
        self.assertEqual(200, self.decision(self.tokens['owner']).status)
        refused = self.accept(self.tokens['worker'], key='k-w-rev388key')
        print('\nF1c', refused.status, 'proposals:', self.backend.proposals)
        self.assertEqual({}, self.backend.proposals)

    def test_f1d_accept_in_archived_room_creates_record(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.assertEqual(200, self.request('POST', self.path('/archive'), {},
                                           token=self.tokens['owner']).status)
        refused = self.accept(self.tokens['owner'], key='late-rev388key')
        print('\nF1d', refused.status, 'requirements:', sorted(self.backend.requirements))
        self.assertEqual({}, self.backend.requirements)

    def test_f1e_moderated_item_accept_reaches_record_write_with_tombstone(self):
        self.post_item(self.tokens['worker'], text='SECRET item text')
        self.assertEqual(201, self.request('POST', self.path('/messages/1/moderate'),
                                           {'reason': 'private'}, token=self.tokens['coord'],
                                           key='mod-rev388key').status)
        refused = self.accept(self.tokens['worker'], key='k-mod-rev388key')
        print('\nF1e', refused.status, 'proposals:', json.dumps(self.backend.proposals))
        self.assertEqual({}, self.backend.proposals)

    # F2: concurrent contributor + owner accept -> requirement AND proposal created.
    def test_f2_concurrent_accept_two_records(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        original = self.backend.owner_requirements
        box = {}

        def slow(principal, project_id, action, fields, key, task=None):
            if action == 'create' and 'w' not in box:
                box['w'] = self.accept(self.tokens['worker'], key='k-worker-rev388key')
            return original(principal, project_id, action, fields, key, task)
        self.backend.owner_requirements = slow
        owner = self.accept(self.tokens['owner'], key='k-owner-rev388key')
        print('\nF2 worker', box['w'].status, 'owner', owner.status,
              owner.data.get('error', {}).get('code'))
        print('F2 requirements', sorted(self.backend.requirements), 'proposals', sorted(self.backend.proposals))
        events = [l for l in self.lines() if l['kind'] == 'item-event']
        print('F2 events', [(e['event'], e['record']['kind']) for e in events])
        self.assertFalse(self.backend.requirements and self.backend.proposals)

    # F3: a viewer can amend (rewrite) someone else's question, and reject it.
    def test_f3_viewer_amends_question(self):
        self.post_item(self.tokens['worker'], kind='question', title='Delimiter?', text='Comma?')
        amended = self.request('POST', self.path('/items/1/amend'),
                               {'title': 'Viewer wrote this', 'text': 'Viewer text'},
                               token=self.tokens['viewer'], key='v-amend-rev388key')
        print('\nF3 viewer amend', amended.status, amended.data.get('title'), amended.data.get('text'))
        self.assertEqual(403, amended.status)

    def test_f3b_viewer_rejects_question(self):
        self.post_item(self.tokens['worker'], kind='question', title='Delimiter?', text='Comma?')
        rejected = self.request('POST', self.path('/items/1/reject'), {'reason': 'viewer says no'},
                                token=self.tokens['viewer'], key='v-rej-rev388key')
        print('\nF3b viewer reject', rejected.status, rejected.data.get('state'))
        self.assertEqual(403, rejected.status)

    # F4: amended title of a moderated item is served by the room read.
    def test_f4_moderated_amend_title_leaks(self):
        self.post_item(self.tokens['worker'], text='Original text')
        self.assertEqual(200, self.request('POST', self.path('/items/1/amend'),
                                           {'title': 'SECRET-AMENDED-TITLE', 'text': 'SECRET-AMENDED-TEXT'},
                                           token=self.tokens['owner'], key='am-rev388key').status)
        self.assertEqual(201, self.request('POST', self.path('/messages/1/moderate'),
                                           {'reason': 'private'}, token=self.tokens['coord'],
                                           key='mod-rev388key').status)
        read = self.read(self.tokens['viewer'])
        dumped = json.dumps(read.data)
        print('\nF4 title in read:', 'SECRET-AMENDED-TITLE' in dumped, 'text in read:', 'SECRET-AMENDED-TEXT' in dumped)
        md = self.request('GET', self.path('/export') + '?format=markdown', token=self.tokens['viewer'])
        js = self.request('GET', self.path('/export') + '?format=jsonl', token=self.tokens['viewer'])
        print('F4 exports', md.status, js.status)
        self.assertNotIn('SECRET-AMENDED-TITLE', dumped)

    # F5: archived PROJECT: event routes still write.
    def test_f5_archived_project_item_events(self):
        self.post_item(self.tokens['worker'], kind='question', title='Q', text='Q?')
        self.post_item(self.tokens['worker'], title='R', text='R', key='i2-rev388key', parent='job_0001')
        self.service.state['projects'][self.pid]['archived'] = True
        self.store.save()
        answered = self.request('POST', self.path('/items/1/answer'), {'text': 'late'},
                                token=self.tokens['viewer'], key='ap-ans-rev388key')
        accepted = self.accept(self.tokens['owner'], sequence=2, key='ap-acc-rev388key')
        print('\nF5 archived project answer', answered.status, 'accept', accepted.status,
              'requirements', sorted(self.backend.requirements))
        self.assertNotEqual(200, answered.status)
        self.assertNotEqual(200, accepted.status)

    # F6: outsider (other project's coordinator) probing a room item before membership.
    def test_f6_outsider_probe(self):
        self.post_item(self.tokens['worker'], kind='question', title='Q', text='Q?')
        a = self.request('POST', self.path('/items/1/answer'), {'text': 'x'}, token=self.tokens['outsider'], key='o1-rev388key')
        b = self.request('POST', self.path('/items/9/answer'), {'text': 'x'}, token=self.tokens['outsider'], key='o2-rev388key')
        c = self.request('POST', self.path('/items/1/reject'), {'reason': 'x'}, token=self.tokens['outsider'], key='o3-rev388key')
        print('\nF6 outsider', a.status, b.status, c.status, a.data, b.data)

    # F7: hand-edited index.json items cache is served as-is when byte checks still pass.
    def test_f7_hand_edited_cache_served(self):
        self.post_item(self.tokens['worker'])
        index_path = Path(self.rooms_root) / self.pid / rooms.FOLDER / rooms.INDEX
        index = json.loads(index_path.read_text(encoding='utf-8'))
        index['rooms'][self.room['id']]['items'][0]['state'] = 'accepted'
        index['rooms'][self.room['id']]['items'][0]['text'] = 'FORGED'
        index_path.write_text(json.dumps(index), encoding='utf-8')
        served = self.items()
        print('\nF7 served', served[0]['state'], served[0]['text'])
        truth = rooms.read_item(Path(self.rooms_root) / self.pid, self.room['id'], 1)
        print('F7 truth', truth['state'])
        self.assertEqual('proposed', served[0]['state'])

    # F8: damaged index.json rebuilds identical items.
    def test_f8_damaged_index_rebuilds(self):
        self.post_item(self.tokens['worker'])
        self.request('POST', self.path('/items/1/amend'), {'title': 'T2', 'text': 'X2'},
                     token=self.tokens['owner'], key='am-rev388key')
        before = self.items()
        index_path = Path(self.rooms_root) / self.pid / rooms.FOLDER / rooms.INDEX
        raw = index_path.read_bytes()
        index_path.write_bytes(raw[: len(raw) // 2])
        after = self.items()
        self.assertEqual(before, after)
        index_path.unlink()
        self.assertEqual(before, self.items())


if __name__ == '__main__':
    unittest.main()
