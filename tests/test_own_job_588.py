"""``waiting-on-own-job`` and the lane-state ``local_fact`` (kittrial-5bb.588, part 3).

A worker waiting on ITS OWN job (a test run, a replay, a build) declares ``waiting-on-own-job`` with a
required one-line ``job`` instead of pretending to wait on the coordinator, and may record what it saw
locally in the lane-state record's optional ``local_fact``. Every clock here is injected and derived from BASE.
"""
import copy
import json
import re
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import agent_prompts
import briefing as b
import client
import lane_state
import lanes
import nudges
import waiting
from test_briefing import checkpoint as briefing_checkpoint, rows as briefing_rows, save_cp, TASK as BRIEF_TASK
from test_lanes import A, B, C, PROJECT, add_checkpoint, add_lane_state, board, entry_of, task

OWN = 'waiting-on-own-job'
BASE = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)


def iso(moment):
    return moment.isoformat().replace('+00:00', 'Z')


def own_job_row(task_id='own-1', holder=A, started=None, next_action='next', **lane_fields):
    """A held task whose newest checkpoint began at ``started`` (BASE by default), with an own-job record."""
    started = started or BASE
    row = task(task_id, holder)
    add_checkpoint(row, 'cp-' + task_id, iso(started), next_action=next_action)
    fields = dict(state=OWN, job='the full test run')
    fields.update(lane_fields)
    add_lane_state(row, 'ls-' + task_id, 'cp-' + task_id, iso(started + timedelta(seconds=1)), **fields)
    return row


def payload(**changes):
    result = dict(schema_version=1, task='t1', state=OWN, job='the full test run')
    result.update(changes)
    return result


class StateRuleTests(unittest.TestCase):
    def test_own_job_requires_job(self):
        found = lane_state.problems(dict(schema_version=1, task='t1', state=OWN), 't1')
        self.assertTrue(any('job is required' in sentence for sentence in found), found)

    def test_own_job_with_job_is_valid(self):
        self.assertEqual([], lane_state.problems(payload(), 't1'))

    def test_own_job_refuses_waiting_on(self):
        found = lane_state.problems(payload(waiting_on='coordinator'), 't1')
        self.assertTrue(any('waiting_on is refused' in sentence for sentence in found), found)

    def test_job_is_refused_with_every_other_state(self):
        for state in lane_state.STATES:
            if state == OWN:
                continue
            extra = {'waiting_on': 'coordinator'} if state in lane_state.WAITING_ON_STATES else {}
            if state == lane_state.FAULT_STATE:
                extra = {'reason': 'broken'}
            found = lane_state.problems(payload(state=state, job='x', **extra), 't1')
            self.assertTrue(any('job is refused' in sentence for sentence in found), (state, found))

    def test_the_three_waiting_on_states_still_need_waiting_on(self):
        self.assertEqual(('waiting-on-coordinator', 'waiting-on-review', 'waiting-on-user'),
                         lane_state.WAITING_ON_STATES)
        for state in lane_state.WAITING_ON_STATES:
            found = lane_state.problems(dict(schema_version=1, task='t1', state=state), 't1')
            self.assertTrue(any('waiting_on is required' in sentence for sentence in found), (state, found))
            self.assertEqual([], lane_state.problems(dict(schema_version=1, task='t1', state=state,
                                                          waiting_on='coordinator'), 't1'))

    def test_own_job_allows_expect_by_and_not_before(self):
        now = BASE
        valid = payload(expect_by=iso(now + timedelta(hours=1)), not_before=iso(now + timedelta(hours=2)))
        self.assertEqual([], lane_state.problems(valid, 't1', now=now))

    def test_job_is_one_bounded_printable_line(self):
        self.assertEqual([], lane_state.problems(payload(job='x' * lane_state.JOB_LIMIT), 't1'))
        for bad in ('x' * (lane_state.JOB_LIMIT + 1), 'two\nlines', '', '   ', 7, 'bidi ‮ text'):
            self.assertTrue(lane_state.problems(payload(job=bad), 't1'), repr(bad))

    def test_the_job_is_escaped_when_shown(self):
        shown = lane_state.display_job('a\nb‮c' + 'x' * 500)
        self.assertNotIn('\n', shown)
        self.assertNotIn('‮', shown)
        self.assertLessEqual(len(shown), lane_state.JOB_LIMIT)

    def test_the_field_set_grows_by_job_and_the_schema_stays_one(self):
        self.assertIn('job', lane_state.OPTIONAL_FIELDS)
        self.assertEqual(1, lane_state.SCHEMA_VERSION)
        # An older record without the field still loads.
        row = task('old-1', A)
        add_checkpoint(row, 'cp-old', iso(BASE))
        add_lane_state(row, 'ls-old', 'cp-old', iso(BASE), state='working')
        self.assertEqual('working', lane_state.newest(row['comments'], 'old-1')[1]['state'])

    def test_every_state_list_names_the_state(self):
        self.assertIn(OWN, lane_state.STATES)
        self.assertIn(OWN, lane_state.WAITING_STATES)
        self.assertIn(OWN, nudges.WAIT_STATES)
        # Walk every module-level and class-level list of states in the kit's Python: the ones that name the
        # waiting states must name this one too (WAITING_ON_STATES names who is waited on, so it is the exception).
        for module in (lane_state, nudges, lanes, waiting, client, client._InlineWait, waiting):
            for name, value in vars(module).items():
                if not isinstance(value, (tuple, list)) or 'waiting-on-user' not in value:
                    continue
                if name == 'WAITING_ON_STATES':
                    self.assertNotIn(OWN, value)
                    continue
                self.assertIn(OWN, value, '%s.%s' % (getattr(module, '__name__', module), name))

    def test_the_prose_lists_name_the_state(self):
        self.assertIn(OWN, agent_prompts.LANE_NUDGE)
        for name in ('web/js/views/task.js', 'web/js/views/project.js', 'web/js/agentSetup.js',
                     'docs/LANE_BOARD_DESIGN.md', 'docs/WORKER_START.md', 'templates/CHECKPOINT.md',
                     'docs/BRIEFINGS.md'):
            self.assertIn(OWN, (KIT / name).read_text(encoding='utf-8'), name)
        notes = ' '.join(b.help_notes('checkpoint'))
        self.assertIn(OWN, notes)
        self.assertIn('local_fact', notes)


class BoardTests(unittest.TestCase):
    def lane_with(self, row, review=None):
        entry = lanes.held_task(row, row['id'], A, BASE + timedelta(minutes=5), 900, review=review)
        return {'items': [{'lane': 'lane-a', 'actor': A, 'held': [entry], 'flag_tasks': {}}]}, entry

    def test_it_is_not_a_checkpoint_to_coordinator_question(self):
        # (a) A declared own-job wait whose review state does not make the coordinator the next actor,
        # even when its next_action text says "coordinator", is not a checkpoint-to-coordinator item.
        for review in (None, {'review_state': 'awaiting-review'}):
            lane, entry = self.lane_with(own_job_row(next_action='ask the coordinator to look'), review)
            self.assertEqual(OWN, entry['state'])
            self.assertNotEqual('coordinator', entry['next_actor'])
            for waiting_module in (waiting, client._InlineWait):
                view = waiting_module.coordinator_view([], lane)
                self.assertEqual([], view['triggers']['checkpoint-to-coordinator'], (waiting_module, review))

    def test_own_job_does_not_hide_a_review_wait_on_the_coordinator(self):
        # (b) While the review state makes the coordinator the next actor (an approved contribution awaiting
        # integration), the same expression still fires: own-job suppresses nothing.
        lane, entry = self.lane_with(own_job_row(), {'review_state': 'awaiting-integration'})
        self.assertEqual(OWN, entry['state'])
        self.assertEqual('coordinator', entry['next_actor'])
        for waiting_module in (waiting, client._InlineWait):
            view = waiting_module.coordinator_view([], lane)
            self.assertEqual(['own-1'], [item['task'] for item in view['triggers']['checkpoint-to-coordinator']],
                             waiting_module)

    def test_the_other_coordinator_waits_still_match(self):
        row = task('wc-1', A)
        add_checkpoint(row, 'cp-wc', iso(BASE))
        add_lane_state(row, 'ls-wc', 'cp-wc', iso(BASE), state='waiting-on-coordinator', waiting_on='coordinator')
        result = board([row], now=BASE + timedelta(minutes=5))
        view = waiting.coordinator_view([], result)
        self.assertEqual(['wc-1'], [item['task'] for item in view['triggers']['checkpoint-to-coordinator']])

    def test_it_is_not_inferred_from_next_action_text(self):
        # No record: the guess still reads "coordinator" in the prose (the existing rule, as a control).
        guessed = task('g-1', A)
        add_checkpoint(guessed, 'cp-g', iso(BASE), next_action='ask the coordinator')
        _row, entry = entry_of(board([guessed], now=BASE + timedelta(minutes=5)), 'g-1')
        self.assertTrue(entry['inferred'])
        self.assertEqual('waiting-on-coordinator', entry['state'])
        # A declared own-job record whose next_action says "coordinator" is declared and not a coordinator wait.
        declared = own_job_row(task_id='d-1', next_action='tell the coordinator when the run ends')
        _row, entry = entry_of(board([declared], now=BASE + timedelta(minutes=5)), 'd-1')
        self.assertTrue(entry['declared'])
        self.assertFalse(entry['inferred'])
        self.assertEqual(OWN, entry['state'])
        self.assertEqual('the full test run', entry['job'])
        self.assertIsNone(entry['waiting_on'])

    def test_default_expect_by_is_four_hours_after_the_state_began(self):
        row = own_job_row()
        before = BASE + timedelta(hours=4) - timedelta(seconds=1)
        after = BASE + timedelta(hours=4) + timedelta(seconds=1)
        result = board([row], now=before)
        lane, entry = entry_of(result, 'own-1')
        self.assertEqual(iso(BASE + timedelta(hours=4)), entry['expect_by_effective'])
        self.assertIsNone(entry['expect_by'])
        self.assertNotIn('expect_by_passed', entry['flags'])
        self.assertNotIn('waiting_too_long', entry['flags'])
        before_rows = waiting.coordinator_view([], result)['triggers']['lane-quiet']
        self.assertTrue(all('expect_by_passed' not in row['flags'] for row in before_rows), before_rows)
        # Passed: the EXISTING flag and the existing lane-quiet trigger fire.
        result = board([row], now=after)
        lane, entry = entry_of(result, 'own-1')
        self.assertIn('expect_by_passed', entry['flags'])
        self.assertTrue(lane['expect_by_passed'])
        for view in (waiting.coordinator_view([], result), client._InlineWait.coordinator_view([], result)):
            quiet = view['triggers']['lane-quiet']
            self.assertEqual([A], [row['actor'] for row in quiet])
            self.assertIn('expect_by_passed', quiet[0]['flags'])
            self.assertEqual([], view['triggers']['checkpoint-to-coordinator'])

    def test_it_is_not_quiet_after_the_idle_threshold(self):
        # The own-job wait does not age by the 15-minute idle threshold (waiting_too_long).
        result = board([own_job_row()], now=BASE + timedelta(hours=2))
        _lane, entry = entry_of(result, 'own-1')
        self.assertNotIn('waiting_too_long', entry['flags'])

    def test_a_given_expect_by_is_kept(self):
        row = own_job_row(expect_by=iso(BASE + timedelta(minutes=10)))
        _lane, entry = entry_of(board([row], now=BASE + timedelta(hours=1)), 'own-1')
        self.assertEqual(iso(BASE + timedelta(minutes=10)), entry['expect_by_effective'])
        self.assertIn('expect_by_passed', entry['flags'])

    def test_the_timer_path_is_the_existing_one(self):
        wake = BASE + timedelta(hours=1)
        row = own_job_row(not_before=iso(wake))
        _lane, entry = entry_of(board([row], now=wake + timedelta(minutes=30)), 'own-1')
        self.assertTrue(entry['not_before_due'])
        result = board([row], now=wake + timedelta(minutes=30))
        self.assertEqual(['own-1'], [item['task'] for item in waiting.coordinator_view([], result)['triggers']['timer-due']])

    def test_nudges_skip_it_before_its_expect_by_and_not_after(self):
        for expected_wait, now in ((True, BASE + timedelta(hours=1)), (False, BASE + timedelta(hours=5))):
            result = board([own_job_row()], now=now)
            wait = nudges.declared_wait(result['items'][0])
            if expected_wait:
                self.assertEqual(OWN, wait['state'])
                self.assertIsNone(wait['waiting_on'])
            else:
                self.assertIsNone(wait)

    def test_the_label_facts_are_on_the_row(self):
        result = board([own_job_row()], now=BASE + timedelta(hours=1))
        _lane, entry = entry_of(result, 'own-1')
        self.assertEqual(('the full test run', iso(BASE + timedelta(seconds=0))),
                         (entry['job'], entry['state_since']))


def local_fact(**changes):
    result = dict(kind='job-finished', what='the full test run passed', at=iso(BASE))
    result.update(changes)
    return result


class LocalFactTests(unittest.TestCase):
    def test_a_valid_fact_is_accepted_with_and_without_detail(self):
        self.assertEqual([], lane_state.local_fact_problems(local_fact()))
        self.assertEqual([], lane_state.local_fact_problems(local_fact(detail='1204 tests, 0 failures')))
        self.assertEqual([], lane_state.local_fact_problems(local_fact(at='2026-11-05T17:30:00+02:00')))

    def test_every_kind_is_accepted_and_nothing_else(self):
        for kind in ('job-finished', 'job-failed', 'job-partial', 'time-reached', 'file-seen'):
            self.assertEqual([], lane_state.local_fact_problems(local_fact(kind=kind)), kind)
        for kind in ('job-done', '', None, 4, ['job-finished']):
            self.assertTrue(lane_state.local_fact_problems(local_fact(kind=kind)), repr(kind))

    def test_what_and_detail_are_bounded(self):
        self.assertEqual([], lane_state.local_fact_problems(local_fact(what='w' * 160)))
        self.assertTrue(lane_state.local_fact_problems(local_fact(what='w' * 161)))
        self.assertTrue(lane_state.local_fact_problems(local_fact(what='two\nlines')))
        self.assertTrue(lane_state.local_fact_problems(local_fact(what='  ')))
        self.assertEqual([], lane_state.local_fact_problems(local_fact(detail='d' * 300)))
        self.assertTrue(lane_state.local_fact_problems(local_fact(detail='d' * 301)))

    def test_it_is_a_closed_object(self):
        self.assertTrue(lane_state.local_fact_problems(local_fact(extra='x')))
        self.assertTrue(lane_state.local_fact_problems('a string'))
        for missing in ('kind', 'what', 'at'):
            fact = local_fact()
            del fact[missing]
            self.assertTrue(lane_state.local_fact_problems(fact), missing)

    def test_at_needs_seconds_and_an_offset(self):
        for bad in ('2026-11-05T17:30Z', '2026-11-05T17:30:00', '2026-11-05', 'yesterday', '', 5,
                    '2026-13-45T17:30:00Z'):
            self.assertTrue(lane_state.local_fact_problems(local_fact(at=bad)), repr(bad))

    def test_it_rides_in_the_lane_state_record_and_not_in_the_checkpoint(self):
        # The checkpoint's key set is closed and unchanged: local_fact there is an unknown field.
        data = briefing_rows()
        refused = b.checkpoint_problems(briefing_checkpoint(data, local_fact=local_fact()), BRIEF_TASK)
        self.assertTrue(any('unknown fields: local_fact' in sentence for sentence in refused), refused)
        # In the lane-state record it is valid with ANY state, and a bad one is refused there.
        for state in lane_state.STATES:
            extra = {'waiting_on': 'user'} if state in lane_state.WAITING_ON_STATES else {}
            extra.update({'job': 'a run'} if state == OWN else {})
            extra.update({'reason': 'broken'} if state == lane_state.FAULT_STATE else {})
            record = dict(schema_version=1, task='t1', state=state, local_fact=local_fact(), **extra)
            self.assertEqual([], lane_state.problems(record, 't1'), state)
        bad = lane_state.problems(payload(local_fact=local_fact(kind='nope')), 't1')
        self.assertTrue(any('local_fact.kind' in sentence for sentence in bad), bad)
        self.assertIn('local_fact', lane_state.OPTIONAL_FIELDS)

    def test_the_line_is_shown_as_observed_locally_and_escaped(self):
        fact = local_fact(what='a\nb')
        self.assertEqual('Observed locally: the full test run passed (job-finished) at ' + iso(BASE),
                         lane_state.display_local_fact(local_fact()))
        self.assertNotIn('\n', lane_state.display_local_fact(fact))

    def test_a_checkpoint_that_only_adds_a_local_fact_is_accepted(self):
        data = briefing_rows()
        save_cp(data, 'cp-one')
        writes = []

        def run(args):
            writes.append(args)
            return json.dumps(dict(id='cp-two' if len(writes) == 1 else 'ls-two'))
        # Same content, the same activity cursor (no new server activity); the only change is a lane_state
        # that carries a new local_fact.
        second = briefing_checkpoint(data, lane_state=dict(schema_version=1, task=BRIEF_TASK, state='working',
                                                           local_fact=local_fact(detail='exit 0')))
        self.assertEqual('cp-one', second['previous'])
        answer = b.save_checkpoint(data, PROJECT, BRIEF_TASK, second, 'alice/session', run, provenance_writes=True)
        self.assertFalse(answer['reconciled'])
        self.assertEqual('cp-two', answer['comment_id'])
        stored = json.loads(writes[0][3][len(b.PREFIX):])
        self.assertNotIn('local_fact', stored)
        record = json.loads(writes[1][3][len(lane_state.PREFIX):])
        self.assertEqual(local_fact(detail='exit 0'), record['local_fact'])
        self.assertEqual('cp-two', record['checkpoint'])
        # The brief shows it from the lane-state record and moves nothing.
        data[0]['comments'].append(dict(id='cp-two', text=writes[0][3], author='alice/session',
                                        created_at='2026-09-15T10:00:00Z'))
        data[0]['comments'].append(dict(id='ls-two', text=writes[1][3], author='alice/session',
                                        created_at='2026-09-15T10:00:01Z'))
        brief = b.brief(data, PROJECT, BRIEF_TASK)
        self.assertEqual(local_fact(detail='exit 0'), brief['checkpoint']['local_fact'])

    def test_the_board_row_carries_the_fact(self):
        row = task('lf-1', A)
        add_checkpoint(row, 'cp-lf', iso(BASE))
        add_lane_state(row, 'ls-lf', 'cp-lf', iso(BASE), state='working', local_fact=local_fact())
        _lane, entry = entry_of(board([row], now=BASE + timedelta(minutes=5)), 'lf-1')
        self.assertEqual(local_fact(), entry['local_fact'])
        _lane, entry = entry_of(board([task('lf-2', B)], now=BASE), 'lf-2')
        self.assertIsNone(entry['local_fact'])


class WebTests(unittest.TestCase):
    def test_the_label_and_the_observed_line(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        script = """
            await import(process.argv[1]);
            const project = await import(process.argv[2]);
            const task = await import(process.argv[3]);
            const entry = { state: 'waiting-on-own-job', job: 'the <b>full</b> run\\u202e', state_since: 'S', expect_by_effective: 'E' };
            console.log(JSON.stringify({
              label: project.ownJobLabelText(entry),
              fact: project.localFactText({ kind: 'job-finished', what: 'it passed', at: 'T' }),
              note: task.localFactNote({ kind: 'job-failed', what: 'it <i>failed</i>', at: 'T2', detail: 'exit 1' }).textContent,
              none: project.localFactText(null),
              nudgeStates: project.NUDGE_WAIT_STATES,
            }));
        """
        done = test_http_web.run_node_module(
            self, node, script,
            (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
            (KIT / 'web' / 'js' / 'views' / 'project.js').as_uri(),
            (KIT / 'web' / 'js' / 'views' / 'task.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual('waiting on own job: the <b>full</b> run\\u202e, since S, expected by E', seen['label'])
        self.assertEqual('Observed locally: it passed (job-finished) at T', seen['fact'])
        self.assertTrue(seen['note'].startswith('Observed locally: it <i>failed</i> (job-failed) at T2'), seen['note'])
        self.assertIsNone(seen['none'])
        self.assertIn(OWN, seen['nudgeStates'])


if __name__ == '__main__':
    unittest.main()
