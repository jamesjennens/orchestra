"""The reserved ``Kind: lane-state-v1`` record and its one writer (kittrial-5bb.263).

Covers the field rules (acceptances and refusals, including a past ``expect_by``
accepted and an unparseable one refused), the checkpoint writer as the only writer, the
closed checkpoint field set, and the reserved-comment rule that a raw ``comments add``
cannot write or alter a lane-state-v1 record of another lane.
"""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing as b
import lane_state
import reserved_comments as rc
from requirements import canonical_bytes
from test_briefing import PROJECT, STAMP, TASK, checkpoint, comment, rows

WAITING = dict(schema_version=1, task=TASK, state='waiting-on-coordinator', waiting_on='coordinator')


class Writes:
    """A native runner stub that stores what the writer posts, so both records are visible."""

    def __init__(self, data, actor='alice/session'):
        self.data = data
        self.actor = actor
        self.calls = []

    def run(self, argv):
        self.calls.append(list(argv))
        if argv[:2] != ['comments', 'add']:
            raise AssertionError('unexpected native call: %r' % (argv,))
        # Number from the task's live comment count, so two saves in one test (a second
        # store) cannot mint colliding ids.
        cid = 'w%d' % (len(self.data[0]['comments']) + 1)
        self.data[0]['comments'].append(comment(cid, argv[3], author=self.actor))
        return json.dumps({'id': cid})


def save(data, actor='alice/session', now=None, **changes):
    store = Writes(data, actor=actor)
    payload = checkpoint(data, **changes)
    result = b.save_checkpoint(data, PROJECT, TASK, payload, actor, store.run, now=now)
    return result, store


class ValidationTests(unittest.TestCase):
    def test_working_and_idle_need_no_waiting_on(self):
        for state in ('working', 'idle'):
            with self.subTest(state=state):
                lane_state.validate(dict(schema_version=1, task=TASK, state=state), TASK)

    def test_each_waiting_state_accepts_a_role_or_an_actor_id(self):
        for state in lane_state.WAITING_STATES:
            for waiting_on in ('coordinator', 'reviewer', 'user', 'session-1234abcd-0000-0000-0000-000000000000',
                               'alice/session'):
                with self.subTest(state=state, waiting_on=waiting_on):
                    lane_state.validate(dict(schema_version=1, task=TASK, state=state, waiting_on=waiting_on), TASK)

    def test_a_waiting_state_requires_waiting_on_and_a_non_waiting_state_refuses_it(self):
        for state in lane_state.WAITING_STATES:
            with self.subTest(state=state):
                with self.assertRaisesRegex(ValueError, 'waiting_on is required'):
                    lane_state.validate(dict(schema_version=1, task=TASK, state=state), TASK)
        for state in ('working', 'idle'):
            with self.subTest(state=state):
                with self.assertRaisesRegex(ValueError, 'waiting_on is refused'):
                    lane_state.validate(dict(schema_version=1, task=TASK, state=state, waiting_on='coordinator'), TASK)

    def test_the_state_set_is_named_in_the_refusal(self):
        with self.assertRaises(ValueError) as caught:
            lane_state.validate(dict(schema_version=1, task=TASK, state='waiting'), TASK)
        for state in lane_state.STATES:
            self.assertIn(state, str(caught.exception))

    def test_a_past_expect_by_is_accepted_and_only_an_unparseable_one_is_refused(self):
        for value in ('2000-01-01T00:00:00+00:00', '2099-01-01T00:00:00Z', '2026-10-12T04:00:00-05:00'):
            with self.subTest(value=value):
                lane_state.validate(dict(schema_version=1, task=TASK, state='idle', expect_by=value), TASK)
        for value in ('yesterday', '2026-10-12', '', '2026-13-40T00:00:00+00:00'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'expect_by'):
                    lane_state.validate(dict(schema_version=1, task=TASK, state='idle', expect_by=value), TASK)

    def test_a_naive_expect_by_is_refused_because_it_cannot_be_aged(self):
        with self.assertRaisesRegex(ValueError, 'offset or Z'):
            lane_state.validate(dict(schema_version=1, task=TASK, state='idle',
                                     expect_by='2026-10-12T04:00:00'), TASK)

    def test_no_person_is_inferred_and_an_actor_is_never_turned_into_a_role(self):
        for value in ('the coordinator', 'The Reviewer', 'coordinator!', 'wait for the user'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'infers no person'):
                    lane_state.validate(dict(schema_version=1, task=TASK, state='waiting-on-coordinator',
                                             waiting_on=value), TASK)
        # A value that is actor-shaped but not the exact role word is kept VERBATIM as an
        # actor id: it is never lower-cased or converted into the role it resembles.
        kept = lane_state.validate(dict(schema_version=1, task=TASK, state='waiting-on-coordinator',
                                        waiting_on='Coordinator'), TASK)
        self.assertEqual(kept['waiting_on'], 'Coordinator')
        self.assertNotEqual(kept['waiting_on'], 'coordinator')

    def test_task_mismatch_unknown_missing_and_schema_are_refused(self):
        cases = (
            (dict(schema_version=1, task='other', state='idle'), 'task mismatch'),
            (dict(schema_version=1, task=TASK, state='idle', extra=1), 'unknown fields: extra'),
            (dict(schema_version=1, state='idle'), 'missing fields: task'),
            (dict(schema_version=2, task=TASK, state='idle'), 'schema_version must be integer 1'),
            (dict(schema_version=True, task=TASK, state='idle'), 'schema_version must be integer 1'),
        )
        for payload, said in cases:
            with self.subTest(said=said):
                with self.assertRaisesRegex(ValueError, said):
                    lane_state.validate(payload, TASK)

    def test_waiting_on_is_bounded_and_printable(self):
        with self.assertRaisesRegex(ValueError, 'longer than 96'):
            lane_state.validate(dict(schema_version=1, task=TASK, state='waiting-on-user', waiting_on='x' * 97), TASK)
        with self.assertRaisesRegex(ValueError, 'control characters'):
            lane_state.validate(dict(schema_version=1, task=TASK, state='waiting-on-user', waiting_on='bad\u0007name'),
                                TASK)

    def test_checkpoint_is_not_sent_in_the_request(self):
        with self.assertRaisesRegex(ValueError, 'written by the checkpoint writer'):
            lane_state.validate(dict(WAITING, checkpoint='someone-elses-checkpoint'), TASK)

    def test_the_written_record_must_name_the_checkpoint_it_was_written_with(self):
        written = dict(WAITING, checkpoint='c1')
        lane_state.validate(written, TASK, 'c1')
        with self.assertRaisesRegex(ValueError, 'not the checkpoint this write just published'):
            lane_state.validate(written, TASK, 'c2')
        with self.assertRaisesRegex(ValueError, 'checkpoint is missing'):
            lane_state.validate(WAITING, TASK, 'c1')

    def test_every_fault_is_reported_at_once(self):
        found = lane_state.problems(dict(schema_version=1, task='other', state='nope', waiting_on='x'), TASK)
        self.assertGreaterEqual(len(found), 3)


class NotBeforeTests(unittest.TestCase):
    """The holder's optional wake time (kittrial-5bb.509).

    Every write-time rule runs against an injected clock, never the real one. The read
    path passes no clock: a record whose wake time has passed is still usable, exactly
    like a past ``expect_by``.
    """

    now = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)

    def test_a_future_wake_time_within_fourteen_days_is_accepted(self):
        for value in ('2026-11-04T12:00:01Z', '2026-11-18T12:00:00Z', '2026-11-04T07:01:00-05:00'):
            with self.subTest(value=value):
                kept = lane_state.validate(dict(WAITING, not_before=value), TASK, now=self.now)
                self.assertEqual(kept['not_before'], value)
        # A working lane may promise a wake time too: the timer is about the lane's next
        # turn, not only a declared wait state.
        lane_state.validate(dict(schema_version=1, task=TASK, state='working',
                                 not_before='2026-11-05T09:00:00Z'), TASK, now=self.now)

    def test_a_wake_time_not_strictly_future_is_refused_at_write(self):
        for value in ('2026-11-04T11:59:59Z', '2026-11-04T12:00:00Z'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'strictly in the future'):
                    lane_state.validate(dict(WAITING, not_before=value), TASK, now=self.now)

    def test_a_wake_time_more_than_fourteen_days_out_is_refused_at_write(self):
        for value in ('2026-11-18T12:00:01Z', '2027-01-01T00:00:00Z'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'more than 14 days ahead'):
                    lane_state.validate(dict(WAITING, not_before=value), TASK, now=self.now)

    def test_a_wake_time_without_seconds_is_refused_at_write(self):
        with self.assertRaisesRegex(ValueError, 'seconds and an offset'):
            lane_state.validate(dict(WAITING, not_before='2026-11-05T17:30Z'), TASK, now=self.now)
        # The shape is a WRITE-time rule: without a clock only the moment itself is
        # checked, so a reader never refuses a stored record over its spelling.
        lane_state.validate(dict(WAITING, not_before='2026-11-05T17:30Z'), TASK)

    def test_a_due_wake_time_stays_readable(self):
        kept = lane_state.validate(dict(WAITING, checkpoint='c1', not_before='2000-01-01T00:00:00Z'),
                                   TASK, 'c1')
        self.assertEqual(kept['not_before'], '2000-01-01T00:00:00Z')
        # An unparseable value is refused everywhere: it names no moment at all.
        with self.assertRaisesRegex(ValueError, 'not_before'):
            lane_state.validate(dict(WAITING, not_before='tomorrow'), TASK)

    def test_a_fault_never_carries_a_wake_time(self):
        fault = dict(schema_version=1, task=TASK, state=lane_state.FAULT_STATE, reason='stuck on a lock')
        lane_state.validate(fault, TASK)
        with self.assertRaisesRegex(ValueError, 'a fault\\s+is not a wait'):
            lane_state.validate(dict(fault, not_before='2026-11-05T09:00:00Z'), TASK, now=self.now)

    def test_the_writer_applies_the_write_time_rules_with_the_supplied_clock(self):
        data = rows()
        before = list(data[0]['comments'])
        with self.assertRaisesRegex(ValueError, 'strictly in the future'):
            save(data, now=self.now, lane_state=dict(WAITING, not_before='2026-11-04T11:00:00Z'))
        self.assertEqual(data[0]['comments'], before)

    def test_the_wake_time_is_written_by_the_holder_only(self):
        # No author rule predates the wake time: ``newest`` (lane_state.py:284) takes the
        # newest usable record by ANY author, and the checkpoint writer never gated on the
        # assignee. The holder rule is the wake time's own, at the writer.
        data = rows()
        before = list(data[0]['comments'])
        with self.assertRaisesRegex(ValueError, "set only by the task's holder"):
            save(data, actor='bob/session', now=self.now,
                 lane_state=dict(WAITING, not_before='2026-11-04T13:00:00Z'))
        self.assertEqual(data[0]['comments'], before)
        save(data, now=self.now, lane_state=dict(WAITING, not_before='2026-11-04T13:00:00Z'))
        self.assertEqual(lane_state.newest(data[0]['comments'], TASK)[1]['not_before'],
                         '2026-11-04T13:00:00Z')

    def test_a_later_record_without_a_wake_time_clears_it(self):
        data = rows()
        save(data, now=self.now, lane_state=dict(WAITING, not_before='2026-11-04T13:00:00Z'))
        self.assertEqual(b.lane_not_before(data[0], TASK), '2026-11-04T13:00:00Z')
        save(data, now=self.now, lane_state=dict(WAITING))
        self.assertIsNone(b.lane_not_before(data[0], TASK))

    def test_a_foreign_record_never_sets_the_work_row_wake_time(self):
        # ``newest`` reads any author's record; the wake time is the holder's own, so the
        # work-row producer ignores one written by somebody else even when it is newest.
        data = rows()
        result, _store = save(data, now=self.now, lane_state=dict(WAITING))
        data[0]['comments'].append(comment('ls-foreign', lane_state.PREFIX + canonical_bytes(
            dict(schema_version=1, task=TASK, checkpoint=result['comment_id'], state='idle',
                 not_before='2026-11-05T09:00:00Z')).decode(), author='bob/session'))
        self.assertEqual(lane_state.newest(data[0]['comments'], TASK)[1]['not_before'],
                         '2026-11-05T09:00:00Z')
        self.assertIsNone(b.lane_not_before(data[0], TASK))

    def test_the_work_row_carries_the_wake_time_for_both_transports(self):
        data = rows()
        save(data, now=self.now, lane_state=dict(WAITING, not_before='2026-11-04T13:00:00Z'))
        fields = b.checkpoint_queue_fields(data, data[0])
        self.assertEqual(fields['lane_not_before'], '2026-11-04T13:00:00Z')


class WriterTests(unittest.TestCase):
    def test_the_writer_publishes_the_record_with_the_checkpoint_id(self):
        data = rows()
        result, store = save(data, lane_state=dict(WAITING))
        self.assertEqual(len(store.calls), 2)
        self.assertTrue(store.calls[0][3].startswith(b.PREFIX))
        self.assertTrue(store.calls[1][3].startswith(lane_state.PREFIX))
        record = lane_state.parse(data[0]['comments'][-1]['text'])
        self.assertEqual(record['checkpoint'], result['comment_id'])
        self.assertEqual(record['state'], 'waiting-on-coordinator')
        self.assertEqual(result['lane_state'], {'comment_id': data[0]['comments'][-1]['id'], 'reconciled': False})

    def test_the_checkpoint_object_keeps_its_closed_field_set(self):
        # The record is carried BESIDE the checkpoint, never inside it: the validator still
        # refuses `lane_state` as an unknown checkpoint field, which is what an older kit
        # relies on (nothing existing grew a key).
        data = rows()
        payload = checkpoint(data, lane_state=dict(WAITING))
        with self.assertRaisesRegex(ValueError, 'unknown fields: lane_state'):
            b.validate_checkpoint(payload, TASK)

    def test_a_bad_lane_state_refuses_before_any_write(self):
        data = rows()
        before = list(data[0]['comments'])
        with self.assertRaisesRegex(ValueError, 'waiting_on is required'):
            save(data, lane_state=dict(schema_version=1, task=TASK, state='waiting-on-review'))
        self.assertEqual(data[0]['comments'], before)

    def test_a_request_naming_a_checkpoint_is_refused_before_any_write(self):
        data = rows()
        before = list(data[0]['comments'])
        with self.assertRaisesRegex(ValueError, 'written by the checkpoint writer'):
            save(data, lane_state=dict(WAITING, checkpoint='not-yet-written'))
        self.assertEqual(data[0]['comments'], before)

    def test_a_retry_reconciles_without_a_second_record(self):
        data = rows()
        store = Writes(data)
        payload = checkpoint(data, lane_state=dict(WAITING))
        first = b.save_checkpoint(data, PROJECT, TASK, payload, 'alice/session', store.run)
        comments = len(data[0]['comments'])
        retry = b.save_checkpoint(data, PROJECT, TASK, payload, 'alice/session', store.run)
        self.assertTrue(retry['reconciled'])
        self.assertEqual(retry['comment_id'], first['comment_id'])
        self.assertTrue(retry['lane_state']['reconciled'])
        self.assertEqual(retry['lane_state']['comment_id'], first['lane_state']['comment_id'])
        self.assertEqual(len(data[0]['comments']), comments)

    def test_a_reconciled_retry_completes_a_record_missing_after_a_crash(self):
        data = rows()
        store = Writes(data)
        payload = checkpoint(data, lane_state=dict(WAITING))
        first = b.save_checkpoint(data, PROJECT, TASK, payload, 'alice/session', store.run)
        data[0]['comments'] = [c for c in data[0]['comments'] if c['id'] != first['lane_state']['comment_id']]
        self.assertIsNone(lane_state.newest(data[0]['comments'], TASK))
        retry = b.save_checkpoint(data, PROJECT, TASK, payload, 'alice/session', store.run)
        self.assertTrue(retry['reconciled'])
        self.assertFalse(retry['lane_state']['reconciled'])
        found = lane_state.newest(data[0]['comments'], TASK)
        self.assertIsNotNone(found)
        self.assertEqual(found[1]['checkpoint'], first['comment_id'])

    def test_a_changed_lane_state_on_a_retry_is_refused(self):
        data = rows()
        store = Writes(data)
        payload = checkpoint(data, lane_state=dict(WAITING))
        b.save_checkpoint(data, PROJECT, TASK, payload, 'alice/session', store.run)
        comments = len(data[0]['comments'])
        changed = dict(payload, lane_state=dict(schema_version=1, task=TASK, state='working'))
        with self.assertRaisesRegex(ValueError, 'never revised on a retry'):
            b.save_checkpoint(data, PROJECT, TASK, changed, 'alice/session', store.run)
        # The already-published record is untouched and no second record was written.
        self.assertEqual(len(data[0]['comments']), comments)
        self.assertEqual(lane_state.newest(data[0]['comments'], TASK)[1]['state'], 'waiting-on-coordinator')


class CheckpointStalenessTests(unittest.TestCase):
    """A declared wait state is part of its checkpoint, not activity written after it.

    kittrial-5bb.263 review ``lane-state-must-not-stale-its-own-checkpoint``: on a real bd
    the record made the checkpoint it was written with read stale, so every lane that
    declared a wait state was told at once that something had changed and so was whoever
    read its brief or its in-progress flag.
    """

    def test_the_record_does_not_make_its_own_checkpoint_stale(self):
        data = rows()
        save(data, lane_state=dict(WAITING))
        read = b.brief(data, PROJECT, TASK)
        self.assertFalse(read['checkpoint']['newer_activity'], read['checkpoint'])
        self.assertIsNone(read['newer'])
        # The queue read (work --mine / the in-progress flag) agrees: nothing is newer.
        fields = b.checkpoint_queue_fields(data, data[0])
        self.assertEqual((fields['newer_activity_by_others'], fields['newer_activity_own'],
                          fields['newer_activity_coverage']), (0, 0, 'current'))

    def test_an_ordinary_comment_still_makes_it_stale(self):
        data = rows()
        save(data, lane_state=dict(WAITING))
        data[0]['comments'].append(comment('later', 'Progress after the checkpoint.', author='bob/session'))
        self.assertTrue(b.brief(data, PROJECT, TASK)['checkpoint']['newer_activity'])

    def test_a_record_naming_an_older_checkpoint_still_counts(self):
        data = rows()
        store = Writes(data)
        first = b.save_checkpoint(data, PROJECT, TASK, checkpoint(data, lane_state=dict(WAITING)),
                                  'alice/session', store.run)
        second = b.save_checkpoint(data, PROJECT, TASK, checkpoint(data), 'alice/session', store.run)
        self.assertEqual(b.brief(data, PROJECT, TASK)['checkpoint']['comment_id'], second['comment_id'])
        # The FIRST checkpoint's record names an older checkpoint: it is ordinary activity.
        data[0]['comments'].append(comment('ls-old', lane_state.PREFIX + canonical_bytes(
            dict(schema_version=1, task=TASK, checkpoint=first['comment_id'], state='working')).decode(),
            author='alice/session'))
        self.assertTrue(b.brief(data, PROJECT, TASK)['checkpoint']['newer_activity'])

    def test_a_record_by_another_author_still_counts(self):
        data = rows()
        result, _store = save(data, lane_state=dict(WAITING))
        data[0]['comments'].append(comment('ls-foreign', lane_state.PREFIX + canonical_bytes(
            dict(schema_version=1, task=TASK, checkpoint=result['comment_id'], state='working')).decode(),
            author='bob/session'))
        self.assertTrue(b.brief(data, PROJECT, TASK)['checkpoint']['newer_activity'])


class ReaderTests(unittest.TestCase):
    def test_newest_returns_the_usable_record_and_skips_malformed_ones(self):
        data = rows()
        result, _store = save(data, lane_state=dict(WAITING))
        found = lane_state.newest(data[0]['comments'], TASK)
        self.assertIsNotNone(found)
        self.assertEqual(found[1]['state'], 'waiting-on-coordinator')
        self.assertEqual(str(found[2]['id']), result['comment_id'])
        # A malformed record and one naming a checkpoint that is not on the task are
        # skipped, never raised.
        data[0]['comments'].append(comment('junk', lane_state.PREFIX + '{not json'))
        data[0]['comments'].append(comment('ghost', lane_state.PREFIX + json.dumps(
            dict(schema_version=1, task=TASK, state='idle', checkpoint='absent'))))
        self.assertEqual(lane_state.newest(data[0]['comments'], TASK)[1]['state'], 'waiting-on-coordinator')

    def test_a_task_with_no_record_has_no_usable_record(self):
        self.assertIsNone(lane_state.newest(rows()[0]['comments'], TASK))
        self.assertIsNone(lane_state.parse('ordinary prose'))
        self.assertFalse(lane_state.is_record('Kind: something-else-v1\n{}'))


class ReservedCommentTests(unittest.TestCase):
    """A contributor's raw bd cannot write or alter another lane's record."""

    def test_the_prefix_is_reserved(self):
        self.assertIn(lane_state.PREFIX, rc.PREFIXES)
        self.assertIsNotNone(rc.reserved_match(lane_state.PREFIX + '{}'))

    def test_a_raw_comment_cannot_write_a_lane_state_record(self):
        body = lane_state.PREFIX + '{"schema_version": 1}'
        for target in (TASK, 'another-lanes-task'):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, 'Refusing raw'):
                    rc.check_raw_request(['comments', 'add', target, body, '--json'], {},
                                         actor='mallory', task=target)

    def test_a_file_transport_body_cannot_write_one_either(self):
        attachments = {'0': {'flag': '--file', 'text': lane_state.PREFIX + '{}'}}
        with self.assertRaisesRegex(ValueError, 'Refusing raw'):
            rc.check_raw_request(['comments', 'add', TASK, '@attachment:0', '--json'], attachments,
                                 actor='mallory', task=TASK)

    def test_the_record_anchors_no_row_and_is_not_hidden(self):
        body = lane_state.PREFIX + canonical_bytes(dict(WAITING)).decode()
        self.assertFalse(rc.is_record_comment(body))
        self.assertIsNone(rc.record_comment_kind(body))
        row = dict(id=TASK, title='A task', issue_type='task', status='in_progress', assignee='alice/session',
                   labels=['open-item'], comments=[comment('ls', body)])
        self.assertFalse(rc.is_record_anchor(row))
        shown = rc.hide_records([row])
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]['comments'][0]['text'], body)

    def test_the_kind_is_not_a_record_anchor_family(self):
        self.assertNotIn(lane_state.PREFIX, {p for prefixes in rc.RECORD_ANCHOR_FAMILIES.values()
                                             for p in prefixes})
        self.assertFalse(any(lane_state.PREFIX.startswith(prefix) for prefix in rc.RECORD_COMMENT_FAMILIES))


if __name__ == '__main__':
    unittest.main()
