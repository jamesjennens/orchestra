"""Audited operator recovery for malformed reserved structured history."""
import contextlib
import io
import json
import shutil
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

    def invoke(self, payload):
        path = self.root / 'void.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        argv = ['admin.py', '--root', str(self.root), 'void-record', 'trial', '--actor', 'operator',
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

    def test_operator_cli_requires_a_task_and_exact_bytes(self):
        with self.assertRaisesRegex(ValueError, 'name its task'):
            self.invoke({'operation': 'void-record'})
        wrong = void('c2', 'different bytes')
        with self.assertRaisesRegex(ValueError, 'exact current bytes'):
            self.invoke(wrong)
        self.assertEqual(self.writes, [])


if __name__ == '__main__':
    unittest.main()
