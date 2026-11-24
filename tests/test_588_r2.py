"""kittrial-5bb.588 r2: the independent review of r1 (review-588r1.md), one class per finding.

MUSTs:
- m1: a declared own-job wait raises no flag at all (so no lane-quiet) before its effective expect_by,
  even past the idle threshold; after it, the existing flags and lane-quiet fire.
- m2: ``local_fact`` text refuses NUL and every non-printable character, in ``detail`` as in ``what``.
- m3: every .588 test module ends with its ``unittest.main()`` block, so a direct run runs every class.
- m4: the in-process brief takes ``newer``, ``activity`` and ``changes_since_checkpoint`` from ONE
  baseline, the assignee's own newest checkpoint.
SHOULDs S1-S5 and notes N1, N3 follow, each named.
"""
import ast
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import briefing as b
import claim_limit
import lane_state
import nudges
import waiting
import test_http_agents
from test_briefing import PROJECT, STAMP, TASK, comment, rows, save_cp
from test_lanes import A, add_checkpoint, add_lane_state, board, entry_of, task
from test_own_job_588 import BASE, OWN, iso, local_fact, own_job_row
from test_wake_activity_588 import COORD, LANE, later, queue_row, save_cp_by


class MustOwnJobIsQuietUntilItsExpectBy(unittest.TestCase):
    """m1: lanes.py set your_move_idle after 15 minutes during an own-job wait."""

    def test_no_flag_and_no_lane_quiet_before_the_effective_expect_by(self):
        row = own_job_row()
        for after in (timedelta(minutes=20), timedelta(hours=2), timedelta(hours=4) - timedelta(seconds=1)):
            with self.subTest(after=after):
                result = board([row], now=BASE + after)
                lane, entry = entry_of(result, 'own-1')
                self.assertEqual([], entry['flags'])
                self.assertFalse(lane.get('your_move_idle'))
                self.assertEqual([], waiting.coordinator_view([], result)['triggers']['lane-quiet'])

    def test_after_it_the_existing_flags_and_lane_quiet_fire(self):
        result = board([own_job_row()], now=BASE + timedelta(hours=4, seconds=1))
        _lane, entry = entry_of(result, 'own-1')
        self.assertEqual(['expect_by_passed', 'your_move_idle'], sorted(entry['flags']))
        quiet = waiting.coordinator_view([], result)['triggers']['lane-quiet']
        self.assertEqual([A], [item['actor'] for item in quiet])

    def test_a_given_expect_by_bounds_the_quiet_time_too(self):
        row = own_job_row(expect_by=iso(BASE + timedelta(minutes=30)))
        _lane, entry = entry_of(board([row], now=BASE + timedelta(minutes=20)), 'own-1')
        self.assertEqual([], entry['flags'])
        _lane, entry = entry_of(board([row], now=BASE + timedelta(minutes=31)), 'own-1')
        self.assertIn('expect_by_passed', entry['flags'])
        self.assertIn('your_move_idle', entry['flags'])

    def test_a_working_lane_is_still_quiet_after_the_idle_threshold(self):
        row = task('w-1', A)
        add_checkpoint(row, 'cp-w', iso(BASE))
        add_lane_state(row, 'ls-w', 'cp-w', iso(BASE), state='working')
        _lane, entry = entry_of(board([row], now=BASE + timedelta(minutes=20)), 'w-1')
        self.assertIn('your_move_idle', entry['flags'])


class MustLocalFactTextIsPrintable(unittest.TestCase):
    """m2: lane_state passed nul=False and checked only ``what`` for printable text."""

    def refused(self, text):
        for key in ('what', 'detail'):
            with self.subTest(key=key):
                found = lane_state.local_fact_problems(local_fact(**{key: 'a' + text + 'b'}))
                self.assertTrue(found, (key, text))
                self.assertTrue(all(('local_fact.' + key) in sentence for sentence in found), found)

    def test_a_nul_is_refused(self):
        self.refused('\x00')

    def test_a_newline_is_refused(self):
        self.refused('\n')

    def test_a_carriage_return_or_tab_is_refused(self):
        self.refused('\r')
        self.refused('\t')

    def test_an_ansi_escape_is_refused(self):
        self.refused('\x1b[31m')

    def test_a_c1_control_is_refused(self):
        self.refused('\x85')
        self.refused('\x9b')

    def test_a_bidi_override_or_isolate_is_refused(self):
        self.refused('‮')
        self.refused('⁦')

    def test_a_line_or_paragraph_separator_is_refused(self):
        self.refused(' ')
        self.refused(' ')

    def test_ordinary_text_and_other_scripts_are_accepted(self):
        for text in ('1204 tests, 0 failures (exit 0)', 'Läufe: 3 – ok', '完了', 'path/to/log.txt: 12 KB'):
            self.assertEqual([], lane_state.local_fact_problems(local_fact(detail=text)), text)
            self.assertEqual([], lane_state.local_fact_problems(local_fact(what=text)), text)


class MustMainBlockIsLast(unittest.TestCase):
    """m3: a ``unittest.main()`` block before a class drops that class from a direct run."""

    def test_every_588_module_ends_with_its_main_block(self):
        modules = sorted(Path(__file__).resolve().parent.glob('test_*588*.py'))
        self.assertGreaterEqual(len(modules), 4, modules)
        for path in modules:
            with self.subTest(module=path.name):
                body = ast.parse(path.read_text(encoding='utf-8')).body
                mains = [index for index, node in enumerate(body)
                         if isinstance(node, ast.If) and 'unittest.main' in ast.unparse(node)]
                self.assertEqual([len(body) - 1], mains, 'the main block must be the last statement')


class ShouldWakeSummaryNamesAFieldEdit(unittest.TestCase):
    """S1: an edit of the title, description or acceptance moves the fingerprint; the sentence says the task changed.

    The checkpoint records no per-field digest, so the sentence cannot say WHICH field; it says the task
    itself changed, whenever the checkpoint is stale and no entry explains it."""

    CHANGED = ('Nothing new by others since your checkpoint at %s; the task itself changed after it '
               '(a field, its status, assignee, labels or dependencies).' % STAMP)

    def test_a_title_edit_is_said(self):
        data = rows()
        save_cp(data, 'cp1')
        data[0]['title'] = 'A task with a longer title'
        self.assertEqual(self.CHANGED, queue_row(data)['wake_summary'])

    def test_a_description_or_acceptance_edit_is_said(self):
        for name in ('description', 'acceptance_criteria'):
            with self.subTest(field=name):
                data = rows()
                save_cp(data, 'cp1')
                data[0][name] = 'Edited'
                self.assertEqual(self.CHANGED, queue_row(data)['wake_summary'])

    def test_brief_says_the_same(self):
        data = rows()
        save_cp(data, 'cp1')
        data[0]['title'] = 'A task with a longer title'
        self.assertEqual(self.CHANGED, b.brief(data, PROJECT, TASK)['activity']['wake_summary'])

    def test_the_lanes_own_comment_is_not_a_task_change(self):
        data = rows()
        save_cp(data, 'cp1')
        data[0]['comments'].append(comment('c-own', 'My note', later(60)))
        self.assertEqual('Nothing new by others since your checkpoint at %s.' % STAMP, queue_row(data)['wake_summary'])

    def test_nothing_changed_still_says_nothing_new(self):
        data = rows()
        save_cp(data, 'cp1')
        self.assertEqual('Nothing new by others since your checkpoint at %s.' % STAMP, queue_row(data)['wake_summary'])


class ShouldNoStaleLocalFact(unittest.TestCase):
    """S2: the brief showed an older record's local_fact on a newer checkpoint that wrote none."""

    def test_a_newer_checkpoint_without_a_lane_state_shows_no_local_fact(self):
        data = rows()
        save_cp(data, 'cp1')
        record = dict(schema_version=1, task=TASK, checkpoint='cp1', state='working',
                      local_fact=local_fact(kind='job-failed', what='old failure'))
        data[0]['comments'].append(comment('ls1', lane_state.PREFIX + json.dumps(record), STAMP))
        self.assertEqual('old failure', b.brief(data, PROJECT, TASK)['checkpoint']['local_fact']['what'])
        save_cp(data, 'cp2')
        brief = b.brief(data, PROJECT, TASK)
        self.assertEqual('cp2', brief['checkpoint']['comment_id'])
        self.assertNotIn('local_fact', brief['checkpoint'])


class ShouldOwnJobEpisodeDoesNotSlide(unittest.TestCase):
    """S4: re-posting waiting-on-own-job must not restart its 4 h clock (the .598 episode rule)."""

    def reposted(self, states):
        """Checkpoints by the holder every 3 h from BASE, each with a lane-state record of ``states[i]``."""
        row = task('rep-1', A)
        previous = None
        for index, state in enumerate(states):
            at = BASE + timedelta(hours=3 * index)
            cid = 'cp-%d' % index
            add_checkpoint(row, cid, iso(at), previous=previous)
            fields = dict(state=state, **({'job': 'the run, try %d' % index} if state == OWN else {}))
            add_lane_state(row, 'ls-%d' % index, cid, iso(at + timedelta(seconds=1)), **fields)
            previous = cid
        return row

    def test_repeated_own_job_waits_keep_the_first_start(self):
        result = board([self.reposted([OWN, OWN, OWN, OWN])], now=BASE + timedelta(hours=10))
        lane, entry = entry_of(result, 'rep-1')
        self.assertEqual(iso(BASE), entry['state_since'])
        self.assertEqual(iso(BASE + timedelta(hours=4)), entry['expect_by_effective'])
        self.assertIn('expect_by_passed', entry['flags'])
        self.assertIsNone(nudges.declared_wait(lane))

    def test_another_state_between_starts_a_new_episode(self):
        result = board([self.reposted([OWN, 'working', OWN])], now=BASE + timedelta(hours=7))
        lane, entry = entry_of(result, 'rep-1')
        self.assertEqual(iso(BASE + timedelta(hours=6)), entry['state_since'])
        self.assertEqual(iso(BASE + timedelta(hours=10)), entry['expect_by_effective'])
        self.assertEqual([], entry['flags'])
        self.assertEqual(OWN, nudges.declared_wait(lane)['state'])


class ShouldWeakTestsMadeStrong(unittest.TestCase):
    """S5: one test for each mutant that survived r1 (M6-M12 in the review)."""

    def test_m6_ctypes_is_imported_inside_the_windows_branch_only(self):
        tree = ast.parse((KIT / 'client.py').read_text(encoding='utf-8'))
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import)
                   and any(alias.name == 'ctypes' for alias in node.names)
                   or isinstance(node, ast.ImportFrom) and node.module == 'ctypes']
        self.assertTrue(imports)
        alive = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_process_alive')
        posix = next(node for node in alive.body if isinstance(node, ast.If) and "'win32'" in ast.unparse(node.test))
        inside_posix = {id(node) for statement in posix.body for node in ast.walk(statement)}
        inside_alive = {id(node) for node in ast.walk(alive)}
        for node in imports:
            self.assertIn(id(node), inside_alive, 'ctypes imported outside _process_alive, line %d' % node.lineno)
            self.assertNotIn(id(node), inside_posix, 'ctypes imported in the POSIX branch, line %d' % node.lineno)

    def test_m7_a_sub_label_of_the_actor_waited_on_counts(self):
        data = rows()
        save_cp(data, 'cp1')
        cp = next(c for c in data[0]['comments'] if c['id'] == 'cp1')
        data[0]['comments'].append(comment('c1', 'Answer from my worker label', later(60), author=COORD + '/w1'))
        self.assertTrue(b.wrote_since(data[0], cp, COORD))

    def test_m8_the_lane_state_record_written_with_the_checkpoint_never_counts(self):
        data = rows()
        save_cp(data, 'cp1')
        save_cp_by(data, 'cp2', COORD, later(60))
        record = dict(schema_version=1, task=TASK, checkpoint='cp2', state='working')
        data[0]['comments'].append(comment('ls2', lane_state.PREFIX + json.dumps(record), later(61), author=COORD))
        cp2 = next(c for c in data[0]['comments'] if c['id'] == 'cp2')
        self.assertFalse(b.wrote_since(data[0], cp2, COORD))
        data[0]['comments'].append(comment('c3', 'And a real answer', later(62), author=COORD))
        self.assertTrue(b.wrote_since(data[0], cp2, COORD))

    def test_m9_an_entry_in_the_same_second_as_the_checkpoint_is_named(self):
        data = rows()
        save_cp(data, 'cp1')
        data[0]['comments'].append(comment('c-coord', 'Same second', STAMP, author=COORD))
        summary = queue_row(data)['wake_summary']
        self.assertIn('newest by %s at %s' % (COORD, STAMP), summary)

    def test_m10_an_overdue_wait_on_another_party_still_counts(self):
        row = task('wc-1', A)
        add_checkpoint(row, 'cp-wc', iso(BASE))
        add_lane_state(row, 'ls-wc', 'cp-wc', iso(BASE), state='waiting-on-coordinator', waiting_on='coordinator',
                       expect_by=iso(BASE + timedelta(minutes=10)))
        result = board([row], now=BASE + timedelta(hours=5))
        lane, entry = entry_of(result, 'wc-1')
        self.assertIn('expect_by_passed', entry['flags'])
        self.assertTrue(nudges.counts_as_wait(entry))
        self.assertEqual('waiting-on-coordinator', nudges.declared_wait(lane)['state'])

    def test_m11_the_shown_line_is_clipped_to_the_limits(self):
        stored = dict(kind='job-finished', what='w' * 500, at=iso(BASE) + 'x' * 100)
        shown = lane_state.display_local_fact(stored)
        self.assertEqual('Observed locally: %s (job-finished) at %s' % ('w' * lane_state.LOCAL_FACT_WHAT_LIMIT,
                                                                       (iso(BASE) + 'x' * 100)[:40]), shown)

    def test_m12_a_condition_already_true_wakes_before_any_sleep(self):
        from test_client_local_waits_588 import Harness

        class Probe(Harness):
            def runTest(self):
                pass
        probe = Probe()
        probe.setUp()
        try:
            code, view, _ = probe.wait('--interval', '60', '--until-file', '/x/y', exists=lambda p: True)
            self.assertEqual((0, 'file-seen'), (code, view['local_wake']['kind']))
            self.assertEqual([], probe.time.sleeps)
            self.assertEqual(1, len(probe.requests))
        finally:
            probe.doCleanups()


class ShouldUntilFileWithTimeoutZero(unittest.TestCase):
    """S3: ``--timeout 0`` with a file already there timed out instead of waking."""

    def harness(self):
        from test_client_local_waits_588 import Harness

        class Probe(Harness):
            def runTest(self):
                pass
        probe = Probe()
        probe.setUp()
        self.addCleanup(probe.doCleanups)
        return probe

    def test_an_existing_file_wakes_with_timeout_zero(self):
        probe = self.harness()
        code, view, _ = probe.wait('--timeout', '0', '--until-file', '/x/y', exists=lambda p: True)
        self.assertEqual((0, False, 'file-seen'), (code, view['timed_out'], view['local_wake']['kind']))
        self.assertEqual(probe.fingerprint, view['fingerprint'])

    def test_an_exited_process_wakes_with_timeout_zero(self):
        probe = self.harness()
        code, view, _ = probe.wait('--timeout', '0', '--until-exit', '4242', alive=lambda p: False)
        self.assertEqual((0, 'process-exited'), (code, view['local_wake']['kind']))

    def test_a_missing_file_still_times_out(self):
        probe = self.harness()
        code, view, _ = probe.wait('--timeout', '0', '--until-file', '/x/y', exists=lambda p: False)
        self.assertEqual((1, True), (code, view['timed_out']))
        self.assertNotIn('local_wake', view)

    def test_a_server_event_still_comes_first(self):
        probe = self.harness()
        probe.changes_at = 1
        code, view, _ = probe.wait('--timeout', '0', '--until-file', '/x/y', exists=lambda p: True)
        self.assertEqual(0, code)
        self.assertNotIn('local_wake', view)
        self.assertNotEqual(probe.fingerprint, view['fingerprint'])


class NoteWaitingSentence(unittest.TestCase):
    """N1: the declared-wait sentence said "waiting on waiting-on-own-job"."""

    def test_the_own_job_sentence_says_its_own_job(self):
        sentence = nudges.waiting_sentence({'state': OWN, 'waiting_on': None, 'since': iso(BASE)})
        self.assertEqual('This lane says it is waiting on its own job since %s. Nudge it anyway? Send the request '
                         'again with acknowledge_declared_wait set to true.' % iso(BASE), sentence)
        self.assertNotIn('waiting on waiting', sentence)
        sentence = nudges.waiting_sentence({'state': 'waiting-on-user', 'waiting_on': 'user', 'since': iso(BASE)})
        self.assertTrue(sentence.startswith('This lane says it is waiting on user since '), sentence)


class NoteSameSecondOrder(unittest.TestCase):
    """N3: the two other same-second orderings use the tracker's sequence too (c10 is newer than c9)."""

    def test_changes_since_checkpoint_lists_c10_before_c9(self):
        entries = [{'entry_id': TASK + '-c10', 'kind': 'comment', 'author': COORD, 'timestamp': later(5), 'body': 'ten'},
                   {'entry_id': TASK + '-c9', 'kind': 'comment', 'author': COORD, 'timestamp': later(5), 'body': 'nine'}]
        section = b.changes_since_checkpoint({'task': TASK, 'entries': entries}, None,
                                             {'comment_id': 'cp1', 'author': LANE, 'timestamp': STAMP}, LANE)
        # Newest first: the tenth comment of the second is the newer one.
        self.assertEqual([TASK + '-c10', TASK + '-c9'], [entry['entry_id'] for entry in section['entries']])

    def test_the_work_summary_last_post_is_the_tenth_comment(self):
        import work_summary
        row = dict(rows()[0], comments=[comment('9', 'nine', later(5), author=COORD),
                                        comment('10', 'ten', later(5), author=COORD)])
        self.assertEqual('10', work_summary.item(row)['last_post']['comment_id'])
        row['comments'].reverse()
        self.assertEqual('10', work_summary.item(row)['last_post']['comment_id'])


class MustInProcessBriefUsesOneBaseline(test_http_agents.AgentHarness):
    """m4: the in-process brief anchored ``newer`` and ``changes_since_checkpoint`` on the newest checkpoint."""

    def test_newer_activity_and_changes_agree_after_a_coordinator_checkpoint(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        _agent, secret, _ = self.agent_secret(alex, projects=[project], claim_limit=claim_limit.LIMIT_MAX)
        task_id = self.request('POST', '/v1/projects/%s/tasks' % project, {'title': 'the task'}, token=alex).data['id']
        base = '/v1/projects/%s/tasks/%s' % (project, task_id)
        self.assertEqual(200, self.request('POST', base + '/claim', token=secret).status)

        def checkpoint(token, previous, summary):
            page = self.request('GET', base + '/brief', token=token).data
            made = self.request('POST', base + '/checkpoints', {
                'schema_version': 1, 'previous': previous, 'activity_cursor': page.get('activity_cursor') or '',
                'source_commit': '', 'branch': '', 'intent': 'do the task', 'acceptance': 'the checks pass',
                'summary': summary, 'next_action': 'wait', 'open_items': [], 'resolved': []}, token=token)
            self.assertEqual(201, made.status, made.data)
            return made.data['checkpoint']['id']

        mine = checkpoint(secret, None, 'stopped')
        said = self.request('POST', base + '/comments', {'text': 'A direction.'}, token=alex)
        self.assertIn(said.status, (200, 201), said.data)
        checkpoint(alex, mine, 'the owner records where things are')
        brief = self.request('GET', base + '/brief', token=secret).data
        others = brief['activity']['newer_activity_by_others']
        self.assertGreaterEqual(others, 1)
        self.assertEqual(others, brief['newer']['other_count'])
        self.assertEqual(mine, brief['changes_since_checkpoint']['checkpoint']['comment_id'])
        self.assertGreaterEqual(brief['changes_since_checkpoint']['total'], 1)
        self.assertNotIn('Nothing new', brief['activity']['wake_summary'])


if __name__ == '__main__':
    unittest.main()
