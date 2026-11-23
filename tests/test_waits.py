"""Slice 1 tests for server-computed waits, silent lanes, and daily metrics (kittrial-5bb.598).

Covers all Slice 1 adversarial cases from design-598-r2b.md §7.2 (#1-#12, #14, #18-#25, #30, #33)
plus review-598r2b.md Conditions 1, 2, and 6. Every test passes an explicit ``now=`` and never
compares a hard-coded date against the real wall clock.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing as b
import lane_state
import lanes
import review_workflow as rw
import waits
from requirements import canonical_bytes

PROJECT = 'kittrial'
BASE_NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)


def actor_id(seed):
    return 'session-' + str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


LANE_A = actor_id('lane-a')
LANE_B = actor_id('lane-b')
COORD = actor_id('coord-1')
REVIEWER = actor_id('rev-1')
OTHER = actor_id('other-1')
OWNER_HUMAN = actor_id('owner-james')


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def make_registry(entries, owners=None, sessions=None):
    records = {}
    for name, act in entries:
        rid = str(uuid.uuid5(uuid.NAMESPACE_URL, name + '|' + act))
        records[rid] = {'request_id': rid, 'actor': act, 'name': name,
                        'created_at': iso(BASE_NOW - timedelta(days=5))}
    reg = {'schema_version': 1, 'records': records}
    if owners is not None:
        reg['owners'] = dict(owners)
    if sessions is not None:
        reg['sessions'] = dict(sessions)
    return reg


def make_comment(cid, text, author, when):
    return {'id': cid, 'text': text, 'author': author, 'created_at': iso(when) if isinstance(when, datetime) else when}


def make_task(task_id, assignee, comments=None, status='in_progress', labels=None,
              created_at=None, updated_at=None, closed_at=None):
    created = created_at or (BASE_NOW - timedelta(hours=10))
    updated = updated_at or created
    row = {
        'id': task_id,
        'title': 'Task ' + task_id,
        'issue_type': 'task',
        'status': status,
        'assignee': assignee,
        'description': 'Intent',
        'acceptance_criteria': 'Acceptance',
        'labels': list(labels or []),
        'priority': 2,
        'created_by': assignee or COORD,
        'created_at': iso(created) if isinstance(created, datetime) else created,
        'updated_at': iso(updated) if isinstance(updated, datetime) else updated,
        'dependencies': [],
        'comments': list(comments or []),
    }
    if closed_at is not None:
        row['closed_at'] = iso(closed_at) if isinstance(closed_at, datetime) else closed_at
    return row


def add_checkpoint(row, cid, when, author=None, previous=None, with_provenance=False, **changes):
    snap = b.snapshot([row], PROJECT, row['id'])
    payload = {
        'schema_version': 1,
        'task': row['id'],
        'previous': previous,
        'activity_cursor': b.activity_cursor(snap),
        'source_commit': '',
        'branch': '',
        'intent': 'intent',
        'acceptance': 'acceptance',
        'summary': 'summary',
        'next_action': 'continue work',
        'open_items': [],
        'resolved': [],
    }
    payload.update(changes)
    if with_provenance:
        payload, _ = b.fit_provenance(payload, snap['entry_digests'])
    who = author if author is not None else row['assignee']
    row['comments'].append(make_comment(cid, b.PREFIX + canonical_bytes(payload).decode(), who, when))
    return payload


def add_lane_state(row, cid, checkpoint_id, when, author=None, **changes):
    payload = {
        'schema_version': 1,
        'task': row['id'],
        'checkpoint': checkpoint_id,
        'state': 'waiting-on-coordinator',
        'waiting_on': 'coordinator',
    }
    payload.update(changes)
    who = author if author is not None else row['assignee']
    row['comments'].append(make_comment(cid, lane_state.PREFIX + canonical_bytes(payload).decode(), who, when))
    return payload


def add_review_record(row, cid, when, author, payload):
    body = rw.PREFIX + canonical_bytes(payload).decode()
    row['comments'].append(make_comment(cid, body, author, when))


def add_contribution(row, cid, when, author=None, op_id='op-c1'):
    who = author if author is not None else row['assignee']
    add_review_record(row, cid, when, who, {
        'schema_version': 1,
        'operation': 'contribute',
        'operation_id': op_id,
        'task': row['id'],
        'previous': None,
        'supersedes': None,
        'follows': None,
        'repository': 'https://example.org/repo.git',
        'commit': '1' * 40,
        'base_commit': '2' * 40,
        'delivery': {'kind': 'bundle', 'path': '/tmp/x.bundle', 'sha256': '3' * 64},
        'summary': 'delivered',
    })


def default_reg():
    return make_registry(
        [('lane-a', LANE_A), ('lane-b', LANE_B), ('coord', COORD), ('rev', REVIEWER), ('james', OWNER_HUMAN)],
        owners={
            LANE_A: 'lane:agyneon',
            LANE_B: 'lane:agyargon',
            COORD: 'person:coord',
            OWNER_HUMAN: 'person:james',
        },
    )


class WaitAnswerTests(unittest.TestCase):
    """Tests #1, #2, #3, #4, #20c (including Condition 1)."""

    def test_answer_by_wrong_actor_does_not_clear_wait(self):
        t0 = BASE_NOW - timedelta(hours=5)
        row = make_task('t-1', LANE_A, created_at=t0)
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        row['comments'].append(make_comment('c-other', 'VERIFIED', OTHER, t0 + timedelta(hours=1)))
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res['waits']), 1)
        self.assertEqual(res['waits'][0]['task'], 't-1')

    def test_holders_own_comment_never_answers_wait(self):
        t0 = BASE_NOW - timedelta(hours=5)
        row = make_task('t-2', LANE_A, created_at=t0)
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        row['comments'].append(make_comment('c-own', 'VERIFIED', LANE_A, t0 + timedelta(hours=1)))
        # Even if holder were listed as operator, same_activity_actor excludes own comment.
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD, LANE_A])
        self.assertEqual(len(res['waits']), 1)

    def test_answer_before_checkpoint_does_not_clear_wait(self):
        t0 = BASE_NOW - timedelta(hours=5)
        row = make_task('t-3', LANE_A, created_at=t0 - timedelta(hours=1))
        row['comments'].append(make_comment('c-prior', 'VERIFIED', COORD, t0 - timedelta(minutes=30)))
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res['waits']), 1)

    def test_coordinator_comment_on_other_task_does_not_clear_wait(self):
        t0 = BASE_NOW - timedelta(hours=5)
        row_a = make_task('t-a', LANE_A, created_at=t0)
        add_checkpoint(row_a, 'cp1', t0)
        add_lane_state(row_a, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        row_b = make_task('t-b', LANE_B, created_at=t0)
        row_b['comments'].append(make_comment('c-b', 'VERIFIED', COORD, t0 + timedelta(hours=1)))
        res = waits.compute_waits([row_a, row_b], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual([w['task'] for w in res['waits']], ['t-a'])

    def test_acknowledgement_does_not_answer_wait_while_verdict_does(self):
        """#20c + Condition 1: case-insensitive header & first ': ' anchor."""
        # Non-verdicts (must stay open)
        non_verdicts = [
            'received',
            'under review',
            'CI started',
            'Coordinator: received r2; for .511: VERIFIED earlier',
            'Coordinator: received r1; see .511 VERIFIED',
            'Kittrial coordinator, 2026-11-23 05:16Z: Slice 0 received (f4f8d602). Not a verdict yet.',
            'Coordinator: VERIFICATION pending',
            'Coordinator: APPROVEDX not a token',
        ]
        # Verdicts (must close the wait)
        verdicts = [
            'Kittrial coordinator, 2026-11-23 00:52Z: REVISE',
            'VERIFIED',
            'VERIFIED WITH CONDITIONS. Build Slice 0.',
            'Coordinator, 2026-11-22 22:35Z: **VERIFIED** plan',
            'Coordinator, 2026-11-23 (00:52Z): REQUEST CHANGES on Slice 0',
            'PM: ANSWER — use option B',
            'REVISE, design round 3, delta only.',
            'Reviewer: APPROVED',
        ]
        t0 = BASE_NOW - timedelta(hours=5)
        for idx, text in enumerate(non_verdicts):
            with self.subTest(non_verdict=text):
                self.assertFalse(waits.is_verdict_body(text))
                row = make_task('t-nv-%d' % idx, LANE_A, created_at=t0)
                add_checkpoint(row, 'cp1', t0)
                add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
                row['comments'].append(make_comment('c1', text, COORD, t0 + timedelta(minutes=30)))
                res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
                self.assertEqual(len(res['waits']), 1, 'ack wrongly closed wait: %r' % text)
        for idx, text in enumerate(verdicts):
            with self.subTest(verdict=text):
                self.assertTrue(waits.is_verdict_body(text))
                row = make_task('t-v-%d' % idx, LANE_A, created_at=t0)
                add_checkpoint(row, 'cp1', t0)
                add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
                row['comments'].append(make_comment('c1', text, COORD, t0 + timedelta(minutes=30)))
                res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
                self.assertEqual(res['waits'], [], 'verdict failed to close wait: %r' % text)


class WaitStatusTests(unittest.TestCase):
    """Tests #5, #6, #7, #12, unassigned tasks."""

    def test_open_status_task_with_lane_state_produces_wait(self):
        t0 = BASE_NOW - timedelta(hours=6)
        row = make_task('t-open', LANE_A, status='open', created_at=t0)
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        out = waits.read([row], COORD, default_reg(), ['--on-me'], now=BASE_NOW, operators=[COORD], project=PROJECT)
        self.assertEqual([w['task'] for w in out['items']], ['t-open'])
        self.assertTrue(out['items'][0]['overdue'])

    def test_blocked_task_keeps_open_wait(self):
        t0 = BASE_NOW - timedelta(hours=6)
        row = make_task('t-blk', LANE_A, status='blocked', created_at=t0)
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res['waits']), 1)
        self.assertEqual(res['waits'][0]['status'], 'blocked')

    def test_closed_task_with_stale_lane_state_produces_no_wait_or_alarm(self):
        t0 = BASE_NOW - timedelta(hours=10)
        row = make_task('t-cls', LANE_A, status='closed', created_at=t0, closed_at=t0 + timedelta(hours=6))
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(res['waits'], [])
        self.assertEqual(res['silent'], [])
        metrics = waits.compute_daily_metrics([row], now=BASE_NOW, days=7, project=PROJECT,
                                              registry=default_reg(), operators=[COORD])
        self.assertEqual(metrics['by_target'][0]['closed_unanswered']['count'], 1)

    def test_labels_and_status_cannot_hide_overdue_wait(self):
        t0 = BASE_NOW - timedelta(hours=8)
        row = make_task('t-lbl', LANE_A, status='open', labels=['ignore', 'deferred', 'paused'], created_at=t0)
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res['waits']), 1)
        self.assertTrue(res['waits'][0]['overdue'])

    def test_unassigned_task_produces_no_wait(self):
        t0 = BASE_NOW - timedelta(hours=8)
        row = make_task('t-un', None, status='open', created_at=t0)
        add_checkpoint(row, 'cp1', t0, author=OTHER, next_action='coordinator review')
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(res['waits'], [])
        self.assertEqual(res['silent'], [])

    def test_standalone_holder_lane_state_without_checkpoint_chain_is_honored_and_answered_by_verdict(self):
        t0 = BASE_NOW - timedelta(hours=6)
        row = make_task('t-standalone', LANE_A, created_at=t0)
        row['comments'].append(make_comment('c-anchor', 'plain status anchor', LANE_A, t0))
        add_lane_state(row, 'ls1', 'c-anchor', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res['waits']), 1)
        self.assertEqual(res['waits'][0]['waiting_on'], 'coordinator')
        # Acknowledgement keeps it open; verdict closes it.
        row['comments'].append(make_comment('c-ack', 'received, looking', COORD, t0 + timedelta(hours=1)))
        res2 = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res2['waits']), 1)
        row['comments'].append(make_comment('c-ver', 'Kittrial coordinator, 2026-11-23 00:52Z: VERIFIED',
                                            COORD, t0 + timedelta(hours=2)))
        res3 = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(res3['waits'], [])


class WaitPrecedenceAndSourceTests(unittest.TestCase):
    """Tests #8, #9, inferred decision items, and lanes.py delegation."""

    def test_newer_checkpoint_dropping_wait_clears_prior_wait(self):
        t0 = BASE_NOW - timedelta(hours=6)
        # Case 1: explicit state='working'
        row1 = make_task('t-work', LANE_A, created_at=t0)
        add_checkpoint(row1, 'cp1', t0)
        add_lane_state(row1, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        add_checkpoint(row1, 'cp2', t0 + timedelta(hours=2), previous='cp1')
        add_lane_state(row1, 'ls2', 'cp2', t0 + timedelta(hours=2), state='working', waiting_on=None)
        res1 = waits.compute_waits([row1], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(res1['waits'], [])

        # Case 2: Case C3 (no paired lane_state, cleared open_items, neutral next_action)
        row2 = make_task('t-c3', LANE_A, created_at=t0)
        add_checkpoint(row2, 'cp1', t0)
        add_lane_state(row2, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        add_checkpoint(row2, 'cp2', t0 + timedelta(hours=2), previous='cp1', next_action='run tests locally', open_items=[])
        res2 = waits.compute_waits([row2], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(res2['waits'], [])

        # Lanes board also clears stale waiting flags (gap 1 of §1.1).
        entry = lanes.held_task(row2, 't-c3', LANE_A, BASE_NOW, 1800, operators=[COORD])
        self.assertNotIn('waiting_too_long', entry['flags'])
        self.assertNotIn('expect_by_passed', entry['flags'])

    def test_plain_comment_without_checkpoint_creates_no_wait_caught_by_silent(self):
        t0 = BASE_NOW - timedelta(hours=6)
        row = make_task('t-prose', LANE_A, created_at=t0)
        row['comments'].append(make_comment('c1', 'Hey coordinator, what should I do?', LANE_A, t0))
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(res['waits'], [])
        self.assertEqual([s['task'] for s in res['silent']], ['t-prose'])

    def test_inferred_decision_open_item_and_page_eligibility(self):
        t0 = BASE_NOW - timedelta(hours=5)
        row_dec = make_task('t-dec', LANE_A, created_at=t0)
        add_checkpoint(row_dec, 'cp1', t0, open_items=[{'id': 'd1', 'kind': 'decision', 'text': 'pick A or B', 'source': 'plan'}])
        row_blk = make_task('t-blk-only', LANE_B, created_at=t0)
        add_checkpoint(row_blk, 'cp1', t0, open_items=[{'id': 'b1', 'kind': 'blocker', 'text': 'external dep', 'source': 'ci'}],
                       next_action='continue locally')
        res = waits.compute_waits([row_dec, row_blk], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        by_t = {w['task']: w for w in res['waits']}
        self.assertTrue(by_t['t-dec']['inferred'])
        self.assertTrue(by_t['t-dec']['page_eligible'])
        self.assertEqual(by_t['t-dec']['waiting_on'], 'coordinator')
        self.assertTrue(by_t['t-blk-only']['inferred'])
        self.assertFalse(by_t['t-blk-only']['page_eligible'])


class WaitClockTests(unittest.TestCase):
    """Tests #10 and #30."""

    def test_past_expect_by_flags_overdue_and_clamps_escalation_base(self):
        t0 = BASE_NOW - timedelta(hours=1)
        row = make_task('t-skew', LANE_A, created_at=t0 - timedelta(hours=1))
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator',
                       expect_by=iso(t0 - timedelta(hours=2)))
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        w = res['waits'][0]
        self.assertTrue(w['overdue'])
        self.assertTrue(w['expect_by_before_checkpoint'])
        self.assertEqual(w['escalation_base'], iso(t0))

    def test_missing_or_garbage_expect_by_defaults_to_4h(self):
        t0 = BASE_NOW - timedelta(hours=5)
        row_missing = make_task('t-miss', LANE_A, created_at=t0)
        add_checkpoint(row_missing, 'cp1', t0)
        add_lane_state(row_missing, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')

        row_garbage = make_task('t-garb', LANE_B, created_at=t0)
        add_checkpoint(row_garbage, 'cp1', t0)
        add_lane_state(row_garbage, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator',
                       expect_by='not-a-timestamp')

        res = waits.compute_waits([row_missing, row_garbage], now=BASE_NOW, project=PROJECT,
                                  registry=default_reg(), operators=[COORD])
        by_t = {w['task']: w for w in res['waits']}
        self.assertEqual(by_t['t-miss']['expect_by'], iso(t0 + timedelta(hours=4)))
        self.assertTrue(by_t['t-miss']['expect_by_defaulted'])
        self.assertEqual(by_t['t-garb']['expect_by'], iso(t0 + timedelta(hours=4)))
        self.assertIn('expect_by_unparseable', by_t['t-garb']['warnings'])

    def test_far_future_expect_by_clamped_to_72h(self):
        t0 = BASE_NOW - timedelta(hours=1)
        row = make_task('t-far', LANE_A, created_at=t0)
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator',
                       expect_by=iso(t0 + timedelta(days=365)))
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        w = res['waits'][0]
        self.assertTrue(w['expect_by_clamped'])
        self.assertEqual(w['expect_by'], iso(t0 + timedelta(hours=72)))


class WaitAnchorAndEpisodeTests(unittest.TestCase):
    """Tests #14, #19, #20a, #20b, #21a, #21b, #21c."""

    def test_two_lanes_waiting_on_one_coordinator_oldest_first(self):
        t_old = BASE_NOW - timedelta(hours=8)
        t_new = BASE_NOW - timedelta(hours=5)
        row1 = make_task('t-new', LANE_A, created_at=t_new)
        add_checkpoint(row1, 'cp1', t_new)
        add_lane_state(row1, 'ls1', 'cp1', t_new, state='waiting-on-coordinator', waiting_on='coordinator')
        row2 = make_task('t-old', LANE_B, created_at=t_old)
        add_checkpoint(row2, 'cp1', t_old)
        add_lane_state(row2, 'ls1', 'cp1', t_old, state='waiting-on-coordinator', waiting_on='coordinator')
        res = waits.compute_waits([row1, row2], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual([w['task'] for w in res['waits']], ['t-old', 't-new'])

        # Answering t-old leaves t-new aging
        row2['comments'].append(make_comment('v1', 'VERIFIED', COORD, t_old + timedelta(hours=1)))
        res2 = waits.compute_waits([row1, row2], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual([w['task'] for w in res2['waits']], ['t-new'])

    def test_non_holder_checkpoint_does_not_clear_or_reage_wait(self):
        t0 = BASE_NOW - timedelta(hours=6)
        row = make_task('t-19', LANE_A, created_at=t0)
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator',
                       expect_by=iso(t0 + timedelta(hours=2)))
        # Third-party checkpoint (by COORD or REVIEWER) with state='working' must NOT clear or re-age!
        add_checkpoint(row, 'cp2', t0 + timedelta(hours=3), author=COORD, previous='cp1', next_action='unrelated')
        add_lane_state(row, 'ls2', 'cp2', t0 + timedelta(hours=3), author=COORD, state='working', waiting_on=None)
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res['waits']), 1)
        self.assertEqual(res['waits'][0]['origin_checkpoint'], 'cp1')
        self.assertEqual(res['waits'][0]['started_at'], iso(t0))

    def test_answer_listed_out_of_native_order_is_still_found(self):
        t0 = BASE_NOW - timedelta(hours=6)
        row = make_task('t-20a', LANE_A, created_at=t0)
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        # Insert newer verdict at index 0 (out of native comment list order)
        row['comments'].insert(0, make_comment('c-verdict', 'VERIFIED', COORD, t0 + timedelta(hours=1)))
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(res['waits'], [])

    def test_backdated_late_answer_seen_by_both_whose_move_and_waits(self):
        """#20b (Change 1): unincorporated backdated verdict seen by BOTH assignee_comment_pending and waits.py."""
        t0 = BASE_NOW - timedelta(hours=6)
        row = make_task('t-20b', LANE_A, created_at=t0 - timedelta(hours=2))
        add_checkpoint(row, 'cp1', t0, with_provenance=True)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        # Late arrival backdated BEFORE cp1, NOT in cp1's provenance digests
        row['comments'].append(make_comment('c-late', 'VERIFIED', COORD, t0 - timedelta(minutes=15)))
        auth = lambda who: who == COORD
        self.assertTrue(b.assignee_comment_pending(row, authority=auth))
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(res['waits'], [])

    def test_recheckpointing_same_wait_every_30m_for_43h_keeps_episode_age(self):
        """#21a (MUST-2): 86 re-checkpoints keep origin started_at, ignore later expect_by, 1 metric sample."""
        t0 = BASE_NOW - timedelta(hours=43)
        row = make_task('t-43h', LANE_A, created_at=t0)
        prev = None
        for i in range(10):
            when = t0 + timedelta(hours=i * 4)
            cid = 'cp%d' % i
            add_checkpoint(row, cid, when, previous=prev)
            add_lane_state(row, 'ls%d' % i, cid, when, state='waiting-on-coordinator', waiting_on='coordinator',
                           expect_by=iso(when + timedelta(hours=4)))
            prev = cid
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res['waits']), 1)
        w = res['waits'][0]
        self.assertEqual(w['origin_checkpoint'], 'cp0')
        self.assertEqual(w['latest_checkpoint'], 'cp9')
        self.assertEqual(w['started_at'], iso(t0))
        self.assertEqual(w['age_seconds'], 43 * 3600)
        self.assertEqual(w['expect_by'], iso(t0 + timedelta(hours=4)))
        self.assertTrue(w['expect_by_extension_ignored'])

    def test_idle_stop_checkpoint_preserves_open_episode_and_started_at(self):
        """#21b (Change 3): wait -> idle-stop (state: idle) -> wait again preserves started_at & episode_id."""
        t0 = BASE_NOW - timedelta(hours=14)
        row = make_task('t-idle', LANE_A, created_at=t0)
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        res1 = waits.compute_waits([row], now=t0 + timedelta(hours=1), project=PROJECT, registry=default_reg(), operators=[COORD])
        eid1 = res1['waits'][0]['episode_id']

        # 12h idle-stop checkpoint with state='idle'
        add_checkpoint(row, 'cp2', t0 + timedelta(hours=12), previous='cp1')
        add_lane_state(row, 'ls2', 'cp2', t0 + timedelta(hours=12), state='idle', waiting_on=None)
        res2 = waits.compute_waits([row], now=t0 + timedelta(hours=13), project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res2['waits']), 1)
        self.assertEqual(res2['waits'][0]['episode_id'], eid1)
        self.assertEqual(res2['waits'][0]['started_at'], iso(t0))

        # Resume and wait again
        add_checkpoint(row, 'cp3', t0 + timedelta(hours=13), previous='cp2')
        add_lane_state(row, 'ls3', 'cp3', t0 + timedelta(hours=13), state='waiting-on-coordinator', waiting_on='coordinator')
        res3 = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(res3['waits'][0]['episode_id'], eid1)
        self.assertEqual(res3['waits'][0]['started_at'], iso(t0))

    def test_alternating_waiting_on_preserves_task_wait_age_and_flags_churn(self):
        """#21c (Change 4): alternating coordinator <-> reviewer without verdict keeps started_at & target_churn=True."""
        t0 = BASE_NOW - timedelta(hours=10)
        row = make_task('t-churn', LANE_A, created_at=t0)
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        add_checkpoint(row, 'cp2', t0 + timedelta(hours=3), previous='cp1')
        add_lane_state(row, 'ls2', 'cp2', t0 + timedelta(hours=3), state='waiting-on-review', waiting_on='reviewer')
        add_checkpoint(row, 'cp3', t0 + timedelta(hours=6), previous='cp2')
        add_lane_state(row, 'ls3', 'cp3', t0 + timedelta(hours=6), state='waiting-on-coordinator', waiting_on='coordinator')
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res['waits']), 1)
        w = res['waits'][0]
        self.assertEqual(w['started_at'], iso(t0))
        self.assertEqual(w['origin_checkpoint'], 'cp1')
        self.assertEqual(w['target_switches'], 2)
        self.assertTrue(w['target_churn'])


class DeliveryWaitTests(unittest.TestCase):
    """Tests #22 and #23 (MUST-3, Change 5)."""

    def test_delivered_work_with_lane_state_working_and_ack_no_review_for_30h_is_overdue(self):
        """#22: pending delivery + lane_state working + coordinator 'received, reviewing' ack stays overdue!"""
        t0 = BASE_NOW - timedelta(hours=30)
        row = make_task('t-deliv', LANE_A, created_at=t0 - timedelta(hours=1))
        add_contribution(row, 'c-contrib', t0, author=LANE_A)
        add_checkpoint(row, 'cp1', t0 + timedelta(minutes=1))
        add_lane_state(row, 'ls1', 'cp1', t0 + timedelta(minutes=1), state='working', waiting_on=None)
        # Coordinator acknowledgement comment (which flips work.py:384-391 next_actor to assignee!)
        row['comments'].append(make_comment('c-ack', 'received, reviewing CI now', COORD, t0 + timedelta(hours=1)))
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res['waits']), 1)
        w = res['waits'][0]
        self.assertEqual(w['source'], 'delivery-awaiting-review')
        self.assertEqual(w['waiting_on'], 'reviewer')
        self.assertTrue(w['overdue'])
        self.assertEqual(w['age_seconds'], 30 * 3600)

    def test_named_pending_review_request_creates_wait_on_named_reviewer(self):
        """#23: named pending_review_requests targets named reviewer independent of comment_pending."""
        t0 = BASE_NOW - timedelta(hours=6)
        row = make_task('t-req', LANE_A, created_at=t0 - timedelta(hours=1))
        add_contribution(row, 'c-contrib', t0, author=LANE_A)
        add_review_record(row, 'c-req', t0 + timedelta(minutes=5), COORD, {
            'schema_version': 1,
            'operation': 'request-review',
            'operation_id': 'op-req-1',
            'task': 't-req',
            'previous': 'c-contrib',
            'contribution': 'c-contrib',
            'reviewer': REVIEWER,
            'summary': 'Please review slice 1',
        })
        row['comments'].append(make_comment('c-ack', 'received, taking a look', REVIEWER, t0 + timedelta(hours=1)))
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        req_waits = [w for w in res['waits'] if w['source'] == 'review-request']
        self.assertEqual(len(req_waits), 1)
        self.assertEqual(req_waits[0]['waiting_on'], REVIEWER)
        self.assertTrue(req_waits[0]['overdue'])


class SilentLaneTests(unittest.TestCase):
    """Tests #18, #24, #25, and Change 6 (SSH lane_owners)."""

    def test_silent_lane_flagged_after_n_hours_while_declared_wait_is_not(self):
        t0 = BASE_NOW - timedelta(hours=6)
        row_silent = make_task('t-sil', LANE_A, created_at=t0)
        row_silent['comments'].append(make_comment('c1', 'starting work', LANE_A, t0))
        row_wait = make_task('t-wait', LANE_B, created_at=t0)
        add_checkpoint(row_wait, 'cp1', t0)
        add_lane_state(row_wait, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        res = waits.compute_waits([row_silent, row_wait], now=BASE_NOW, project=PROJECT,
                                  registry=default_reg(), operators=[COORD])
        self.assertEqual([w['task'] for w in res['waits']], ['t-wait'])
        self.assertEqual([s['task'] for s in res['silent']], ['t-sil'])

    def test_label_edit_and_coordinator_comment_do_not_reset_silent_lane_clock(self):
        """#24: neither updated_at bump nor coordinator comment resets last_holder_activity_at."""
        t0 = BASE_NOW - timedelta(hours=6)
        row = make_task('t-24', LANE_A, created_at=t0, updated_at=BASE_NOW - timedelta(minutes=5))
        row['comments'].append(make_comment('c-coord', 'ping are you there?', COORD, BASE_NOW - timedelta(minutes=10)))
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual(len(res['silent']), 1)
        self.assertEqual(res['silent'][0]['age_seconds'], 6 * 3600)

    def test_open_status_held_task_goes_silent_after_n_hours(self):
        """#25: status='open' held task flags silent after N hours."""
        t0 = BASE_NOW - timedelta(hours=5)
        row = make_task('t-25', LANE_A, status='open', created_at=t0)
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[COORD])
        self.assertEqual([s['task'] for s in res['silent']], ['t-25'])

    def test_ssh_lane_owners_resolves_person_for_user_role_and_silent_on_me(self):
        """Change 6: lane:* -> person:* via .escalation-config.json lane_owners, fallback to operators."""
        t0 = BASE_NOW - timedelta(hours=6)
        with tempfile.TemporaryDirectory() as tmp:
            jdir = Path(tmp) / PROJECT
            jdir.mkdir()
            (jdir / waits.ESCALATION_CONFIG_FILE).write_text(
                json.dumps({'lane_owners': {'lane:agyneon': 'person:james'}}),
                encoding='utf-8',
            )
            # 1. waiting_on='user': LANE_B (lane:agyargon) cannot answer; OWNER_HUMAN (person:james) CAN.
            row_u = make_task('t-user', LANE_A, created_at=t0)
            add_checkpoint(row_u, 'cp1', t0)
            add_lane_state(row_u, 'ls1', 'cp1', t0, state='waiting-on-user', waiting_on='user')
            row_u['comments'].append(make_comment('c-lane', 'ANSWER: ok', LANE_B, t0 + timedelta(hours=1)))
            res1 = waits.compute_waits([row_u], now=BASE_NOW, project=PROJECT, registry=default_reg(),
                                       operators=[COORD], journal=jdir)
            self.assertEqual(len(res1['waits']), 1)
            row_u['comments'].append(make_comment('c-james', 'Owner: ANSWER yes', OWNER_HUMAN, t0 + timedelta(hours=2)))
            res2 = waits.compute_waits([row_u], now=BASE_NOW, project=PROJECT, registry=default_reg(),
                                       operators=[COORD], journal=jdir)
            self.assertEqual(res2['waits'], [])

            # 2. Silent lane owned by person:james appears on OWNER_HUMAN's --on-me!
            row_s = make_task('t-sil-owner', LANE_A, created_at=t0)
            view = waits.read([row_s], OWNER_HUMAN, default_reg(), ['--on-me'], now=BASE_NOW,
                              operators=[COORD], journal=jdir, project=PROJECT)
            self.assertEqual([s['task'] for s in view['silent']], ['t-sil-owner'])

            # 3. Unmapped lane:agyargon falls back to operators.
            mapped, fell_back = waits.resolve_ssh_lane_owner(
                LANE_B, default_reg()['owners'], {'lane:agyneon': 'person:james'}, [COORD]
            )
            self.assertTrue(fell_back)
            self.assertEqual(mapped, frozenset([COORD]))


class WaitRoleAndMetricTests(unittest.TestCase):
    """Tests #11, #33, read-only guarantee (S6), CLI parsing, and scope isolation."""

    def test_role_with_no_members_flagged_visible(self):
        t0 = BASE_NOW - timedelta(hours=5)
        row = make_task('t-11', LANE_A, created_at=t0)
        add_checkpoint(row, 'cp1', t0)
        add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        res = waits.compute_waits([row], now=BASE_NOW, project=PROJECT, registry=default_reg(), operators=[])
        self.assertEqual(len(res['waits']), 1)
        self.assertTrue(res['waits'][0]['role_has_no_members'])

    def test_closed_unanswered_superseded_and_by_actor_metrics(self):
        """#33 (SHOULD-7): all 4 buckets + by_target and by_actor metrics."""
        t0 = BASE_NOW - timedelta(hours=6)
        # Task 1: answered after 2h by COORD
        r_ans = make_task('t-ans', LANE_A, created_at=t0)
        add_checkpoint(r_ans, 'cp1', t0)
        add_lane_state(r_ans, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        r_ans['comments'].append(make_comment('v1', 'VERIFIED', COORD, t0 + timedelta(hours=2)))
        # Task 2: closed unanswered after 3h
        r_cls = make_task('t-cls', LANE_A, status='closed', created_at=t0, closed_at=t0 + timedelta(hours=3))
        add_checkpoint(r_cls, 'cp1', t0)
        add_lane_state(r_cls, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        # Task 3: superseded after 1h by state='working'
        r_sup = make_task('t-sup', LANE_B, created_at=t0)
        add_checkpoint(r_sup, 'cp1', t0)
        add_lane_state(r_sup, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
        add_checkpoint(r_sup, 'cp2', t0 + timedelta(hours=1), previous='cp1')
        add_lane_state(r_sup, 'ls2', 'cp2', t0 + timedelta(hours=1), state='working', waiting_on=None)
        # Task 4: still open after 6h
        r_opn = make_task('t-opn', LANE_B, created_at=t0)
        add_checkpoint(r_opn, 'cp1', t0)
        add_lane_state(r_opn, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')

        m = waits.compute_daily_metrics(
            [r_ans, r_cls, r_sup, r_opn], now=BASE_NOW, days=7, project=PROJECT,
            registry=default_reg(), operators=[COORD],
        )
        coord_row = next(x for x in m['by_target'] if x['waiting_on'] == 'coordinator')
        self.assertEqual(coord_row['answered']['count'], 1)
        self.assertEqual(coord_row['answered']['median_seconds'], 7200)
        self.assertEqual(coord_row['closed_unanswered']['count'], 1)
        self.assertEqual(coord_row['superseded']['count'], 1)
        self.assertEqual(coord_row['open_now']['open_count'], 1)
        self.assertEqual(coord_row['open_now']['open_max_seconds'], 6 * 3600)
        actor_row = next(x for x in m['by_actor'] if x['actor'] == COORD)
        self.assertEqual(actor_row['answered']['count'], 1)
        self.assertEqual(actor_row['answered']['median_seconds'], 7200)

    def test_plain_read_never_writes_escalation_ledger_and_parses_all_cli_spellings(self):
        with tempfile.TemporaryDirectory() as tmp:
            jdir = Path(tmp) / PROJECT
            jdir.mkdir()
            t0 = BASE_NOW - timedelta(hours=5)
            row = make_task('t-ro', LANE_A, created_at=t0)
            add_checkpoint(row, 'cp1', t0)
            add_lane_state(row, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
            # Separate and '=' option spellings
            p1 = waits.parse_args(['--role', 'coordinator', '--silent-hours', '6', '--limit', '10', '--offset', '0', '--json'])
            p2 = waits.parse_args(['--role=coordinator', '--silent-hours=6', '--limit=10', '--offset=0'])
            self.assertEqual((p1.role, p1.silent_hours, p1.limit, p1.offset),
                             (p2.role, p2.silent_hours, p2.limit, p2.offset))
            p3 = waits.parse_args(['--metrics', '--days=7'])
            self.assertTrue(p3.metrics)
            self.assertEqual(p3.days, 7)
            with self.assertRaises(ValueError):
                waits.parse_args(['--on-me', '--all'])
            with self.assertRaises(ValueError):
                waits.parse_args(['--unknown-flag'])
            # Plain read must not create .wait-escalations/
            waits.read([row], COORD, default_reg(), ['--on-me'], now=BASE_NOW, operators=[COORD], journal=jdir, project=PROJECT)
            self.assertFalse((jdir / waits.ESCALATION_LEDGER_DIR).exists())
            # Scope='own' hides other lanes
            row_b = make_task('t-b', LANE_B, created_at=t0)
            add_checkpoint(row_b, 'cp1', t0)
            add_lane_state(row_b, 'ls1', 'cp1', t0, state='waiting-on-coordinator', waiting_on='coordinator')
            own_view = waits.read([row, row_b], LANE_A, default_reg(), ['--all'], now=BASE_NOW,
                                  operators=[COORD], journal=jdir, scope='own', project=PROJECT)
            self.assertEqual([w['task'] for w in own_view['items']], ['t-ro'])


if __name__ == '__main__':
    unittest.main()
