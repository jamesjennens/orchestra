"""Reviewer real-bd repros for kittrial-5bb.388 r5 (not part of the delivery).

Prints facts; asserts only harness preconditions, so every case reports.
"""
import json
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_rev388r4_bd as r4
import rooms
import http_service
from http_authority import journal_path


class Rev388r5Bd(r4.Rev388r4Bd):
    http_timeout = 180

    # ---- helpers ----
    def jrows(self, where='1=1', args=()):
        with sqlite3.connect(str(journal_path(self.project))) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(
                'SELECT operation_id, state, route, principal FROM operations WHERE ' + where, args)]

    def jexec(self, sql, args=()):
        with sqlite3.connect(str(journal_path(self.project))) as c:
            return c.execute(sql, args).rowcount

    def req_rows(self, rid):
        p = self.pointer(rid)
        return [r['id'] for r in self.export_rows() if p in (r.get('description') or '')
                and 'requirement' in (r.get('labels') or [])]

    def dec_rows(self, rid):
        p = self.pointer(rid)
        return [r['id'] for r in self.export_rows() if p in (r.get('description') or '')
                and (r.get('issue_type') or r.get('type')) == 'decision']

    def cancel(self, rid, who, seq=1, key='cancel-key-01', token=None):
        return self.request('POST', '/v1/projects/pp/rooms/%s/items/%d/intent/cancel' % (rid, seq),
                            {'reason': 'reviewer'}, token=token or self.tokens[who], key=key)

    def kinds(self, rid):
        return [(l['sequence'], l['kind'], (l.get('author') or {}).get('name'),
                 l.get('identity', '')[-14:], l.get('records'), l.get('record_may_exist'))
                for l in rooms.load_lines(self.project, rid)
                if l['kind'] in rooms.INTENT_KINDS or l['kind'] == 'item-event']

    def identity(self, rid, seq=1):
        lines = rooms.load_lines(self.project, rid)
        return rooms.accept_identity(lines, rid, rooms.read_item(self.project, rid, seq))

    # ---- item 2: same author, journal row missing (pruned / beyond tombstone window) ----
    def test_v5_same_author_retry_after_missing_row_requirement(self):
        rid = self.room('V1')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v1-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='v1-accept-1')
        rows1 = self.req_rows(rid)
        deleted = self.jexec("DELETE FROM operations WHERE route LIKE 'requirements.%'")
        retry = self.decide(rid, 'alex', key='v1-accept-2')
        print('\nV1 alex', first.status, '| rows', rows1, '| deleted journal rows', deleted,
              '| alex retry', retry.status, self.code(retry), self.err(retry), '| rows after', self.req_rows(rid),
              '| events', len(self.events(rid)))

    def test_v5_same_author_retry_after_missing_row_decision(self):
        rid = self.room('V2')
        self.assertEqual(201, self.item(rid, kind='decision', title='Use CSV', text='CSV first.',
                                        key='v2-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='v2-accept-1')
        rows1 = self.dec_rows(rid)
        deleted = self.jexec("DELETE FROM operations WHERE route = 'rooms.item.accept'")
        retry = self.decide(rid, 'alex', key='v2-accept-2')
        print('\nV2 alex', first.status, '| decisions', rows1, '| deleted', deleted, '| alex retry',
              retry.status, self.code(retry), self.err(retry), '| decisions after', self.dec_rows(rid),
              '| events', len(self.events(rid)))

    def test_v5_same_author_retry_new_login_session_decision(self):
        rid = self.room('V2b')
        self.assertEqual(201, self.item(rid, kind='decision', title='Use CSV', text='CSV first.',
                                        key='v2b-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='v2b-accept-1')
        rows1 = self.dec_rows(rid)
        old = self.tokens['alex']
        self.tokens['alex'] = self.login('alex', 'alex-password-1')[0]
        self.addCleanup(self.tokens.__setitem__, 'alex', old)
        retry = self.decide(rid, 'alex', key='v2b-accept-2')
        out = ['V2b alex', first.status, '| decisions', rows1, '| alex NEW SESSION retry', retry.status,
               self.code(retry), self.err(retry), '| decisions after', self.dec_rows(rid),
               '| pending', self.pending(rid)]
        if retry.status != 200:
            c = self.cancel(rid, 'alex', key='v2b-cancel')
            again = self.decide(rid, 'alex', key='v2b-accept-3')
            out += ['| cancel', c.status, (c.data or {}).get('records'), (c.data or {}).get('record_may_exist'),
                    '| accept afresh', again.status, '| decisions now', self.dec_rows(rid)]
        print('\n' + ' '.join(str(x) for x in out))

    def test_v5_same_author_retry_new_login_session_requirement(self):
        rid = self.room('V1b')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v1b-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='v1b-accept-1')
        rows1 = self.req_rows(rid)
        old = self.tokens['alex']
        self.tokens['alex'] = self.login('alex', 'alex-password-1')[0]
        self.addCleanup(self.tokens.__setitem__, 'alex', old)
        retry = self.decide(rid, 'alex', key='v1b-accept-2')
        print('\nV1b alex', first.status, '| rows', rows1, '| alex NEW SESSION retry', retry.status,
              self.code(retry), self.err(retry), '| rows after', self.req_rows(rid))

    # ---- item 2: committed / uncertain / missing across people ----
    def test_v5_uncertain_row_other_editor(self):
        self.set_role('drew', 'coordinator')
        self.addCleanup(self.set_role, 'drew', 'contributor')
        rid = self.room('V3')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v3-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='v3-accept-1')
        n = self.jexec("UPDATE operations SET state='unknown' WHERE route='requirements.create'")
        retry = self.decide(rid, 'drew', key='v3-accept-drew')
        c = self.cancel(rid, 'alex', key='v3-cancel')
        print('\nV3 alex', first.status, '| rows', self.req_rows(rid), '| set unknown', n, '| drew retry',
              retry.status, self.err(retry), '| cancel', c.status, (c.data or {}).get('identity', '')[-20:],
              (c.data or {}).get('records'), (c.data or {}).get('record_may_exist'), '| lines', self.kinds(rid))

    def test_v5_missing_row_other_editor_then_cancel(self):
        self.set_role('drew', 'coordinator')
        self.addCleanup(self.set_role, 'drew', 'contributor')
        rid = self.room('V4')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v4-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='v4-accept-1')
        ident = self.identity(rid)
        n = self.jexec("DELETE FROM operations WHERE route LIKE 'requirements.%'")
        retry = self.decide(rid, 'drew', key='v4-accept-drew')
        c = self.cancel(rid, 'drew', key='v4-cancel')
        print('\nV4 alex', first.status, '| rows', self.req_rows(rid), '| deleted', n, '| drew retry', retry.status,
              self.err(retry), '| cancel', c.status, 'identity==intent', (c.data or {}).get('identity') == ident,
              (c.data or {}).get('records'), (c.data or {}).get('record_may_exist'), '| rows', self.req_rows(rid))

    def test_v5_committed_adopt_counts(self):
        self.set_role('drew', 'coordinator')
        self.addCleanup(self.set_role, 'drew', 'contributor')
        rid = self.room('V5')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v5-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='v5-accept-1')
        retry = self.decide(rid, 'drew', key='v5-accept-drew')
        print('\nV5 alex', first.status, '| drew retry', retry.status, (retry.data or {}).get('record'),
              '| req rows', self.req_rows(rid), '| decisions', self.dec_rows(rid), '| events', len(self.events(rid)),
              '| with_pointer', self.with_pointer(rid), '| lines', self.kinds(rid))

    def test_v5_created_not_accepted_then_third_editor(self):
        # A's create commits, A's accept never runs; B adopts the created requirement and its
        # own accept commits but B's append fails; C retries.
        self.set_role('drew', 'coordinator')
        self.addCleanup(self.set_role, 'drew', 'contributor')
        rid = self.room('V6')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v6-item-key').status)
        orig = self.backend.owner_requirements
        def fail_accept(principal, pid, action, *a, **kw):
            if action == 'accept':
                raise OSError('reviewer: accept not run')
            return orig(principal, pid, action, *a, **kw)
        self.backend.owner_requirements = fail_accept
        first = self.decide(rid, 'alex', key='v6-accept-1')
        self.backend.owner_requirements = orig
        self.fail_appends()
        second = self.decide(rid, 'drew', key='v6-accept-drew')
        third = self.decide(rid, 'alex', key='v6-accept-2')
        print('\nV6 alex(create only)', first.status, '| drew adopt, append fails', second.status,
              '| alex retry', third.status, self.err(third), (third.data or {}).get('record'),
              '| req rows', self.req_rows(rid), '| decisions', self.dec_rows(rid), '| events', len(self.events(rid)),
              '| lines', self.kinds(rid))

    def test_v5_concurrent_retries_two_callers(self):
        self.set_role('drew', 'coordinator')
        self.addCleanup(self.set_role, 'drew', 'contributor')
        rid = self.room('V7')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v7-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='v7-accept-1')
        out = {}
        def go(who):
            out[who] = self.decide(rid, who, key='v7-accept-' + who + '-2')
        threads = [threading.Thread(target=go, args=(w,)) for w in ('alex', 'drew')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        print('\nV7 alex', first.status, '| concurrent', {w: (a.status, self.code(a)) for w, a in out.items()},
              '| req rows', self.req_rows(rid), '| decisions', self.dec_rows(rid), '| events', len(self.events(rid)))

    def test_v5_concurrent_first_accepts_two_callers(self):
        self.set_role('drew', 'coordinator')
        self.addCleanup(self.set_role, 'drew', 'contributor')
        rid = self.room('V7b')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v7b-item-key').status)
        out = {}
        def go(who):
            out[who] = self.decide(rid, who, key='v7b-accept-' + who)
        threads = [threading.Thread(target=go, args=(w,)) for w in ('alex', 'drew')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        print('\nV7b concurrent first', {w: (a.status, self.code(a)) for w, a in out.items()},
              '| req rows', self.req_rows(rid), '| events', len(self.events(rid)))

    # ---- item 2: a planted journal row through the SSH endpoint path ----
    def plant(self, operation_id, title):
        import endpoint
        request = {'project': 'pp', 'actor': 'casey', 'action': 'bd',
                   'args': ['create', title, '--json'], 'operation_id': operation_id}
        try:
            return endpoint.execute(Path(self.root), request, key_projects=frozenset({'pp'}))
        except BaseException as error:
            return {'error': repr(error)[:200]}

    def test_v5_ssh_planted_journal_row_requirement(self):
        self.set_role('drew', 'coordinator')
        self.addCleanup(self.set_role, 'drew', 'contributor')
        rid = self.room('V8')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v8-item-key').status)
        ident = self.identity(rid)
        key = self.backend._result_key(SimpleNamespace(user_id=self.ids['alex'], credential_id=None),
                                       'pp', 'requirements.create', ident + '-requirement-create', None)
        planted = self.plant(key, 'Planted by casey over SSH')
        pid_ = None
        try:
            pid_ = json.loads(planted.get('stdout') or '{}').get('id')
        except ValueError:
            pass
        row = self.jrows('operation_id = ?', (key,))
        first = self.decide(rid, 'alex', key='v8-accept-1')
        retry = self.decide(rid, 'drew', key='v8-accept-drew')
        c = self.cancel(rid, 'alex', key='v8-cancel')
        print('\nV8 plant rc', planted.get('returncode'), planted.get('error'), (planted.get('stderr') or '')[:120],
              'planted id', pid_, '| journal row', row, '| alex accept', first.status, self.err(first),
              '| drew retry', retry.status, self.err(retry), (retry.data or {}).get('record'),
              '| cancel', c.status, (c.data or {}).get('records'), (c.data or {}).get('record_may_exist'),
              '| req rows', self.req_rows(rid))

    def test_v5_ssh_planted_journal_row_decision(self):
        self.set_role('drew', 'coordinator')
        self.addCleanup(self.set_role, 'drew', 'contributor')
        rid = self.room('V9')
        self.assertEqual(201, self.item(rid, kind='decision', title='Use CSV', text='CSV first.',
                                        key='v9-item-key').status)
        ident = self.identity(rid)
        key = http_service.ApiHandler._room_decision_operation(ident, self.ids['alex'])
        planted = self.plant(key, 'Planted decision by casey')
        first = self.decide(rid, 'alex', key='v9-accept-1')
        retry = self.decide(rid, 'drew', key='v9-accept-drew')
        print('\nV9 plant rc', planted.get('returncode'), planted.get('error'), (planted.get('stderr') or '')[:100],
              '| alex accept', first.status, self.err(first), '| drew retry', retry.status, self.err(retry),
              '| pending', self.pending(rid), '| decisions', self.dec_rows(rid))

    # ---- item 3: moderation / archive / cancel rights on real bd ----
    def test_v5_moderation_cancels_then_retry_refused(self):
        rid = self.room('V10')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v10-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='v10-accept-1')
        ident = self.identity(rid)
        mod = self.moderate(rid, 1, key='v10-moderate')
        retry = self.decide(rid, 'alex', key='v10-accept-2')
        cancels = [l for l in rooms.load_lines(self.project, rid) if l['kind'] == 'item-intent-cancel']
        ok = None
        try:
            rooms.accept_item(self.project, rid, 1, ident, {'id': self.ids['alex'], 'name': 'alex'},
                              dict(rooms.LIMIT_DEFAULTS), {'kind': 'requirement', 'id': 'x-1', 'revision': None,
                                                           'sha256': None, 'decision_id': None,
                                                           'evidence': self.pointer(rid)})
            ok = 'ACCEPTED'
        except rooms.Refusal as r:
            ok = (r.status, r.code) if hasattr(r, 'code') else repr(r)
        except Exception as e:
            ok = repr(e)[:80]
        print('\nV10 alex', first.status, '| moderate', mod.status, '| cancel lines',
              [(c['identity'] == ident, c['records'], c['record_may_exist'], c['reason']) for c in cancels],
              '| retry', retry.status, self.code(retry), '| module accept of cancelled identity', ok,
              '| served', self.served(rid, 'casey'), '| rows', self.req_rows(rid))

    def test_v5_archive_cancels(self):
        rid = self.room('V11')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v11-item-key').status)
        self.assertEqual(201, self.item(rid, kind='decision', title='D', text='Dtext', key='v11-item-2').status)
        self.fail_appends(which=(1, 2))
        a1 = self.decide(rid, 'alex', key='v11-accept-1')
        a2 = self.decide(rid, 'alex', sequence=2, key='v11-accept-2')
        arch = self.request('POST', '/v1/projects/pp/rooms/%s/archive' % rid, {}, token=self.tokens['alex'],
                            key='v11-archive')
        mod = self.moderate(rid, 1, key='v11-moderate')
        retry = self.decide(rid, 'alex', key='v11-accept-3')
        print('\nV11 accepts', a1.status, a2.status, '| archive', arch.status, '| pending after',
              self.pending(rid, 1), self.pending(rid, 2), '| cancels', [k for k in self.kinds(rid) if k[1] == 'item-intent-cancel'],
              '| moderate item in archived room', mod.status, self.code(mod), '| retry', retry.status, self.code(retry),
              '| served', self.served(rid, 'casey'))

    def test_v5_cancel_rights(self):
        self.set_role('drew', 'coordinator')
        self.addCleanup(self.set_role, 'drew', 'contributor')
        rid = self.room('V12')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v12-item-1').status)
        self.assertEqual(201, self.item(rid, title='P2', text='p2 text', key='v12-item-2').status)
        restore = self.fail_native()
        e1 = self.decide(rid, 'alex', key='v12-acc-1')               # requirement intent by alex
        e2 = self.decide(rid, 'casey', sequence=2, key='v12-acc-2')  # proposal intent by casey
        restore()
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'projects': ['pp']},
                            token=self.tokens['alex'])
        secret = (made.data or {}).get('credential', {}).get('secret') if made.status == 201 else None
        res = [('intents', e1.status, e2.status, self.pending(rid, 1), self.pending(rid, 2))]
        res.append(('agent cancel 1', self.cancel(rid, None, 1, 'v12-c-agent', token=secret).status if secret else ('no agent', made.status)))
        res.append(('vic viewer cancel 1', self.cancel(rid, 'vic', 1, 'v12-c-vic').status))
        res.append(('casey contributor cancel 1 (not author)', self.cancel(rid, 'casey', 1, 'v12-c-casey1').status))
        res.append(('casey author cancel own proposal 2', self.cancel(rid, 'casey', 2, 'v12-c-casey2').status))
        self.set_role('drew', 'contributor')
        e3 = self.decide(rid, 'casey', sequence=2, key='v12-acc-3-fail')
        res.append(('drew (contributor) accept 2 after cancel', e3.status))
        res.append(('drew contributor cancel 1', self.cancel(rid, 'drew', 1, 'v12-c-drew0').status))
        self.set_role('drew', 'coordinator')
        res.append(('drew coordinator cancel 1', self.cancel(rid, 'drew', 1, 'v12-c-drew').status))
        res.append(('owner cancel 1 again', self.cancel(rid, 'alex', 1, 'v12-c-alex').status))
        for r in res:
            print('V12', r)

    def test_v5_proposal_intent_editor_cannot_complete_and_no_attempt_line(self):
        rid = self.room('V13')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v13-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'casey', key='v13-accept-casey')
        before = len(rooms.load_lines(self.project, rid))
        ed = self.decide(rid, 'alex', key='v13-accept-alex')
        drew = self.decide(rid, 'drew', key='v13-accept-drew')
        after = len(rooms.load_lines(self.project, rid))
        own = self.decide(rid, 'casey', key='v13-accept-casey-2')
        print('\nV13 casey', first.status, '| alex(editor) retry', ed.status, self.err(ed), '| drew(other contributor)',
              drew.status, self.err(drew), '| lines added by refused retries', after - before,
              '| casey own retry', own.status, (own.data or {}).get('record'), '| props', self.with_pointer(rid),
              '| lines', self.kinds(rid))

    def test_v5_reserved_prefixes_and_request_id(self):
        rid = self.room('V14')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v14-item-key').status)
        out = []
        for key in ('room-x-12345678', 'intent-x-12345678'):
            out.append((key, self.message(rid, 'casey', 'hi', key).status))
        a = self.request('POST', '/v1/projects/pp/rooms/%s/messages' % rid, {'text': 'hi'},
                         token=self.tokens['casey'], headers={'X-Request-Id': 'intent-req-12345678'})
        b = self.request('POST', '/v1/projects/pp/rooms/%s/messages' % rid, {'text': 'hi'},
                         token=self.tokens['casey'], headers={'X-Request-Id': 'room-req-12345678'})
        out.append(('X-Request-Id intent- without key', a.status, self.err(a)))
        out.append(('X-Request-Id room- without key', b.status, self.err(b)))
        out.append(('lines', [(l['sequence'], l['kind'], l['operation_id']) for l in rooms.load_lines(self.project, rid)]))
        for o in out:
            print('V14', o)


    def test_v5_moderation_timing_requirement(self):
        import time
        rid = self.room('V15')
        self.assertEqual(201, self.item(rid, parent=self.job, key='v15-item-key').status)
        self.fail_appends()
        first = self.decide(rid, 'alex', key='v15-accept-1')
        rows = self.req_rows(rid)
        calls = []
        orig = self.backend.requirements_read
        def timed(*a, **kw):
            t = time.time()
            try:
                return orig(*a, **kw)
            except BaseException as e:
                calls.append(('raised', type(e).__name__, str(e)[:100], round(time.time() - t, 1)))
                raise
            finally:
                calls.append(('read', a[1] if len(a) > 1 else a, round(time.time() - t, 1)))
        self.backend.requirements_read = timed
        self.addCleanup(setattr, self.backend, 'requirements_read', orig)
        t0 = time.time()
        mod = self.moderate(rid, 1, key='v15-moderate')
        took = round(time.time() - t0, 1)
        cancels = [(l['records'], l['record_may_exist']) for l in rooms.load_lines(self.project, rid)
                   if l['kind'] == 'item-intent-cancel']
        print('V15 alex', first.status, '| rows', rows, '| moderate', mod.status, 'took', took, 's',
              '| requirements_read calls', calls, '| cancel line', cancels)


if __name__ == '__main__':
    unittest.main()
