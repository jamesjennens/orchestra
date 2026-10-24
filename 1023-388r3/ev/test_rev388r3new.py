"""Reviewer repros for kittrial-5bb.388 r3 (not part of the delivery). Stub backend."""
import json
import os
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
    for candidate in range(29800, 29900):
        try:
            return _orig_create(service, backend, host='127.0.0.1', port=candidate, **kw)
        except OSError:
            continue
    raise RuntimeError('no free port in 29800-29899')


agents.create_server = _ranged_create


class Rev388r3New(base.RoomsItemsHttpTests):
    def lines(self, rid=None):
        return rooms.load_lines(Path(self.rooms_root) / self.pid, rid or self.room['id'])

    def counts(self, tag):
        c = (len(self.backend.requirements), len(self.backend.proposals), len(self.backend.decisions))
        print('%s requirements/proposals/decisions = %s' % (tag, c))
        return c

    def amend(self, token, title, text, key, seq=1, parent=None):
        body = {'title': title, 'text': text}
        if parent is not None:
            body['parent'] = parent
        return self.request('POST', self.path('/items/%d/amend' % seq), body, token=token, key=key)

    def item_events(self, event=None):
        return [l for l in self.lines() if l['kind'] == 'item-event' and (event is None or l['event'] == event)]

    def fail_first_append(self):
        original = rooms.accept_item
        state = {'n': 0}

        def failing(*a, **kw):
            state['n'] += 1
            if state['n'] in state.get('fail', {1}):
                raise OSError('disk full (reviewer injected)')
            return original(*a, **kw)
        rooms.accept_item = failing
        self.addCleanup(setattr, rooms, 'accept_item', original)
        return state

    def req_rows(self, seq=1):
        pointer = 'orchestra:room/%s#%d' % (self.room['id'], seq)
        return [(r['id'], r['title'], r['description'].split('\n')[0], r.get('parent'), r.get('decision_id'))
                for r in self.backend.requirements.values() if pointer in r['description']]

    # ---- B-1: amend identity ----
    def test_d1b_amend_a_b_a(self):
        self.post_item(self.tokens['worker'], title='T0', text='X0', parent='job_0001')
        a1 = self.amend(self.tokens['worker'], 'TA', 'XA', 'am1-r3key')
        a2 = self.amend(self.tokens['owner'], 'TB', 'XB', 'am2-r3key')
        a3 = self.amend(self.tokens['owner'], 'TA', 'XA', 'am3-r3key')
        cur3 = self.items()[0]
        ev3 = len(self.item_events('amend'))
        print('\nD1b A->B->A statuses', a1.status, a2.status, a3.status,
              '| third answer title/text', a3.data.get('title'), a3.data.get('text'),
              '| item now', cur3['state'], cur3['title'], cur3['text'], '| amend lines', ev3)
        a4 = self.amend(self.tokens['owner'], 'T0', 'X0', 'am4-r3key')
        cur4 = self.items()[0]
        print('D1b amend back to the POSTED text', a4.status, '| item now', cur4['title'], cur4['text'],
              '| amend lines', len(self.item_events('amend')))
        acc = self.accept(self.tokens['owner'], key='acc-r3key')
        print('D1b accept', acc.status, '| record rows', self.req_rows())
        self.assertEqual(('TA', 'XA'), (cur3['title'], cur3['text']), 'third amend (back to A) did not apply')

    def test_d1c_concurrent_amends(self):
        self.post_item(self.tokens['worker'], title='T0', text='X0', parent='job_0001')
        answers = {}
        barrier = threading.Barrier(2)

        def run(who, title, text):
            barrier.wait(5)
            answers[who] = self.amend(self.tokens[who], title, text, 'cam-%s-r3key' % who)
        threads = [threading.Thread(target=run, args=a) for a in
                   (('owner', 'TO', 'XO'), ('coord', 'TC', 'XC'))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(90)
        cur = self.items()[0]
        events = self.item_events('amend')
        print('\nD1c concurrent amends', {k: v.status for k, v in answers.items()},
              '| lines', [(e['title'], e['text']) for e in events], '| item now', cur['title'], cur['text'])
        acc = self.accept(self.tokens['owner'], key='cacc-r3key')
        print('D1c accept', acc.status, '| record rows', self.req_rows())
        self.assertEqual(2, len(events))
        self.assertEqual(1, len(self.req_rows()))
        self.assertEqual(cur['title'], self.req_rows()[0][1])

    def test_d1d_amend_twice_then_accept_records_latest(self):
        self.post_item(self.tokens['worker'], title='T0', text='X0', parent='job_0001')
        self.assertEqual(200, self.amend(self.tokens['worker'], 'TB', 'XB', 'd-am1-r3key').status)
        self.assertEqual(200, self.amend(self.tokens['owner'], 'TC', 'XC', 'd-am2-r3key').status)
        acc = self.accept(self.tokens['owner'], key='d-acc-r3key')
        rows = self.req_rows()
        print('\nD1d accept', acc.status, '| rows', rows)
        self.assertEqual(1, len(rows))
        self.assertEqual(('TC', 'XC'), rows[0][1:3])

    # ---- B-2: archive vs accept (archive now waits on the item lock) ----
    def test_d2_room_archived_during_record_write(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        original = self.backend.owner_requirements
        box = {}

        def archive():
            t0 = time.monotonic()
            box['a'] = self.request('POST', self.path('/archive'), {}, token=self.tokens['coord'],
                                    key='d2-arch-r3key')
            box['dt'] = time.monotonic() - t0

        def slow(principal, project_id, action, fields, key, task=None):
            if action == 'create' and 't' not in box:
                box['t'] = threading.Thread(target=archive)
                box['t'].start()
                time.sleep(1.5)  # the archive is now waiting (or not) on the item lock
                box['archived_mid'] = rooms.read_room(Path(self.rooms_root) / self.pid,
                                                      self.room['id'])['state']
            return original(principal, project_id, action, fields, key, task)
        self.backend.owner_requirements = slow
        owner = self.accept(self.tokens['owner'], key='d2-owner-r3key')
        box['t'].join(90)
        events = self.item_events()
        print('\nD2 room state while the record write ran', box['archived_mid'],
              '| archive', box['a'].status, '(took %.2fs)' % box['dt'],
              '| owner accept', owner.status, owner.data.get('error', {}).get('code'),
              '| item events', len(events), '| item state', self.items()[0]['state'])
        c = self.counts('D2')
        self.assertEqual('open', box['archived_mid'])
        self.assertEqual(200, owner.status)
        self.assertEqual((1, 0, 1), c)
        self.assertEqual(1, len(events))

    def test_d2b_moderation_during_record_write(self):
        self.post_item(self.tokens['worker'], parent='job_0001', text='SECRET-D2B')
        original = self.backend.owner_requirements
        box = {}

        def moderate():
            box['m'] = self.request('POST', self.path('/messages/1/moderate'), {'reason': 'private'},
                                    token=self.tokens['coord'], key='d2b-mod-r3key')

        def slow(principal, project_id, action, fields, key, task=None):
            if action == 'create' and 't' not in box:
                box['t'] = threading.Thread(target=moderate)
                box['t'].start()
                time.sleep(1.5)
            return original(principal, project_id, action, fields, key, task)
        self.backend.owner_requirements = slow
        owner = self.accept(self.tokens['owner'], key='d2b-owner-r3key')
        box['t'].join(90)
        print('\nD2b moderation during write: moderate', box['m'].status, '| accept', owner.status,
              '| item', self.items()[0]['state'], self.items()[0]['moderated'])
        self.counts('D2b')

    def test_d2c_supersede_during_record_write(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        original = self.backend.owner_requirements
        box = {}

        def supersede():
            box['s'] = self.post_item(self.tokens['worker'], key='d2c-sup-r3key', supersedes=1,
                                      parent='job_0001')

        def slow(principal, project_id, action, fields, key, task=None):
            if action == 'create' and 't' not in box:
                box['t'] = threading.Thread(target=supersede)
                box['t'].start()
                time.sleep(1.5)
            return original(principal, project_id, action, fields, key, task)
        self.backend.owner_requirements = slow
        owner = self.accept(self.tokens['owner'], key='d2c-owner-r3key')
        box['t'].join(90)
        items = self.items()
        print('\nD2c supersede during write: supersede', box['s'].status,
              (box['s'].data.get('error') or {}).get('code'), '| accept', owner.status,
              '| items', [(i['sequence'], i['state']) for i in items])
        self.counts('D2c')

    def test_d2d_project_archived_during_record_write(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        original = self.backend.owner_requirements
        box = {}

        def slow(principal, project_id, action, fields, key, task=None):
            if action == 'create' and 'done' not in box:
                box['done'] = True
                self.service.state['projects'][self.pid]['archived'] = True
                self.store.save()
            return original(principal, project_id, action, fields, key, task)
        self.backend.owner_requirements = slow
        owner = self.accept(self.tokens['owner'], key='d2d-owner-r3key')
        print('\nD2d project archived during write: accept', owner.status,
              (owner.data.get('error') or {}).get('code'), '| item events', len(self.item_events()),
              '| item', self.items()[0]['state'])
        self.counts('D2d')

    # ---- S-1: failed append, amend, accept ----
    def test_s1a_failed_append_amend_accept(self):
        self.post_item(self.tokens['worker'], title='TA', text='XA', parent='job_0001')
        self.fail_first_append()
        first = self.accept(self.tokens['owner'], key='s1a-acc1-r3key')
        rows1 = self.req_rows()
        am = self.amend(self.tokens['owner'], 'TB', 'XB', 's1a-am-r3key')
        second = self.accept(self.tokens['owner'], key='s1a-acc2-r3key')
        rows2 = self.req_rows()
        item = self.items()[0]
        print('\nS1a first accept', first.status, (first.data.get('error') or {}).get('message'),
              '| rows after first', rows1)
        print('S1a amend', am.status, '| second accept', second.status, (second.data.get('record') or {}).get('id'),
              '| rows after', rows2, '| item', item['state'], item['title'], (item.get('record') or {}).get('id'))
        self.counts('S1a')
        self.assertEqual(1, len(rows2), 'two requirement records carry this item pointer')

    def test_s1b_failed_append_retry_unchanged(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.fail_first_append()
        first = self.accept(self.tokens['owner'], key='s1b-acc1-r3key')
        second = self.accept(self.tokens['owner'], key='s1b-acc2-r3key')
        print('\nS1b first', first.status, '| retry (fresh key, no change)', second.status,
              (second.data.get('record') or {}).get('id'), '| rows', self.req_rows())
        self.counts('S1b')
        self.assertEqual(200, second.status)
        self.assertEqual(1, len(self.req_rows()))

    def test_s1c_a_fail_b_fail_a(self):
        self.post_item(self.tokens['worker'], title='TA', text='XA', parent='job_0001')
        state = self.fail_first_append()
        state['fail'] = {1, 2}
        r = [self.accept(self.tokens['owner'], key='s1c-acc1-r3key').status]
        r.append(self.amend(self.tokens['owner'], 'TB', 'XB', 's1c-am1-r3key').status)
        r.append(self.accept(self.tokens['owner'], key='s1c-acc2-r3key').status)
        r.append(self.amend(self.tokens['owner'], 'TA', 'XA', 's1c-am2-r3key').status)
        last = self.accept(self.tokens['owner'], key='s1c-acc3-r3key')
        r.append(last.status)
        item = self.items()[0]
        print('\nS1c statuses acc/amend/acc/amend/acc', r, '| rows', self.req_rows(),
              '| item', item['state'], item['title'], (item.get('record') or {}).get('id'))
        self.counts('S1c')

    def test_s1d_failed_append_then_reject(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        self.fail_first_append()
        first = self.accept(self.tokens['owner'], key='s1d-acc1-r3key')
        rej = self.decision(self.tokens['owner'], key='s1d-rej-r3key')
        print('\nS1d failed accept', first.status, '| reject', rej.status, '| rows', self.req_rows(),
              '| item', self.items()[0]['state'])
        self.counts('S1d')

    # ---- N-2: parent choice ----
    def test_n2_parent_choices(self):
        out = []
        self.post_item(self.tokens['worker'], parent='job_0001')                       # 1
        for label, body in (('missing', {'parent': 'job_9999'}), ('int', {'parent': 7}),
                            ('list', {'parent': ['job_0001']}), ('other-proj', {'parent': 'pp-abc'})):
            a = self.accept(self.tokens['owner'], key='n2-%s-r3key' % label, body=body)
            out.append(('owner accept parent ' + label, a.status, (a.data.get('error') or {}).get('message', '')[:60]))
        out.append(('rows after refused accepts', len(self.req_rows())))
        # contributor amends own item with a parent
        a = self.amend(self.tokens['worker'], 'Add export', 'We need CSV export.', 'n2-wam1-r3key', parent='job_9999')
        out.append(('contributor amend own item bad parent', a.status))
        a = self.amend(self.tokens['worker'], 'Add export', 'We need CSV export.', 'n2-wam2-r3key', parent='job_0001')
        out.append(('contributor amend own item good parent', a.status))
        # contributor amends owner's item with a parent
        s2 = self.post_item(self.tokens['owner'], key='n2-i2-r3key', title='Owner', text='Owner', parent='job_0001').data['sequence']
        a = self.amend(self.tokens['worker'], 'W', 'W', 'n2-wam3-r3key', seq=s2, parent='job_0001')
        out.append(('contributor amend owner item with parent', a.status))
        # contributor accept with a payload parent (ignored: a proposal)
        s3 = self.post_item(self.tokens['worker'], key='n2-i3-r3key', title='W3', text='W3').data['sequence']
        a = self.accept(self.tokens['worker'], sequence=s3, key='n2-wacc-r3key', body={'parent': 'job_9999'})
        out.append(('contributor accept with bad parent', a.status, a.data.get('record', {}).get('kind') if a.status == 200 else a.data.get('error')))
        # a parent on a decision / question amend
        s4 = self.post_item(self.tokens['owner'], kind='decision', key='n2-i4-r3key', title='D', text='D').data['sequence']
        a = self.amend(self.tokens['owner'], 'D2', 'D2', 'n2-dam-r3key', seq=s4, parent='job_0001')
        out.append(('amend decision with parent', a.status))
        # empty job list
        s5 = self.post_item(self.tokens['worker'], key='n2-i5-r3key', title='E', text='E', parent='job_0001').data['sequence']
        orig = self.backend.requirements_read

        def nojobs(project_id, args):
            answer = orig(project_id, args)
            if args[:1] != ['get']:
                answer = dict(answer, jobs=[])
            return answer
        self.backend.requirements_read = nojobs
        try:
            a = self.accept(self.tokens['owner'], sequence=s5, key='n2-empty-r3key')
            out.append(('owner accept, project has no jobs, item parent job_0001', a.status,
                        (a.data.get('error') or {}).get('message', '')[:60]))
            a = self.accept(self.tokens['owner'], sequence=s5, key='n2-empty2-r3key', body={'parent': 'job_0001'})
            out.append(('owner accept, no jobs, explicit parent', a.status))
            a = self.amend(self.tokens['owner'], 'E', 'E', 'n2-eam-r3key', seq=s5, parent='job_0001')
            out.append(('owner amend, no jobs, parent', a.status))
        finally:
            self.backend.requirements_read = orig
        out.append(('rows for item 5', len(self.req_rows(s5))))
        for line in out:
            print('N2', line)
        self.counts('N2')

    # ---- N-3: symlinked lock files and directory ----
    def test_n3_symlinked_locks(self):
        self.post_item(self.tokens['worker'], parent='job_0001')
        project = Path(self.rooms_root) / self.pid
        out = []
        for name in ('.item-decisions.lock', '.coordination.lock'):
            lock = project / name
            victim = Path(self.rooms_root) / ('victim' + name)
            if lock.exists() or lock.is_symlink():
                lock.unlink()
            os.symlink(str(victim), str(lock))
            a = self.accept(self.tokens['owner'], key='n3-%s-r3key' % name.strip('.'))
            m = self.request('POST', self.path('/messages'), {'text': 'hi'}, token=self.tokens['worker'],
                             key='n3-msg-%s-r3key' % name.strip('.'))
            rd = self.read(self.tokens['viewer'])
            out.append((name, 'accept', a.status, (a.data.get('error') or {}).get('code'), 'post', m.status,
                        'read', rd.status, 'victim created', victim.exists()))
            lock.unlink()
        # symlinked project directory
        real = Path(self.rooms_root) / 'realdir'
        os.rename(str(project), str(real))
        os.symlink(str(real), str(project))
        a = self.accept(self.tokens['owner'], key='n3-dir-r3key')
        out.append(('project dir symlink', 'accept', a.status, (a.data.get('error') or {}).get('code'),
                    'lock created in real dir', (real / '.item-decisions.lock').exists()))
        for line in out:
            print('N3', line)
        self.counts('N3')

    # ---- N-1 extension: an amend parent on a moderated item ----
    def test_n1b_moderated_amend_parent(self):
        self.post_item(self.tokens['worker'], title='SECRET-T', text='SECRET-X', parent='job_0001')
        self.assertEqual(200, self.amend(self.tokens['owner'], 'SECRET-T2', 'SECRET-X2', 'n1b-am-r3key',
                                         parent='job_0001').status)
        self.assertEqual(201, self.request('POST', self.path('/messages/1/moderate'), {'reason': 'p'},
                                           token=self.tokens['coord'], key='n1b-mod-r3key').status)
        surfaces = {
            'read': json.dumps(self.read(self.tokens['viewer']).data),
            'export_md': json.dumps(self.request('GET', self.path('/export') + '?format=markdown',
                                                 token=self.tokens['viewer']).data),
            'export_jsonl': json.dumps(self.request('GET', self.path('/export') + '?format=jsonl',
                                                    token=self.tokens['viewer']).data),
            'index.json': (Path(self.rooms_root) / self.pid / rooms.FOLDER / rooms.INDEX).read_text(),
        }
        print('\nN1b moderated item: secret words / amend parent per surface',
              {k: (sorted({w for w in ('SECRET-T', 'SECRET-X', 'SECRET-T2', 'SECRET-X2') if w in v}),
                   v.count('job_0001')) for k, v in surfaces.items()})


    def test_n2b_editor_choice_wins_on_accept(self):
        orig = self.backend.requirements_read

        def twojobs(project_id, args):
            answer = orig(project_id, args)
            if args[:1] != ['get']:
                answer = dict(answer, jobs=[{'id': 'job_0001'}, {'id': 'job_0002'}])
            return answer
        self.backend.requirements_read = twojobs
        self.post_item(self.tokens['worker'], parent='job_0001')
        acc = self.accept(self.tokens['owner'], key='n2b-acc-r3key', body={'parent': 'job_0002'})
        print('\nN2b editor picks job_0002 over item parent job_0001:', acc.status, self.req_rows())
        self.assertEqual('job_0002', self.req_rows()[0][3])


if __name__ == '__main__':
    unittest.main()
