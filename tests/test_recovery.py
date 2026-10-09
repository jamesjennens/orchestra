"""Audited operator recovery for malformed reserved structured history."""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin
import briefing
import recovery
import review_workflow as w
import work
from requirements import canonical_bytes

TASK = 'trial-task'
PROJECT = 'trial'
STAMP = '2026-09-20T00:00:00Z'
# Server-side operator allowlist used by the read model in these tests. It is
# deployment configuration, never the void payload's own `operator` string.
OPERATORS = 'operator ops other'


def native(comments):
    return dict(id=TASK, title='A task', issue_type='task', status='in_progress', assignee='worker',
                description='Intent', acceptance_criteria='Acceptance', labels=[], comments=comments)


def contribute(operation_id='c1', **extra):
    p = dict(schema_version=1, operation='contribute', operation_id=operation_id, task=TASK, previous=None,
             repository='ssh://git.example/project', commit='a' * 40, base_commit='b' * 40,
             delivery=dict(kind='bundle', path='koopa:/deliveries/rev.bundle', sha256='c' * 64),
             summary='Implementation and test evidence', supersedes=None)
    p.update(extra)
    return p


def approve(contribution, operation_id='a1', previous=None):
    return dict(schema_version=1, operation='approve', operation_id=operation_id, task=TASK, previous=previous,
                contribution=contribution, summary='Reviewed the revision and its test evidence')


def review_comment(cid, p, author='worker', stamp=STAMP):
    return dict(id=str(cid), text=w.PREFIX + canonical_bytes(p).decode(), author=author, created_at=stamp)


def void(target, original, operation_id='v1', operator='operator', **extra):
    p = dict(schema_version=1, operation='void-record', operation_id=operation_id, task=TASK, target=str(target),
             target_kind='contribution-review', target_sha256=recovery.digest(original), original=original,
             reason='Malformed record; reconcile before further review', disposition='void', operator=operator)
    p.update(extra)
    return p


def void_comment(cid, p, author='operator', stamp=STAMP):
    return dict(id=str(cid), text=recovery.PREFIX + canonical_bytes(p).decode(), author=author, created_at=stamp)


def broken_review(cid='c2', text='not-json'):
    return dict(id=cid, text=w.PREFIX + text, author='worker', created_at=STAMP)


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'ORCHESTRA_OPERATORS': OPERATORS})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_native(self, data, author='worker'):
        calls = []

        def run(args):
            if args[:2] == ['export', '--all']:
                return '\n'.join(json.dumps(row) for row in data)
            calls.append(args)
            cid = 'w' + str(len(calls))
            data[0]['comments'].append(dict(id=cid, text=args[3], author=author, created_at=STAMP))
            return json.dumps({'id': cid})

        return calls, run

    def test_malformed_record_blocks_reads_until_an_operator_voids_it(self):
        bad = broken_review()
        data = [native([review_comment('c1', contribute()), bad])]
        with self.assertRaisesRegex(ValueError, 'operator reconciliation'):
            w.project(data[0])
        data[0]['comments'].append(void_comment('v1', void('c2', bad['text'])))
        state = w.project(data[0])
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual(state['contribution']['comment_id'], 'c1')
        self.assertEqual(len(state['recoveries']), 1)
        record = state['recoveries'][0]
        self.assertEqual(record['target'], 'c2')
        self.assertEqual(record['target_sha256'], recovery.digest(bad['text']))
        self.assertEqual(record['author'], 'operator')
        self.assertEqual(record['timestamp'], STAMP)
        self.assertEqual(record['disposition'], 'void')
        self.assertIn('Malformed record', record['reason'])
        self.assertIn('void record', ' '.join(state['warnings']))
        # The disposable fixture stays inspectable: the original bytes are still present.
        self.assertIn(bad['text'], [c['text'] for c in data[0]['comments']])
        entries = briefing.snapshot(data, PROJECT, TASK)['entries']
        self.assertTrue(any(e['body'] == bad['text'] for e in entries))
        # `brief` renders the projection verbatim, so the incident reaches the worker read.
        rendered = briefing.format_brief(briefing.brief(data, PROJECT, TASK))
        self.assertIn('recoveries', rendered)
        self.assertIn('c2', rendered)

    def test_unauthorized_recovery_fails_before_any_write(self):
        bad = broken_review()
        data = [native([bad])]
        calls, run = self.run_native(data)
        with self.assertRaisesRegex(ValueError, 'admin.py void-record'):
            w.apply_void(data, TASK, 'worker', void('c2', bad['text']), run)
        payload = void('c2', bad['text'])
        with self.assertRaisesRegex(ValueError, 'contributor review transport'):
            w.execute(data, TASK, 'worker', payload, run)
        with self.assertRaisesRegex(ValueError, 'contributor review transport'):
            work.execute(Path('.'), 'worker', 'review', [TASK, '@attachment:0'],
                         {'0': {'flag': '--file', 'text': json.dumps(payload)}}, run)
        self.assertEqual(calls, [])
        self.assertEqual(len(data[0]['comments']), 1)

    def test_operator_void_is_idempotent_and_auditable(self):
        bad = broken_review()
        data = [native([bad])]
        calls, run = self.run_native(data, author='operator')
        payload = void('c2', bad['text'])
        first = w.apply_void(data, TASK, 'operator', payload, run, operator=True)
        second = w.apply_void(data, TASK, 'operator', payload, run, operator=True)
        self.assertEqual(first, dict(comment_id='w1', reconciled=False, target='c2'))
        self.assertEqual(second, dict(comment_id='w1', reconciled=True, target='c2'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(data[0]['comments']), 2)
        with self.assertRaisesRegex(ValueError, 'different payload or actor'):
            w.apply_void(data, TASK, 'other', dict(payload, operator='other'), run, operator=True)
        with self.assertRaisesRegex(ValueError, 'already targets'):
            w.apply_void(data, TASK, 'operator', dict(payload, operation_id='v2'), run, operator=True)
        self.assertEqual(len(calls), 1)
        state = w.project(data[0])
        self.assertEqual(state['recoveries'][0]['author'], 'operator')
        self.assertEqual(state['recoveries'][0]['reason'], payload['reason'])

    def test_void_cannot_suppress_the_current_contribution_or_approval(self):
        first = review_comment('c1', contribute())
        second = review_comment('c2', approve('c1', previous='c1'), author='reviewer')
        data = [native([first, second])]
        self.assertEqual(w.project(data[0])['review_state'], 'awaiting-integration')
        calls, run = self.run_native(data, author='operator')
        for target, text in (('c1', first['text']), ('c2', second['text'])):
            with self.assertRaisesRegex(ValueError, 'currently form'):
                w.apply_void(data, TASK, 'operator', void(target, text), run, operator=True)
        self.assertEqual(calls, [])
        # A void edited directly into the database is refused on read: it never
        # suppresses the record, reads stay healthy and the refusal is surfaced.
        data[0]['comments'].append(void_comment('v9', void('c2', second['text'], operation_id='v9')))
        state = w.project(data[0])
        self.assertEqual(state['review_state'], 'awaiting-integration')
        self.assertEqual(state['contribution']['comment_id'], 'c1')
        self.assertEqual(len(state['recoveries']), 1)
        refused = state['recoveries'][0]
        self.assertFalse(refused['applied'])
        self.assertEqual(refused['disposition'], 'refused')
        self.assertEqual(refused['target'], 'c2')
        self.assertIn('currently form', refused['refusal'])
        self.assertIn('refused', ' '.join(state['warnings']))
        # The whole task stays readable and writable through the work queue.
        self.assertEqual([(i['task'], i['review_state']) for i in work.queue(data, 'worker', [])['items']],
                         [(TASK, 'awaiting-integration')])
        data[0]['comments'].pop()
        self.assertEqual(w.project(data[0])['review_state'], 'awaiting-integration')

    def test_refused_void_targeting_the_surviving_contribution_is_inert(self):
        first = review_comment('c1', contribute())
        second = review_comment('c2', approve('c1', previous='c1'), author='reviewer')
        data = [native([first, second, void_comment('v1', void('c1', first['text']))])]
        state = w.project(data[0])
        self.assertEqual(state['review_state'], 'awaiting-integration')
        self.assertEqual([(r['target'], r['applied']) for r in state['recoveries']], [('c1', False)])
        self.assertIn('refused', ' '.join(state['warnings']))
        # A refused void is not an incident: the original stays inspectable and
        # the incident list keeps the refusal rather than dropping it silently.
        entries = briefing.snapshot(data, PROJECT, TASK)['entries']
        self.assertTrue(any(e['body'] == first['text'] for e in entries))

    def test_void_is_bound_to_its_native_operator_provenance(self):
        first = review_comment('c1', contribute(operation_id='c1'))
        second = review_comment('c2', approve('c1', operation_id='a1', previous='c1'), author='reviewer')
        # A foreign-authored record cannot remove a healthy approval, even with
        # a well-formed payload that names the operator.
        forged = void_comment('v1', void('c2', second['text']), author='mallory/session9')
        state = w.project(native([first, second, forged]))
        self.assertEqual(state['review_state'], 'awaiting-integration')
        self.assertEqual(state['recoveries'], [])
        self.assertIn('v1', ' '.join(state['warnings']))
        # Nor can it reconcile a malformed record: the chain still fails closed.
        bad = broken_review()
        data = native([first, bad, void_comment('v2', void('c2', bad['text']), author='mallory/session9')])
        with self.assertRaisesRegex(ValueError, 'operator reconciliation'):
            w.project(data)
        # A payload that claims a different operator than the issuing actor is
        # refused before any native write.
        rows = [native([broken_review()])]
        calls, run = self.run_native(rows, author='operator')
        with self.assertRaisesRegex(ValueError, 'must match the issuing actor'):
            w.apply_void(rows, TASK, 'operator', void('c2', broken_review()['text'], operator='someone-else'),
                         run, operator=True)
        self.assertEqual(calls, [])

    def test_void_after_an_approval_requires_a_fresh_approval(self):
        first = review_comment('c1', contribute())
        second = review_comment('c2', approve('c1', previous='c1'), author='reviewer')
        bad = dict(id='r1', text=w.PREFIX + json.dumps(
            dict(schema_version=1, operation='request-changes', operation_id='r1', task=TASK, previous='c2',
                 contribution='c1', items=[dict(id='i1', text='fix', extra='x')]), sort_keys=True),
            author='reviewer', created_at=STAMP)
        data = [native([first, second, bad, void_comment('v1', void('r1', bad['text']))])]
        state = w.project(data[0])
        self.assertNotEqual(state['review_state'], 'awaiting-integration')
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual(state['recoveries'][0]['target'], 'r1')
        self.assertTrue(state['recoveries'][0]['applied'])
        self.assertTrue(state['recoveries'][0]['invalidates_approval'])
        self.assertIn('fresh approval', ' '.join(state['warnings']))
        # A fresh approval recorded after the void restores eligibility.
        fresh = review_comment('c3', approve('c1', operation_id='a2', previous='c2'), author='reviewer')
        data[0]['comments'].append(fresh)
        self.assertEqual(w.project(data[0])['review_state'], 'awaiting-integration')

    def test_voiding_a_forked_request_changes_does_not_yield_an_approval(self):
        first = review_comment('c1', contribute())
        request = dict(schema_version=1, operation='request-changes', operation_id='rA', task=TASK, previous='c1',
                       contribution='c1', items=[dict(id='i1', text='fix')])
        forked = review_comment('rA', request, author='reviewer')
        approval = review_comment('aB', approve('c1', operation_id='aB', previous='c1'), author='reviewer2')
        data = [native([first, forked, approval, void_comment('w1', void('rA', forked['text']))])]
        state = w.project(data[0])
        self.assertNotEqual(state['review_state'], 'awaiting-integration')
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertIn('fresh approval', ' '.join(state['warnings']))

    def test_voiding_a_revision_with_a_downstream_approval_fails_closed(self):
        good = review_comment('c1', contribute())
        bad = broken_review()
        downstream = review_comment('c3', approve('c1', previous='c2'), author='reviewer')
        data = [native([good, bad, downstream])]
        data[0]['comments'].append(void_comment('v1', void('c2', bad['text'])))
        with self.assertRaisesRegex(ValueError, 'still reference voided record'):
            w.project(data[0])
        data[0]['comments'].append(void_comment('v2', void('c3', downstream['text'], operation_id='v2')))
        state = w.project(data[0])
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertNotEqual(state['review_state'], 'awaiting-integration')
        self.assertEqual([r['target'] for r in state['recoveries']], ['c2', 'c3'])
        self.assertEqual(state['contribution']['comment_id'], 'c1')

    def test_malformed_or_stale_void_records_never_take_effect(self):
        first = review_comment('c1', contribute())
        second = review_comment('c2', approve('c1', previous='c1'), author='reviewer')
        data = [native([first, second])]
        stale = void('c1', first['text'], operation_id='v1')
        stale['target_sha256'] = '0' * 64
        data[0]['comments'].append(void_comment('v9', stale))
        data[0]['comments'].append(dict(id='v8', text=recovery.PREFIX + '{bad', author='operator',
                                        created_at=STAMP))
        state = w.project(data[0])
        self.assertEqual(state['review_state'], 'awaiting-integration')
        self.assertEqual(state['recoveries'], [])
        self.assertIn('ignored', ' '.join(state['warnings']))

    def test_operator_void_refuses_targets_it_cannot_reconcile(self):
        prose = dict(id='p1', text='Ordinary prose finding', author='worker', created_at=STAMP)
        checkpoint = dict(id='k1', text='Kind: task-checkpoint-v1\n{"summary":"x"}', author='worker',
                          created_at=STAMP)
        data = [native([prose, checkpoint, review_comment('c1', contribute())])]
        for comment in (prose, checkpoint):
            with self.assertRaisesRegex(ValueError, 'not a contribution-review record'):
                w.apply_void(data, TASK, 'operator', void(comment['id'], comment['text']),
                             lambda _: self.fail('no write'), operator=True)
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            w.apply_void(data, TASK, 'operator',
                         void('p1', prose['text'], target_kind='checkpoint'), lambda _: self.fail('no write'),
                         operator=True)
        # A directly edited void record for a non-review target is ignored, not applied.
        data[0]['comments'].append(void_comment('v3', void('p1', prose['text'], operation_id='v3')))
        state = w.project(data[0])
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual(state['recoveries'], [])

    def test_self_authored_void_is_inert_without_server_side_operator_authority(self):
        # A contributor names their own actor as the payload operator; without a
        # configured allowlist entry that void has no authority at all.
        first = review_comment('c1', contribute())
        second = review_comment('c2', approve('c1', previous='c1'), author='reviewer')
        forged = void_comment('v1', void('c2', second['text'], operator='worker'), author='worker')
        state = w.project(native([first, second, forged]))
        self.assertEqual(state['review_state'], 'awaiting-integration')
        self.assertEqual(state['recoveries'], [])
        self.assertIn('v1', ' '.join(state['warnings']))
        # Nor can it reconcile a malformed record even though actor and payload
        # operator agree on the contributor's own name.
        bad = broken_review()
        data = native([first, bad, void_comment('v2', void('c2', bad['text'], operator='worker'), author='worker')])
        with self.assertRaisesRegex(ValueError, 'operator reconciliation'):
            w.project(data)

    def test_configured_operator_allowlist_bounds_void_authors(self):
        first = review_comment('c1', contribute())
        bad = broken_review()
        # 'operator' is not configured here; the same record authored by a
        # configured 'ops' is accepted.
        forged = void_comment('v1', void('c2', bad['text'], operator='operator'), author='operator')
        with self.assertRaisesRegex(ValueError, 'operator reconciliation'):
            w.project(native([first, bad, forged]), operators=('ops',))
        allowed = void_comment('v2', void('c2', bad['text'], operator='ops'), author='ops')
        state = w.project(native([first, bad, allowed]), operators=('ops',))
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual([r['target'] for r in state['recoveries']], ['c2'])
        # An unconfigured allowlist authorizes nobody.
        with self.assertRaisesRegex(ValueError, 'operator reconciliation'):
            w.project(native([first, bad, allowed]), operators=())

    def test_apply_void_requires_a_server_side_configured_operator(self):
        bad = broken_review()
        rows = [native([bad])]
        calls, run = self.run_native(rows, author='operator')
        with self.assertRaisesRegex(ValueError, 'No operator allowlist'):
            w.apply_void(rows, TASK, 'operator', void('c2', bad['text']), run, operator=True, operators=())
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            w.apply_void(rows, TASK, 'rogue', void('c2', bad['text'], operator='rogue'), run,
                         operator=True, operators=('operator',))
        self.assertEqual(calls, [])

    def test_void_must_follow_its_target_in_native_order(self):
        first = review_comment('c1', contribute())
        request = review_comment('x', dict(schema_version=1, operation='request-changes', operation_id='rX',
                                           task=TASK, previous='c1', contribution='c1',
                                           items=[dict(id='i1', text='fix')]), author='reviewer')
        approval = review_comment('aB', approve('c1', operation_id='aB', previous='c1'), author='reviewer2')
        # The void is recorded before the record it targets: it cannot reconcile
        # the fork, so the conflicting history stays fail-closed.
        data = native([first, void_comment('V', void('x', request['text'])), approval, request])
        with self.assertRaisesRegex(ValueError, 'Conflicting or unlinked'):
            w.project(data)
        self.assertIn('V', recovery.records(data, OPERATORS)[2])
        # The same void recorded after its target still applies.
        data = native([first, approval, request, void_comment('V', void('x', request['text']))])
        state = w.project(data)
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual([r['target'] for r in state['recoveries']], ['x'])

    def test_voiding_a_malformed_record_cannot_be_resurrected_by_exact_retry(self):
        good = review_comment('c1', contribute())
        bad = broken_review()
        approval = approve('c1', previous='c2')
        downstream = review_comment('c3', approval, author='reviewer')
        data = [native([good, bad, downstream])]
        data[0]['comments'].append(void_comment('v1', void('c2', bad['text'])))
        data[0]['comments'].append(void_comment('v2', void('c3', downstream['text'], operation_id='v2')))
        self.assertEqual(w.project(data[0])['review_state'], 'awaiting-review')
        calls, run = self.run_native(data, author='reviewer')
        with self.assertRaisesRegex(ValueError, 'voided contribution-review record'):
            w.execute(data, TASK, 'reviewer', approval, run)
        self.assertEqual(calls, [])
        self.assertEqual(len(data[0]['comments']), 5)

    def test_native_export_backup_restore_preserves_original_and_disposition(self):
        bad = broken_review()
        data = [native([review_comment('c1', contribute()), bad])]
        calls, run = self.run_native(data, author='operator')
        w.apply_void(data, TASK, 'operator', void('c2', bad['text']), run, operator=True)
        before = w.project(data[0])
        # A native `bd backup`/`restore-new` pair carries issues and comments verbatim.
        backup = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in data)
        restored = [json.loads(line) for line in backup.split('\n') if line.strip()]
        stored = {c['id']: c['text'] for c in restored[0]['comments']}
        self.assertEqual(stored['c2'], bad['text'])
        self.assertTrue(stored['w1'].startswith(recovery.PREFIX))
        self.assertEqual(recovery.digest(stored['c2']), before['recoveries'][0]['target_sha256'])
        self.assertEqual(before, w.project(restored[0]))

    def test_record_kind_prefix_matches_the_owning_module(self):
        self.assertEqual(recovery.KIND_PREFIXES['contribution-review'], w.PREFIX)

    def test_keyed_kind_prefixes_match_the_reserved_record_kinds(self):
        # kittrial-5bb.74: recovery repeats the keyed prefixes, which reserved_comments
        # reserves; the proposal kinds (kittrial-5bb.68) are not void targets yet.
        import reserved_comments as r
        self.assertEqual(recovery.KEYED_KIND_PREFIXES, {
            'reference-entry': (r.REFERENCE_ENTRY_PREFIX, r.REFERENCE_ENTRY_V2_PREFIX, r.REFERENCE_ENTRY_V3_PREFIX),
            'reference-acceptance': r.REFERENCE_ACCEPTANCE_PREFIX,
            'capability-entry': r.CAPABILITY_ENTRY_PREFIX,
            'capability-acceptance': r.CAPABILITY_ACCEPTANCE_PREFIX,
            'capability-verification': r.CAPABILITY_VERIFICATION_PREFIX,
            'capability-alias': r.CAPABILITY_ALIAS_PREFIX})
        self.assertEqual(recovery.PROPOSAL_KIND_PREFIXES, {})
        for kind in ('requirement-proposal', 'proposal-disposition', 'contribution-settings'):
            self.assertNotIn(kind, recovery.KIND_PREFIXES)
        self.assertEqual(set(recovery.KIND_PREFIXES),
                         set(recovery.REVIEW_KIND_PREFIXES) | set(recovery.KEYED_KIND_PREFIXES))


class AdminVoidRecordTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        try:
            (self.root / 'projects' / 'trial').mkdir(parents=True)
        except OSError as exc:  # confined environments may forbid nested temp directories
            self.skipTest('nested temporary directory unavailable: ' + str(exc))
        # Server-side operator configuration the host CLI enforces.
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': ['operator']}), encoding='utf-8')
        self.flock = Mock()
        self.patcher = patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=self.flock, LOCK_EX=2)})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.writes = []
        self.rows = [native([broken_review()])]

    def native(self, root, name, args):
        if args[:1] == ['export']:
            return '\n'.join(json.dumps(row) for row in self.rows)
        argv = args[2:] if args[:1] == ['--actor'] else args
        self.assertEqual(argv[:3], ['comments', 'add', TASK])
        self.assertEqual(self.flock.call_count, 1)
        self.writes.append(argv)
        self.rows[0]['comments'].append(dict(id='w1', text=argv[3], author='operator', created_at=STAMP))
        return json.dumps({'id': 'w1'})

    def invoke(self, payload, actor='operator'):
        path = self.root / 'void.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        argv = ['admin.py', '--root', str(self.root), 'void-record', 'trial', '--actor', actor,
                '--file', str(path)]
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', side_effect=self.native), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        return json.loads(out.getvalue())

    def test_operator_cli_writes_one_void_record_and_retries_idempotently(self):
        payload = void('c2', self.rows[0]['comments'][0]['text'])
        self.assertEqual(self.invoke(payload), dict(comment_id='w1', reconciled=False, target='c2'))
        self.assertEqual(self.invoke(payload), dict(comment_id='w1', reconciled=True, target='c2'))
        self.assertEqual(len(self.writes), 1)

    def test_operator_cli_refuses_a_void_against_a_complete_history(self):
        self.rows = [native([review_comment('c1', contribute())])]
        with self.assertRaisesRegex(ValueError, 'currently form'):
            self.invoke(void('c1', self.rows[0]['comments'][0]['text']))
        self.assertEqual(self.writes, [])

    def test_deployment_operator_allowlist_is_manageable_and_enforced(self):
        self.assertEqual(admin.operators(self.root), frozenset({'operator'}))

        def cli(*argv):
            with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                    patch.object(admin, 'root_path', return_value=self.root), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                admin.main()
            return json.loads(out.getvalue())['operators']

        self.assertEqual(cli('operators', 'list'), ['operator'])
        self.assertEqual(cli('operators', 'add', 'coordinator'), ['operator', 'coordinator'])
        # P2 removal semantics: revocation is explicit, never silent.
        with self.assertRaisesRegex(ValueError, 'confirm-revoke'):
            cli('operators', 'remove', 'operator')
        self.assertEqual(admin.operators(self.root), frozenset({'operator', 'coordinator'}))
        self.assertEqual(cli('operators', 'remove', 'operator', '--confirm-revoke'), ['coordinator'])
        self.assertEqual(admin.operators(self.root), frozenset({'coordinator'}))

    def test_operator_cli_refuses_an_actor_outside_the_allowlist(self):
        payload = void('c2', self.rows[0]['comments'][0]['text'], operator='rogue')
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.invoke(payload, actor='rogue')
        self.assertEqual(self.writes, [])

    def test_operator_cli_requires_a_task_and_exact_bytes(self):
        with self.assertRaisesRegex(ValueError, 'name its task'):
            self.invoke({'operation': 'void-record'})
        wrong = void('c2', 'different bytes')
        with self.assertRaisesRegex(ValueError, 'exact current bytes'):
            self.invoke(wrong)
        self.assertEqual(self.writes, [])

    # --- P2 render-allowlist ------------------------------------------------

    def test_render_uses_the_same_operator_allowlist_as_work_and_review(self):
        from render import render
        bad = broken_review()
        rows = [native([review_comment('c1', contribute()), bad,
                        void_comment('v1', void('c2', bad['text'], operator='operator'))])]
        with patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''}):
            authority = admin.operators(self.root)
            self.assertEqual([(i['task'], i['review_state'])
                              for i in work.queue(rows, 'worker', [], operators=authority)['items']],
                             [(TASK, 'awaiting-review')])
            dest = self.root / 'views-valid'
            render(rows, dest, authority)
            row = [line for line in (dest / 'CURRENT.md').read_text(encoding='utf-8').splitlines()
                   if line.startswith('| ' + TASK)]
            self.assertEqual(len(row), 1)
            self.assertIn('| awaiting-review |', row[0])
            self.assertNotIn('| error |', row[0])
            # An unconfigured/unavailable allowlist is the same incident the
            # other reads report (state `error`), not a second, quieter story.
            dest2 = self.root / 'views-invalid'
            render(rows, dest2, ())
            row2 = [line for line in (dest2 / 'CURRENT.md').read_text(encoding='utf-8').splitlines()
                    if line.startswith('| ' + TASK)]
            self.assertEqual(len(row2), 1)
            self.assertIn('| error |', row2[0])

    # --- P2 operator-removal-and-restore ------------------------------------

    def test_operator_removal_revokes_and_readding_restores_void_dispositions(self):
        bad = broken_review()
        issue = native([review_comment('c1', contribute()), bad,
                        void_comment('v1', void('c2', bad['text'], operator='ops'), author='ops')])
        self.assertEqual(w.project(issue, ('ops',))['review_state'], 'awaiting-review')
        with self.assertRaisesRegex(ValueError, 'operator reconciliation'):
            w.project(issue, ('operator',))
        # Re-adding the operator restores the disposition: nothing was deleted.
        self.assertEqual(w.project(issue, ('ops',))['review_state'], 'awaiting-review')

    def test_project_backup_carries_the_allowlist_and_restore_reports_it_without_regranting(self):
        marker = self.root / 'deployment.private.json'
        marker.write_text(json.dumps({'password': 'x', 'unit': 'none', 'operators': ['operator', 'ops']}),
                          encoding='utf-8')
        (self.root / 'backups').mkdir()
        (self.root / 'projects' / 'other').mkdir()
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'trial')
        bundle = json.loads((self.root / 'backups' / 'trial.coordination.json').read_text(encoding='utf-8'))
        self.assertEqual(bundle['operators'], ['operator', 'ops'])
        self.assertEqual(admin.coordination_operators(self.root, 'trial'), ['operator', 'ops'])
        self.assertEqual(admin.missing_operators(self.root, 'trial'), [])
        # A host that does not list `ops` treats the void `ops` authored as
        # inert. Restoring the backup reports that difference; it must not
        # change the deployment allowlist by itself.
        marker.write_text(json.dumps({'password': 'x', 'unit': 'none', 'operators': ['operator']}),
                          encoding='utf-8')
        self.assertEqual(admin.missing_operators(self.root, 'trial'), ['ops'])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            admin.restore_coordination(self.root, 'trial', 'other')
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['operators'], ['operator'])
        self.assertIn('NOT restored', out.getvalue())
        self.assertIn('ops', out.getvalue())
        # The explicit flag is the only way a restore re-grants recorded authority.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            admin.restore_coordination(self.root, 'trial', 'other', restore_operators=True)
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['operators'], ['operator', 'ops'])
        self.assertIn('ops', out.getvalue())
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)

    def test_restore_does_not_regrant_an_operator_revoked_after_the_backup(self):
        """Review item restore-regrants-operator (F6).

        Repro: `operators remove ops-james --confirm-revoke`, then restore an
        older project backup. The allowlist is deployment-wide, so re-adding the
        entry would restore authority for every project from a stale backup with
        only a printed line. It must stay revoked until an operator says
        otherwise, and the void must stay inert on the restored project.
        """
        marker = self.root / 'deployment.private.json'
        marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                      'operators': ['operator', 'ops-james']}), encoding='utf-8')
        (self.root / 'backups').mkdir()
        (self.root / 'backups' / 'trial').mkdir()
        (self.root / 'projects' / 'other').mkdir()

        def cli(*argv):
            with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                    patch.object(admin, 'root_path', return_value=self.root), \
                    patch.object(admin, 'add_project'), \
                    patch.object(admin, 'run_bd', return_value='restored'), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                admin.main()
            return out.getvalue()

        # 1. A backup taken while ops-james is an operator.
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'trial')
        self.assertEqual(admin.coordination_operators(self.root, 'trial'), ['operator', 'ops-james'])
        # 2. Revoke ops-james from the deployment allowlist.
        cli('operators', 'remove', 'ops-james', '--confirm-revoke')
        self.assertEqual(admin.operators(self.root), frozenset({'operator'}))
        # 3. Restore the older backup: the native records come back, the revoked
        #    authority does not.
        out = cli('restore-new', 'trial', 'other')
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['operators'], ['operator'])
        self.assertIn('ops-james', out)
        self.assertIn('--restore-operators', out)
        # 4. The preserved void by the revoked operator stays inert on the
        #    restored project: authority is the deployment allowlist, not the
        #    backup sidecar.
        bad = broken_review()
        issue = native([review_comment('c1', contribute()), bad,
                        void_comment('v1', void('c2', bad['text'], operator='ops-james'), author='ops-james')])
        with self.assertRaisesRegex(ValueError, 'operator reconciliation'):
            w.project(issue, admin.operators(self.root))
        self.assertEqual(w.project(issue, ('ops-james',))['review_state'], 'awaiting-review')
        # 5. An operator can still re-grant deliberately.
        cli('operators', 'add', 'ops-james')
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['operators'], ['operator', 'ops-james'])

    def test_restore_operators_flag_regrants_the_recorded_allowlist(self):
        marker = self.root / 'deployment.private.json'
        marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                      'operators': ['operator', 'ops-james']}), encoding='utf-8')
        (self.root / 'backups').mkdir()
        (self.root / 'backups' / 'trial').mkdir()
        (self.root / 'projects' / 'other').mkdir()
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'trial')
        marker.write_text(json.dumps({'password': 'x', 'unit': 'none', 'operators': ['operator']}),
                          encoding='utf-8')
        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'trial', 'other', '--restore-operators']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'add_project'), \
                patch.object(admin, 'run_bd', return_value='restored'), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['operators'], ['operator', 'ops-james'])
        self.assertIn('ops-james', out.getvalue())
        self.assertIn('--restore-operators', out.getvalue())

    def test_merge_operators_treats_a_string_allowlist_as_one_identity(self):
        """Review item restore-operators-string-allowlist (kittrial-5bb.47).

        ``deployment.private.json`` may hold a bare string, which ``operators()``
        has always accepted as a one-element allowlist. ``merge_operators`` used
        ``list(cfg.get('operators') or [])``, so ``restore-new
        --restore-operators`` rewrote 'alice' as ['a','l','i','c','e'] plus the
        re-granted identity: the real operator lost authority while five
        one-letter identities gained it.
        """
        marker = self.root / 'deployment.private.json'
        marker.write_text(json.dumps({'password': 'x', 'unit': 'none', 'operators': 'alice'}),
                          encoding='utf-8')
        self.assertEqual(admin.operators(self.root), frozenset({'alice'}))
        self.assertEqual(admin.merge_operators(self.root, ['ops-a']), ['ops-a'])
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['operators'], ['alice', 'ops-a'])
        self.assertEqual(admin.operators(self.root), frozenset({'alice', 'ops-a'}))

    def test_merge_operators_keeps_a_list_allowlist_and_is_additive(self):
        marker = self.root / 'deployment.private.json'
        self.assertEqual(admin.merge_operators(self.root, ['observer']), ['observer'])
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['operators'], ['operator', 'observer'])
        # A second pass re-grants nothing: an identity already listed is untouched.
        self.assertEqual(admin.merge_operators(self.root, ['operator', 'observer']), [])
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['operators'], ['operator', 'observer'])

    def test_merge_operators_refuses_a_malformed_allowlist_before_a_write(self):
        marker = self.root / 'deployment.private.json'
        marker.write_text(json.dumps({'password': 'x', 'unit': 'none', 'operators': {'alice': 1}}),
                          encoding='utf-8')
        before = marker.read_text(encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'operators must be a list'):
            admin.merge_operators(self.root, ['ops-a'])
        self.assertEqual(marker.read_text(encoding='utf-8'), before)
        marker.write_text(json.dumps({'password': 'x', 'unit': 'none', 'operators': ['ok', 'not an identity!']}),
                          encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Invalid operator identity'):
            admin.merge_operators(self.root, ['ops-a'])

    def test_operators_cli_reports_and_extends_a_string_allowlist(self):
        marker = self.root / 'deployment.private.json'
        marker.write_text(json.dumps({'password': 'x', 'unit': 'none', 'operators': 'alice'}),
                          encoding='utf-8')

        def cli(*argv):
            with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                    patch.object(admin, 'root_path', return_value=self.root), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                admin.main()
            return json.loads(out.getvalue())['operators']

        self.assertEqual(cli('operators', 'list'), ['alice'])
        self.assertEqual(cli('operators', 'add', 'ops-a'), ['alice', 'ops-a'])
        self.assertEqual(admin.operators(self.root), frozenset({'alice', 'ops-a'}))

    def test_restore_operators_flag_regrants_around_a_string_allowlist(self):
        """End-to-end repro of kittrial-5bb.47 through ``restore-new``."""
        marker = self.root / 'deployment.private.json'
        marker.write_text(json.dumps({'password': 'x', 'unit': 'none', 'operators': 'operator'}),
                          encoding='utf-8')
        (self.root / 'backups').mkdir()
        (self.root / 'backups' / 'trial').mkdir()
        (self.root / 'projects' / 'other').mkdir()
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'trial')
        self.assertEqual(admin.coordination_operators(self.root, 'trial'), ['operator'])
        # The deployment now lists a different single operator as a bare string.
        marker.write_text(json.dumps({'password': 'x', 'unit': 'none', 'operators': 'other'}),
                          encoding='utf-8')
        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'trial', 'other', '--restore-operators']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'add_project'), \
                patch.object(admin, 'run_bd', return_value='restored'), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['operators'], ['other', 'operator'])
        self.assertIn('operator', out.getvalue())

    def test_coordination_backup_refuses_a_malformed_operator_snapshot(self):
        (self.root / 'backups').mkdir()
        (self.root / 'backups' / 'trial.coordination.json').write_text(
            json.dumps({'schema_version': 1, 'status': 'complete', 'files': {}, 'operators': 'not-a-list'}),
            encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'operators must be a list'):
            admin.coordination_backup(self.root, 'trial')

    # --- P3 config-write-safety ---------------------------------------------

    def test_operator_config_write_is_atomic_and_private(self):
        marker = self.root / 'deployment.private.json'
        os.chmod(marker, 0o664)
        replaced = []
        real_replace = os.replace
        with patch.object(os, 'fsync', wraps=os.fsync) as fsync, \
                patch.object(os, 'replace',
                             side_effect=lambda *a, **k: (replaced.append(a), real_replace(*a, **k))[1]):
            admin.atomic_private_write(marker, json.dumps({'password': 'x', 'operators': ['operator']}))
        self.assertTrue(replaced, 'config rewrite must go through os.replace')
        self.assertTrue(fsync.called, 'config rewrite must fsync before replace')
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['operators'], ['operator'])
        if os.name == 'posix':
            # An existing permissive mode must not survive the rewrite.
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)

    def test_operators_add_rewrites_the_private_config_0600(self):
        marker = self.root / 'deployment.private.json'
        os.chmod(marker, 0o664)
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'operators', 'add', 'ops2']), \
                patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(io.StringIO()):
            admin.main()
        self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['operators'], ['operator', 'ops2'])
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        self.assertEqual(admin.operators(self.root), frozenset({'operator', 'ops2'}))

    # --- P3 one authority source --------------------------------------------

    def test_orchestra_operators_env_is_not_an_authority_source(self):
        with patch.dict(os.environ, {'ORCHESTRA_OPERATORS': 'shell-only'}):
            # Reads and writes use the config file only.
            self.assertEqual(admin.operators(self.root), frozenset({'operator'}))
            # A write command refuses the mismatch loudly instead of letting the
            # admin shell authorize an actor the endpoint would ignore.
            with self.assertRaisesRegex(ValueError, 'ORCHESTRA_OPERATORS'):
                admin.operators(self.root, strict=True)
            with self.assertRaisesRegex(ValueError, 'ORCHESTRA_OPERATORS'):
                self.invoke(void('c2', self.rows[0]['comments'][0]['text'], operator='shell-only'),
                            actor='shell-only')
        self.assertEqual(self.writes, [])


class AdminRevertRecordTests(unittest.TestCase):
    """The audited integration revert record has one operator-only write route.

    The task id is the one the shared follow-on/revert fixture uses, so these
    tests drive the same rows the projection tests do through the operator CLI.
    """

    MERGE = 'e' * 40
    SOURCE = 'a' * 40
    TASK_ID = 'task-1'
    # A valid admin project name; the native task id stays the fixture's.
    PROJECT_ID = 'reverttask'

    def setUp(self):
        # Reuse the whole-task fixture (contribution, independent approval and
        # passed scoped integration evidence) the revert tests already build.
        from test_follow_on_contributions import IntegrationRevertTests
        self.fixture = IntegrationRevertTests('run')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        # Build the integrated chain: contribution, independent approval and the
        # passed scoped integration the revert must name.
        self.fixture.integration_case()
        self.rows = self.fixture.rows
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        try:
            (self.root / 'projects' / self.PROJECT_ID).mkdir(parents=True)
        except OSError as exc:  # confined environments may forbid nested temp directories
            self.skipTest('nested temporary directory unavailable: ' + str(exc))
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': ['operator']}), encoding='utf-8')
        self.patcher = patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=Mock(), LOCK_EX=2)})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.writes = []

    def native(self, root, name, args):
        argv = args[2:] if args[:1] == ['--actor'] else args
        if argv[:1] == ['export']:
            return '\n'.join(json.dumps(row) for row in self.rows)
        self.assertEqual(argv[:3], ['comments', 'add', self.TASK_ID])
        self.writes.append(argv)
        cid = 'w%d' % len(self.writes)
        self.rows[0]['comments'].append(
            dict(id=cid, text=argv[3], author='operator', created_at=STAMP))
        return json.dumps({'id': cid})

    def payload(self, **extra):
        p = dict(schema_version=1, operation='revert-record', operation_id='rv1', task=self.TASK_ID,
                 contribution='1', integration_commit=self.MERGE, revert_commit='f' * 40,
                 reason='The integration commit no longer contains the reviewed change')
        p.update(extra)
        return p

    def invoke(self, payload, actor='operator'):
        path = self.root / 'revert.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        argv = ['admin.py', '--root', str(self.root), 'revert-record', self.PROJECT_ID,
                '--actor', actor, '--file', str(path)]
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', side_effect=self.native), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        return json.loads(out.getvalue())

    # --- shared helpers for the retraction/revocation wording (item smaller b) --

    def project(self):
        return self.root / 'projects' / self.PROJECT_ID

    def set_operators(self, *names):
        """Write the deployment allowlist the CLI and the reads both consult."""
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': list(names)}), encoding='utf-8')

    def run_as(self, actor):
        """A native run callable attributing each written comment to ``actor``."""
        def run(argv):
            cid = 'w%d' % (len(self.rows[0]['comments']) + 1)
            self.rows[0]['comments'].append(dict(id=cid, text=argv[3], author=actor,
                                                 created_at=STAMP))
            return json.dumps({'id': cid})
        return run

    def retract_as(self, revert_comment_id, actor, operators):
        """Host-issue a retraction of one revert as ``actor`` (journaled)."""
        original = next(c['text'] for c in self.rows[0]['comments']
                        if str(c['id']) == revert_comment_id)
        payload = dict(void(revert_comment_id, original, operation_id='void-' + actor,
                            target_kind='integration-revert', operator=actor), task=self.TASK_ID)
        return w.apply_void(self.rows, self.TASK_ID, actor, payload, self.run_as(actor),
                            operator=True, operators=list(operators), journal=self.project())

    def remove_message(self, actor):
        """The interactive refusal `operators remove ACTOR` raises, as the CLI text."""
        argv = ['admin.py', '--root', str(self.root), 'operators', 'remove', actor]
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'initialized_projects', return_value=[self.PROJECT_ID]), \
                patch.object(admin, 'run_bd', side_effect=self.native), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'confirm-revoke') as caught:
                admin.main()
        return str(caught.exception)

    def test_operator_cli_writes_one_audited_revert_and_retries_idempotently(self):
        result = self.invoke(self.payload())
        self.assertEqual(result['contribution'], '1')
        self.assertEqual(result['integration_commit'], self.MERGE)
        self.assertFalse(result['reconciled'])
        self.assertEqual(len(self.writes), 1)
        self.assertTrue(self.writes[0][3].startswith(w.REVERT_PREFIX))
        retried = self.invoke(self.payload())
        self.assertTrue(retried['reconciled'])
        self.assertEqual(retried['comment_id'], result['comment_id'])
        self.assertEqual(len(self.writes), 1)

    def test_operator_cli_refuses_an_actor_outside_the_allowlist(self):
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.invoke(self.payload(operator='rogue'), actor='rogue')
        self.assertEqual(self.writes, [])

    def test_operator_cli_refuses_without_a_task(self):
        with self.assertRaisesRegex(ValueError, 'name its task'):
            self.invoke({'operation': 'revert-record'})
        self.assertEqual(self.writes, [])

    def test_operator_cli_refuses_a_commit_that_is_not_the_integration(self):
        with self.assertRaisesRegex(ValueError, 'must name the contribution integration commit'):
            self.invoke(self.payload(integration_commit='9' * 40))
        self.assertEqual(self.writes, [])

    def test_revert_record_reads_the_chain_through_an_applied_void(self):
        # kittrial-5bb.92 item 5: a malformed review comment an operator already voided
        # must not make revert-record fail with 'Malformed contribution-review history'.
        malformed = dict(id='badreview', text=w.PREFIX + 'not-json', author='worker', created_at=STAMP)
        self.rows[0]['comments'].append(malformed)
        payload = void('badreview', malformed['text'], task=self.TASK_ID)
        w.apply_void(self.rows, self.TASK_ID, 'operator', payload, self.run_as('operator'),
                     operator=True, operators=['operator'], journal=self.project())
        result = self.invoke(self.payload())
        self.assertEqual(result['contribution'], '1')
        self.assertEqual(len(self.writes), 1)

    def test_operator_cli_refuses_an_unintegrated_contribution(self):
        # A second, superseding contribution with no scoped integration of its own:
        # the named commit is not the one the shared projection reports as passing
        # for it, so nothing is written.
        self.fixture.issue['assignee'] = 'worker'
        p = self.fixture.contribution('d' * 40, base='b' * 40)
        second = self.fixture.send(p)['comment_id']
        with self.assertRaisesRegex(ValueError, 'currently integrated'):
            self.invoke(self.payload(contribution=second, integration_commit=self.MERGE))
        self.assertEqual(self.writes, [])


    def test_operator_cli_writes_the_host_journal_entry_beside_the_native_record(self):
        """The P1 fix: the CLI is the only writer of the host-issued proof.

        A native revert comment is not authority by itself (its stored author is the
        self-declared request actor), so the write route also journals the record
        under the project lock and the reader honours the comment only with it.
        """
        result = self.invoke(self.payload())
        directory = self.root / 'projects' / self.PROJECT_ID / w.JOURNAL_DIR
        entries = sorted(directory.glob('*.json'))
        self.assertEqual(len(entries), 1)
        entry = json.loads(entries[0].read_text(encoding='utf-8'))
        self.assertEqual(entries[0].name, entry['sha256'] + '.json')
        self.assertEqual(w.validate_revert_journal_entry(entry, entries[0].name), entry)
        self.assertEqual(entry['comment_id'], result['comment_id'])
        self.assertEqual((entry['kind'], entry['task'], entry['contribution']), (
            w.JOURNAL_REVERT, self.TASK_ID, '1'))
        self.assertEqual(entry['operator'], 'operator')
        # The reader honours the record WITH the journal and ignores it WITHOUT it.
        issue = self.rows[0]
        honoured, invalid = w.revert_records(issue, ['operator'], self.root / 'projects' / self.PROJECT_ID)
        self.assertEqual([r['comment_id'] for r in honoured], [result['comment_id']])
        self.assertEqual(invalid, [])
        forged_only, problems = w.revert_records(issue, ['operator'])
        self.assertEqual(forged_only, [])
        self.assertIn(result['comment_id'], problems)
        # A retry writes no second native comment and re-persists the same entry.
        retried = self.invoke(self.payload())
        self.assertTrue(retried['reconciled'])
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(len(sorted(directory.glob('*.json'))), 1)

    def test_operator_cli_refuses_a_symlinked_journal_path_with_no_write(self):
        journal = self.root / 'projects' / self.PROJECT_ID / w.JOURNAL_DIR
        try:
            journal.symlink_to(self.root / 'projects' / self.PROJECT_ID, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.invoke(self.payload())
        self.assertEqual(self.writes, [])

    def test_retraction_of_the_host_record_through_the_operator_cli(self):
        """`void-record` retracts a host-issued revert and journals the retraction."""
        result = self.invoke(self.payload())
        revert_text = next(c['text'] for c in self.rows[0]['comments']
                           if str(c['id']) == result['comment_id'])
        payload = dict(void(result['comment_id'], revert_text, operation_id='vr1',
                            target_kind='integration-revert'), task=self.TASK_ID)
        path = self.root / 'void.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        argv = ['admin.py', '--root', str(self.root), 'void-record', self.PROJECT_ID,
                '--actor', 'operator', '--file', str(path)]
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', side_effect=self.native), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        receipt = json.loads(out.getvalue())
        self.assertEqual(receipt['target'], result['comment_id'])
        project = self.root / 'projects' / self.PROJECT_ID
        kinds = sorted(json.loads(p.read_text(encoding='utf-8'))['kind']
                       for p in (project / w.JOURNAL_DIR).glob('*.json'))
        self.assertEqual(kinds, [w.JOURNAL_REVERT, w.JOURNAL_RETRACTION])
        honoured, _ = w.revert_records(self.rows[0], ['operator'], project)
        self.assertEqual(honoured, [])

    def test_operator_cli_refuses_a_revert_when_the_journal_cannot_be_used(self):
        """Refused BEFORE the native write, so no un-journaled revert exists."""
        journal = self.root / 'projects' / self.PROJECT_ID / w.JOURNAL_DIR
        journal.mkdir(parents=True, exist_ok=True)
        # A FILE where the journal directory belongs: every reader and writer treats
        # that as unusable, so the operator route refuses before any write.
        journal.rmdir()
        journal.write_text('not a directory', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, r'\.integration-reverts'):
            self.invoke(self.payload())
        self.assertEqual(self.writes, [])

    def test_operators_remove_warns_about_the_revert_records_it_revokes(self):
        """kittrial-5bb.52 item 2: revocation must name the reverts it disables."""
        result = self.invoke(self.payload())
        argv = ['admin.py', '--root', str(self.root), 'operators', 'remove', 'operator']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'initialized_projects', return_value=[self.PROJECT_ID]), \
                patch.object(admin, 'run_bd', side_effect=self.native), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'confirm-revoke') as caught:
                admin.main()
        message = str(caught.exception)
        self.assertIn('integration revert record', message.lower())
        self.assertIn(self.TASK_ID + '/' + result['comment_id'], message)

    def test_operator_cli_refuses_an_unwritable_journal_before_any_write(self):
        """kittrial-5bb.52 item smaller (a): refuse, with no native comment.

        The directory exists, so the pre-fix `open_revert_journal` accepted it; the
        native revert comment was written first and the journal write then died with
        a raw PermissionError. Writability is now proven before the write.
        """
        journal = self.project() / w.JOURNAL_DIR
        journal.mkdir(parents=True, exist_ok=True)
        journal.chmod(0o500)
        self.addCleanup(journal.chmod, 0o700)
        if os.access(str(journal), os.W_OK):
            self.skipTest('the environment does not enforce directory write permission')
        with self.assertRaisesRegex(ValueError, 'not writable'):
            self.invoke(self.payload())
        self.assertEqual(self.writes, [])
        self.assertEqual(list(journal.glob('*.json')), [])

    def test_current_md_uses_the_host_journal_for_a_journaled_revert(self):
        """render-journal regression: CURRENT.md must not read a reverted task as integrated.

        `render` builds its queue rows, so it must be given the same project journal
        `work`/`review`/`brief` get (the parent of the views destination the refresh
        route renders into). Pre-fix it passed no journal, trusted no revert and
        printed `integrated` here while every other read said awaiting-integration.
        """
        from render import render
        from review_state import project as reviewed, scopes_for
        result = self.invoke(self.payload())
        dest = self.project() / 'views'
        render(self.rows, dest, ['operator'])
        rendered = [line for line in (dest / 'CURRENT.md').read_text(encoding='utf-8').splitlines()
                    if line.startswith('| ' + self.TASK_ID + ' ')]
        self.assertEqual(len(rendered), 1)
        self.assertIn('| awaiting-integration |', rendered[0])
        self.assertNotIn('| integrated |', rendered[0])
        # ...and it is exactly the shared projection review/brief/work report.
        state = reviewed(self.rows[0], scopes_for(self.rows, self.TASK_ID), ['operator'], None,
                         self.project())
        self.assertEqual(state['review_state'], 'awaiting-integration')
        self.assertTrue(state['integration']['reverted'])
        self.assertEqual(state['integration']['fact'], 'reverted')

    def test_operators_remove_warns_that_a_retraction_stops_applying(self):
        """item smaller (b): removing the operator who RETRACTED a revert re-applies it."""
        self.set_operators('operator', 'ops2')
        result = self.invoke(self.payload())
        self.retract_as(result['comment_id'], 'ops2', ('operator', 'ops2'))
        honoured, _ = w.revert_records(self.rows[0], ['operator', 'ops2'], self.project())
        self.assertEqual(honoured, [])
        message = self.remove_message('ops2')
        self.assertIn(self.TASK_ID + '/' + result['comment_id'], message)
        self.assertIn('re-apply', message)
        self.assertIn('retraction', message)

    def test_operators_remove_does_not_list_a_revert_retracted_by_another_operator(self):
        """item smaller (b): the scan must use the live allowlist, not the revoked actor.

        A revert already retracted by another operator is neither stopping nor
        re-applying; reading it under the single revoked actor made it look honoured
        and listed it as "will stop applying".
        """
        self.set_operators('operator', 'ops2')
        result = self.invoke(self.payload())
        self.retract_as(result['comment_id'], 'ops2', ('operator', 'ops2'))
        message = self.remove_message('operator')
        self.assertNotIn(self.TASK_ID + '/' + result['comment_id'], message)
        self.assertIn('no host-issued integration revert record stops or re-applies', message)

    def test_operators_remove_a_lone_operator_reports_no_reapplied_revert(self):
        """item smaller (b), single-operator case: a self-retracted revert is not 'applying'."""
        result = self.invoke(self.payload())
        self.retract_as(result['comment_id'], 'operator', ('operator',))
        message = self.remove_message('operator')
        self.assertNotIn(self.TASK_ID + '/' + result['comment_id'], message)
        self.assertIn('no host-issued integration revert record stops or re-applies', message)


class AdminRevertJournalBackupTests(unittest.TestCase):
    """The host revert journal survives backup, validation and restore (P1)."""

    MERGE = 'e' * 40
    SOURCE = 'a' * 40

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        try:
            (self.root / 'projects' / 'trial').mkdir(parents=True)
        except OSError as exc:  # confined environments may forbid nested temp directories
            self.skipTest('nested temporary directory unavailable: ' + str(exc))
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': ['operator']}), encoding='utf-8')
        self.patcher = patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=Mock(), LOCK_EX=2)})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.entry = self.journal_entry()

    def payload(self, operation_id='rv1', comment_id='c9'):
        return dict(schema_version=1, operation='revert-record', operation_id=operation_id,
                    task=TASK, contribution='c1', integration_commit=self.MERGE,
                    revert_commit='f' * 40, reason='Re-merge dropped the change',
                    operator='operator')

    def journal_entry(self):
        return w.revert_journal_entry(w.JOURNAL_REVERT, self.payload(), 'c9', 'operator',
                                      task=TASK, contribution='c1',
                                      integration_commit=self.MERGE, revert_commit='f' * 40)

    def write_entry(self, entry=None, name=None):
        entry = entry or self.entry
        directory = self.root / 'projects' / 'trial' / w.JOURNAL_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (name or (entry['sha256'] + '.json'))
        path.write_text(json.dumps(entry), encoding='utf-8')
        return path

    def test_backup_and_restore_round_trip_the_host_revert_journal(self):
        self.write_entry()
        (self.root / 'backups').mkdir()
        (self.root / 'projects' / 'other').mkdir()
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'trial')
        bundle = json.loads((self.root / 'backups' / 'trial.coordination.json').read_text(encoding='utf-8'))
        name = w.JOURNAL_DIR + '/' + self.entry['sha256'] + '.json'
        self.assertIn(name, bundle['files'])
        self.assertEqual(bundle['files'][name], self.entry)
        admin.restore_coordination(self.root, 'trial', 'other')
        restored = self.root / 'projects' / 'other' / name
        self.assertTrue(restored.is_file())
        self.assertEqual(json.loads(restored.read_text(encoding='utf-8')), self.entry)

    def test_backup_refuses_a_malformed_or_misnamed_journal_entry(self):
        broken = dict(self.entry)
        broken['payload'] = dict(self.entry['payload'], reason='different bytes')
        self.write_entry(broken)
        (self.root / 'backups').mkdir()
        with patch.object(admin, 'run_bd', return_value='synced'):
            with self.assertRaisesRegex(ValueError, 'hash does not match'):
                admin.backup_project(self.root, 'trial')

    def test_validate_coordination_files_checks_the_journal_shape_and_path(self):
        good = {w.JOURNAL_DIR + '/' + self.entry['sha256'] + '.json': self.entry}
        admin.validate_coordination_files(good)
        wrong_path = {w.JOURNAL_DIR + '/' + '0' * 64 + '.json': self.entry}
        with self.assertRaisesRegex(ValueError, 'path mismatch'):
            admin.validate_coordination_files(wrong_path)
        with self.assertRaisesRegex(ValueError, 'Invalid coordination backup path'):
            admin.validate_coordination_files({w.JOURNAL_DIR + '/not-a-hash.json': self.entry})
        with self.assertRaisesRegex(ValueError, 'journal'):
            admin.validate_coordination_files({w.JOURNAL_DIR + '/' + self.entry['sha256'] + '.json':
                                               dict(self.entry, sha256='0' * 64)})

    def test_backup_refuses_a_symlinked_revert_journal(self):
        target = self.root / 'elsewhere'
        target.mkdir()
        journal = self.root / 'projects' / 'trial' / w.JOURNAL_DIR
        try:
            journal.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        (self.root / 'backups').mkdir()
        with patch.object(admin, 'run_bd', return_value='synced'):
            with self.assertRaisesRegex(ValueError, 'symlink'):
                admin.backup_project(self.root, 'trial')

    def test_restore_refuses_a_journal_entry_that_does_not_match_its_name(self):
        name = w.JOURNAL_DIR + '/' + '0' * 64 + '.json'
        (self.root / 'backups').mkdir()
        (self.root / 'backups' / 'trial').mkdir()
        (self.root / 'backups' / 'trial.coordination.json').write_text(
            json.dumps({'schema_version': 1, 'status': 'complete', 'operators': ['operator'],
                        'files': {name: self.entry}}), encoding='utf-8')
        (self.root / 'projects' / 'other').mkdir()
        with self.assertRaisesRegex(ValueError, 'path mismatch'):
            admin.restore_coordination(self.root, 'trial', 'other')
        self.assertFalse((self.root / 'projects' / 'other' / w.JOURNAL_DIR).exists())


class RevertDocumentationTests(unittest.TestCase):
    """The revert docs match the measured rollback/restore behaviour (5bb.52).

    Item ``rollback-restore-docs``: REVIEWS.md used to call a rollback to the
    pre-journal kit "the safe direction" and to imply a two-release staging plan.
    Item ``smaller`` (c): the revert example used the predictable operation id
    ``revert-001``, which the retry path can adopt.
    """

    def doc(self):
        return (Path(__file__).resolve().parents[1] / 'docs' / 'REVIEWS.md').read_text(encoding='utf-8')

    def test_rollback_is_not_documented_as_the_safe_direction(self):
        text = self.doc()
        self.assertNotIn('the safe direction', text)
        self.assertIn('Rollback and restore limits', text)
        self.assertIn('no protection for the revert journal', text)
        self.assertIn("new kit's `admin.py restore-new`", text)
        self.assertIn('drops the journal', text)
        self.assertIn('Two-release staging is not required', text)
        self.assertIn('take a fresh backup with the new kit after roll-forward', text)

    def test_revert_example_uses_an_unpredictable_operation_id(self):
        text = self.doc()
        self.assertNotIn('"revert-001"', text)
        self.assertIn('must be **unpredictable**', text)
        self.assertIn('adopted', text)
        # The fixture that writes reverts in tests must not hand out a predictable
        # shared id either: an exact same-id/same-payload retry is adopted onto the
        # earlier comment, so two calls must not collide.
        from test_follow_on_contributions import revert_payload
        self.assertNotEqual(revert_payload()['operation_id'], revert_payload()['operation_id'])


if __name__ == '__main__':
    unittest.main()
