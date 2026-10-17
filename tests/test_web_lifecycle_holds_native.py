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
                self.skipTest('no pinned Node for the native hold page proof')
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

    def test_a_voided_release_hold_still_refuses_the_web_lifecycle_route(self):
        """kittrial-5bb.272 review item 1 (r3): the merge-forward threading.

        The scenario of the lifecycle test, through the WEB route: a release-hold the
        surviving chain no longer links (a repair revision recorded at the hold) is
        voided by an operator, and the canonical readers read the hold back in force.
        The four callers require_unheld_fact gained on main - the endpoint's identity
        check and web_lifecycle's read, affordances and apply - pass the same operators
        and journal, so the web lifecycle route refuses reviewed/integrated for the task
        instead of ignoring the void."""
        import recovery
        from requirements import canonical_bytes
        harness, token = self.web()
        self.deployment(operators=['ops'], review_workflow_writes=True,
                        web_lifecycle_mark_writes=True)
        task = self.task('Voided release web', assignee='other-author')
        contribution = json.loads(self.contribute(task, 'other-author')['stdout'])['comment_id']
        approval = json.loads(self.approve(task, 'ops', contribution)['stdout'])['comment_id']
        held = json.loads(self.post_review(task, 'ops', dict(
            schema_version=1, operation='hold', operation_id='webv-hold', task=task,
            previous=approval, contribution=contribution, reason='Held for the voided-release check'))['stdout'])['comment_id']
        released = json.loads(self.post_review(task, 'ops', dict(
            schema_version=1, operation='release-hold', operation_id='webv-release', task=task,
            previous=held, contribution=contribution, hold=held, reason='Released before the fork'))['stdout'])['comment_id']
        # The repair revision recorded at the hold forks the chain: the release-hold is
        # no longer linked into the history the surviving records form. The endpoint's
        # own write path refuses a stale previous - a raw native comment is how such a
        # history actually comes to be, and what an operator reconciles with voids.
        fork = dict(
            schema_version=1, operation='contribute', operation_id='webv-fork', task=task,
            previous=held, repository='ssh://git/example', commit='9' * 40, base_commit=BASE,
            delivery={'kind': 'bundle', 'path': 'server:/fork.bundle', 'sha256': 'e' * 64},
            summary='Repair revision recorded at the hold', supersedes=contribution)
        self.assertEqual(self.r.bd('comments', 'add', task,
                                   'Kind: contribution-review-v1\n' + canonical_bytes(fork).decode(),
                                   actor='other-author').returncode, 0)
        release_text = next(comment['text'] for row in self.rows() if row['id'] == task
                            for comment in row.get('comments') or []
                            if comment.get('id') == released)
        void = dict(schema_version=1, operation='void-record', operation_id='webv-void', task=task,
                    target=released, target_kind='contribution-review',
                    target_sha256=recovery.digest(release_text), original=release_text,
                    reason='Reconciling the forked repair', disposition='void', operator='ops')
        self.assertEqual(self.r.bd('comments', 'add', task,
                                   recovery.PREFIX + canonical_bytes(void).decode(),
                                   actor='ops').returncode, 0)
        # The web read shows the hold refusals again, and the fact write is refused.
        # The scope is set first: the read's affordances answer the scope question
        # before the hold one.
        scope = dict(source_commit=COMMIT, integration_commit='e' * 40,
                     release_id='voided-release-web', environment='vr-web')
        self.assertEqual(self.lifecycle('other-author', 'lifecycle-scope', task, scope=scope)['returncode'], 0)
        path = '/v1/projects/pp/tasks/' + task + '/lifecycle'
        state = harness.request('GET', path, token=token)
        self.assertEqual(state.status, 200, state.data)
        for dimension in ('reviewed', 'integrated'):
            self.assertIn('listed operator', state.data['hold_refusals'][dimension],
                          state.data['hold_refusals'])
            self.assertIn('listed operator', state.data['affordances'][dimension]['reason'],
                          state.data['affordances'][dimension])
        refused = harness.request('POST', path + '/facts/integrated',
                                  dict(scope=scope, value='passed',
                                       evidence=['after the void'], provenance='performed'),
                                  token=token, key='webv-fact')
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('listed operator', refused.data['error']['message'])

    def test_an_unreadable_hold_chain_fails_closed_through_the_web(self):
        """kittrial-5bb.297, the real-stack reproduction: a chain that carries a hold AND
        cannot be parsed (a damaged write or a hostile edit leaves the kit's own prefix
        with unparsable bytes) must stop what the hold exists to stop, on every web
        surface: the lifecycle read reports it in hold_refusals and the affordances,
        the fact write refuses 409, and the merge slot refuses the acquisition."""
        import review_workflow as rw
        harness, token = self.web()
        self.deployment(operators=['ops'], review_workflow_writes=True,
                        web_lifecycle_mark_writes=True)
        task = self.task('Unreadable hold web', assignee='other-author')
        contribution = json.loads(self.contribute(task, 'other-author')['stdout'])['comment_id']
        approval = json.loads(self.approve(task, 'ops', contribution)['stdout'])['comment_id']
        hold_id = json.loads(self.post_review(task, 'ops', dict(
            schema_version=1, operation='hold', operation_id='unread-hold', task=task,
            previous=approval, contribution=contribution, reason='Held before the damage'))['stdout'])['comment_id']
        # The damage: the kit's own prefix with unparsable bytes, planted raw.
        self.assertEqual(self.r.bd('comments', 'add', task,
                                   rw.PREFIX + '{"broken": not json',
                                   actor='other-author').returncode, 0)
        # The scope is set first (the read's affordances answer the scope question
        # before the hold one), then the web lifecycle read reports the unreadable
        # chain instead of reading the task as unheld.
        scope = dict(source_commit=COMMIT, integration_commit='d' * 40,
                     release_id='unreadable-hold-web', environment='uh-web')
        self.assertEqual(self.lifecycle('other-author', 'lifecycle-scope', task, scope=scope)['returncode'], 0)
        path = '/v1/projects/pp/tasks/' + task + '/lifecycle'
        state = harness.request('GET', path, token=token)
        self.assertEqual(state.status, 200, state.data)
        for dimension in ('reviewed', 'integrated'):
            self.assertIn('cannot be read', state.data['hold_refusals'][dimension],
                          state.data['hold_refusals'])
            self.assertIn('cannot be read', state.data['affordances'][dimension]['reason'],
                          state.data['affordances'][dimension])
        # The fact write fails closed, and the merge slot refuses the acquisition.
        refused = harness.request('POST', path + '/facts/integrated',
                                  dict(scope=scope, value='passed',
                                       evidence=['after the damage'], provenance='performed'),
                                  token=token, key='unread-fact')
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('cannot be read', refused.data['error']['message'])
        acquired = harness.request('POST', '/v1/projects/pp/merge-slot/acquire',
                                   {'task_id': task, 'target': 'main'},
                                   token=token, key='unread-slot')
        self.assertEqual(409, acquired.status, acquired.data)
        self.assertIn('cannot be read', acquired.data['error']['message'])
        slot = harness.request('GET', '/v1/projects/pp/merge-slot', token=token)
        self.assertEqual(200, slot.status, slot.data)
        self.assertTrue(slot.data.get('available'), slot.data)
        # kittrial-5bb.297 review item 'operators-on-every-surface': ONE operator void
        # of the broken comment repairs the chain, and every surface sees it - the
        # configured operators are passed always now, so the web acquire and the
        # facts agree with the coordinate route instead of staying refused.
        import recovery
        from requirements import canonical_bytes
        broken = next(comment for row_ in self.rows() if row_['id'] == task
                      for comment in row_.get('comments') or []
                      if (comment.get('text') or '').startswith(rw.PREFIX)
                      and '"broken"' in comment['text'])
        void = dict(schema_version=1, operation='void-record', operation_id='void-broken-web',
                    task=task, target=broken['id'], target_kind='contribution-review',
                    target_sha256=recovery.digest(broken['text']), original=broken['text'],
                    reason='Repairing the damaged record', disposition='void', operator='ops')
        self.assertEqual(self.r.bd('comments', 'add', task,
                                   recovery.PREFIX + canonical_bytes(void).decode(),
                                   actor='ops').returncode, 0)
        # The chain now READS again - and it reads the hold that was hidden. The
        # void also stales the approval recorded before it (a repaired history needs
        # a fresh review), so the full repair is: re-approve, then release the hold.
        # After that every surface allows.
        reapproved = json.loads(self.post_review(task, 'ops', dict(
            schema_version=1, operation='approve', operation_id='unread-reapprove',
            task=task, previous=hold_id, contribution=contribution,
            summary='Approved again on the repaired chain'))['stdout'])['comment_id']
        cleared = self.post_review(task, 'ops', dict(
            schema_version=1, operation='release-hold', operation_id='unread-release',
            task=task, previous=reapproved, contribution=contribution, hold=hold_id,
            reason='Cleared after the repair'))
        self.assertEqual(cleared['returncode'], 0, cleared)
        repaired = harness.request('GET', path, token=token)
        self.assertEqual(200, repaired.status, repaired.data)
        self.assertNotIn('cannot be read', json.dumps(repaired.data['hold_refusals']))
        # The integrated fact needs the merge slot held first; the acquisition that
        # was refused while the chain was unreadable now succeeds.
        try:
            acquired = harness.request('POST', '/v1/projects/pp/merge-slot/acquire',
                                       {'task_id': task, 'target': 'main'},
                                       token=token, key='unread-slot-2')
            self.assertEqual(200, acquired.status, acquired.data)
            self.assertTrue(acquired.data['result']['acquired'], acquired.data)
            accepted = harness.request('POST', path + '/facts/integrated',
                                       dict(scope=scope, value='passed',
                                            evidence=['after the repair'], provenance='performed'),
                                       token=token, key='unread-fact-2')
            self.assertEqual(201, accepted.status, accepted.data)
        finally:
            # The slot is the project's one; leave it free for the sibling tests.
            harness.request('POST', '/v1/projects/pp/merge-slot/release', {},
                            token=token, key='unread-slot-release')

    def test_the_endpoint_coordinate_route_passes_the_operators_always(self):
        """kittrial-5bb.297 review item 'operators-everywhere-really' (a): the coordinate
        merge-acquire through the ENDPOINT refuses while the chain is unreadable even
        with every coordinator rule off, and one operator void releases it - the route
        passes the configured operators always, not only when a rule is on."""
        import review_workflow as rw
        import recovery
        from requirements import canonical_bytes
        harness, token = self.web()
        # Every rule key popped: coordinator_writes, independent_approval and
        # lifecycle_authors are all OFF; only the operator allowlist is configured.
        self.deployment(operators=['ops'], review_workflow_writes=True)
        task = self.task('Unreadable coordinate web', assignee='other-author')
        contribution = json.loads(self.contribute(task, 'other-author')['stdout'])['comment_id']
        approval = json.loads(self.approve(task, 'ops', contribution)['stdout'])['comment_id']
        self.post_review(task, 'ops', dict(
            schema_version=1, operation='hold', operation_id='coord-hold', task=task,
            previous=approval, contribution=contribution, reason='Held before the damage'))
        broken = dict(id=None, text=rw.PREFIX + '{"broken": not json')
        made = self.r.bd('comments', 'add', task, broken['text'], actor='other-author')
        self.assertEqual(made.returncode, 0, made.stderr)

        def coordinate():
            return self.endpoint('coordinate', 'ops', [json.dumps(
                {'operation': 'merge-acquire', 'task': task, 'target': 'main'})])

        refused = coordinate()
        self.assertEqual(refused['returncode'], 2, refused)
        self.assertIn('cannot be read', refused['stderr'])
        # One operator void of the broken comment: the coordinate route, reading with
        # the configured operators, now reads the chain (and the hold it carried, so
        # the refusal names the hold - released, the acquisition succeeds).
        row = next(row_ for row_ in self.rows() if row_['id'] == task)
        target = next(comment for comment in row['comments'] if comment['text'] == broken['text'])
        void = dict(schema_version=1, operation='void-record', operation_id='void-coord-web',
                    task=task, target=target['id'], target_kind='contribution-review',
                    target_sha256=recovery.digest(target['text']), original=target['text'],
                    reason='Repairing the damaged record', disposition='void', operator='ops')
        self.assertEqual(self.r.bd('comments', 'add', task,
                                   recovery.PREFIX + canonical_bytes(void).decode(),
                                   actor='ops').returncode, 0)
        held = coordinate()
        self.assertEqual(held['returncode'], 2, held)
        self.assertIn('open integration hold', held['stderr'])
        hold_id = next(comment['id'] for comment in self.rows() and
                       next(r_ for r_ in self.rows() if r_['id'] == task)['comments']
                       if comment['text'].startswith(rw.PREFIX)
                       and '"operation":"hold"' in comment['text'])
        self.post_review(task, 'ops', dict(
            schema_version=1, operation='release-hold', operation_id='coord-release',
            task=task, previous=hold_id, contribution=contribution, hold=hold_id,
            reason='Cleared after the repair'))
        acquired = coordinate()
        self.assertEqual(acquired['returncode'], 0, acquired)
        # The slot is the project's one; an owner force-releases it (the holder is
        # the native actor 'ops') so the sibling tests find it free.
        freed = harness.request('POST', '/v1/projects/pp/merge-slot/force-release',
                                {'expected_holder': 'ops',
                                 'reason': 'test cleanup: native coordinate acquisition'},
                                token=token, key='coord-force-release')
        self.assertEqual(200, freed.status, freed.data)
