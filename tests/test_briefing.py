"""Worker handoff invariants and lossless, snapshot-bound history reads."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing as b
import client
from requirements import canonical_bytes
from lifecycle import DIMENSIONS
from test_lifecycle import NativeStore, payload

TASK = 'trial-task'
PROJECT = 'trial'
STAMP = '2026-09-15T10:00:00Z'


def comment(cid, text='A finding', stamp=STAMP):
    return dict(id=str(cid), text=text, author='alice/session', created_at=stamp)


def rows():
    return [dict(id=TASK, title='A task', issue_type='task', status='in_progress',
                 assignee='alice/session', description='Intent', acceptance_criteria='Acceptance',
                 labels=[], comments=[comment('first')])]


def checkpoint(data, **changes):
    current, _ = b.checkpoints(data[0])
    result = dict(schema_version=1, task=TASK,
                  previous=str(current[1]['id']) if current else None,
                  activity_cursor=b.activity_cursor(b.snapshot(data, PROJECT, TASK)),
                  source_commit='abc123', branch='work/feature', intent='Deliver the feature',
                  acceptance='Pass the agreed checks', summary='Implementation is underway',
                  next_action='Run the focused tests', open_items=[], resolved=[])
    result.update(changes)
    return result


def append_checkpoint(data, cid, p):
    data[0]['comments'].append(comment(cid, b.PREFIX + canonical_bytes(p).decode()))


def blocker():
    return dict(id='blocker-1', kind='blocker', text='Waiting for schema decision', source='trial-task-cfirst')


class CheckpointTests(unittest.TestCase):
    def test_legacy_unknown_is_not_assertion_of_no_unresolved_work(self):
        data = rows()
        result = b.brief(data, PROJECT, TASK)
        self.assertIsNone(result['unresolved']['total'])
        self.assertIsNone(result['checkpoint'])
        self.assertIn('UNKNOWN', b.format_brief(result))
        append_checkpoint(data, 'cp1', checkpoint(data))
        self.assertEqual(b.brief(data, PROJECT, TASK)['unresolved']['total'], 0)

    def test_provenance_and_new_edited_deleted_activity(self):
        data = rows()
        p = checkpoint(data)
        append_checkpoint(data, 'cp1', p)
        result = b.brief(data, PROJECT, TASK)
        cp = result['checkpoint']
        self.assertEqual(cp['author']['text'], 'alice/session')
        self.assertEqual(cp['timestamp'], STAMP)
        self.assertEqual(cp['source_commit'], 'abc123')
        self.assertEqual(cp['incorporated_activity_cursor'], p['activity_cursor'])
        self.assertFalse(cp['newer_activity'])
        mutations = [lambda d: d[0]['comments'].append(comment('late', stamp='2026-09-14T00:00:00Z')),
                     lambda d: d[0]['comments'][0].update(text='Edited finding'),
                     lambda d: d[0]['comments'].pop(0),
                     lambda d: d[0].update(assignee='bob/session')]
        for mutate in mutations:
            revised = copy.deepcopy(data)
            mutate(revised)
            self.assertTrue(b.brief(revised, PROJECT, TASK)['checkpoint']['newer_activity'])

    def test_checkpoint_save_checks_both_previous_and_incorporated_activity(self):
        data = rows()
        p = checkpoint(data)
        append_checkpoint(data, 'cp1', p)
        next_p = checkpoint(data)
        stale = dict(next_p, previous=None)
        with self.assertRaisesRegex(ValueError, 'Stale previous'):
            b.save_checkpoint(data, PROJECT, TASK, stale, 'alice/session', lambda _: self.fail('write'))
        data[0]['comments'].append(comment('new'))
        with self.assertRaisesRegex(ValueError, 'Activity changed'):
            b.save_checkpoint(data, PROJECT, TASK, next_p, 'alice/session', lambda _: self.fail('write'))

    def test_lost_response_reconciles_exact_checkpoint_without_second_write(self):
        data = rows()
        p = checkpoint(data)
        append_checkpoint(data, 'cp1', p)
        data[0]['comments'].append(comment('new'))
        result = b.save_checkpoint(data, PROJECT, TASK, p, 'alice/session', lambda _: self.fail('duplicate write'))
        self.assertEqual(result, dict(comment_id='cp1', reconciled=True))

    def test_concurrent_checkpoint_branches_fail_instead_of_arbitrary_winner(self):
        data = rows()
        p = checkpoint(data)
        append_checkpoint(data, 'cp1', p)
        append_checkpoint(data, 'cp2', dict(p, summary='Competing summary'))
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            b.brief(data, PROJECT, TASK)

    def test_old_items_survive_recent_prose_until_explicit_resolution(self):
        data = rows()
        first = checkpoint(data, open_items=[blocker()])
        append_checkpoint(data, 'cp1', first)
        data[0]['comments'].extend(comment(str(n), 'Everything is now resolved!') for n in range(30))
        second = checkpoint(data, open_items=[blocker()])
        append_checkpoint(data, 'cp2', second)
        self.assertEqual(b.brief(data, PROJECT, TASK)['unresolved']['items'], [blocker()])
        dropped = checkpoint(data)
        with self.assertRaisesRegex(ValueError, 'Carry every unresolved'):
            b.save_checkpoint(data, PROJECT, TASK, dropped, 'alice/session', lambda _: self.fail('write'))
        resolved = dict(dropped, resolved=[dict(id='blocker-1', reason='Superseded by accepted schema', evidence='decision-2')])
        append_checkpoint(data, 'cp3', resolved)
        self.assertEqual(b.brief(data, PROJECT, TASK)['unresolved']['total'], 0)
        history = b.snapshot(data, PROJECT, TASK)['entries']
        self.assertTrue(any('decision-2' in e['body'] for e in history))
        self.assertTrue(any('Waiting for schema decision' in e['body'] for e in history))

    def test_open_item_cannot_be_replaced_in_place_to_evade_resolution(self):
        first = checkpoint(rows(), open_items=[blocker()])
        replacement = dict(blocker(), kind='question', text='Unrelated question', source='different-source')
        with self.assertRaises(ValueError):
            b.transition(first, dict(first, open_items=[replacement]))

    def test_resolution_requires_evidence(self):
        p = checkpoint(rows(), resolved=[dict(id='blocker-1', reason='Resolved', evidence='')])
        with self.assertRaises(ValueError):
            b.validate_checkpoint(p, TASK)

    def test_native_dependencies_and_six_completion_facts_are_separate(self):
        store = NativeStore().seed()
        store.record(payload('tested', 'failed', operation='test-failed'))
        store.rows[0]['status'] = 'closed'
        store.rows[0]['dependencies'] = [dict(type='blocks', depends_on_id='trial-upstream')]
        result = b.brief(store.rows, PROJECT, TASK)
        self.assertEqual(set(result['lifecycle']), set(DIMENSIONS))
        self.assertEqual(result['lifecycle']['implemented']['value'], 'passed')
        self.assertEqual(result['lifecycle']['tested']['value'], 'failed')
        for dim in ('reviewed', 'integrated', 'deployed', 'live-verified'):
            self.assertEqual(result['lifecycle'][dim]['value'], 'unknown')
        self.assertEqual(result['dependencies']['items'][0]['depends_on_id']['text'], 'trial-upstream')


class HistoryTests(unittest.TestCase):
    def test_order_uses_instants_when_fractional_seconds_differ(self):
        data = rows()
        data[0]['comments'] = [comment('later', stamp='2026-09-15T10:00:00.500Z'),
                               comment('earlier', stamp='2026-09-15T10:00:00Z')]
        page = b.history_page(b.snapshot(data, PROJECT, TASK), PROJECT, TASK)
        self.assertEqual([e['entry_id'] for e in page['entries']],
                         [TASK + '-cearlier', TASK + '-clater'])

    def test_tied_timestamps_paginate_without_gaps_or_duplicate_entries(self):
        data = rows()
        data[0]['comments'] = [comment(n) for n in reversed(range(23))]
        snap = b.snapshot(data, PROJECT, TASK)
        cursor = None
        delivered = []
        while True:
            page = b.history_page(snap, PROJECT, TASK, limit=3, cursor=cursor)
            delivered.extend(e['entry_id'] for e in page['entries'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(delivered, [e['entry_id'] for e in snap['entries']])
        self.assertEqual(len(set(delivered)), 23)

    def test_since_requires_timezone_and_is_inclusive_with_offset(self):
        snap = b.snapshot(rows(), PROJECT, TASK)
        for invalid in ('2026-09-15', '2026-09-15T10:00:00', 'yesterday'):
            with self.assertRaises(ValueError):
                b.history_page(snap, PROJECT, TASK, since=invalid)
        page = b.history_page(snap, PROJECT, TASK, since='2026-09-15T06:00:00-04:00')
        self.assertEqual(page['total_entries'], 1)
        self.assertTrue(page['since_inclusive'])
        self.assertEqual(b.history_page(snap, PROJECT, TASK, since='2026-09-15T10:00:00.001Z')['total_entries'], 0)

    def test_large_unicode_and_control_text_reconstructs_exactly(self):
        data = rows()
        body = ('😀漢字\n\t"\\' * 300) + 'tail'
        data[0]['comments'] = [comment('huge', body), comment('last', 'following entry')]
        snap = b.snapshot(data, PROJECT, TASK)
        cursor = None
        reconstructed = {}
        for _ in range(200):
            page = b.history_page(snap, PROJECT, TASK, cursor=cursor, body_budget=256)
            self.assertTrue(page['entries'])
            self.assertLessEqual(sum(len(json.dumps(e['body'], ensure_ascii=False).encode()) - 2 for e in page['entries']), 256)
            for entry in page['entries']:
                previous = reconstructed.get(entry['entry_id'], '')
                self.assertEqual(entry['body_offset'], len(previous))
                reconstructed[entry['entry_id']] = previous + entry['body']
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertIsNone(cursor)
        self.assertEqual(reconstructed, {TASK + '-chuge': body, TASK + '-clast': 'following entry'})

    def test_malformed_cross_task_and_cross_snapshot_cursor_rejected(self):
        data = rows()
        data[0]['comments'].append(comment('second'))
        snap = b.snapshot(data, PROJECT, TASK)
        cursor = b.history_page(snap, PROJECT, TASK, limit=1)['next_cursor']
        decoded = b.untoken(cursor)
        bad = ['!', b.token([]), b.token(dict(decoded, task='trial-other')),
               b.token(dict(decoded, project='other')), b.token(dict(decoded, snapshot='0' * 64)),
               b.token(dict(decoded, index=-1)), b.token(dict(decoded, offset=True)),
               b.token(dict(decoded, index=999)), b.token(dict(decoded, extra=1))]
        for value in bad:
            with self.subTest(cursor=value), self.assertRaises(ValueError):
                b.history_page(snap, PROJECT, TASK, cursor=value)

    def test_cache_continuation_retains_snapshot_despite_live_edits_and_late_arrivals(self):
        data = rows()
        data[0]['comments'].append(comment('second', 'Original second'))
        calls = []
        def run(args):
            calls.append(args)
            return '\n'.join(json.dumps(row) for row in data)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            first = json.loads(b.execute(path, path, PROJECT, 'alice/session', 'history', [TASK, '--limit', '1'], {}, run))
            data[0]['comments'][1]['text'] = 'Edited second'
            data[0]['comments'].append(comment('late', 'Late arrival', '2026-09-14T00:00:00Z'))
            second = json.loads(b.execute(path, path, PROJECT, 'alice/session', 'history', [TASK, '--cursor', first['next_cursor']], {}, run))
            self.assertEqual(len(calls), 1)
            self.assertEqual(second['entries'][0]['body'], 'Original second')
            self.assertEqual(second['snapshot'], first['snapshot'])
            refreshed = json.loads(b.execute(path, path, PROJECT, 'alice/session', 'history', [TASK], {}, run))
            self.assertEqual(refreshed['total_entries'], 3)
            self.assertNotEqual(refreshed['snapshot'], first['snapshot'])
            self.assertIn('Edited second', [e['body'] for e in refreshed['entries']])
            cache = path / '.history-snapshots' / (first['snapshot'] + '.json')
            cache.unlink()
            with self.assertRaisesRegex(ValueError, 'unavailable/expired'):
                b.execute(path, path, PROJECT, 'alice/session', 'history', [TASK, '--cursor', first['next_cursor']], {}, run)


class ClientBriefingTests(unittest.TestCase):
    def test_cli_routes_brief_history_and_checkpoint_as_endpoint_actions(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'client.json'
            config.write_text('{}', encoding='utf-8')
            for action, suffix in [('brief', ['--json']), ('history', ['--limit', '3']),
                                   ('checkpoint', ['--file', 'checkpoint.json'])]:
                with self.subTest(action=action), patch.object(sys, 'argv',
                        ['client.py', '--config', str(config), '--project', PROJECT,
                         '--actor', 'alice/session', '--', action, TASK, *suffix]), \
                        patch.object(client, 'request', return_value=dict(stdout='', stderr='', returncode=0)) as request:
                    self.assertEqual(client.main(), 0)
                    self.assertEqual(request.call_args.args[3], [TASK, *suffix])
                    self.assertEqual(request.call_args.args[4], action)

    def test_checkpoint_file_is_transported_as_text_not_remote_file_path(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'checkpoint with spaces.json'
            contents = canonical_bytes(checkpoint(rows())).decode()
            path.write_text(contents, encoding='utf-8')
            wire = json.loads(client._wire(PROJECT, 'alice/session',
                              [TASK, '--file', str(path)], 'checkpoint', None))
            self.assertEqual(wire['args'], [TASK, '@attachment:0'])
            self.assertEqual(wire['attachments']['0'], dict(flag='--file', text=contents))


if __name__ == '__main__':
    unittest.main()
