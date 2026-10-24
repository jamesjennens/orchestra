"""Reviewer real-bd repros for kittrial-5bb.388 r4 (not part of the delivery)."""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_rev388r3new_bd as r3   # also patches the port range
import test_rooms_items_real_bd as base
import rooms

H = r3.Rev388r3Bd


class Rev388r4Bd(base.RoomsItemsRealBdTests):
    with_pointer = H.with_pointer
    all_props = H.all_props
    events = H.events
    fail_appends = H.fail_appends
    err = H.err

    def code(self, a):
        return (a.data.get('error') or {}).get('code') if isinstance(a.data, dict) else None

    def lines(self, rid):
        return [(l['sequence'], l['kind'], l.get('event') or l.get('action') or '',
                 l.get('target_sequence', '')) for l in rooms.load_lines(self.project, rid)]

    def pending(self, rid, seq=1):
        p = rooms.pending_accept_intent(rooms.load_lines(self.project, rid), seq)
        return None if p is None else (p['sequence'], p['author'].get('name'), p['identity'][-30:])

    def set_role(self, who, role):
        admin = self.admin_token()
        a = self.request('PUT', '/v1/projects/pp/members/%s' % self.ids[who], {'role': role}, token=admin)
        self.assertIn(a.status, (200, 201), a.data)

    def moderate(self, rid, seq, who='alex', key='mod-key-0001'):
        return self.request('POST', '/v1/projects/pp/rooms/%s/messages/%d/moderate' % (rid, seq),
                            {'reason': 'abusive'}, token=self.tokens[who], key=key)

    def message(self, rid, who, text, key):
        return self.request('POST', '/v1/projects/pp/rooms/%s/messages' % rid, {'text': text},
                            token=self.tokens[who], key=key)

    def served(self, rid, who='alex'):
        a = self.request('GET', '/v1/projects/pp/rooms/%s' % rid, token=self.tokens[who])
        d = a.data if isinstance(a.data, dict) else {}
        msgs = [(m.get('sequence'), m.get('kind'), m.get('title'), (m.get('text') or '')[:30])
                for m in (d.get('messages') or [])]
        items = [(i.get('sequence'), i.get('state'), i.get('title'), (i.get('text') or '')[:30])
                 for i in (d.get('items') or [])]
        return a.status, msgs, items

    def fail_native(self):
        orig_req = self.backend.owner_requirements
        orig_inv = self.backend.invoke
        orig_dec = getattr(self.backend, 'room_decision', None)

        def req(principal, pid, action, *a, **kw):
            if action == 'create':
                raise OSError('bd unavailable (reviewer injected)')
            return orig_req(principal, pid, action, *a, **kw)

        def inv(route, *a, **kw):
            if route == 'proposals.submit':
                raise OSError('bd unavailable (reviewer injected)')
            return orig_inv(route, *a, **kw)

        def dec(*a, **kw):
            raise OSError('bd unavailable (reviewer injected)')
        self.backend.owner_requirements = req
        self.backend.invoke = inv
        self.backend.room_decision = dec

        def restore():
            self.backend.owner_requirements = orig_req
            self.backend.invoke = orig_inv
            self.backend.room_decision = orig_dec
        self.addCleanup(restore)
        return restore

    # ---- item 1: retries ----
    def test_n1_retry_by_different_editor(self):
        self.set_role('drew', 'coordinator')
        self.addCleanup(self.set_role, 'drew', 'contributor')
        rid = self.room('N1')
        self.assertEqual(201, self.item(rid, parent=self.job, key='n1-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='n1-accept-alex')
        mid = self.pending(rid)
        second = self.decide(rid, 'drew', key='n1-accept-drew')
        rows = self.with_pointer(rid)
        print('\nN1 alex', first.status, '| pending', mid, '| drew retry', second.status, self.err(second),
              '| rows', rows, '| events', len(self.events(rid)), '| pending after', self.pending(rid))

    def test_n2_retry_after_room_archived(self):
        rid = self.room('N2')
        self.assertEqual(201, self.item(rid, parent=self.job, key='n2-item-key').status)
        self.assertEqual(201, self.message(rid, 'casey', 'abusive message', 'n2-msg-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='n2-accept-1')
        arch = self.request('POST', '/v1/projects/pp/rooms/%s/archive' % rid, {}, token=self.tokens['alex'], key='n2-archive')
        retry = self.decide(rid, 'alex', key='n2-accept-2')
        mod_item = self.moderate(rid, 1, key='n2-mod-item')
        mod_msg = self.moderate(rid, 2, key='n2-mod-msg')
        rej = self.decide(rid, 'alex', operation='reject', body={'reason': 'no'}, key='n2-reject')
        item = rooms.read_item(self.project, rid, 1)
        print('\nN2 accept', first.status, '| archive', arch.status, '| retry', retry.status, self.code(retry),
              '| moderate item', mod_item.status, self.code(mod_item), '| moderate message (control)', mod_msg.status,
              '| reject', rej.status, self.code(rej))
        print('N2 item', item['state'], item['moderated'], item['title'], item['text'], '| pending', self.pending(rid),
              '| rows', self.with_pointer(rid), '| served', self.served(rid))

    def test_n3_retry_after_decider_lost_role(self):
        self.set_role('drew', 'coordinator')
        self.addCleanup(self.set_role, 'drew', 'contributor')
        rid = self.room('N3')
        self.assertEqual(201, self.item(rid, parent=self.job, key='n3-item-key').status)
        before = self.all_props()
        self.fail_appends()
        first = self.decide(rid, 'drew', key='n3-accept-1')
        rows1 = self.with_pointer(rid)
        self.set_role('drew', 'contributor')
        retry = self.decide(rid, 'drew', key='n3-accept-2')
        props = [p for p in self.all_props() if p not in before]
        print('\nN3 drew(coord) accept', first.status, '| rows', rows1, '| drew demoted retry', retry.status,
              self.code(retry), retry.data.get('record'), '| rows after', self.with_pointer(rid),
              '| new proposals', props, '| events', len(self.events(rid)), '| pending', self.pending(rid),
              '| item', rooms.read_item(self.project, rid, 1)['state'])
        self.set_role('drew', 'viewer')
        retry2 = self.decide(rid, 'drew', key='n3-accept-3')
        print('N3 drew viewer retry', retry2.status, self.code(retry2))

    def test_n4_editor_fails_then_contributor_accepts(self):
        rid = self.room('N4')
        self.assertEqual(201, self.item(rid, parent=self.job, key='n4-item-key').status)
        before = self.all_props()
        self.fail_appends()
        first = self.decide(rid, 'alex', key='n4-accept-alex')
        rows1 = self.with_pointer(rid)
        second = self.decide(rid, 'casey', key='n4-accept-casey')
        props = [p for p in self.all_props() if p not in before]
        print('\nN4 alex', first.status, '| rows', rows1, '| casey', second.status, self.code(second),
              second.data.get('record'), '| rows after', self.with_pointer(rid), '| new proposals', props,
              '| events', len(self.events(rid)))

    def test_n5_contributor_fails_then_editor_accepts(self):
        rid = self.room('N5')
        self.assertEqual(201, self.item(rid, parent=self.job, key='n5-item-key').status)
        before = self.all_props()
        self.fail_appends()
        first = self.decide(rid, 'casey', key='n5-accept-casey')
        props1 = [p for p in self.all_props() if p not in before]
        second = self.decide(rid, 'alex', key='n5-accept-alex')
        props = [p for p in self.all_props() if p not in before]
        print('\nN5 casey', first.status, '| props', props1, '| alex', second.status, self.code(second),
              second.data.get('record'), '| rows after', self.with_pointer(rid), '| new proposals', props,
              '| events', len(self.events(rid)))

    # ---- item 2: stuck ----
    def test_n6_native_write_fails_then_moderation(self):
        rid = self.room('N6')
        self.assertEqual(201, self.item(rid, kind='decision', title='Slur title', text='abusive text',
                                        key='n6-item-key').status)
        restore = self.fail_native()
        first = self.decide(rid, 'alex', key='n6-accept-1')
        restore()
        out = [('accept', first.status, self.code(first)), ('pending', self.pending(rid))]
        for label, a in (
                ('moderate', self.moderate(rid, 1, key='n6-mod-1')),
                ('reject', self.decide(rid, 'alex', operation='reject', body={'reason': 'no'}, key='n6-reject')),
                ('amend', self.decide(rid, 'alex', operation='amend', body={'title': 'Clean', 'text': 'clean'}, key='n6-amend')),
                ('supersede', self.item(rid, who='alex', kind='decision', title='Clean', text='clean', key='n6-supersede', supersedes=1))):
            out.append((label, a.status, self.code(a), self.err(a)[:60]))
        out.append(('served to casey', self.served(rid, 'casey')))
        out.append(('rows', self.with_pointer(rid)))
        later = self.moderate(rid, 1, key='n6-mod-2')
        out.append(('moderate again later', later.status, self.code(later)))
        for line in out:
            print('N6', line)

    def test_n7_contributor_intent_blocks_owner_moderation(self):
        rid = self.room('N7')
        self.assertEqual(201, self.item(rid, title='Abusive', text='abusive text', key='n7-item-key').status)
        restore = self.fail_native()
        first = self.decide(rid, 'casey', key='n7-accept-casey')
        restore()
        mod = self.moderate(rid, 1, key='n7-moderate')
        rej = self.decide(rid, 'alex', operation='reject', body={'reason': 'abuse'}, key='n7-reject')
        print('\nN7 casey accept', first.status, '| pending', self.pending(rid), '| owner moderate', mod.status,
              self.code(mod), '| owner reject', rej.status, self.code(rej), '| served to drew', self.served(rid, 'drew'))

    def test_n8_squat_intent_key(self):
        rid = self.room('N8')
        self.assertEqual(201, self.item(rid, parent=self.job, key='n8-item-key').status)
        identity = rooms.item_decision_operation(rid, 1, 'accept',
                                                 content=rooms.item_content_digest('Add export', 'We need CSV export.'))
        squat = self.message(rid, 'drew', 'hello', rooms.item_intent_operation(identity))
        acc = self.decide(rid, 'alex', key='n8-accept')
        print('\nN8 drew posts message keyed', rooms.item_intent_operation(identity), squat.status,
              '| alex accept', acc.status, self.code(acc), self.err(acc), '| pending', self.pending(rid),
              '| rows', self.with_pointer(rid))
        am = self.decide(rid, 'alex', operation='amend', body={'title': 'Add export!', 'text': 'We need CSV export.'}, key='n8-amend-1')
        acc2 = self.decide(rid, 'alex', key='n8-accept-2')
        print('N8 amend', am.status, '| accept after amend', acc2.status, self.code(acc2))

    def test_n8b_squat_identity_after_failed_append(self):
        rid = self.room('N8b')
        self.assertEqual(201, self.item(rid, parent=self.job, key='n8b-item-key').status)
        identity = rooms.item_decision_operation(rid, 1, 'accept',
                                                 content=rooms.item_content_digest('Add export', 'We need CSV export.'))
        self.fail_appends()
        first = self.decide(rid, 'alex', key='n8b-accept-1')
        squat = self.message(rid, 'drew', 'hello', identity)
        out = [('accept', first.status), ('drew message keyed with identity', squat.status, identity), ('pending', self.pending(rid))]
        for label, a in (
                ('retry accept', self.decide(rid, 'alex', key='n8b-accept-2')),
                ('moderate', self.moderate(rid, 1, key='n8b-moderate')),
                ('reject', self.decide(rid, 'alex', operation='reject', body={'reason': 'no'}, key='n8b-reject')),
                ('amend', self.decide(rid, 'alex', operation='amend', body={'title': 'B', 'text': 'B text'}, key='n8b-amend')),
                ('supersede', self.item(rid, who='alex', title='B', text='B text', key='n8b-supersede', supersedes=1, parent=self.job))):
            out.append((label, a.status, self.code(a), self.err(a)[:70]))
        out.append(('item', rooms.read_item(self.project, rid, 1)['state']))
        out.append(('rows', self.with_pointer(rid)))
        out.append(('lines', self.lines(rid)))
        out.append(('served to casey', self.served(rid, 'casey')))
        for line in out:
            print('N8b', line)

    def test_n9_editor_parent_choice_then_retry(self):
        rid = self.room('N9')
        job2 = json.loads(self.bd('create', 'Second job', '--type', 'epic', '--json').stdout)['id']
        self.assertEqual(201, self.item(rid, parent=self.job, key='n9-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='n9-accept-1', body={'parent': job2})
        retry_default = self.decide(rid, 'alex', key='n9-accept-2')
        mod = self.moderate(rid, 1, key='n9-moderate')
        closed = self.bd('close', job2, '--reason', 'done', '--json')
        retry_job2 = self.decide(rid, 'alex', key='n9-accept-3', body={'parent': job2})
        print('\nN9 accept(job2)', first.status, '| retry default parent', retry_default.status, self.err(retry_default),
              '| moderate', mod.status, self.code(mod), '| close job2 rc', closed.returncode, closed.stderr[:100],
              '| retry job2 after close', retry_job2.status, self.err(retry_job2), '| pending', self.pending(rid),
              '| rows', self.with_pointer(rid))


if __name__ == '__main__':
    unittest.main()
