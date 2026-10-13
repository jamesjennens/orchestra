"""Accepted native hold policy through real HTTP, receipt replay and the page."""
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT/'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers, COMMIT, BASE
import test_web_merge_slot as slots
from test_http_web import run_node_module
import coordination
import lifecycle


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeHoldWebTests(RulesHelpers, unittest.TestCase):
    web = slots.NativeWebSlotTests.web

    def test_native_holds_and_failed_attestations_survive_switches_and_receipt_replay(self):
        harness, token = self.web()
        for kind in ('hold', 'attest'):
            self.deployment(operators=['ops'], review_workflow_writes=True,
                            web_lifecycle_mark_writes=True)
            task = self.task('Synthetic held web '+kind, assignee='other-author')
            contribution = json.loads(self.contribute(task, 'other-author')['stdout'])['comment_id']
            approval = json.loads(self.approve(task, 'ops', contribution)['stdout'])['comment_id']
            scope = dict(source_commit=COMMIT, integration_commit='e'*40,
                         release_id='synthetic-held-'+kind, environment='held-native')
            self.assertEqual(self.lifecycle('other-author', 'lifecycle-scope', task, scope=scope)['returncode'], 0)
            path = '/v1/projects/pp/tasks/'+task+'/lifecycle'
            slot_path = '/v1/projects/pp/merge-slot/'
            slot_body = dict(task_id=task, target='main')
            slot_key = 'held-slot-'+kind
            acquired = harness.request('POST', slot_path+'acquire', slot_body, token=token, key=slot_key)
            self.assertEqual(acquired.status, 200, acquired.data)
            fact_body = dict(scope=scope, value='passed', evidence=['Synthetic cached fact'], provenance='performed')
            fact_key = 'held-fact-'+kind
            first = harness.request('POST', path+'/facts/integrated', fact_body, token=token, key=fact_key)
            self.assertEqual(first.status, 201, first.data)
            held = dict(schema_version=1, operation=kind, operation_id='held-native-'+kind,
                        task=task, previous=approval, contribution=contribution,
                        reason='Synthetic definite hold')
            if kind == 'attest':
                held.update(verdict='failed', commit=COMMIT, base_commit=BASE,
                            method='Synthetic mismatching observation', sha256='0'*64, bytes=10)
            result = self.post_review(task, 'ops', held)
            self.assertEqual(result['returncode'], 0, result)
            hold_id = json.loads(result['stdout'])['comment_id']
            messages = {}
            issue = next(row for row in self.rows() if row['id'] == task)
            for dimension in ('reviewed', 'integrated'):
                with self.assertRaises(ValueError) as refused:
                    lifecycle.require_unheld_fact(issue, dimension, task)
                messages[dimension] = str(refused.exception)
            with self.assertRaises(ValueError) as refused:
                coordination.require_unheld_merge_task(issue, task, ['ops'], self.project)
            messages['merge-acquire'] = str(refused.exception)
            self.assertIn(hold_id, messages['integrated'])
            self.assertIn('listed operator', messages['integrated'])

            def refusal(url, body, key, expected):
                before = self.rows()
                context = (self.project/'.merge-context.json').read_bytes()
                reply = harness.request('POST', url, body, token=token, key=key)
                self.assertEqual(reply.status, 409, reply.data)
                self.assertEqual(reply.data['error']['message'], expected)
                self.assertEqual(self.rows(), before)
                self.assertEqual((self.project/'.merge-context.json').read_bytes(), context)
                return reply

            for enabled in (False, True):
                self.deployment(operators=['ops'], review_workflow_writes=enabled,
                                web_lifecycle_mark_writes=True)
                state = harness.request('GET', path, token=token)
                self.assertEqual(state.status, 200, state.data)
                for dimension in ('reviewed', 'integrated'):
                    self.assertFalse(state.data['affordances'][dimension]['allowed'])
                    self.assertEqual(state.data['affordances'][dimension]['reason'], messages[dimension])
                    for value in lifecycle.VALUES:
                        body = dict(fact_body, value=value)
                        if value == 'pending': body['trigger'] = 'Synthetic waiting'
                        refusal(path+'/facts/'+dimension, body,
                                'held-'+kind+'-'+str(enabled)+'-'+dimension+'-'+value, messages[dimension])
                # Both the HTTP receipt and the endpoint receipt must consult the stored hold.
                refusal(path+'/facts/integrated', fact_body, fact_key, messages['integrated'])
                refusal(slot_path+'acquire', slot_body, slot_key, messages['merge-acquire'])
                reserve = harness.service.idempotency_reserve
                def reconcile(principal, project, route, key, body_hash, **options):
                    answer = reserve(principal, project, route, key, body_hash, **options)
                    if answer[0] == 'replay':
                        return ('reconcile', harness.service._idempotency_key(principal, project, route, key), None)
                    return answer
                with patch.object(harness.service, 'idempotency_reserve', side_effect=reconcile):
                    refusal(path+'/facts/integrated', fact_body, fact_key, messages['integrated'])
                    refusal(slot_path+'acquire', slot_body, slot_key, messages['merge-acquire'])
                    with patch.object(harness.service, 'has_result', return_value=False):
                        refusal(path+'/facts/integrated', fact_body, fact_key, messages['integrated'])
                        refusal(slot_path+'acquire', slot_body, slot_key, messages['merge-acquire'])
                ordinary = harness.request('POST', path+'/facts/tested', fact_body, token=token,
                                           key='held-ordinary-'+kind+'-'+str(enabled))
                self.assertEqual(ordinary.status, 201, ordinary.data)
                selection = lifecycle.release_selection(self.rows(), scope, lambda a, b: True, subset=[task])
                self.assertEqual(selection['targets'], [])
                self.assertTrue(any(item['task'] == task and 'held from integration' in item['reason']
                                    for item in selection['skipped']))
            node = shutil.which('node')
            if node:
                viewed = run_node_module(self, node, 'await import(process.argv[1])',
                    (KIT/'tests/web_lifecycle_hold_screen.mjs').as_uri(),
                    (KIT/'tests/web_dom_shim.mjs').as_uri(), (KIT/'web/js/api.js').as_uri(),
                    (KIT/'web/js/views/lifecycle.js').as_uri(),
                    'http://127.0.0.1:'+str(harness.port), 'pp', token, task, json.dumps(messages))
                self.assertEqual(viewed.returncode, 0, viewed.stderr[-4000:])
            else:
                self.fail('Pinned Node is required for the native hold page proof')
            self.deployment(operators=['ops'], review_workflow_writes=True,
                            web_lifecycle_mark_writes=True)
            head = json.loads(self.endpoint('review', 'ops', [task])['stdout'])['latest_comment_id']
            clear = dict(schema_version=1, operation='release-hold' if kind == 'hold' else 'attest',
                         operation_id='clear-native-'+kind, task=task, previous=head,
                         contribution=contribution, reason='Synthetic gate clear')
            if kind == 'hold': clear['hold'] = hold_id
            else:
                clear.update(verdict='unknown', commit=COMMIT, base_commit=BASE,
                             method='Synthetic unreachable delivery; not a verification pass')
            released = self.post_review(task, 'ops', clear)
            self.assertEqual(released['returncode'], 0, released)
            for enabled in (False, True):
                self.deployment(operators=['ops'], review_workflow_writes=enabled,
                                web_lifecycle_mark_writes=True)
                before = self.rows()
                replay = harness.request('POST', path+'/facts/integrated', fact_body, token=token, key=fact_key)
                self.assertEqual(replay.status, 201, replay.data)
                self.assertEqual(replay.data['event_id'], first.data['event_id'])
                self.assertEqual(self.rows(), before)
                passed = harness.request('POST', path+'/facts/reviewed', fact_body, token=token,
                                         key='cleared-'+kind+'-'+str(enabled))
                self.assertEqual(passed.status, 201, passed.data)
            freed = harness.request('POST', slot_path+'release', {}, token=token, key='held-free-'+kind)
            self.assertEqual(freed.status, 200, freed.data)
            if os.environ.get('ORCHESTRA_LIFECYCLE_TEST_EXPORTS'):
                folder = Path(os.environ['ORCHESTRA_LIFECYCLE_TEST_EXPORTS']);folder.mkdir(parents=True, exist_ok=True)
                (folder/(kind+'-after.jsonl')).write_text(''.join(json.dumps(row)+'\n' for row in self.rows()))
                (folder/(kind+'-messages.json')).write_text(json.dumps(messages, indent=2))
