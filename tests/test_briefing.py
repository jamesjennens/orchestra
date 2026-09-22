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


def comment(cid, text='A finding', stamp=STAMP, author='alice/session'):
    return dict(id=str(cid), text=text, author=author, created_at=stamp)


def rows():
    return [dict(id=TASK, title='A task', issue_type='task', status='in_progress',
                 assignee='alice/session', description='Intent', acceptance_criteria='Acceptance',
                 labels=[], comments=[comment('first')])]


def checkpoint(data, **changes):
    current, _ = b.checkpoints(data[0])
    snap = b.snapshot(data, PROJECT, TASK)
    result = dict(schema_version=1, task=TASK,
                  previous=str(current[1]['id']) if current else None,
                  activity_cursor=b.activity_cursor(snap),
                  source_commit='abc123', branch='work/feature', intent='Deliver the feature',
                  acceptance='Pass the agreed checks', summary='Implementation is underway',
                  next_action='Run the focused tests', open_items=[], resolved=[])
    result.update(changes)
    return result


def append_checkpoint(data, cid, p):
    data[0]['comments'].append(comment(cid, b.PREFIX + canonical_bytes(p).decode()))


def save_cp(data, cid, **changes):
    """Save a checkpoint through save_checkpoint (server-computed provenance) and
    append the stored comment. Returns the stored payload."""
    writes = []
    def run(args):
        writes.append(args)
        return json.dumps(dict(id=cid))
    p = checkpoint(data, **changes)
    b.save_checkpoint(data, PROJECT, TASK, p, 'alice/session', run)
    # The stored comment is what save_checkpoint wrote (with server provenance).
    stored_text = writes[0][3]
    data[0]['comments'].append(comment(cid, stored_text))
    return json.loads(stored_text[len(b.PREFIX):])


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
        self.assertEqual(result['comment_id'], 'cp1')
        self.assertTrue(result['reconciled'])
        self.assertGreater(result['bytes'], 0)

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


class NewerActivityTests(unittest.TestCase):
    """kittrial-5bb.1: a stale checkpoint must not hide newer directions."""

    def directed_rows(self, directions=3):
        """Analysis task, no contribution, an old no-action checkpoint, then
        `directions` later coordinator comments plus one later own comment.
        The checkpoint is saved through save_checkpoint so it carries the
        server-computed provenance (digests) of the activity it incorporates."""
        data = rows()
        data[0]['description'] = 'Analyze the options; no code contribution is expected.'
        save_cp(data, 'cp1', summary='Analysis complete; nothing is pending',
                next_action='No work outstanding; wait quietly')
        for n in range(directions):
            data[0]['comments'].append(comment(f'dir{n}', f'Coordinator direction {n}',
                                               f'2026-09-16T0{n}:00:00Z', author='coordinator/session'))
        data[0]['comments'].append(comment('own', 'Own working note', '2026-09-16T09:00:00Z'))
        return data

    def test_outstanding_directions_surface_with_entry_ids_and_qualified_next_action(self):
        data = self.directed_rows(3)
        result = b.brief(data, PROJECT, TASK)
        self.assertTrue(result['checkpoint']['newer_activity'])
        newer = result['newer']
        self.assertIsNotNone(newer)
        self.assertEqual(newer['other_count'], 3)
        self.assertEqual(newer['own_count'], 1)
        self.assertEqual([a['text'] for a in newer['other_authors']['items']], ['coordinator/session'])
        self.assertEqual(newer['other_authors']['omitted'], 0)
        self.assertEqual(newer['coverage'], 'snapshot')
        surfaced = {e['entry_id'] for e in newer['entries']}
        self.assertTrue({f'{TASK}-cdir{n}' for n in range(3)} <= surfaced)
        self.assertEqual(newer['history'], 'history ' + TASK)
        self.assertIn('--since ', newer['history_new'])
        self.assertTrue(all(e['author']['text'] for e in newer['entries']))
        self.assertIn('STALE CHECKPOINT', result['next_action'])
        self.assertIn('No work outstanding; wait quietly', result['next_action'])
        self.assertNotEqual(result['next_action'], 'No work outstanding; wait quietly')

    def test_reading_clears_nothing_and_checkpoint_stays_authoritative(self):
        data = self.directed_rows()
        before = b.activity_cursor(b.snapshot(data, PROJECT, TASK))
        first = b.brief(data, PROJECT, TASK)
        second = b.brief(data, PROJECT, TASK)
        self.assertEqual(b.activity_cursor(b.snapshot(data, PROJECT, TASK)), before)
        self.assertEqual(first['newer'], second['newer'])
        self.assertEqual(first['newer']['other_count'], 3)
        # A checkpoint retry against the same activity still reconciles instead of writing.
        replay = json.loads(data[0]['comments'][1]['text'][len(b.PREFIX):])
        retried = b.save_checkpoint(data, PROJECT, TASK, replay, 'alice/session',
                                    lambda _: self.fail('duplicate write'))
        self.assertEqual(retried['comment_id'], 'cp1')
        self.assertTrue(retried['reconciled'])

    def test_bounded_output_still_identifies_every_unincorporated_entry(self):
        data = self.directed_rows(3)
        for n in range(9):
            data[0]['comments'].append(comment(f'extra{n}', f'More direction {n}',
                                               '2026-09-17T00:00:00Z', author='coordinator/session'))
        data[0]['comments'].append(comment('late', 'Late backdated note', '2026-09-14T00:00:00Z',
                                           author='coordinator/session'))
        data[0]['comments'][0]['text'] = 'Edited original finding'
        result = b.brief(data, PROJECT, TASK)
        newer = result['newer']
        # Per-entry digests identify the late backdated note AND the edited
        # comment exactly, even though both predate/retain old timestamps.
        self.assertEqual(newer['other_count'], 13)
        self.assertEqual(newer['own_count'], 2)
        self.assertEqual(newer['fresh_count'], 14)
        self.assertEqual(newer['changed_or_late_count'], 1)
        self.assertEqual(len(newer['entries']), b.NEWER_MAX)
        self.assertEqual(newer['omitted'], 15 - b.NEWER_MAX)
        # Fresh entries fill the bounded excerpt first; the edited pre-checkpoint
        # comment is still counted exactly via its changed content digest.
        changed = [e for e in newer['entries'] if e['changed']]
        self.assertEqual(changed, [])
        # Direct summary with few fresh entries surfaces the changed one with its flag.
        small = self.directed_rows(1)
        small[0]['comments'][0]['text'] = 'Edited original finding'
        newer_small = b.brief(small, PROJECT, TASK)['newer']
        flagged = [e['entry_id'] for e in newer_small['entries'] if e['changed']]
        self.assertEqual(flagged, [f'{TASK}-cfirst'])
        self.assertEqual(newer_small['changed_or_late_count'], 1)

    def test_legacy_checkpoint_without_digests_reports_unknown_coverage(self):
        data = rows()
        legacy = dict(schema_version=1, task=TASK, previous=None,
                      activity_cursor=b.activity_cursor(b.snapshot(data, PROJECT, TASK)),
                      source_commit='abc123', branch='work/feature', intent='Deliver the feature',
                      acceptance='Pass the agreed checks', summary='Implementation is underway',
                      next_action='Run the focused tests', open_items=[], resolved=[])
        data[0]['comments'].append(comment('cp1', b.PREFIX + canonical_bytes(legacy).decode(),
                                           '2026-09-15T12:00:00Z'))
        data[0]['comments'].append(comment('dir0', 'Coordinator direction 0',
                                           '2026-09-16T00:00:00Z', author='coordinator/session'))
        # Sanity: the stored cursor covered only the original comment, so dir0 diverges.
        self.assertNotEqual(legacy['activity_cursor'],
                            b.activity_cursor(b.snapshot(data, PROJECT, TASK, 'cp1')))
        result = b.brief(data, PROJECT, TASK)
        newer = result['newer']
        self.assertIsNotNone(newer)
        self.assertEqual(newer['coverage'], 'unknown')
        self.assertIn('UNKNOWN', result['next_action'])
        self.assertIn('UNKNOWN', newer['note'])

    def test_no_checkpoint_and_current_checkpoint_have_no_newer_summary(self):
        self.assertIsNone(b.brief(rows(), PROJECT, TASK)['newer'])
        data = rows()
        append_checkpoint(data, 'cp1', checkpoint(data))
        result = b.brief(data, PROJECT, TASK)
        self.assertIsNone(result['newer'])
        self.assertNotIn('STALE CHECKPOINT', result['next_action'])
        text = b.format_brief(b.brief(self.directed_rows(), PROJECT, TASK))
        self.assertIn('coordinator/session', text)
        self.assertIn('history ' + TASK, text)
        # An unchanged checkpoint must NOT print the no-checkpoint line.
        quiet = b.format_brief(b.brief(data, PROJECT, TASK))
        self.assertIn('Checkpoint: cp1', quiet)
        self.assertNotIn('No checkpoint yet', quiet)

    def test_newer_summary_tracks_the_current_checkpoint_of_a_chain(self):
        data = rows()
        save_cp(data, 'cp1')
        data[0]['comments'].append(comment('mid', 'Middle direction', '2026-09-16T00:00:00Z',
                                           author='coordinator/session'))
        save_cp(data, 'cp2')
        data[0]['comments'].append(comment('after', 'Direction after cp2', '2026-09-18T00:00:00Z',
                                           author='coordinator/session'))
        newer = b.brief(data, PROJECT, TASK)['newer']
        self.assertIsNotNone(newer)
        entry_ids = {e['entry_id'] for e in newer['entries']}
        self.assertIn(f'{TASK}-cafter', entry_ids)
        self.assertNotIn(f'{TASK}-ccp1', entry_ids)
        self.assertNotIn(f'{TASK}-ccp2', entry_ids)
        self.assertNotIn(f'{TASK}-cmid', entry_ids)
        self.assertEqual(newer['other_count'], 1)


class ReviewV3Tests(unittest.TestCase):
    """kittrial-5bb.1 review 01a0bc6c: digest-binding, ack-not-resolution, compatibility-cap."""

    def writes(self):
        calls = []
        return calls, lambda args: (calls.append(args), json.dumps(dict(id='cpX')))[1]

    def test_fabricated_digests_are_rejected_and_correct_map_accepted(self):
        data = rows()
        snap = b.snapshot(data, PROJECT, TASK)
        real = dict(snap['entry_digests'])
        p = checkpoint(data)
        bads = [{'fabricated': '0' * 64}, {**real, 'extra': '1' * 64}]
        if real:
            first = next(iter(real))
            bads.append({**real, first: '0' * 64})
        for bad in bads:
            calls, run = self.writes()
            with self.subTest(bad=sorted(bad)), self.assertRaisesRegex(ValueError, 'does not match'):
                b.save_checkpoint(data, PROJECT, TASK, dict(p, incorporated_digests=bad), 'alice/session', run)
            self.assertEqual(calls, [])
        calls, run = self.writes()
        result = b.save_checkpoint(data, PROJECT, TASK, dict(p, incorporated_digests=real), 'alice/session', run)
        self.assertFalse(result['reconciled'])
        self.assertEqual(len(calls), 1)
        stored = json.loads(calls[0][3][len(b.PREFIX):])
        self.assertEqual(stored['provenance']['digests'], real)
        self.assertNotIn('incorporated_digests', stored)

    def test_omitted_digests_are_computed_server_side(self):
        data = rows()
        calls, run = self.writes()
        b.save_checkpoint(data, PROJECT, TASK, checkpoint(data), 'alice/session', run)
        stored = json.loads(calls[0][3][len(b.PREFIX):])
        self.assertEqual(stored['provenance']['covered'], 1)
        self.assertEqual(set(stored['provenance']['digests']), {f'{TASK}-cfirst'})

    def test_provenance_endpoint_supplies_cursor_and_map(self):
        data = rows()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            def run(args):
                return '\n'.join(json.dumps(r) for r in data)
            out = json.loads(b.execute(path, path, PROJECT, 'alice/session', 'checkpoint',
                                       [TASK, '--provenance'], {}, run))
            self.assertEqual(out['activity_cursor'], b.activity_cursor(b.snapshot(data, PROJECT, TASK)))
            self.assertEqual(set(out['provenance']['digests']), {f'{TASK}-cfirst'})
            self.assertEqual(out['provenance']['covered'], 1)
            p = checkpoint(data, provenance=out['provenance'])
            p['activity_cursor'] = out['activity_cursor']
            calls, run2 = self.writes()
            b.save_checkpoint(data, PROJECT, TASK, p, 'alice/session', run2)
            self.assertEqual(len(calls), 1)
            data[0]['comments'].append(comment('cp1', calls[0][3]))
            # The served verification path classifies the snapshot exactly.
            verified = json.loads(b.execute(path, path, PROJECT, 'alice/session', 'checkpoint',
                                            [TASK, '--verify'], {}, run))
            self.assertEqual(verified['coverage'], 'verified')
            self.assertEqual(verified['unchanged'], 1)


    def test_acknowledgement_is_not_resolution_and_edits_invalidate_it(self):
        data = rows()
        save_cp(data, 'cp0')
        data[0]['comments'].append(comment('dir0', 'Coordinator direction', '2026-09-16T00:00:00Z',
                                           author='coordinator/session'))
        # A checkpoint that incorporates the direction via digests only still leaves it outstanding.
        newer = b.brief(data, PROJECT, TASK)['newer']
        self.assertEqual(newer['unresolved_directions']['total'], 1)
        self.assertEqual(newer['unresolved_directions']['items'][0]['state'], 'unacknowledged')
        digest = b.snapshot(data, PROJECT, TASK)['entry_digests'][f'{TASK}-cdir0']
        save_cp(data, 'cp1', directions=[dict(id=f'{TASK}-cdir0', state='acknowledged', digest=digest)])
        after = b.brief(data, PROJECT, TASK)
        self.assertIsNone(after['newer'])
        self.assertEqual(after['directions']['total'], 1)
        self.assertEqual(after['directions']['items'][0]['state'], 'acknowledged')
        # Editing the direction invalidates the earlier acknowledgement.
        dir_comment = next(cm for cm in data[0]['comments'] if cm['id'] == 'dir0')
        dir_comment['text'] = 'Edited coordinator direction'
        revised = b.brief(data, PROJECT, TASK)
        self.assertTrue(revised['checkpoint']['newer_activity'])
        self.assertIn('outstanding direction', revised['next_action'])
        self.assertEqual(revised['newer']['unresolved_directions']['items'][0]['state'], 'acknowledged')
        # Only an explicit resolved/superseded disposition with matching digest clears it.
        edited = b.snapshot(data, PROJECT, TASK)['entry_digests'][f'{TASK}-cdir0']
        save_cp(data, 'cp2', directions=[dict(id=f'{TASK}-cdir0', state='resolved', digest=edited,
                                              note='Direction implemented', evidence='commit abc123')])
        self.assertEqual(b.brief(data, PROJECT, TASK)['directions']['total'], 0)
        # A resolution against a stale digest is rejected before any write.
        dir_comment['text'] = 'Re-edited coordinator direction'
        calls, run = self.writes()
        stale = checkpoint(data, directions=[dict(id=f'{TASK}-cdir0', state='resolved', digest=edited,
                                                  note='x', evidence='y')])
        with self.assertRaisesRegex(ValueError, 'does not match the incorporated entry'):
            b.save_checkpoint(data, PROJECT, TASK, stale, 'alice/session', run)
        self.assertEqual(calls, [])

    def test_wrong_state_or_missing_evidence_rejected(self):
        data = rows()
        digest = b.snapshot(data, PROJECT, TASK)['entry_digests'][f'{TASK}-cfirst']
        for dirs in ([dict(id=f'{TASK}-cfirst', state='done', digest=digest)],
                     [dict(id=f'{TASK}-cfirst', state='resolved', digest=digest)],
                     [dict(id=f'{TASK}-cfirst', state='acknowledged', digest='bad')]):
            with self.subTest(dirs=dirs), self.assertRaises(ValueError):
                b.validate_checkpoint(checkpoint(data, directions=dirs), TASK)

    def test_large_history_stays_bounded_and_template_roundtrips(self):
        data = rows()
        for n in range(250):
            data[0]['comments'].append(comment(f'c{n}', f'Comment {n}', '2026-09-17T00:00:00Z'))
        calls, run = self.writes()
        b.save_checkpoint(data, PROJECT, TASK, checkpoint(data), 'alice/session', run)
        stored_text = calls[0][3]
        stored = json.loads(stored_text[len(b.PREFIX):])
        self.assertLessEqual(len(stored['provenance']['digests']), b.DIGEST_WINDOW)
        self.assertEqual(stored['provenance']['covered'], 251)
        self.assertNotEqual(stored['provenance']['chain'], b.ZERO_HASH)
        self.assertLessEqual(len(stored_text.encode('utf-8')), 80 * 1024)
        # Served template roundtrip: the shipped template parses into a valid write.
        template = json.loads((Path(__file__).resolve().parents[1] / 'templates' / 'CHECKPOINT.json').read_text(encoding='utf-8'))
        payload = checkpoint(data, **{k: v for k, v in template.items() if k in
                                      ('source_commit', 'branch', 'intent', 'acceptance', 'summary', 'next_action')})
        b.validate_checkpoint(payload, TASK)
        # Legacy payload (pre-digest fields only) still validates for read/retry paths.
        b.validate_checkpoint(payload, TASK, require_digests=False)
        self.assertNotIn('incorporated_digests', payload)
        self.assertNotIn('provenance', payload)


class ReviewV4Tests(unittest.TestCase):
    """kittrial-5bb.1 review 01a0c65a: exact retry, provenance roundtrip, bounded coverage,
    direction continuity, final-record cap."""

    def writes(self):
        calls = []
        return calls, lambda args: (calls.append(args), json.dumps(dict(id='cpN')))[1]

    def many(self, count=251):
        data = rows()
        for n in range(count - 1):
            data[0]['comments'].append(comment(f'c{n}', f'Comment {n}', '2026-09-17T00:00:00Z'))
        return data

    def store(self, data, cid, **changes):
        """Save through save_checkpoint, append the stored comment, return payload."""
        calls, run = self.writes()
        b.save_checkpoint(data, PROJECT, TASK, checkpoint(data, **changes), 'alice/session', run)
        data[0]['comments'].append(comment(cid, calls[0][3]))
        return json.loads(calls[0][3][len(b.PREFIX):])

    def test_exact_original_request_retry_reconciles_without_second_write(self):
        data = rows()
        p = checkpoint(data)
        calls, run = self.writes()
        first = b.save_checkpoint(data, PROJECT, TASK, dict(p), 'alice/session', run)
        self.assertFalse(first['reconciled'])
        data[0]['comments'].append(comment('cp1', calls[0][3]))
        # Identical retry of the ORIGINAL request (no provenance fields at all).
        again, run2 = self.writes()
        retry = b.save_checkpoint(data, PROJECT, TASK, dict(p), 'alice/session', run2)
        self.assertTrue(retry['reconciled'])
        self.assertEqual(retry['comment_id'], 'cp1')
        self.assertEqual(again, [])
        # A legacy payload stored without provenance also reconciles exactly.
        legacy = rows()
        lp = checkpoint(legacy)
        append_checkpoint(legacy, 'cpL', lp)
        retried = b.save_checkpoint(legacy, PROJECT, TASK, dict(lp), 'alice/session', lambda _: self.fail('write'))
        self.assertTrue(retried['reconciled'])


    def test_provenance_roundtrip_at_and_beyond_the_window_boundary(self):
        data = self.many(251)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            def run(args):
                return '\n'.join(json.dumps(r) for r in data)
            out = json.loads(b.execute(path, path, PROJECT, 'alice/session', 'checkpoint',
                                       [TASK, '--provenance'], {}, run))
            self.assertEqual(out['provenance']['covered'], 251)
            self.assertLessEqual(len(out['provenance']['digests']), b.DIGEST_WINDOW)
            p = checkpoint(data, provenance=out['provenance'])
            p['activity_cursor'] = out['activity_cursor']
            calls, run2 = self.writes()
            result = b.save_checkpoint(data, PROJECT, TASK, p, 'alice/session', run2)
            self.assertEqual(result['covered'], 251)
            self.assertEqual(len(calls), 1)
            # A fabricated map is still rejected at and beyond the boundary.
            bad = dict(p, provenance=dict(out['provenance'], covered=999))
            with self.assertRaisesRegex(ValueError, 'does not match'):
                b.save_checkpoint(data, PROJECT, TASK, bad, 'alice/session', lambda _: self.fail('write'))

    def test_unchanged_old_history_is_not_reported_fresh_and_verify_finds_old_edits(self):
        data = self.many(251)
        self.store(data, 'cp1')
        data[0]['title'] = 'A task (retitled)'
        result = b.brief(data, PROJECT, TASK)
        self.assertTrue(result['checkpoint']['newer_activity'])
        newer = result['newer']
        self.assertEqual(newer['coverage'], 'windowed')
        self.assertEqual(newer['other_count'], 0)
        self.assertEqual(newer['fresh_count'], 0)
        self.assertEqual(newer['unverified_count'], 0)
        self.assertIn('WINDOWED', newer['note'])
        data[0]['comments'][1]['text'] = 'Edited old comment'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            def run(args):
                return '\n'.join(json.dumps(r) for r in data)
            verified = json.loads(b.execute(path, path, PROJECT, 'alice/session', 'checkpoint',
                                            [TASK, '--verify'], {}, run))
            self.assertEqual(verified['recorded_coverage'], 'windowed')
            self.assertEqual(verified['changed'], 1)
            self.assertEqual(verified['changed_entry_ids'], [f'{TASK}-cc0'])
            self.assertEqual(verified['unchanged'], 250)
        data[0]['comments'].append(comment('backdated', 'Backdated addition', '2026-09-10T00:00:00Z'))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            def run(args):
                return '\n'.join(json.dumps(r) for r in data)
            verified = json.loads(b.execute(path, path, PROJECT, 'alice/session', 'checkpoint',
                                            [TASK, '--verify'], {}, run))
            self.assertEqual(verified['fresh'], 1)
            self.assertEqual(verified['changed'], 1)
            backdated = b.brief(data, PROJECT, TASK)['newer']
            self.assertEqual(backdated['fresh_count'], 1)
            self.assertEqual(backdated['unverified_count'], 0)


    def test_outstanding_directions_visible_when_current_and_dispositions_persist(self):
        import work
        data = rows()
        self.store(data, 'cp0')
        data[0]['comments'].append(comment('dir0', 'Coordinator direction', '2026-09-16T00:00:00Z',
                                           author='coordinator/session'))
        digest = b.snapshot(data, PROJECT, TASK)['entry_digests'][f'{TASK}-cdir0']
        self.store(data, 'cp1', directions=[dict(id=f'{TASK}-cdir0', state='acknowledged', digest=digest)])
        current = b.brief(data, PROJECT, TASK)
        self.assertIsNone(current['newer'])
        self.assertEqual(current['directions']['total'], 1)
        text = b.format_brief(current)
        self.assertIn('Outstanding directions: 1', text)
        self.assertIn(f'{TASK}-cdir0', text)
        self.assertIn('OUTSTANDING DIRECTIONS', current['next_action'])
        item = work.queue(data, 'alice/session', ['--mine'])['items'][0]
        self.assertEqual(item['unresolved_directions'], 1)
        # An ordinary checkpoint (no directions field) must not drop the disposition.
        self.store(data, 'cp2')
        after = b.brief(data, PROJECT, TASK)
        self.assertEqual(after['directions']['total'], 1)
        self.assertEqual(after['directions']['items'][0]['state'], 'acknowledged')
        # Resolution persists across a later ordinary checkpoint too.
        self.store(data, 'cp3', directions=[dict(id=f'{TASK}-cdir0', state='resolved', digest=digest,
                                                 note='Implemented', evidence='commit abc')])
        self.store(data, 'cp4')
        self.assertEqual(b.brief(data, PROJECT, TASK)['directions']['total'], 0)
        # An edit still invalidates the recorded disposition.
        idx = next(i for i, c in enumerate(data[0]['comments']) if c['id'] == 'dir0')
        data[0]['comments'][idx]['text'] = 'Edited'
        self.assertEqual(b.brief(data, PROJECT, TASK)['directions']['total'], 1)

    def test_final_record_stays_within_cap_and_over_cap_is_rejected_without_write(self):
        data = self.many(251)
        big = checkpoint(data, open_items=[dict(id=f'item-{n}', kind='blocker',
                                               text='x' * 400, source='s' * 240) for n in range(100)])
        calls, run = self.writes()
        result = b.save_checkpoint(data, PROJECT, TASK, big, 'alice/session', run)
        self.assertLessEqual(result['bytes'], b.RECORD_MAX)
        self.assertEqual(len(calls), 1)
        stored = calls[0][3]
        payload = json.loads(stored[len(b.PREFIX):])
        self.assertEqual(payload['provenance']['covered'], 251)
        self.assertLessEqual(len(payload['provenance']['digests']), b.DIGEST_WINDOW)
        # The accepted write reads back as a valid checkpoint.
        data[0]['comments'].append(comment('cp1', stored))
        current, invalid = b.checkpoints(data[0])
        self.assertEqual(invalid, [])
        self.assertEqual(current[1]['id'], 'cp1')
        # Server-written records are bounded by the same cap the reader enforces.
        self.assertLessEqual(len(stored.encode('utf-8')) + len(b.PREFIX.encode('utf-8')), b.RECORD_MAX)
        # A payload that cannot fit even with an empty window is refused before any
        # write, with actionable size guidance.
        oversized = dict(checkpoint(data), summary='z' * 1000, acceptance='a' * 1000, intent='i' * 600,
                         next_action='n' * 600,
                         open_items=[dict(id=f'h-{n}', kind='blocker', text='y' * 400, source='s' * 240) for n in range(100)],
                         resolved=[dict(id=f'r-{n}', reason='q' * 400, evidence='e' * 240) for n in range(100)])
        with self.assertRaisesRegex(ValueError, 'record cap'):
            b.save_checkpoint(data, PROJECT, TASK, oversized, 'alice/session', lambda _: self.fail('write'))
        # And the fitting helper refuses a body that cannot fit any window.
        with self.assertRaisesRegex(ValueError, 'record cap'):
            b.fit_provenance({'task': TASK, 'pad': 'p' * (b.RECORD_MAX + 1)}, {'a': '0' * 64})


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
