"""Reviewer stub repros for kittrial-5bb.388 r5 (not part of the delivery)."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rooms
import test_rooms_items_http as base


class Rev388r5(base.RoomsItemsHttpTests):

    def test_w1_jsonl_intent_parent_cleared_and_replays(self):
        self.backend.jobs.append({'id': 'job_0002', 'title': 'Second job'})
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'w1-accept-key', body={'parent': 'job_0002'})
        intent = self.kinds('item-intent')[0]
        print('\nW1 intent parent recorded', intent.get('parent'), intent.get('record_kind'))
        m1 = self.moderate(self.tokens['owner'], key='w1-moderate-key')
        m2 = self.moderate(self.tokens['owner'], key='w1-moderate-key')
        m3 = self.moderate(self.tokens['coord'], key='w1-moderate-key2')
        cancels = self.kinds('item-intent-cancel')
        exported = rooms.export_jsonl(Path(self.rooms_root) / self.pid, self.room['id'])
        text = exported if isinstance(exported, str) else json.dumps(exported)
        print('W1 moderate', m1.status, '| same key again', m2.status, (m2.data or {}).get('replayed'),
              '| other key', m3.status, '| cancel lines', len(cancels),
              [(c['records'], c['record_may_exist']) for c in cancels],
              '| job_0002 in JSONL', 'job_0002' in text, '| title in JSONL', 'Add export' in text)

    def test_w2_agent_and_outsider_cancel(self):
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['owner'], 'w2-accept-key')
        agent_id, secret, _ = self.agent_secret(self.tokens['owner'], projects=[self.pid])
        self.service.state['coordinator_grants'] = {
            self.pid: {agent_id: {'granted_by': self.ids['owner'], 'granted_at': '2026-10-23T00:00:00Z'}}}
        self.store.save()
        a = self.cancel(secret, key='w2-cancel-agent')
        o = self.cancel(self.tokens['outsider'], key='w2-cancel-outsider')
        c1 = self.cancel(self.tokens['coord'], key='w2-cancel-coord')
        c2 = self.cancel(self.tokens['coord'], key='w2-cancel-coord')
        print('\nW2 agent with coordinator grant', a.status, '| outsider', o.status, '| coord', c1.status,
              '| coord same key', c2.status, (c2.data or {}).get('replayed'),
              '| cancel lines', len(self.kinds('item-intent-cancel')))

    def test_w3_author_demoted_own_proposal_cancel(self):
        self.post_item(self.tokens['worker'])
        self.fail_after_native(self.tokens['worker'], 'w3-accept-key')
        put = self.request('PUT', '/v1/projects/%s/members/%s' % (self.pid, self.ids['worker']),
                           {'role': 'viewer'}, token=self.admin)
        c = self.cancel(self.tokens['worker'], key='w3-cancel-key')
        print('\nW3 demoted-to-viewer author cancels own proposal intent', put.status, '->', c.status,
              (c.data or {}).get('error'))
