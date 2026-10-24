"""Selection facts from the one shared projection (kittrial-5bb.6).

Three additive deliverables, all derived in ``review_state.effective`` so `review`,
`brief` and `work` cannot disagree:

* a next-action ACTOR field, separate from the assignee (`next_actor`);
* a bounded claimable-work view that excludes epics, decisions, gates, jobs and
  delivered or owned tasks (`work.claimable`, the `claimable` row field,
  `work --claimable`, the HTTP agent claimable list);
* a filterable `reconciliation-needed` work state that shows the mismatch when the
  recorded lifecycle scope lags the current contribution or integration facts
  disagree, without clearing any blocker.

The tests here are in-memory except the HTTP case, which uses the in-process backend
harness (and is therefore exercised on the Linux runner).
"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import briefing
import review_state
import test_review_state as shared
import test_work_queue
import work
from requirements import canonical_bytes
from test_briefing import PROJECT, TASK, append_checkpoint, checkpoint, rows, save_cp
from test_http_agents import AgentHarness
from test_lifecycle import NativeStore, payload, scope

COMMIT = shared.COMMIT
OTHER = shared.OTHER


def surfaces(row_list, actor='alice/session'):
    """The three read surfaces under test, from the same rows."""
    review = work.execute(Path('.'), actor, 'review', [TASK], {},
                          lambda args: '\n'.join(json.dumps(r) for r in row_list))
    brief = briefing.brief(row_list, PROJECT, TASK)
    item = work.queue(row_list, actor, [])['items'][0]
    return review, brief, item


def delivered_rows(commit=COMMIT, operation='contribute'):
    data = rows()
    data[0]['assignee'] = 'alice/session'
    shared.add(data, dict(schema_version=1, operation=operation, operation_id='send-1', task=TASK,
                          previous=None, repository='ssh://git/example', commit=commit,
                          base_commit='b' * 40,
                          delivery=dict(kind='bundle', path='server:/rev.bundle', sha256='c' * 64),
                          summary='Contribution delivered', supersedes=None), 'delivery')
    return data


class BadRecord:
    """One unparseable request record, as the journal reader sees it."""

    name = 'bad.json'

    def read_text(self, encoding=None):
        raise ValueError('bad json')


class BadJournal:
    """A request directory holding only :class:`BadRecord` (no filesystem needed)."""

    def is_dir(self):
        return True

    def glob(self, pattern):
        return [BadRecord()]


class NextActorUnitTests(unittest.TestCase):
    """`next_actor` is a role token, closed set, never the assignee field itself."""

    def test_every_state_maps_into_the_documented_set(self):
        for state in work.WORK_STATES + [None, 'approved']:
            for assignee in ('alice/session', None):
                for closed in (False, True):
                    with self.subTest(state=state, assignee=assignee, closed=closed):
                        self.assertIn(review_state.next_actor(state, assignee, closed),
                                      review_state.NEXT_ACTORS)

    def test_the_role_follows_the_state_and_the_assignee_presence(self):
        self.assertEqual(review_state.next_actor('changes-requested', 'alice/session'), 'assignee')
        self.assertEqual(review_state.next_actor('awaiting-review', 'alice/session'), 'reviewer')
        self.assertEqual(review_state.next_actor('legacy-review-ready', None), 'reviewer')
        self.assertEqual(review_state.next_actor('awaiting-integration', 'alice/session'), 'coordinator')
        # kittrial-5bb.336: open integrated work is the coordinator's to close or
        # record, with or without an assignee - never the assignee's guess.
        self.assertEqual(review_state.next_actor('integrated', 'alice/session'), 'coordinator')
        self.assertEqual(review_state.next_actor('integrated', None), 'coordinator')
        self.assertEqual(review_state.next_actor('error', 'alice/session'), 'operator')
        self.assertEqual(review_state.next_actor('reconciliation-needed', 'alice/session'), 'operator')
        self.assertEqual(review_state.next_actor('none', 'alice/session'), 'assignee')
        self.assertEqual(review_state.next_actor('none', None), 'anyone')
        self.assertEqual(review_state.next_actor(None, None), 'anyone')
        self.assertEqual(review_state.next_actor('not-a-state', 'alice/session'), 'unknown')

    def test_the_role_is_unknown_where_the_move_cannot_be_told(self):
        """Item 2: the two disagreements revision 1 shipped are gone, and no null.

        A withdrawn or superseded final revision with nothing open owes nobody a move;
        an unassigned row nobody could claim is not `anyone`; a row whose recorded
        evidence must be reconciled keeps the REVIEW state's role, because the
        reconciliation signal is `work_state` and never a review state.
        """
        for state in ('withdrawn', 'superseded'):
            self.assertEqual(review_state.next_actor(state, 'alice/session'), 'unknown')
            self.assertEqual(review_state.next_actor(state, 'alice/session', closed=True), 'unknown')
            # ... but an open item on the final revision is still the assignee's work.
            self.assertEqual(review_state.next_actor(state, 'alice/session', pending_items=1), 'assignee')
        # Unassigned and undelivered, but not claimable: nothing can take it.
        self.assertEqual(review_state.next_actor('none', None, claimable=False), 'unknown')
        self.assertEqual(review_state.next_actor('none', None, claimable=True), 'anyone')
        # A reconciliation row keeps the review state it shows (never `operator`, which
        # is the role of the reconciliation STATE token, not of the signal).
        self.assertEqual(review_state.next_actor('awaiting-review', 'alice/session',
                                                 pending_items=0, claimable=False), 'reviewer')
        self.assertEqual(review_state.next_actor('integrated', 'alice/session',
                                                 pending_items=0, claimable=False), 'coordinator')

    def test_a_closed_settled_task_has_no_next_actor_but_active_review_does(self):
        self.assertEqual(review_state.next_actor('integrated', 'alice/session', closed=True), 'none')
        self.assertEqual(review_state.next_actor('none', 'alice/session', closed=True), 'none')
        # A closed task with outstanding requested changes is still the assignee's move:
        # closure is not acceptance (kittrial-5bb.94), and the work queue keeps it listed.
        self.assertEqual(review_state.next_actor('changes-requested', 'alice/session', closed=True), 'assignee')


class SharedNextActorTests(unittest.TestCase):
    """The three read surfaces report one `next_actor` for the same input."""

    def test_a_delivered_contribution_awaiting_review_names_the_reviewer(self):
        review, brief, item = surfaces(delivered_rows())
        self.assertEqual((review['review_state'], brief['review']['review_state'], item['review_state']),
                         ('awaiting-review',) * 3)
        # The brief carries the role ONCE, in its shared review block (review item 2).
        self.assertEqual([review['next_actor'], brief['review']['next_actor'],
                          item['next_actor']], ['reviewer'] * 3)
        self.assertNotIn('next_actor', brief)

    def test_requested_changes_name_the_assignee_not_the_reviewer(self):
        review, brief, item = surfaces(test_work_queue.reviewed_rows())
        self.assertEqual([review['next_actor'], brief['review']['next_actor'],
                          item['next_actor']], ['assignee'] * 3)
        # The assignee field is still exactly the assignee and is untouched by the new field.
        self.assertEqual(item['owner'], 'alice/session')

    def test_open_integrated_work_is_the_coordinators_and_free_work_anyone(self):
        store = shared.reviewed()
        save_cp(store.rows, 'read-integration')
        review, brief, item = surfaces(store.rows)
        # kittrial-5bb.336: integration of one revision leaves the OPEN task the
        # coordinator's to close or record, on every surface alike.
        self.assertEqual(item['whose_move'], 'coordinator')
        self.assertIn('the coordinator closes the task or records what remains', item['next'])
        self.assertEqual([review['next_actor'], brief['review']['next_actor'],
                          item['next_actor']], ['coordinator'] * 3)
        free = rows()
        free[0].update(assignee=None, status='open', comments=[])
        review, brief, item = surfaces(free)
        self.assertTrue(item['claimable'])
        self.assertEqual([review['next_actor'], brief['review']['next_actor'],
                          item['next_actor']], ['anyone'] * 3)
        self.assertIsNone(item['owner'])

    def test_an_unassigned_row_nobody_could_claim_names_no_one(self):
        """Item 2: `anyone` on a row whose claimable is false was a claim the row denies."""
        for change in (dict(issue_type='epic'), dict(issue_type='decision'), dict(issue_type='job'),
                       dict(issue_type='gate'), dict(issue_type='merge-slot'),
                       dict(labels=['decision']), dict(status='blocked'),
                       dict(status='in_progress')):
            free = rows()
            free[0].update(assignee=None, status='open', comments=[], labels=[])
            free[0].update(change)
            self.assertFalse(work.claimable(free[0].get('issue_type'), free[0]['status'], None, None,
                                            'none', labels=free[0].get('labels')), change)
            # The one rule drives the role: a row nobody can take names no mover.
            self.assertEqual(review_state.next_actor('none', None, claimable=False), 'unknown', change)
        # A row the queue still lists (a task LABELLED decision is not skipped by the queue)
        # reads the same on every surface.
        labelled = rows()
        labelled[0].update(assignee=None, status='open', comments=[], labels=['decision'])
        review, brief, item = surfaces(labelled)
        self.assertFalse(item['claimable'])
        self.assertEqual([review['next_actor'], brief['review']['next_actor'],
                          item['next_actor']], ['unknown'] * 3)


class ReconciliationTests(unittest.TestCase):
    """`reconciliation-needed` is filterable, shows the mismatch and clears nothing."""

    def disagreeing(self):
        """An approved contribution whose newer matching scope records a failure."""
        store = shared.reviewed()
        second = scope(source_commit=COMMIT, integration_commit='merge-b', release_id='release-2')
        store.record(payload('lifecycle-scope', operation='scope-2', scope=second))
        store.record(payload('integrated', 'failed', 'fact-2', scope=second,
                             evidence=['report:newer-failure']))
        return store

    def lagging(self):
        """A follow-on contribution whose newest recorded scope names the prior revision."""
        store = NativeStore()
        seeded = scope(source_commit=COMMIT)
        store.record(payload('lifecycle-scope', operation='scope-1', scope=seeded))
        store.record(payload('integrated', operation='fact-1', scope=seeded))
        store.rows[0]['comments'] = []
        store.rows[0]['assignee'] = 'alice/session'
        shared.add(store.rows, shared.contribute(COMMIT, cid='delivery-1'), 'delivery-1')
        shared.add(store.rows, shared.approve('delivery-1'), 'approval-1', 'reviewer')
        follow_on = dict(shared.contribute(OTHER, cid='delivery-2', opid='send-2'),
                         previous='approval-1', supersedes='delivery-1')
        shared.add(store.rows, follow_on, 'delivery-2')
        return store

    def reasons(self, block):
        return [item['reason'] for item in block]

    def test_disagreeing_integration_facts_are_filterable_and_shown_on_every_view(self):
        store = self.disagreeing()
        save_cp(store.rows, 'read-integration')
        review, brief, item = surfaces(store.rows)
        self.assertEqual(review['review_state'], 'integrated')
        self.assertEqual([review['work_state'], brief['review']['work_state'],
                          item['work_state']], ['reconciliation-needed'] * 3)
        self.assertTrue(item['reconciliation_needed'])
        self.assertIn('integration-facts-disagree', self.reasons(review['reconciliation_reasons']))
        self.assertIn('integration-facts-disagree', self.reasons(item['reconciliation_reasons']))
        # The mismatch names both scopes and both values, not just a flag.
        entry = next(e for e in review['reconciliation_reasons']
                     if e['reason'] == 'integration-facts-disagree')['entries'][0]
        self.assertEqual((entry['kind'], entry['fact'], entry['newest_fact']),
                         ('newest-scope-disagrees', 'passed', 'failed'))
        page = work.queue(store.rows, 'alice/session', ['--state', 'reconciliation-needed'])
        self.assertEqual([row['task'] for row in page['items']], [TASK])
        # Item 1: `--state` keeps main's meaning -- the review state the row SHOWS -- so the
        # row is still found under `integrated` as well as under the new selection value.
        self.assertEqual(work.queue(store.rows, 'alice/session', ['--state', 'integrated'])['total'], 1)
        self.assertEqual([row['task'] for row in
                          work.queue(store.rows, 'alice/session', ['--state', 'integrated'])['items']],
                         [TASK])
        # The bounded brief keeps the signal in its review block as `work_state`, which is
        # `reconciliation-needed` exactly when `reconciliation_needed` is true, and does not
        # double the disagreement as a second warning line (the disagreement warning names both).
        self.assertEqual(brief['review']['work_state'], 'reconciliation-needed')
        self.assertNotIn('reconciliation_needed', brief['review'])
        self.assertNotIn('next_actor', brief)
        self.assertNotIn('reconciliation_reasons', brief)
        self.assertTrue(any(w.startswith('Integration fact disagreement') for w in item['integration_warnings']))
        # Item 2 (next-actor-from-work-state): the role follows the REVIEW state the row
        # shows, never the reconciliation `work_state`. The open integrated task is the
        # coordinator's to close (kittrial-5bb.336); the mismatch remains visible.
        self.assertEqual([review['next_actor'], item['next_actor']], ['coordinator'] * 2)

    def test_a_lagging_lifecycle_scope_names_both_commits(self):
        store = self.lagging()
        review, brief, item = surfaces(store.rows)
        self.assertEqual(review['review_state'], 'awaiting-review')
        self.assertEqual([brief['review']['work_state'], item['work_state']],
                         ['reconciliation-needed'] * 2)
        for block in (review, brief['review'], item):
            reason = next(e for e in block['reconciliation_reasons'] if e['reason'] == 'lifecycle-scope-lags')
            self.assertEqual(reason['contribution_commit'], OTHER)
            self.assertEqual(reason['scope_source_commit'], COMMIT)
        self.assertTrue(any(w.startswith('Reconciliation needed:') for w in item['integration_warnings']))
        # Item 3c: the line stays in the EXISTING integration_warnings and in the top-level
        # warnings of a read, it is not a new warning surface.
        self.assertTrue(any(w.startswith('Reconciliation needed:')
                            for w in brief['review']['warnings']))
        self.assertTrue(any(w.startswith('Reconciliation needed:') for w in brief['warnings']))
        # Item 4c: the ADDITIVE line is appended AFTER the top-level warning lines main
        # already had, so the first warning a reader sees is still the one main showed.
        self.assertTrue(brief['warnings'][0].startswith('Newer activity'))
        self.assertTrue(brief['warnings'][-1].startswith('Reconciliation needed:'))
        self.assertEqual(work.queue(store.rows, 'alice/session',
                                    ['--state', 'reconciliation-needed'])['total'], 1)
        # Item 1: a lagging scope is the normal condition of a follow-on contribution, so
        # the reviewer still finds it under the review state the row shows.
        self.assertEqual(work.queue(store.rows, 'alice/session',
                                    ['--state', 'awaiting-review'])['total'], 1)
        self.assertEqual(work.queue(store.rows, 'alice/session',
                                    ['--state', 'changes-requested'])['total'], 0)
        # Reconciliation keeps the evidence-only review role. Independently, unread
        # coordinator comments make the assignee acknowledge them before waiting again.
        self.assertEqual(review['next_actor'], 'reviewer')
        self.assertTrue(item['assignee_comment_pending'])
        self.assertEqual(item['next_actor'], 'assignee')
        save_cp(store.rows, 'read-lagging-scope')
        self.assertEqual(surfaces(store.rows)[2]['next_actor'], 'reviewer')

    def test_an_unrelated_newest_scope_is_not_a_lag(self):
        """lag-raised-for-unrelated-scope: only a PRIOR revision of the current
        contribution can make the newest recorded scope lag it."""
        store = NativeStore()
        store.rows[0]['comments'] = []
        store.rows[0]['assignee'] = 'alice/session'
        shared.add(store.rows, shared.contribute(COMMIT, cid='delivery-1'), 'delivery-1')
        other = scope(source_commit='d' * 40, release_id='release-other')
        store.record(payload('lifecycle-scope', operation='scope-other', scope=other))
        store.record(payload('integrated', operation='fact-other', scope=other))
        review, brief, item = surfaces(store.rows)
        self.assertEqual(review['review_state'], 'awaiting-review')
        self.assertFalse(review['reconciliation_needed'])
        self.assertEqual(item['work_state'], 'awaiting-review')

    def test_a_passing_current_contribution_is_not_a_lag(self):
        """lag-raised-although-current-passes: the recorded facts DO include the current
        contribution once a scope passes for its own commit."""
        store = NativeStore()
        prior = scope(source_commit=COMMIT, release_id='release-prior')
        store.record(payload('lifecycle-scope', operation='scope-prior', scope=prior))
        store.record(payload('integrated', operation='fact-prior', scope=prior))
        store.rows[0]['comments'] = []
        store.rows[0]['assignee'] = 'alice/session'
        shared.add(store.rows, shared.contribute(COMMIT, cid='delivery-1'), 'delivery-1')
        shared.add(store.rows, shared.approve('delivery-1', opid='approve-1'), 'approval-1', 'reviewer')
        follow_on = dict(shared.contribute(OTHER, cid='delivery-2', opid='send-2'),
                         previous='approval-1', supersedes='delivery-1')
        shared.add(store.rows, follow_on, 'delivery-2')
        shared.add(store.rows, shared.approve('delivery-2', opid='approve-2'), 'approval-2', 'reviewer')
        own = scope(source_commit=OTHER, release_id='release-current')
        store.record(payload('lifecycle-scope', operation='scope-current', scope=own))
        store.record(payload('integrated', operation='fact-current', scope=own))
        # A LATER recorded scope names the prior revision again, but the current
        # contribution's own commit records a pass, so nothing lags it.
        store.record(payload('lifecycle-scope', operation='scope-prior-2', scope=prior))
        review, brief, item = surfaces(store.rows)
        self.assertEqual(review['review_state'], 'integrated')
        self.assertFalse(review['reconciliation_needed'])
        self.assertEqual(review['integration']['fact'], 'passed')

    def test_a_short_scope_hash_is_never_a_prefix_match(self):
        """prefix-matching-accepted: only the FULL commit names a contribution."""
        store = NativeStore()
        short = scope(source_commit=COMMIT[:7], release_id='release-short')
        store.record(payload('lifecycle-scope', operation='scope-short', scope=short))
        store.record(payload('integrated', operation='fact-short', scope=short))
        store.rows[0]['comments'] = []
        store.rows[0]['assignee'] = 'alice/session'
        shared.add(store.rows, shared.contribute(COMMIT, cid='delivery-1'), 'delivery-1')
        review, brief, item = surfaces(store.rows)
        self.assertEqual(review['review_state'], 'awaiting-review')
        self.assertEqual(review['integration']['fact'], 'unknown')
        self.assertFalse(review['integration']['matches_contribution'])
        self.assertFalse(item['lifecycle_matches_contribution'])

    def test_a_malformed_handoff_journal_reads_its_error_state_consistently(self):
        """Item 3a: review_state, work_state and next_actor agree on a journal error.

        The journal directory is a stub, so this pins the read's own facts rather than
        the filesystem: one unparseable request record.
        """
        item = work.queue(rows(), 'alice/session', [], request_dir=BadJournal())['items'][0]
        self.assertEqual((item['review_state'], item['work_state'], item['next_actor'],
                          item['claimable']),
                         ('error', 'error', 'operator', False))
        # `--state` keeps main's meaning: main compares the state it computed BEFORE the
        # journal override, so this row is still found under `none`; the additive `error`
        # case additionally finds the row the override turned into a shown error
        # (kittrial-5bb.6 review item 1).
        self.assertEqual(work.queue(rows(), 'alice/session', ['--state', 'none'],
                                    request_dir=BadJournal())['total'], 1)
        self.assertEqual(work.queue(rows(), 'alice/session', ['--state', 'error'],
                                    request_dir=BadJournal())['total'], 1)
        self.assertEqual(work.queue(rows(), 'alice/session', ['--state', 'changes-requested'],
                                    request_dir=BadJournal())['total'], 0)

    def test_a_malformed_journal_file_does_not_list_the_closed_tasks(self):
        """Item 1: the closed-row rule is decided on the state BEFORE the journal override.

        On main the override came after the closed-row rule, so a malformed handoff journal
        never put the project's closed tasks into a queue. The override still makes the
        SHOWN state `error`, but the closed rows must stay out of `work --mine`.
        """
        data = rows()
        data[0]['assignee'] = 'alice/session'
        closed = []
        for index in range(30):
            row = copy.deepcopy(data[0])
            row.update(id='closed-%02d' % index, title='closed-%02d' % index,
                       status='closed', assignee=None)
            closed.append(row)
        data = closed + data
        page = work.queue(data, 'alice/session', ['--mine'], request_dir=BadJournal())
        self.assertEqual(page['total'], 1)
        self.assertEqual([row['task'] for row in page['items']], [TASK])
        self.assertEqual(page['items'][0]['review_state'], 'error')
        self.assertEqual(page['items'][0]['work_state'], 'error')
        # The closed rows are not listed unfiltered either, and `--state` still answers on
        # the state main computed for each row.
        self.assertEqual(work.queue(data, 'alice/session', [],
                                    request_dir=BadJournal())['total'], 1)
        self.assertEqual(work.queue(data, 'alice/session', ['--mine', '--state', 'none'],
                                    request_dir=BadJournal())['total'], 1)
        self.assertEqual(work.queue(data, 'alice/session', ['--mine', '--state', 'error'],
                                    request_dir=BadJournal())['total'], 1)

    def test_a_malformed_review_chain_reads_operator_and_is_never_claimable(self):
        """Item 2/6: a review-error row is never null and is never offered as work."""
        data = rows()
        data[0]['comments'].append(dict(id='bad', text='Kind: contribution-review-v1\n{}'))
        item = work.queue(data, 'alice/session', [])['items'][0]
        self.assertEqual((item['review_state'], item['work_state'], item['next_actor'],
                          item['claimable']),
                         ('error', 'error', 'operator', False))
        self.assertEqual(work.queue(data, 'alice/session', ['--claimable'])['total'], 0)

    def test_a_malformed_row_reads_operator_and_is_never_claimable(self):
        data = rows()
        data.insert(0, dict(id='bad-row', malformed=True, title='Broken'))
        page = work.queue(data, 'alice/session', [])
        item = next(row for row in page['items'] if row['task'] == 'bad-row')
        self.assertEqual((item['review_state'], item['work_state'], item['next_actor'],
                          item['claimable']),
                         ('error', 'error', 'operator', False))
        self.assertEqual(work.queue(data, 'alice/session', ['--claimable'])['total'], 0)
        self.assertEqual(work.queue(data, 'alice/session', ['--state', 'error'])['total'], 1)

    def test_a_newer_unrelated_scope_alone_is_not_reconciliation(self):
        """The normal post-release read (newest scope for other work) stays unflagged."""
        store = shared.reviewed(later_scope=scope(source_commit='d' * 40, release_id='release-2',
                                                  environment='production'))
        review, brief, item = surfaces(store.rows)
        self.assertFalse(review['reconciliation_needed'])
        self.assertEqual(brief['review']['work_state'], 'integrated')
        self.assertFalse(item['reconciliation_needed'])
        self.assertEqual([review['work_state'], brief['review']['work_state'], item['work_state']],
                         ['integrated'] * 3)

    def test_reconciliation_does_not_clear_a_recorded_blocker(self):
        store = self.disagreeing()
        blocker = dict(id='need-key', kind='blocker', text='Need the release key.', source='owner')
        append_checkpoint(store.rows, 'cp-block', checkpoint(store.rows, open_items=[blocker]))
        item = work.queue(store.rows, 'alice/session', [])['items'][0]
        self.assertTrue(item['reconciliation_needed'])
        # The blocker is still recorded, still named, and still counted: the
        # reconciliation signal clears nothing.
        self.assertEqual(item['blocking_items'], 1)
        self.assertEqual(item['checkpoint_wait']['items'][0]['id'], 'need-key')
        self.assertEqual(item['checkpoint_wait']['total'], 1)

    def test_the_reconciliation_filter_selects_the_reconciling_rows_only(self):
        """`--state reconciliation-needed` is a FILTER, not a blanket pass (review item 4).

        Round 3's surviving mutant replaced the `reconciliation_needed` test with `pass`,
        which made every row match. One reconciling row and one ordinary row are in the
        queue, so a blanket pass lists both.
        """
        store = self.disagreeing()
        plain = copy.deepcopy(store.rows[0])
        plain.update(id='plain-task', title='plain-task', labels=[], comments=[],
                     assignee='alice/session')
        data = store.rows + [plain]
        # Both rows are in the queue when nothing is filtered...
        self.assertEqual(work.queue(data, 'alice/session', [])['total'], 2)
        # ...and exactly the reconciling one is selected by the additive value.
        selected = work.queue(data, 'alice/session', ['--state', 'reconciliation-needed'])
        self.assertEqual(selected['total'], 1)
        self.assertEqual([row['task'] for row in selected['items']], [TASK])
        self.assertTrue(selected['items'][0]['reconciliation_needed'])


class ClaimableViewTests(unittest.TestCase):
    """The one bounded claimability interpretation, used by `work --claimable`."""

    def mixed_rows(self):
        base = rows()[0]
        def make(task_id, **changes):
            row = copy.deepcopy(base)
            row.update(id=task_id, title=task_id, comments=[], labels=[])
            row.update(changes)
            return row
        delivered = copy.deepcopy(test_work_queue.reviewed_rows()[0])
        delivered.update(id='delivered-open', title='delivered-open', status='open', assignee=None)
        return [make('plain-open', status='open', assignee=None),
                make('plain-owned', status='open', assignee='alice/session'),
                make('epic-open', status='open', assignee=None, issue_type='epic'),
                make('decision-open', status='open', assignee=None, issue_type='decision'),
                make('job-open', status='open', assignee=None, issue_type='job'),
                make('closed-free', status='closed', assignee=None),
                delivered]

    def test_the_predicate_excludes_scaffolding_delivered_and_owned_work(self):
        self.assertTrue(work.claimable('task', 'open', None, None, 'none'))
        self.assertTrue(work.claimable(None, 'open', None, None, None))
        for issue_type in work.CLAIMABLE_EXCLUDED_TYPES:
            self.assertFalse(work.claimable(issue_type, 'open', None, None, 'none'),
                             issue_type)
        self.assertFalse(work.claimable('task', 'closed', None, None, 'none'))
        self.assertFalse(work.claimable('task', 'open', 'alice/session', None, 'none'))
        self.assertFalse(work.claimable('task', 'open', None, 'delivery', 'awaiting-review'))
        self.assertFalse(work.claimable('task', 'open', None, None, 'awaiting-review'))
        self.assertFalse(work.claimable('task', 'open', None, None, 'none', record_anchor=True))

    def test_the_claimable_view_lists_only_the_bounded_candidates(self):
        data = self.mixed_rows()
        page = work.queue(data, 'alice/session', ['--claimable'])
        self.assertEqual([row['task'] for row in page['items']], ['plain-open'])
        self.assertTrue(page['items'][0]['claimable'])
        flags = {row['task']: row['claimable']
                 for row in work.queue(data, 'alice/session', ['--limit', '100'])['items']}
        self.assertEqual(flags['plain-open'], True)
        for task in ('plain-owned', 'epic-open', 'decision-open', 'job-open', 'delivered-open'):
            self.assertFalse(flags[task], task)

    def test_a_page_bound_does_not_turn_claimability_into_a_reservation(self):
        data = self.mixed_rows()
        page = work.queue(data, 'alice/session', ['--claimable', '--limit', '1'])
        self.assertEqual([row['task'] for row in page['items']], ['plain-open'])
        self.assertIsNone(page['next_offset'])

    def requirement_rows(self):
        """Two open, unassigned, undelivered requirement records (kittrial-5bb.277)."""
        base = rows()[0]
        def make(task_id, labels):
            row = copy.deepcopy(base)
            row.update(id=task_id, title=task_id, comments=[], status='open', assignee=None,
                       labels=list(labels))
            return row
        return [make('requirement-open', ['requirement', 'requirement:draft']),
                make('brd-section-open', ['brd-section', 'requirement:draft'])]

    def test_the_predicate_excludes_requirement_records(self):
        # A requirement record is never ordinary claimable work, whatever the status,
        # assignee or contribution (kittrial-5bb.277).
        for labels in (['requirement'], ['brd-section'], ['requirement', 'requirement:draft']):
            self.assertFalse(work.claimable('task', 'open', None, None, 'none', labels=labels),
                             labels)
        self.assertFalse(work.claimable('task', 'closed', 'alice/session', 'delivery',
                                        'awaiting-review', labels=['requirement']))
        # The helper tolerates a missing or malformed label list, and an ordinary label.
        self.assertFalse(work.requirement_record(None))
        self.assertFalse(work.requirement_record(['requirement:draft']))
        self.assertFalse(work.requirement_record('requirement'))
        self.assertTrue(work.requirement_record(['requirement:draft', 'requirement']))
        self.assertTrue(work.claimable('task', 'open', None, None, 'none', labels=None))

    def test_a_requirement_record_is_never_claimable_and_is_marked_on_the_row(self):
        data = self.mixed_rows() + self.requirement_rows()
        page = work.queue(data, 'alice/session', ['--claimable'])
        self.assertEqual([row['task'] for row in page['items']], ['plain-open'])
        rows_by_task = {row['task']: row
                        for row in work.queue(data, 'alice/session', ['--limit', '100'])['items']}
        for task in ('requirement-open', 'brd-section-open'):
            self.assertFalse(rows_by_task[task]['claimable'], task)
            self.assertTrue(rows_by_task[task]['requirement_record'], task)
        self.assertFalse(rows_by_task['plain-open']['requirement_record'])


class ReadyDropsInternalRecordsTests(unittest.TestCase):
    """kittrial-5bb.290: `ready` agrees with `work --claimable` on internal records."""

    def slot_row(self, **more):
        row = {'id': 'pp-merge-slot', 'title': 'merge slot', 'status': 'open', 'assignee': None,
               'labels': ['gt:slot'], 'issue_type': 'task', 'comments': []}
        row.update(more)
        return row

    def anchor_row(self, **more):
        # A reference record anchor: the record label AND a v1 record comment.
        row = {'id': 'pp-ref-1', 'title': 'reference record', 'status': 'closed', 'assignee': None,
               'labels': ['reference'], 'issue_type': 'task',
               'comments': [{'id': 'c1', 'text': 'Kind: reference-entry-v1\nkey: k\n'}]}
        row.update(more)
        return row

    def test_the_merge_slot_is_dropped(self):
        rows = [self.slot_row(), {'id': 'pp-t1', 'title': 'T', 'status': 'open', 'assignee': None,
                                  'labels': [], 'comments': []}]
        shown = work.drop_internal_from_ready(rows)
        self.assertEqual([r['id'] for r in shown], ['pp-t1'])

    def test_a_record_anchor_is_dropped(self):
        rows = [self.anchor_row(), {'id': 'pp-t1', 'title': 'T', 'status': 'open', 'assignee': None,
                                    'labels': [], 'comments': []}]
        shown = work.drop_internal_from_ready(rows)
        self.assertEqual([r['id'] for r in shown], ['pp-t1'])

    def test_a_labelled_row_without_comments_is_marked_not_offered_as_claimable(self):
        # Label alone is not anchor proof (a project may use `reference` as an ordinary
        # label), but without comments it cannot prove it is NOT an anchor: mark it so a
        # client can filter without knowing ids, and never call it claimable.
        rows = [{'id': 'pp-ref-2', 'title': 'maybe', 'status': 'open', 'assignee': None,
                 'labels': ['reference'], 'comments': None}]
        shown = work.drop_internal_from_ready(rows)
        self.assertEqual([r['id'] for r in shown], ['pp-ref-2'])
        self.assertTrue(shown[0].get('internal'))
        self.assertIs(shown[0].get('claimable'), False)

    def test_a_requirement_record_stays_visible(self):
        # kittrial-5bb.277: requirement records are never claimable work but stay a
        # visible row; they are not record anchors, so ready still lists them.
        rows = [{'id': 'pp-req-1', 'title': 'requirement', 'status': 'open', 'assignee': None,
                 'labels': ['requirement'], 'comments': []}]
        shown = work.drop_internal_from_ready(rows)
        self.assertEqual([r['id'] for r in shown], ['pp-req-1'])
        self.assertFalse(shown[0].get('internal'))

    def test_an_ordinary_open_task_passes_unchanged(self):
        row = {'id': 'pp-t1', 'title': 'T', 'status': 'open', 'assignee': None,
               'labels': [], 'comments': []}
        shown = work.drop_internal_from_ready([row])
        self.assertEqual(shown, [row])

    def test_ready_agrees_with_claimable_on_the_slot(self):
        slot = self.slot_row()
        self.assertFalse(work.claimable(slot.get('issue_type'), slot['status'], slot.get('assignee'),
                                        None, 'none', record_anchor=False, labels=slot.get('labels')))
        self.assertEqual(work.drop_internal_from_ready([slot]), [])


class WorkerFeedbackBriefTests(unittest.TestCase):
    """kittrial-5bb.293 items 1 and 2: the brief says how to read a cut field, and a
    field change after a checkpoint is newer activity."""

    def test_a_cut_field_says_how_to_read_the_whole(self):
        data = rows()
        data[0]['title'] = 'a very long title that will be cut because it exceeds the clip limit ' + 'x' * 300
        data[0]['description'] = 'd' * 2000
        _review, brief, _item = surfaces(data)
        self.assertIsNotNone(brief.get('read_whole'), sorted(brief.keys()))
        self.assertIn('show %s' % TASK, brief['read_whole']['route'])
        self.assertIn('complete text', brief['read_whole']['note'])

    def test_a_short_task_says_nothing_about_reading_the_whole(self):
        _review, brief, _item = surfaces(rows())
        self.assertIsNone(brief.get('read_whole'))

    def test_a_field_change_after_the_checkpoint_is_newer_activity(self):
        data = rows()
        data[0]['description'] = 'original description'
        # A checkpoint with provenance, so the comment itself is incorporated and the
        # only thing left is the field change (kittrial-5bb.293 item 2 rev2).
        save_cp(data, 'cp-1')
        # Change the description: it must appear as newer activity with the lengths.
        data[0]['description'] = 'a much longer replacement description'
        _review, brief, _item = surfaces(data)
        self.assertTrue((brief.get('checkpoint') or {}).get('newer_activity'), brief.get('checkpoint'))
        changes = (brief.get('newer') or {}).get('field_changes') or []
        # The checkpoint's cursor is written by this kit and carries no lengths
        # (cursor compatibility): one summary entry, never all three fields with
        # old_length None (kittrial-5bb.293 item 2 rev2).
        self.assertEqual(len(changes), 1, changes)
        self.assertEqual(changes[0]['field'], 'task_fields')
        self.assertIsNone(changes[0]['old_length'])
        self.assertIn('changed', changes[0].get('note') or '')


class QueueItemClaimableCompatTests(unittest.TestCase):
    """An endpoint that does not state `claimable` keeps the older reading."""

    def test_unstated_claimability_falls_back_to_open_and_unassigned(self):
        from http_service import queue_item
        free = queue_item('proj', {'id': 't1', 'title': 'T', 'status': 'open'}, 'none', None, 0)
        owned = queue_item('proj', {'id': 't2', 'title': 'T', 'status': 'open',
                                    'assignee': 'worker'}, 'none', None, 0)
        stated = queue_item('proj', {'id': 't3', 'title': 'T', 'status': 'open'}, 'none', None, 0,
                            claimable=False)
        self.assertTrue(free['claimable'])
        self.assertFalse(owned['claimable'])
        self.assertFalse(stated['claimable'])


class ClaimableHttpTests(AgentHarness):
    """The HTTP agent claimable list and the claim fall-through use the same rule."""

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.project = self.create_project(admin, 'Alpha')
        self.alex = self.create_account(admin, 'alex', 'alex-password-1')
        self.blair = self.create_account(admin, 'blair', 'blair-password-1')
        for user in (self.alex, self.blair):
            added = self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, user),
                                 {'role': 'contributor'}, token=admin)
            self.assertIn(added.status, (200, 201), added.data)
        self.alex_token = self.login('alex', 'alex-password-1')[0]
        self.blair_token = self.login('blair', 'blair-password-1')[0]
        self.alex_agent = self.agent_secret(self.alex_token, name='Kestrel', projects=[self.project])
        self.blair_agent = self.agent_secret(self.blair_token, name='Merlin', projects=[self.project])
        self.tasks = '/v1/projects/%s/tasks' % self.project

    def new(self, title, issue_type=None, labels=None):
        made = self.request('POST', self.tasks, {'title': title}, token=self.alex_token)
        self.assertEqual(201, made.status, made.data)
        if issue_type:
            self.backend.state['tasks'][made.data['id']]['issue_type'] = issue_type
        if labels:
            self.backend.state['tasks'][made.data['id']]['labels'] = list(labels)
        return made.data['id']

    def next_actions(self, secret):
        answer = self.request('GET', '/v1/agents/me/next', token=secret)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def claim(self, secret, task):
        return self.request('POST', '%s/%s/claim' % (self.tasks, task), {}, token=secret)

    def test_attention_offers_only_the_bounded_claimable_rows(self):
        plain = self.new('plain')
        self.new('an epic', issue_type='epic')
        self.new('a decision', issue_type='decision')
        self.new('a job', issue_type='job')
        data = self.next_actions(self.alex_agent[1])
        self.assertEqual(data['attention']['counts']['claimable'], 1)
        offered = [action['task'] for action in data['next_actions']]
        self.assertEqual(offered, [plain])

    def test_a_requirement_record_is_not_offered_as_claimable(self):
        # kittrial-5bb.277: a requirement record is visible, but never claimable work.
        plain = self.new('plain')
        self.new('a requirement', labels=['requirement', 'requirement:draft'])
        self.new('a BRD section', labels=['brd-section', 'requirement:draft'])
        data = self.next_actions(self.alex_agent[1])
        self.assertEqual(data['attention']['counts']['claimable'], 1)
        self.assertEqual([action['task'] for action in data['next_actions']], [plain])

    def test_claiming_a_requirement_record_is_refused_with_the_sentence(self):
        task = self.new('a requirement', labels=['requirement', 'requirement:draft'])
        refused = self.claim(self.alex_agent[1], task)
        self.assertEqual(409, refused.status, refused.data)
        message = refused.data['error']['message']
        self.assertIn('requirement record', message)
        self.assertIn('not a unit of work, so it cannot be claimed', message)
        self.assertIn('requirement_records.py draft|revise', message)
        self.assertIn('admin.py requirement-apply', message)
        # A BRD section is the same kind of record, and names its own label.
        section = self.new('a BRD section', labels=['brd-section', 'requirement:draft'])
        other = self.claim(self.alex_agent[1], section)
        self.assertEqual(409, other.status, other.data)
        self.assertIn('brd-section record', other.data['error']['message'])

    def test_a_refused_claim_falls_through_to_the_next_candidate(self):
        first = self.new('first')
        second = self.new('second')
        self.assertEqual(200, self.claim(self.alex_agent[1], first).status)
        refused = self.claim(self.blair_agent[1], first)
        self.assertEqual(409, refused.status, refused.data)
        detail = refused.data['error']['detail']
        self.assertEqual([entry['task'] for entry in detail['candidates']], [second])
        # The candidate is advisory: the next claim is still atomic and succeeds.
        self.assertEqual(200, self.claim(self.blair_agent[1], second).status)

    def test_a_task_blocked_by_an_open_dependency_is_not_claimable(self):
        """kittrial-5bb.293 item 3: a bd dependency whose other end is still open
        blocks the task. claimable() refuses it, the claim route refuses it with one
        sentence naming the open dependency, and the agent list leaves it out."""
        blocked = self.new('blocked work')
        opener = self.new('open dependency')
        self.backend.state['tasks'][blocked]['dependencies'] = [
            {'depends_on_id': opener, 'type': 'blocks'}]
        # claimable() refuses it.
        from work import claimable, open_dependency_ids, open_dependency_sentence
        row = self.backend.state['tasks'][blocked]
        snapshot = list(self.backend.state['tasks'].values())
        open_deps = open_dependency_ids(row, snapshot)
        self.assertEqual(open_deps, [opener])
        self.assertFalse(claimable(row.get('issue_type'), row.get('status'), row.get('assignee'),
                                   None, 'none', labels=row.get('labels'),
                                   open_dependencies=open_deps))
        self.assertIn(opener, open_dependency_sentence(open_deps))
        # The claim route refuses it with that sentence.
        refused = self.claim(self.alex_agent[1], blocked)
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('open dependency', refused.data['error']['message'])
        self.assertIn(opener, refused.data['error']['message'])
        # The agent list leaves it out.
        data = self.next_actions(self.alex_agent[1])
        self.assertEqual(data['attention']['counts']['claimable'], 1)  # only the opener
        self.assertEqual([a['task'] for a in data['next_actions']], [opener])

    def test_the_endpoint_raw_claim_is_refused_for_an_open_dependency(self):
        """kittrial-5bb.293 item 3a / mutant g6: the endpoint's own bd claim is gated."""
        import endpoint as ep
        blocked = self.new('blocked work')
        opener = self.new('open dependency')
        self.backend.state['tasks'][blocked]['dependencies'] = [
            {'depends_on_id': opener, 'type': 'blocks'}]
        # The guard is the one the endpoint runs before any native write.
        row = dict(self.backend.state['tasks'][blocked])
        snapshot = list(self.backend.state['tasks'].values())
        from work import dependency_index, open_dependency_ids
        open_deps = open_dependency_ids(row, snapshot, index=dependency_index(snapshot))
        self.assertEqual(open_deps, [opener])
        # The parsed-flag matcher sees only a real --claim, never a --notes text.
        self.assertTrue(ep._claims_the_row(['update', blocked, '--claim', '--json']))
        self.assertTrue(ep._claims_the_row(['update', blocked, '--status', 'in_progress', '--json']))
        self.assertTrue(ep._claims_the_row(['update', blocked, '-s', 'in_progress', '--json']))
        self.assertFalse(ep._claims_the_row(['update', blocked, '--notes', 'in_progress', '--json']))
        self.assertFalse(ep._claims_the_row(['update', blocked, '--title', 'x', '--json']))

    def test_the_web_claim_route_on_a_blocked_task_is_refused(self):
        """kittrial-5bb.293 rev4 / mutant g4: the HTTP claim route is gated."""
        blocked = self.new('blocked work')
        opener = self.new('open dependency')
        self.backend.state['tasks'][blocked]['dependencies'] = [
            {'id': opener, 'dependency_type': 'blocks', 'status': 'open'}]
        refused = self.claim(self.alex_agent[1], blocked)
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('open dependency', refused.data['error']['message'])
        self.assertIn(opener, refused.data['error']['message'])

    def test_work_claimable_leaves_out_a_blocked_task(self):
        """kittrial-5bb.293 rev4 / mutant g5: the work queue's claimable is gated."""
        blocked = self.new('blocked work')
        opener = self.new('open dependency')
        self.backend.state['tasks'][blocked]['dependencies'] = [
            {'id': opener, 'dependency_type': 'blocks', 'status': 'open'}]
        data = self.next_actions(self.alex_agent[1])
        offered = [a['task'] for a in data['next_actions']]
        self.assertNotIn(blocked, offered)
        self.assertIn(opener, offered)

    def test_only_the_blocking_dependency_type_blocks(self):
        """kittrial-5bb.293 item 3b: a `related` dependency must not block a claim."""
        task = self.new('related to something open')
        opener = self.new('open neighbour')
        self.backend.state['tasks'][task]['dependencies'] = [
            {'depends_on_id': opener, 'type': 'related'}]
        ok = self.claim(self.alex_agent[1], task)
        self.assertEqual(200, ok.status, ok.data)

    def test_a_parent_child_dependency_does_not_block_the_task(self):
        task = self.new('child work')
        parent = self.new('open parent')
        self.backend.state['tasks'][task]['dependencies'] = [
            {'depends_on_id': parent, 'type': 'parent-child'}]
        ok = self.claim(self.alex_agent[1], task)
        self.assertEqual(200, ok.status, ok.data)

    def test_a_closed_dependency_does_not_block_the_task(self):
        blocked = self.new('blocked work')
        closed = self.new('closed dependency')
        self.backend.state['tasks'][blocked]['dependencies'] = [
            {'depends_on_id': closed, 'type': 'blocks'}]
        self.backend.state['tasks'][closed]['status'] = 'closed'
        ok = self.claim(self.alex_agent[1], blocked)
        self.assertEqual(200, ok.status, ok.data)

    def test_a_refusal_with_nothing_to_fall_through_to_is_unchanged(self):
        only = self.new('only')
        self.assertEqual(200, self.claim(self.alex_agent[1], only).status)
        refused = self.claim(self.blair_agent[1], only)
        self.assertEqual(409, refused.status, refused.data)
        self.assertNotIn('candidates', refused.data['error'].get('detail') or {})


if __name__ == '__main__':
    unittest.main()
