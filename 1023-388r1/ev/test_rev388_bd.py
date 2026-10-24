"""Reviewer real-bd repros for kittrial-5bb.388 r1 (not part of the delivery)."""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_http_review_fixes as fixes
import test_rooms_items_real_bd as base
import http_authority

_orig = fixes.create_server


def _ranged(service, backend, host='127.0.0.1', port=0, **kw):
    for candidate in range(29600, 29700):
        try:
            return _orig(service, backend, host='127.0.0.1', port=candidate, **kw)
        except OSError:
            continue
    raise RuntimeError('no free port in 29600-29699')


fixes.create_server = _ranged

for _name in [n for n in dir(base.RoomsItemsRealBdTests) if n.startswith('test_')]:
    pass


class Rev388Bd(base.RoomsItemsRealBdTests):
    def with_pointer(self, rid, sequence=1):
        pointer = self.pointer(rid, sequence)
        return [(row.get('id'), row.get('issue_type') or row.get('type'), row.get('title'))
                for row in self.export_rows() if pointer in (row.get('description') or '')]

    def principal(self, who):
        return self.service.authenticate(self.tokens[who])

    def fields(self, rid, item, op):
        return {'operation': 'issue', 'room': rid, 'sequence': item['sequence'],
                'title': item['title'], 'text': item['text'], 'evidence': item['evidence'],
                'operation_id': op}

    def read_item(self, rid, seq=1):
        import rooms
        return rooms.read_item(self.project, rid, seq)

    def test_r1_accept_after_accept_second_requirement(self):
        rid = self.room('R1')
        self.assertEqual(201, self.item(rid, parent=self.job, key='r1-item-key').status)
        first = self.decide(rid, 'alex', key='r1-accept-alex')
        self.assertEqual(200, first.status, first.data)
        # drew becomes coordinator; on a stale page he presses Accept too.
        admin = self.admin_token()
        self.request('PUT', '/v1/projects/pp/members/%s' % self.ids['drew'], {'role': 'coordinator'}, token=admin)
        second = self.decide(rid, 'drew', key='r1-accept-drew')
        rows = self.with_pointer(rid)
        print('\nR1 second accept', second.status, second.data.get('error', {}).get('code'))
        print('R1 rows carrying the pointer:', rows)
        self.assertEqual(2, len(rows), 'one requirement + one decision only')

    def test_r2_contributor_accept_of_moderated_item(self):
        rid = self.room('R2')
        self.assertEqual(201, self.item(rid, text='SECRET R2 text', key='r2-item-key').status)
        mod = self.request('POST', '/v1/projects/pp/rooms/%s/messages/1/moderate' % rid,
                           {'reason': 'private'}, token=self.tokens['alex'], key='r2-moderate')
        self.assertEqual(201, mod.status, mod.data)
        answer = self.decide(rid, 'casey', key='r2-accept-casey')
        print('\nR2 contributor accept of moderated item', answer.status, answer.data)
        listing = self.request('GET', '/v1/projects/pp/proposals', token=self.tokens['alex'])
        dumped = json.dumps(listing.data)
        print('R2 proposals list status', listing.status, 'tombstone in proposals:', 'removed by' in dumped)
        self.assertNotIn('removed by', dumped)

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

    def test_r4_direct_endpoint_attacks(self):
        rid = self.room('R4')
        self.assertEqual(201, self.item(rid, kind='decision', title='Use CSV', text='CSV first.',
                                        key='r4-item-key').status)
        self.assertEqual(201, self.item(rid, kind='decision', title='Second', text='Second text.',
                                        key='r4-item-key2').status)
        item = self.read_item(rid, 1)
        item2 = self.read_item(rid, 2)
        op = 'room-%s-1-0-accept' % rid
        results = {}

        def attempt(name, principal, fields, mutate=None):
            try:
                if mutate is None:
                    answer = self.backend.room_decision(principal, 'pp', fields, fields['operation_id'])
                else:
                    authority = http_authority.authority_request(principal, 'pp', 'coordinate',
                                                                 now=self.service._expiry_now())
                    mutate(authority)
                    answer = self.backend._endpoint('room-decision', 'pp', principal.user_id,
                                                    [json.dumps(dict(fields, schema_version=1), sort_keys=True)],
                                                    operation_id=fields['operation_id'], authority=authority,
                                                    require_authority=True, route='rooms.item.accept')
                results[name] = ('ANSWER', str(answer)[:300])
            except Exception as error:  # noqa
                results[name] = ('REFUSED', type(error).__name__, str(error)[:200])

        attempt('contributor-session', self.principal('casey'), self.fields(rid, item, op))
        attempt('viewer-session', self.principal('vic'), self.fields(rid, item, op))
        forged = dict(self.fields(rid, item, op), text='FORGED decision text.')
        attempt('owner-forged-text', self.principal('alex'), forged)
        attempt('owner-wrong-item-evidence', self.principal('alex'),
                dict(self.fields(rid, item, op), evidence=item2['evidence']))
        attempt('owner-descriptor-other-project', self.principal('alex'), self.fields(rid, item, op),
                mutate=lambda a: a.update(project='qq'))
        attempt('owner-descriptor-credential', self.principal('alex'), self.fields(rid, item, op),
                mutate=lambda a: a.update(via='credential', credential_id='cred_x'))
        attempt('owner-descriptor-capability-read', self.principal('alex'), self.fields(rid, item, op),
                mutate=lambda a: a.update(capability='read'))
        # agent credential holding the coordinator grant
        created = self.request('POST', '/v1/agents', {
            'name': 'Kestrel', 'working_directory': '/home/priya/work/kestrel', 'projects': ['pp']},
            token=self.tokens['alex'])
        self.assertEqual(201, created.status, created.data)
        agent_id = created.data.get('agent', {}).get('id') or created.data.get('id')
        secret = created.data['credential']['secret']
        grant = self.request('PUT', '/v1/projects/pp/agents/%s/coordinator' % agent_id, {},
                             token=self.tokens['alex'], key='r4-grant-key')
        print('\nR4 grant status', grant.status, str(grant.data)[:200])
        agent_principal = self.service.authenticate(secret)
        attempt('agent-credential', agent_principal, self.fields(rid, item, op))
        # live demotion: drew is coordinator at page load, contributor at decision time
        admin = self.admin_token()
        self.request('PUT', '/v1/projects/pp/members/%s' % self.ids['drew'], {'role': 'coordinator'}, token=admin)
        drew = self.principal('drew')
        self.request('PUT', '/v1/projects/pp/members/%s' % self.ids['drew'], {'role': 'contributor'}, token=admin)
        attempt('demoted-coordinator', drew, self.fields(rid, item, op))
        # now the real owner decides item 1, then reuses item 1's op id for item 2
        attempt('owner-ok', self.principal('alex'), self.fields(rid, item, op))
        attempt('owner-replay-same', self.principal('alex'), self.fields(rid, item, op))
        attempt('owner-opid-reuse-other-item', self.principal('alex'), self.fields(rid, item2, op))
        for name, value in results.items():
            print('R4', name, value)
        rows1 = self.with_pointer(rid, 1)
        rows2 = self.with_pointer(rid, 2)
        print('R4 rows item1', rows1, 'item2', rows2)

    def test_r5_contributor_chosen_parent_is_a_decision_row(self):
        made = self.bd('create', 'A decision row', '--type', 'decision', '--json')
        self.assertEqual(0, made.returncode, made.stderr)
        decision_row = json.loads(made.stdout)['id']
        rid = self.room('R5')
        posted = self.item(rid, parent=decision_row, key='r5-item-key')
        print('R5 post with decision-row parent', posted.status)
        accepted = self.decide(rid, 'alex', key='r5-accept-alex')
        print('R5 owner accept', accepted.status, str(accepted.data)[:200])
        if accepted.status == 200:
            row = self.row(accepted.data['record']['id'])
            print('R5 requirement parent/deps', row.get('parent'), [d for d in (row.get('dependencies') or [])][:3])
        self.assertNotEqual(200, accepted.status)

    def test_r6_superuser_non_member_direct(self):
        rid = self.room('R6')
        self.assertEqual(201, self.item(rid, kind='decision', title='Use CSV', text='CSV first.',
                                        key='r6-item-key').status)
        item = self.read_item(rid, 1)
        admin_token = self.admin_token()
        sue = self.create_account(admin_token, 'sue', 'sue-password-1')
        self.service.state['users'][sue]['superuser'] = True
        self.service.store.save()
        admin = self.service.authenticate(self.login('sue', 'sue-password-1')[0])
        print('R6 admin superuser', admin.superuser, 'member role', self.service.state['memberships'].get('pp', {}).get(admin.user_id))
        try:
            answer = self.backend.room_decision(admin, 'pp', self.fields(rid, item, 'room-%s-1-0-accept' % rid))
            print('R6 ANSWER', answer)
        except Exception as error:  # noqa
            answer = None
            print('R6 REFUSED', type(error).__name__, str(error)[:200])
        self.assertIsNone(answer)


if __name__ == '__main__':
    unittest.main()
