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
        restored = [json.loads(line) for line in backup.splitlines() if line.strip()]
        stored = {c['id']: c['text'] for c in restored[0]['comments']}
        self.assertEqual(stored['c2'], bad['text'])
        self.assertTrue(stored['w1'].startswith(recovery.PREFIX))
        self.assertEqual(recovery.digest(stored['c2']), before['recoveries'][0]['target_sha256'])
        self.assertEqual(before, w.project(restored[0]))

    def test_record_kind_prefix_matches_the_owning_module(self):
        self.assertEqual(recovery.KIND_PREFIXES['contribution-review'], w.PREFIX)


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
    """The audited integration revert record has one operator-only write route."""

    MERGE = 'e' * 40
    SOURCE = 'a' * 40

    def setUp(self):
        # Reuse the whole-task fixture (contribution, independent approval and
        # passed scoped integration evidence) the revert tests already build.
        from test_follow_on_contributions import IntegrationRevertTests
        self.fixture = IntegrationRevertTests('run')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.rows = self.fixture.rows
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        try:
            (self.root / 'projects' / PROJECT).mkdir(parents=True)
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
        self.assertEqual(argv[:3], ['comments', 'add', TASK])
        self.writes.append(argv)
        self.rows[0]['comments'].append(
            dict(id='r1', text=argv[3], author='operator', created_at=STAMP))
        return json.dumps({'id': 'r1'})

    def payload(self, **extra):
        p = dict(schema_version=1, operation='revert-record', operation_id='rv1', task=TASK,
                 contribution='1', integration_commit=self.MERGE, revert_commit='f' * 40,
                 reason='The integration commit no longer contains the reviewed change')
        p.update(extra)
        return p

    def invoke(self, payload, actor='operator'):
        path = self.root / 'revert.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        argv = ['admin.py', '--root', str(self.root), 'revert-record', PROJECT, '--actor', actor,
                '--file', str(path)]
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', side_effect=self.native), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        return json.loads(out.getvalue())

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

    def test_operator_cli_refuses_an_unintegrated_contribution(self):
        # A second, unintegrated contribution: the named commit is not the one the
        # shared projection reports as passing for it, so nothing is written.
        self.fixture.issue['assignee'] = 'worker'
        p = self.fixture.contribution('d' * 40, base='b' * 40)
        p['supersedes'] = None
        second = self.fixture.send(p)['comment_id']
        with self.assertRaisesRegex(ValueError, 'currently integrated'):
            self.invoke(self.payload(contribution=second))
        self.assertEqual(self.writes, [])


if __name__ == '__main__':
    unittest.main()
