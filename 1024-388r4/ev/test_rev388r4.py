"""Reviewer stub repros for kittrial-5bb.388 r4 (not part of the delivery)."""
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rooms
import test_rev388r3new as r3   # patches the port range

H = r3.Rev388r3New


class Rev388r4(r3.base.RoomsItemsHttpTests):
    lines = H.lines
    counts = H.counts
    amend = H.amend
    item_events = H.item_events
    fail_first_append = H.fail_first_append
    req_rows = H.req_rows

    def test_s_a_b_a_b(self):
        self.post_item(self.tokens['worker'], title='T0', text='X0', parent='job_0001')
        out = []
        for i, (t, x) in enumerate((('TA', 'XA'), ('TB', 'XB'), ('TA', 'XA'), ('TB', 'XB'))):
            a = self.amend(self.tokens['owner'], t, x, 'sab-am%d-r4key' % i)
            cur = self.items()[0]
            out.append((t, a.status, a.data.get('replayed'), cur['title']))
        same = self.amend(self.tokens['owner'], 'TB', 'XB', 'sab-same-r4key')
        print('\nSABAB', out, '| unchanged fresh-key', same.status, same.data.get('replayed'),
              '| amend lines', len(self.item_events('amend')))
        acc = self.accept(self.tokens['owner'], key='sab-acc-r4key')
        print('SABAB accept', acc.status, self.req_rows())
        self.assertEqual(['TA', 'TB', 'TA', 'TB'], [o[3] for o in out])
        self.assertEqual(4, len(self.item_events('amend')))

    def test_s_concurrent_same_and_different(self):
        self.post_item(self.tokens['worker'], title='T0', text='X0', parent='job_0001')
        answers = {}
        barrier = threading.Barrier(3)

        def run(who, title, text, key):
            barrier.wait(5)
            answers[key] = self.amend(self.tokens[who], title, text, key)
        threads = [threading.Thread(target=run, args=a) for a in
                   (('owner', 'TO', 'XO', 'cc-owner-r4key'), ('coord', 'TO', 'XO', 'cc-coord-r4key'),
                    ('worker', 'TW', 'XW', 'cc-worker-r4key'))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(90)
        events = self.item_events('amend')
        print('\nSCC', {k: (v.status, v.data.get('replayed')) for k, v in answers.items()},
              '| lines', [(e['title'], e['author']['name']) for e in events], '| item', self.items()[0]['title'])

    def test_s_amend_pending_then_reconciled(self):
        self.post_item(self.tokens['worker'], title='TA', text='XA', parent='job_0001')
        self.fail_first_append()
        first = self.accept(self.tokens['owner'], key='sp-acc1-r4key')
        am1 = self.amend(self.tokens['owner'], 'TB', 'XB', 'sp-am1-r4key')
        acc2 = self.accept(self.tokens['owner'], key='sp-acc2-r4key')
        am2 = self.amend(self.tokens['owner'], 'TB', 'XB', 'sp-am2-r4key')
        print('\nSP accept', first.status, '| amend while pending', am1.status, (am1.data.get('error') or {}).get('code'),
              '| retry accept', acc2.status, '| amend after reconcile', am2.status, (am2.data.get('error') or {}).get('code'),
              '| rows', self.req_rows())
        self.counts('SP')

    def test_n2_jsonl_parent_cleared(self):
        self.post_item(self.tokens['worker'], title='T0', text='X0', parent='job_0001')
        self.assertEqual(200, self.amend(self.tokens['owner'], 'TSECRET', 'XSECRET', 'n2j-am-r4key',
                                         parent='job_0002').status)
        mod = self.request('POST', self.path('/messages/1/moderate'), {'reason': 'abuse'},
                           token=self.tokens['owner'], key='n2j-mod-r4key')
        exp = self.request('GET', self.path('/export') + '?format=jsonl', token=self.tokens['owner'])
        body = exp.data if isinstance(exp.data, str) else json.dumps(exp.data)
        raw = getattr(exp, 'body', None) or getattr(exp, 'raw', None) or body
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        print('\nN2J moderate', mod.status, '| export', exp.status, '| job_0002 in export', 'job_0002' in text,
              '| SECRET in export', 'SECRET' in text, '| T0/X0 in export', 'X0' in text)
        md = self.request('GET', self.path('/export'), token=self.tokens['owner'])
        mtext = str(getattr(md, 'body', None) or md.data)
        print('N2J md export', md.status, '| job_0002', 'job_0002' in mtext, '| SECRET', 'SECRET' in mtext)


if __name__ == '__main__':
    unittest.main()
