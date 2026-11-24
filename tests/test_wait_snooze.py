"""Unit tests for wait_snooze.py — Slice 2 (#13, #29, #31, SHOULD-1/5)."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import briefing
import lane_state
import reserved_comments
import wait_snooze
import waiting
import waits


def _iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _checkpoint_comment(cid, author, created_at, *, task='proj-1', previous=None,
                        summary='waiting for review', open_items=None):
    payload = {
        'schema_version': 1,
        'task': task,
        'intent': 'test',
        'summary': summary,
        'next_action': 'wait',
        'acceptance': 'verified',
        'open_items': list(open_items or ()),
        'previous': previous,
    }
    return {
        'id': cid,
        'issue_id': task,
        'author': author,
        'created_at': _iso(created_at),
        'text': 'Kind: task-checkpoint-v1\n' + json.dumps(payload, sort_keys=True, separators=(',', ':')),
    }


def _lane_state_comment(cid, author, created_at, *, task='proj-1', checkpoint, state='waiting-on-coordinator',
                        waiting_on='coordinator', expect_by=None):
    payload = {
        'schema_version': 1,
        'task': task,
        'checkpoint': checkpoint,
        'state': state,
        'waiting_on': waiting_on,
    }
    if expect_by is not None:
        payload['expect_by'] = _iso(expect_by)
    return {
        'id': cid,
        'issue_id': task,
        'author': author,
        'created_at': _iso(created_at),
        'text': 'Kind: lane-state-v1\n' + json.dumps(payload, sort_keys=True, separators=(',', ':')),
    }


def _make_waiting_row(now, *, task='proj-1', holder='lane-a', waiting_on='coordinator',
                      state='waiting-on-coordinator', hours_ago=6, expect_hours=2):
    started = now - timedelta(hours=hours_ago)
    expect = started + timedelta(hours=expect_hours)
    cp = _checkpoint_comment('cp-1', holder, started, task=task)
    ls = _lane_state_comment('ls-1', holder, started + timedelta(seconds=1), task=task,
                             checkpoint='cp-1', state=state, waiting_on=waiting_on, expect_by=expect)
    return {
        'id': task,
        'title': 'Secret title for ' + task,
        'status': 'in_progress',
        'issue_type': 'task',
        'assignee': holder,
        'created_at': _iso(started - timedelta(hours=1)),
        'updated_at': _iso(started + timedelta(seconds=1)),
        'labels': [],
        'dependencies': [],
        'comments': [cp, ls],
    }


def _authority_state(project='proj'):
    return {
        'users': {
            'usr_owner': {'id': 'usr_owner', 'display_name': 'Owner Pat', 'disabled': False},
            'usr_coord': {'id': 'usr_coord', 'display_name': 'Coord Sam', 'disabled': False},
            'usr_rev': {'id': 'usr_rev', 'display_name': 'Reviewer Kai', 'disabled': False},
            'usr_contrib': {'id': 'usr_contrib', 'display_name': 'Contrib Lee', 'disabled': False},
        },
        'memberships': {
            project: {
                'usr_owner': 'owner',
                'usr_coord': 'coordinator',
                'usr_rev': 'reviewer',
                'usr_contrib': 'contributor',
            },
        },
        'agents': {},
    }


class SnoozeValidationTests(unittest.TestCase):
    """Matrix #13: every snooze requires a bounded future until and single-line reason (422)."""

    def setUp(self):
        self.now = datetime(2026, 11, 24, 16, 0, 0, tzinfo=timezone.utc)
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_snooze_without_end_time_is_refused(self):
        """Matrix #13: missing or empty `until` is refused (422), writes nothing, wait stays visible."""
        row = _make_waiting_row(self.now)
        computed = waits.compute_waits([row], now=self.now, project='proj',
                                       authority_state=_authority_state())
        wait_rec = computed['waits'][0]
        wrote = []

        for bad_body in (
            {'task': 'proj-1', 'episode_id': wait_rec['episode_id'], 'reason': 'busy today'},
            {'task': 'proj-1', 'episode_id': wait_rec['episode_id'], 'reason': 'busy', 'until': ''},
            {'task': 'proj-1', 'episode_id': wait_rec['episode_id'], 'reason': 'busy', 'until': None},
        ):
            with self.assertRaises(wait_snooze.SnoozeRefusal) as ctx:
                wait_snooze.apply(
                    bad_body, actor='usr_coord', wait_record=wait_rec, now=self.now,
                    journal=self.pdir, resolver=computed['resolver'],
                    authority_state=_authority_state(), project='proj',
                    write_comment=lambda tid, text: wrote.append((tid, text)) or 'c-1',
                )
            self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(wrote, [])
        self.assertEqual(wait_snooze.load_snoozes(self.pdir), {})
        after = waits.compute_waits([row], now=self.now, project='proj',
                                    authority_state=_authority_state(),
                                    snoozes=wait_snooze.load_snoozes(self.pdir))
        self.assertEqual(len(after['waits']), 1)
        self.assertFalse(after['waits'][0]['snoozed'])

    def test_validation_rejects_past_far_future_malformed_until_bad_reason_and_unknown_fields(self):
        good_until = _iso(self.now + timedelta(hours=6))
        base = {'task': 'proj-1', 'episode_id': 'abcdef1234567890', 'reason': 'ok', 'until': good_until}
        bad_cases = [
            dict(base, until=_iso(self.now)),                             # not strictly future
            dict(base, until=_iso(self.now - timedelta(seconds=1))),      # past
            dict(base, until=_iso(self.now + timedelta(days=7, seconds=1))),  # > 7 days
            dict(base, until='2026-11-25'),                               # missing seconds & offset
            dict(base, until='2026-11-25T12:00:00'),                      # missing offset
            dict(base, reason='   '),                                     # empty
            dict(base, reason='line1\nline2'),                            # multiline
            dict(base, reason='x' * (wait_snooze.REASON_LIMIT + 1)),      # > 300 chars
            dict(base, extra='nope'),                                     # unknown field
            dict(base, schema_version=2),                                 # bad schema_version
        ]
        for body in bad_cases:
            with self.assertRaises(wait_snooze.SnoozeRefusal) as ctx:
                wait_snooze.validate_request(body, now=self.now)
            self.assertEqual(ctx.exception.status, 422, f'Expected 422 for {body!r}')

        # Exactly 7 days ahead is accepted.
        valid = wait_snooze.validate_request(
            dict(base, until=_iso(self.now + timedelta(days=7))), now=self.now
        )
        self.assertEqual(valid['reason'], 'ok')

    def test_cli_snooze_parser_handles_all_spellings_and_refuses_bad_flags(self):
        until = _iso(self.now + timedelta(hours=5))
        parsed1 = wait_snooze.parse_cli_snooze_args(
            ['snooze', 'proj-1', '--until', until, '--reason', 'waiting on batch', '--episode', 'ep12345678', '--json']
        )
        parsed2 = wait_snooze.parse_cli_snooze_args(
            ['snooze', 'proj-1', f'--until={until}', '--reason=waiting on batch', '--episode=ep12345678']
        )
        self.assertEqual(parsed1, parsed2)
        self.assertEqual(parsed1, {'task': 'proj-1', 'until': until, 'reason': 'waiting on batch', 'episode_id': 'ep12345678'})

        for bad_args in (
            ['snooze'],
            ['snooze', 'proj-1', '--until'],
            ['snooze', 'proj-1', '--bogus', '1'],
            ['snooze', 'proj-1', '--until', until, '--until', until],
            ['snooze', 'proj-1', 'extra-task', '--until', until],
        ):
            with self.assertRaises(wait_snooze.SnoozeRefusal) as ctx:
                wait_snooze.parse_cli_snooze_args(bad_args)
            self.assertEqual(ctx.exception.status, 422, f'Expected 422 for {bad_args!r}')

    @unittest.skipIf(os.name == 'nt', 'endpoint requires POSIX fcntl')
    def test_endpoint_waiting_lock_mode_and_service_only_wait_snooze(self):
        import endpoint
        self.assertIn('wait-snooze', endpoint.SERVICE_ONLY_ACTIONS)
        self.assertTrue(endpoint.READ_ONLY_ACTIONS['waiting'](['--on-me'], None))
        self.assertTrue(endpoint.READ_ONLY_ACTIONS['waiting'](['--metrics'], None))
        self.assertFalse(endpoint.READ_ONLY_ACTIONS['waiting'](['snooze', 'proj-1'], None))


class SnoozeAuthorityTests(unittest.TestCase):
    """Matrix #29 (SHOULD-1): anti-self-suppression, 2-renewal cap (409), auto-expiry, visibility."""

    def setUp(self):
        self.now = datetime(2026, 11, 24, 16, 0, 0, tzinfo=timezone.utc)
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _apply(self, row, wait_rec, computed, *, actor, until, reason, is_human_owner=False, now=None):
        now = now or self.now
        existing = wait_snooze.load_snoozes(self.pdir).get(wait_rec['episode_id'], [])
        counter = [len(row['comments'])]

        def write_comment(tid, text):
            counter[0] += 1
            cid = f'c-sn-{counter[0]}'
            row['comments'].append({
                'id': cid, 'issue_id': tid, 'author': actor,
                'created_at': _iso(now), 'text': text,
            })
            return cid

        return wait_snooze.apply(
            {'task': row['id'], 'episode_id': wait_rec['episode_id'], 'reason': reason, 'until': _iso(until)},
            actor=actor, actor_display=actor, wait_record=wait_rec, now=now,
            journal=self.pdir, existing_entries=existing, resolver=computed['resolver'],
            authority_state=_authority_state(), project='proj',
            is_human_project_owner=is_human_owner, write_comment=write_comment,
        )

    def test_waited_on_snooze_still_notifies_owner_and_third_renewal_refused(self):
        """Matrix #29: waited-on party snooze keeps suppresses_owner_notice=False; 3rd renewal -> 409."""
        auth = _authority_state()
        row = _make_waiting_row(self.now, waiting_on='coordinator')
        computed = waits.compute_waits([row], now=self.now, project='proj', authority_state=auth)
        wait_rec = computed['waits'][0]

        # 1st snooze by coordinator (who IS a member of waiting_on='coordinator'):
        r1 = self._apply(row, wait_rec, computed, actor='usr_coord',
                         until=self.now + timedelta(hours=8), reason='In release freeze')
        self.assertTrue(r1['waited_on_party'])
        self.assertFalse(r1['suppresses_owner_notice'])
        self.assertEqual(r1['snooze_count'], 1)

        # Wait stays visible on reads, carrying snooze metadata + anti-self-suppression flag.
        loaded = wait_snooze.load_snoozes(self.pdir)
        view = waits.read([row], 'usr_coord', {}, ['--all'], now=self.now + timedelta(minutes=5),
                          project='proj', authority_state=auth, snoozes=loaded)
        self.assertEqual(view['total'], 1)
        self.assertEqual(view['overdue_count'], 1)
        self.assertEqual(view['snoozed_count'], 1)
        item = view['items'][0]
        self.assertTrue(item['snoozed'])
        self.assertEqual(item['snooze_count'], 1)
        self.assertTrue(item['snooze']['waited_on_party'])
        self.assertFalse(item['snooze']['suppresses_owner_notice'])
        self.assertEqual(item['snooze']['reason'], 'In release freeze')

        # 2nd snooze succeeds.
        r2 = self._apply(row, wait_rec, computed, actor='usr_coord',
                         until=self.now + timedelta(hours=16), reason='Still in freeze',
                         now=self.now + timedelta(hours=1))
        self.assertEqual(r2['snooze_count'], 2)
        self.assertFalse(r2['suppresses_owner_notice'])

        # 3rd snooze by waited-on party (even a human owner who IS waited-on via coordinator role) -> 409!
        with self.assertRaises(wait_snooze.SnoozeRefusal) as ctx:
            self._apply(row, wait_rec, computed, actor='usr_owner',
                        until=self.now + timedelta(hours=24), reason='Third try',
                        is_human_owner=True, now=self.now + timedelta(hours=2))
        self.assertEqual(ctx.exception.status, 409)

        # Auto-expiry once clock reaches `until`: snoozed flips False, row stays visible, count stays 2.
        expired = waits.read([row], 'usr_coord', {}, ['--all'], now=self.now + timedelta(hours=16),
                             project='proj', authority_state=auth,
                             snoozes=wait_snooze.load_snoozes(self.pdir))
        self.assertEqual(expired['total'], 1)
        self.assertEqual(expired['snoozed_count'], 0)
        self.assertFalse(expired['items'][0]['snoozed'])
        self.assertIsNone(expired['items'][0]['snooze'])
        self.assertEqual(expired['items'][0]['snooze_count'], 2)

    def test_non_waited_on_human_owner_suppresses_owner_notice_and_may_renew_past_cap(self):
        """A human project owner who is NOT the waited-on actor suppresses notice and may override 3rd cap."""
        auth = _authority_state()
        row = _make_waiting_row(self.now, waiting_on='usr_rev', state='waiting-on-review')
        computed = waits.compute_waits([row], now=self.now, project='proj', authority_state=auth)
        wait_rec = computed['waits'][0]

        # 1st & 2nd snooze by usr_coord (not usr_rev) -> suppresses_owner_notice=True
        r1 = self._apply(row, wait_rec, computed, actor='usr_coord',
                         until=self.now + timedelta(hours=4), reason='Reviewer on PTO')
        self.assertFalse(r1['waited_on_party'])
        self.assertTrue(r1['suppresses_owner_notice'])
        self._apply(row, wait_rec, computed, actor='usr_coord',
                    until=self.now + timedelta(hours=8), reason='Reviewer still out',
                    now=self.now + timedelta(hours=1))

        # 3rd snooze by non-owner coordinator is refused (409).
        with self.assertRaises(wait_snooze.SnoozeRefusal) as ctx:
            self._apply(row, wait_rec, computed, actor='usr_coord',
                        until=self.now + timedelta(hours=12), reason='Coord 3rd try',
                        is_human_owner=False, now=self.now + timedelta(hours=2))
        self.assertEqual(ctx.exception.status, 409)

        # 3rd snooze by non-waited-on human project owner IS allowed (even past 2 renewals).
        r3 = self._apply(row, wait_rec, computed, actor='usr_owner',
                         until=self.now + timedelta(hours=12), reason='Owner approved extension',
                         is_human_owner=True, now=self.now + timedelta(hours=2))
        self.assertFalse(r3['waited_on_party'])
        self.assertTrue(r3['suppresses_owner_notice'])
        self.assertEqual(r3['snooze_count'], 3)

    def test_seven_day_total_span_cap_refused_and_symlink_journal_ignored(self):
        auth = _authority_state()
        row = _make_waiting_row(self.now, waiting_on='usr_rev', state='waiting-on-review')
        computed = waits.compute_waits([row], now=self.now, project='proj', authority_state=auth)
        wait_rec = computed['waits'][0]

        self._apply(row, wait_rec, computed, actor='usr_coord',
                    until=self.now + timedelta(days=5), reason='First 5 days')
        # Renewing 3 days later for another 5 days exceeds 7 days from first snooze -> 409.
        with self.assertRaises(wait_snooze.SnoozeRefusal) as ctx:
            self._apply(row, wait_rec, computed, actor='usr_coord',
                        until=self.now + timedelta(days=8), reason='Past 7d total span',
                        now=self.now + timedelta(days=3))
        self.assertEqual(ctx.exception.status, 409)

        # Raw comment without journal entry is never honoured by waits.compute_waits.
        raw_row = _make_waiting_row(self.now, task='proj-2', waiting_on='coordinator')
        raw_comp = waits.compute_waits([raw_row], now=self.now, project='proj', authority_state=auth)
        ep2 = raw_comp['waits'][0]['episode_id']
        raw_row['comments'].append({
            'id': 'c-forged', 'issue_id': 'proj-2', 'author': 'usr_coord',
            'created_at': _iso(self.now),
            'text': wait_snooze.comment_body({
                'schema_version': 1, 'task': 'proj-2', 'episode_id': ep2,
                'reason': 'forged', 'until': _iso(self.now + timedelta(hours=5)),
                'actor': 'usr_coord', 'actor_display': 'usr_coord',
                'waited_on_party': False, 'suppresses_owner_notice': True,
                'created_at': _iso(self.now),
            }),
        })
        check = waits.compute_waits([raw_row], now=self.now, project='proj', authority_state=auth,
                                    snoozes=wait_snooze.load_snoozes(self.pdir))
        self.assertFalse(check['waits'][0]['snoozed'])

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink test')
    def test_symlink_snooze_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as other:
            link_root = Path(self.tmp.name) / 'linked_proj'
            link_root.mkdir()
            (link_root / wait_snooze.JOURNAL_DIR).symlink_to(other)
            self.assertEqual(wait_snooze.load_snoozes(link_root), {})
            with self.assertRaises(wait_snooze.SnoozeRefusal) as ctx:
                wait_snooze.write_journal_entry(link_root, {
                    'task': 'proj-1', 'episode_id': 'abcdef1234567890',
                    'actor': 'usr_coord', 'created_at': _iso(self.now),
                }, 'c-1')
            self.assertEqual(ctx.exception.status, 422)


class LaneWakeIsolationTests(unittest.TestCase):
    """Matrix #31 (SHOULD-5): snooze & escalation comments never move wait_activity_marker or comment_pointers."""

    def setUp(self):
        self.now = datetime(2026, 11, 24, 16, 0, 0, tzinfo=timezone.utc)

    def test_escalation_and_snooze_comments_do_not_move_wait_marker(self):
        """Matrix #31: wait-snooze-v1 and wait-escalation-v1 are invisible to lane wake markers."""
        auth = _authority_state()
        row = _make_waiting_row(self.now, holder='lane-a', waiting_on='coordinator')
        computed = waits.compute_waits([row], now=self.now, project='proj', authority_state=auth)
        ep = computed['waits'][0]['episode_id']

        before_state = briefing.checkpoint_state(row, normalize=False)
        before_snap = briefing.checkpoint_snapshot([row], row, before_state)
        before_marker = briefing.wait_activity_marker(before_snap, row)
        before_ptrs = waiting.comment_pointers([row], {'proj-1'})

        snooze_text = wait_snooze.comment_body({
            'schema_version': 1, 'task': 'proj-1', 'episode_id': ep,
            'reason': 'Coordinating batch', 'until': _iso(self.now + timedelta(hours=6)),
            'actor': 'usr_coord', 'actor_display': 'Coord Sam',
            'waited_on_party': True, 'suppresses_owner_notice': False,
            'created_at': _iso(self.now),
        })
        esc_text = (
            reserved_comments.WAIT_ESCALATION_PREFIX
            + json.dumps({'schema_version': 1, 'task': 'proj-1', 'episode_id': ep,
                          'stage': 'owner-notified', 'at': _iso(self.now)}, sort_keys=True)
        )
        row['comments'].append({
            'id': 'c-sn-1', 'issue_id': 'proj-1', 'author': 'usr_coord',
            'created_at': _iso(self.now + timedelta(seconds=5)), 'text': snooze_text,
        })
        row['comments'].append({
            'id': 'c-esc-1', 'issue_id': 'proj-1', 'author': 'system:wait-escalator',
            'created_at': _iso(self.now + timedelta(seconds=10)), 'text': esc_text,
        })

        after_state = briefing.checkpoint_state(row, normalize=False)
        after_snap = briefing.checkpoint_snapshot([row], row, after_state)
        after_marker = briefing.wait_activity_marker(after_snap, row)
        after_ptrs = waiting.comment_pointers([row], {'proj-1'})

        self.assertEqual(before_marker, after_marker)
        self.assertEqual(before_ptrs, after_ptrs)
        # Neither comment answers the wait episode.
        after_waits = waits.compute_waits([row], now=self.now + timedelta(seconds=20),
                                          project='proj', authority_state=auth)
        self.assertEqual(len(after_waits['waits']), 1)

        # Both prefixes are reserved against raw comments add (and NOT in RECORD_COMMENT_FAMILIES).
        self.assertIsNotNone(reserved_comments.reserved_match(snooze_text))
        self.assertIsNotNone(reserved_comments.reserved_match(esc_text))
        self.assertNotIn('wait-snooze-v1', reserved_comments.RECORD_COMMENT_FAMILIES)
        self.assertNotIn('wait-escalation-v1', reserved_comments.RECORD_COMMENT_FAMILIES)
        self.assertTrue(reserved_comments.is_wait_meta_comment(snooze_text))
        self.assertTrue(reserved_comments.is_wait_meta_comment(esc_text))
        self.assertFalse(reserved_comments.is_wait_meta_comment('VERIFIED'))


if __name__ == '__main__':
    unittest.main()
