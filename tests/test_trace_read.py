"""Traceability slice 4 (kittrial-5bb.425): the trace read, answered from records alone.

TRACEABILITY_DESIGN §5.2-§5.4 and §7. One fixture project carries every flag of every view exactly once;
each rule has a named test, and hidden equals absent is checked for every view and the summary.
"""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reference_records
import requirement_records as rr
import review_workflow as w
import trace_read as t
from requirements import canonical_bytes
from test_reference_records import RefNative, OPERATOR, TODAY, entry, acceptance
from test_requirement_records import Native

PROJECT = 'trial'
NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
MEMBERS = {'u-owner': 'owner', 'u-coord': 'coordinator', 'u-contrib': 'contributor', 'u-view': 'viewer'}
OPERATORS = ['operator', OPERATOR]
DOCUMENT = '''<!-- design-v1
scope: server
status: candidate
requirements: [R01@3]
decisions-open: [trial-d1]
-->
# Server design

## Storage
Stores req:R01@3 and req:R02 here.

## Backup
Covers req:R02 only.

## Typos
See req:NOPE here.

## Plan

## Plan phase 1
- Contract: store things
- Requirements: req:R01@3
- Epic: trial-epic-1

## Plan phase 2
- Contract: back things up
'''


def stamp(days=0, hours=0):
    return (NOW - timedelta(days=days, hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


def note(cid, text, author='u-contrib', days=1):
    return dict(id=cid, text=text, author=author, created_at=stamp(days))


def task(tid, description='', status='open', issue_type='task', parent=None, comments=(), closed=None,
         assignee=None, title=None):
    row = dict(id=tid, title=title or 'Task ' + tid, description=description, status=status,
               issue_type=issue_type, labels=[], comments=list(comments), created_at=stamp(30))
    if parent is not None:
        row['dependencies'] = [dict(issue_id=tid, depends_on_id=parent, type='parent-child')]
    if closed is not None:
        row['closed_at'] = stamp(closed)
    if assignee is not None:
        row['assignee'] = assignee
    return row


def contribution_note(tid, cid, author, days):
    record = dict(schema_version=1, operation='contribute', operation_id='send-' + tid, task=tid,
                  previous=None, repository='ssh://git/example', commit='a' * 40, base_commit='b' * 40,
                  delivery=dict(kind='bundle', path='server:/rev.bundle', sha256='c' * 64),
                  summary='Delivered ' + tid, supersedes=None)
    return dict(id=cid, author=author, created_at=stamp(days), text=w.PREFIX + canonical_bytes(record).decode())


class Fixture(unittest.TestCase):
    """Requirements R01 (revisions 2 and 3 accepted), R02 (revision 2 accepted), R03 (draft only); design.server accepted
    with an uploaded document; tasks, a decision, an owner's comment and a design round."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        clock = patch('time.gmtime', return_value=TODAY)
        clock.start()
        self.addCleanup(clock.stop)
        self.requirements = Native()
        self.requirements.seed('job-1', issue_type='job')
        self.requirement('R01', 'op-r01', accepted=(2, 3))
        self.requirement('R02', 'op-r02', accepted=(2,))
        self.requirement('R03', 'op-r03')
        self.references = RefNative()
        self.design(DOCUMENT)
        self.work = self.work_rows()

    def requirement(self, key, op, accepted=()):
        """Revision 1 is a draft; each number in `accepted` is a later revision written accepted (HostRequirementTests)."""
        native = self.requirements
        native.actor = 'alice'
        result = rr.apply_native(dict(schema_version=1, kind='requirement', title=key + ': intent', key=key,
                                      description='Statement.', acceptance_state='draft', operation_id=op,
                                      operation='draft', parent='job-1'), 'alice', native, self.project)
        rid = result['id']
        for number in accepted:
            native.actor = 'operator'
            rr.apply_native(dict(schema_version=1, kind='requirement', title=key + ': intent', key=key,
                                 description='Statement %d.' % number, acceptance_state='accepted',
                                 operation_id='%s-%d' % (op, number), operation='revise', task=rid,
                                 revision=number,
                                 acceptance=dict(owners=['owner-a'], approvers=['owner-a'], policy='any-owner',
                                                 decision_id='decision-%s-%d' % (key, number), evidence='review')),
                            'operator', native, self.project, operator=True, operators=['operator'])

    def design(self, text, scope='server', accept=True, tag=''):
        key = 'design.' + scope
        native = self.references
        native.actor = 'alice'
        # The entry's decision link is checked against the tracker at the write; the work rows hold that issue.
        seeded = native.seed('trial-d1', issue_type='decision')
        authority = {'type': 'repo-document', 'path': 'docs/design/SERVER.md', 'commit': 'a' * 40,
                     'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        reference_records.apply_native(entry(key=key, authority=authority, operation_id='propose-' + scope + tag,
                                             decisions=['trial-d1']),
                                       'alice', native, self.project, document=text)
        if accept:
            row, _ = reference_records.anchor_for(reference_records.read_key_rows(native, key), key)
            digest = reference_records.existing_revisions(row)[1]['sha256']
            native.actor = OPERATOR
            reference_records.apply_native(
                {'schema_version': 1, 'operation_id': 'accept-' + scope + tag, 'operation': 'accept', 'key': key,
                 'revision': 1, 'record_sha256': digest, 'acceptance_state': 'accepted', 'acceptance': acceptance()},
                OPERATOR, native, self.project, operator=True, operators=[OPERATOR])
        native.rows.remove(seeded)

    def work_rows(self):
        return [
            task('trial-1', 'Implements req:R01@2 and design:server#storage here.', comments=[
                note('n-1', 'After the call: we will go with B for storage.', author='u-owner', days=2),
                note('n-2', 'We will go with B, I think.', author='u-contrib', days=2)]),
            task('trial-2', 'Uses req:R02 design:server#nowhere and req:GHOST here.'),
            task('trial-epic-1', 'Plan phase 1 of design:server#plan-phase-1', issue_type='epic'),
            task('trial-3', 'No tokens here.', parent='trial-epic-1'),
            task('trial-old', 'Old req:R02', status='closed', closed=20),
            task('trial-recent', 'Recent req:R02', status='closed', closed=3),
            task('trial-d1', 'Supersedes: decision:trial-d0\nSettles comment:n-3', issue_type='decision',
                 title='Storage decision'),
            task('trial-4', 'Owner note, already decided.', comments=[
                note('n-3', 'We decided: keep B.', author='u-owner', days=1)]),
            task('trial-round', 'Round for design-round:server', issue_type='epic', comments=[
                note('p-1', 'Published drafts of server at 2026-10-10T00:00:00Z', author='u-owner'),
                note('p-2', 'Published drafts of server at 2026-10-01T00:00:00Z', author='u-contrib')]),
            task('trial-l1', 'Lane A', parent='trial-round', assignee='lane-a', comments=[
                contribution_note('trial-l1', 'k-1', 'lane-a', 12),
                note('x-1', 'Cross-review of trial-l2 at abcdef1\nAdopt B.', author='lane-a', days=5)]),
            task('trial-l2', 'Lane B', parent='trial-round', assignee='lane-b', comments=[
                contribution_note('trial-l2', 'k-2', 'lane-b', 9)]),
        ]

    def rows(self):
        return self.requirements.rows + self.references.rows + self.work

    def snap(self, rows=None, **kwargs):
        kwargs.setdefault('members', MEMBERS)
        return t.Snapshot(self.rows() if rows is None else rows, PROJECT, now=NOW, operators=OPERATORS, **kwargs)

    def view(self, name, rows=None, **kwargs):
        scope = kwargs.pop('scope', None)
        return t.trace(self.snap(rows, **kwargs), name, scope=scope)


class ViewTests(Fixture):
    def test_requirements_view_lists_each_accepted_requirement_with_its_flags(self):
        answer = self.view('requirements')
        rows = {row['key']: row for row in answer['rows']}
        self.assertEqual(sorted(rows), ['R01', 'R02'])   # R03 is a draft: not an accepted requirement
        self.assertEqual(rows['R01']['designs'], ['server'])
        self.assertEqual(rows['R01']['tasks'], ['trial-1'])
        self.assertEqual(rows['R01']['flags'], [])
        self.assertEqual(rows['R02']['tasks'], ['trial-2', 'trial-old', 'trial-recent'])
        # trial-old and trial-recent are closed, so delivered: no capability links R02.
        self.assertEqual(rows['R02']['flags'], ['no-capability'])

    def test_no_task_and_no_design_flags(self):
        self.work = [row for row in self.work if 'R02' not in row['description']]
        self.references = RefNative()
        self.design(DOCUMENT.replace('req:R02', 'prose'), tag='-again')
        rows = {row['key']: row for row in self.view('requirements')['rows']}
        self.assertEqual(rows['R02']['flags'], ['no-design', 'no-task'])
        self.assertNotIn('no-capability', rows['R02']['flags'])   # nothing delivered cites it

    def test_tasks_view_flags_superseded_citations_through_analyze_and_cites_nothing(self):
        answer = self.view('tasks')
        rows = {row['id']: row for row in answer['rows']}
        self.assertEqual(sorted(rows), ['trial-1', 'trial-2', 'trial-3', 'trial-4', 'trial-l1', 'trial-l2',
                                        'trial-recent'])
        self.assertEqual(rows['trial-1']['flags'], ['cites-superseded-revision'])
        self.assertEqual(rows['trial-1']['affected_requirements'], ['R01'])
        self.assertEqual(rows['trial-3']['flags'], ['cites-nothing'])
        self.assertEqual([row['id'] for row in answer['rows'] if row['flags']], ['trial-1', 'trial-3'])
        self.assertEqual(answer['coverage']['impact'], 'analyzed 4 tasks')

    def test_analyze_is_the_source_of_the_superseded_flag(self):
        with patch('requirement_impact.analyze', side_effect=ValueError('refused')):
            answer = self.view('tasks')
        self.assertEqual([row['id'] for row in answer['rows'] if 'cites-superseded-revision' in row['flags']], [])
        self.assertTrue(answer['coverage']['impact'].startswith('impact analysis unavailable'))

    def test_a_draft_or_newer_citation_never_reaches_analyze(self):
        self.work[1]['description'] = 'req:R03 req:R01@9'
        rows = {row['id']: row for row in self.view('tasks')['rows']}
        self.assertEqual(rows['trial-2']['flags'], [])

    def test_tokens_view_lists_every_unresolved_token_once_with_where_it_is(self):
        rows = self.view('tokens')['rows']
        found = sorted((row['pointer'], json.dumps(row['where'], sort_keys=True), tuple(row['flags'])) for row in rows)
        self.assertEqual(found, sorted([
            ('req:R01@2', '{"field": "description", "task": "trial-1"}', ('superseded',)),
            ('design:server#nowhere', '{"field": "description", "task": "trial-2"}', ('unknown-section',)),
            ('req:GHOST', '{"field": "description", "task": "trial-2"}', ('unknown-key',)),
            ('req:NOPE', '{"design": "server", "section": "typos"}', ('unknown-key',)),
        ]))

    def test_tokens_view_reads_checkpoint_items(self):
        from test_briefing import TASK, append_checkpoint, checkpoint
        row = task(TASK, 'No tokens here.')
        self.work.append(row)
        append_checkpoint([row], 'cp-1', checkpoint([row], open_items=[
            dict(id='q', kind='question', text='Which?', source='req:GHOST2')]))
        rows = [r for r in self.view('tokens')['rows'] if r['pointer'] == 'req:GHOST2']
        self.assertEqual(rows, [{'pointer': 'req:GHOST2', 'where': {'task': TASK, 'field': 'checkpoint-source'},
                                 'flags': ['unknown-key']}])

    def test_decisions_view_and_the_owner_only_heuristic(self):
        answer = self.view('decisions')
        decisions = [row for row in answer['rows'] if row['kind'] == 'decision']
        self.assertEqual([(row['id'], row['supersedes'], row['designs']) for row in decisions],
                         [('trial-d1', ['trial-d0'], ['server'])])
        possible = [row for row in answer['rows'] if row['kind'] != 'decision']
        # n-1 is the owner's; n-2 is a contributor's; n-3 is cited by the decision.
        self.assertEqual([(row['id'], row['flags'], row['heuristic']) for row in possible],
                         [('n-1', ['possible-decision-in-comment'], True)])
        self.assertEqual(answer['coverage']['comments_scanned']['limit'], t.COMMENT_SCAN_MAX)

    def test_an_agent_or_a_coordinator_is_never_an_owner_for_the_heuristic(self):
        for author in ('u-coord', 'lane-a', 'operator'):
            self.work[0]['comments'][0]['author'] = author
            with self.subTest(author=author):
                self.assertEqual([r for r in self.view('decisions')['rows'] if r['kind'] != 'decision'], [])

    def test_the_comment_scan_is_bounded_and_says_so(self):
        filler = [note('f-%d' % n, 'filler', days=0) for n in range(30)]
        self.work.append(task('trial-filler', 'x', comments=filler))
        with patch.object(t, 'COMMENT_SCAN_MAX', 10):
            answer = self.view('decisions')
        self.assertEqual([r for r in answer['rows'] if r['kind'] != 'decision'], [])
        self.assertEqual(answer['coverage']['comments_scanned'], {'scanned': 10, 'total': 36, 'limit': 10})
        self.assertIn('the newest 10 of 36 comments scanned', answer['coverage']['text'])

    def test_designs_view_reports_state_and_plan_coverage(self):
        rows = self.view('designs')['rows']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['scope'], row['selected'], row['state'], row['front_status']),
                         ('server', True, 'unverified', 'candidate'))
        self.assertEqual(row['flags'], ['unverified', 'front-status-disagrees', 'section-without-plan-phase',
                                        'plan-phase-without-epic'])
        self.assertEqual(row['sections_without_plan_phase'], ['backup', 'typos'])
        self.assertEqual(row['plan_phases_without_epic'], ['plan-phase-2'])
        self.assertEqual(row['epics_without_task'], [])
        self.assertEqual(row['requirements'], {'cited': 3, 'planned': 1})

    def test_an_epic_with_no_task_and_a_review_by_past(self):
        self.work = [row for row in self.work if row['id'] != 'trial-3']
        with patch.object(t, 'RECENT_DAYS', 14):
            snap = t.Snapshot(self.rows(), PROJECT, now=datetime(2027, 2, 1, tzinfo=timezone.utc),
                              operators=OPERATORS, members=MEMBERS)
            row = t.trace(snap, 'designs')['rows'][0]
        self.assertIn('epic-without-task', row['flags'])
        self.assertEqual(row['epics_without_task'], ['trial-epic-1'])
        self.assertIn('review-by-past', row['flags'])

    def test_round_view_puts_lane_times_side_by_side(self):
        rows = self.view('round')['rows']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['epic'], row['scope'], row['published_at'], row['publication']),
                         ('trial-round', 'server', '2026-10-10T00:00:00Z', 'recorded'))
        lanes = {lane['task']: lane for lane in row['lanes']}
        self.assertEqual(lanes['trial-l1']['delivered_at'], stamp(12))
        self.assertEqual(lanes['trial-l1']['flags'], [])
        self.assertEqual(lanes['trial-l2']['flags'], ['delivered-after-publication'])
        self.assertEqual(lanes['trial-l1']['cross_reviews'], [{'of': 'trial-l2', 'commit': 'abcdef1', 'at': stamp(5)}])
        self.assertEqual(row['converged'], {'key': 'design.server', 'selected': True, 'revision': 2,
                                            'decisions': ['trial-d1']})

    def test_a_contributor_cannot_move_the_publication_time(self):
        # Q7: only a person owner or coordinator in current membership, or a listed operator, counts.
        round_row = self.work[8]
        round_row['comments'] = [note('p-2', 'Published drafts of server at 2026-10-01T00:00:00Z', author='u-contrib')]
        row = self.view('round')['rows'][0]
        self.assertEqual((row['published_at'], row['publication'], row['flags']),
                         (None, 'publication not recorded', []))
        for author in ('u-coord', 'operator'):
            round_row['comments'][0]['author'] = author
            with self.subTest(author=author):
                row = self.view('round')['rows'][0]
                self.assertEqual(row['published_at'], '2026-10-01T00:00:00Z')
                self.assertEqual(row['flags'], ['delivered-after-publication'])
        round_row['comments'][0]['author'] = 'u-owner'
        self.assertEqual(self.view('round', members={})['rows'][0]['published_at'], None)   # no longer a member

    def test_summary_counts_come_from_the_views(self):
        lines = {line['view']: line for line in self.view('summary')['rows']}
        self.assertEqual(lines['requirements'], {'view': 'requirements', 'total': 2,
                                                 'counts': {'no-task': 0, 'no-capability': 1, 'no-design': 0}})
        self.assertEqual(lines['tokens']['total'], 4)
        self.assertEqual(lines['tasks']['counts'], {'cites-superseded-revision': 1, 'cites-nothing': 1})
        self.assertEqual(lines['decisions'], {'view': 'decisions', 'total': 1,
                                              'counts': {'possible-decision-in-comment': 1}})
        self.assertEqual(lines['designs']['counts']['unverified'], 1)
        self.assertEqual(lines['round']['counts'], {'delivered-after-publication': 1})


class NoDesignTests(Fixture):
    """§5.4: a project with no design.* entries stays useful and cheap."""

    def setUp(self):
        super().setUp()
        self.references = RefNative()

    def test_design_columns_say_once_and_no_design_is_never_flagged(self):
        answer = self.view('requirements')
        self.assertEqual(answer['note'], t.NO_DESIGNS)
        self.assertFalse(answer['designs'])
        for row in answer['rows']:
            self.assertNotIn('no-design', row['flags'])

    def test_requirement_task_token_and_decision_views_still_answer(self):
        self.assertEqual({row['id'] for row in self.view('tasks')['rows'] if row['flags']}, {'trial-1'})
        self.assertIn('req:GHOST', [row['pointer'] for row in self.view('tokens')['rows']])
        self.assertEqual(len([r for r in self.view('decisions')['rows'] if r['kind'] == 'decision']), 1)
        self.assertEqual(self.view('designs')['rows'], [])
        self.assertEqual(self.view('round')['rows'], [])

    def test_summary_shows_the_requirement_and_token_lines_only(self):
        self.assertEqual([line['view'] for line in self.view('summary')['rows']], ['requirements', 'tokens'])

    def test_no_index_or_blob_is_read(self):
        import design_documents
        with patch.object(design_documents, 'read_document', side_effect=AssertionError('blob read')), \
                patch.object(design_documents, 'selected_index', side_effect=AssertionError('index read')):
            for view in t.VIEWS:
                self.view(view)


class HiddenEqualsAbsentTests(Fixture):
    """§5.3 bounds: permission filtering happens before counts; a caller learns nothing it cannot read."""

    def answers(self, rows=None, **kwargs):
        return {view: json.dumps(self.view(view, rows, **kwargs), sort_keys=True) for view in t.VIEWS}

    def test_another_projects_rows_change_no_view_and_no_count(self):
        foreign = copy.deepcopy(self.work)
        for row in foreign:
            row['id'] = 'other-' + row['id']
            row['project_id'] = 'other'
        alone = self.answers()
        self.assertEqual(self.answers(self.rows() + foreign), alone)

    def test_an_excluded_row_is_absent_from_every_view_and_count(self):
        for hidden in ('trial-1', 'trial-2', 'trial-d1', 'trial-round', 'trial-l2', 'trial-epic-1'):
            with self.subTest(hidden=hidden):
                absent = self.answers([row for row in self.rows() if row['id'] != hidden])
                self.assertEqual(self.answers(exclude=lambda row: row.get('id') == hidden), absent)

    def test_a_merge_slot_and_record_comments_are_never_work(self):
        slot = task('trial-merge-slot', 'req:SLOT design:server#slot')
        slot['labels'] = ['gt:slot']
        alone = self.answers()
        self.assertEqual(self.answers(self.rows() + [slot]), alone)
        planted = copy.deepcopy(self.work)
        planted[0]['comments'].append(note('r-1', 'Kind: reference-entry-v1\nwe will go with C req:PLANT',
                                           author='u-owner'))
        self.assertEqual(self.answers(self.rows()[:-len(self.work)] + planted), alone)

    def test_requirement_drafts_and_owner_states_read_the_same_for_every_role(self):
        # Q1: requirements_list/get (http_service.py:11834, :11977) give every CAP_READ member the drafts and
        # owner states; only operation handles are stripped (_requirements_state_only, :11608), and trace
        # never carries one. So no role sees a different requirement row or count.
        answer = json.dumps(self.answers())
        for handle in ('operation_id', 'pending_receipt_sha256', 'pending_author'):
            self.assertNotIn(handle, answer)


class BoundsTests(Fixture):
    def many(self, count):
        self.work += [task('trial-x%03d' % n, 'req:MISSING%03d' % n) for n in range(count)]

    def test_pages_hold_at_most_fifty_rows_and_continue_by_offset(self):
        self.many(51)
        snap = self.snap()
        first = t.trace(snap, 'tokens')
        self.assertEqual(len(first['rows']), t.PAGE_MAX)
        self.assertEqual(first['next_offset'], 50)
        second = t.trace(snap, 'tokens', offset=first['next_offset'])
        self.assertEqual(second['next_offset'], None)
        self.assertEqual(len(first['rows']) + len(second['rows']), first['total'])
        with self.assertRaises(ValueError):
            t.trace(snap, 'tokens', limit=51)
        with self.assertRaises(ValueError):
            t.trace(snap, 'tokens', offset=-1)

    def test_the_answer_stays_under_its_byte_cap(self):
        long = 'x' * 2000
        self.work += [task('trial-y%03d' % n, 'req:M%03d' % n, title=long) for n in range(50)]
        answer = t.trace(self.snap(), 'tasks')
        self.assertLessEqual(len(json.dumps(answer, ensure_ascii=False).encode('utf-8')), t.ANSWER_MAX)
        with patch.object(t, 'ANSWER_MAX', 4000):
            small = t.trace(self.snap(), 'tasks')
        self.assertLess(len(small['rows']), len(answer['rows']))
        self.assertEqual(small['next_offset'], len(small['rows']))

    def test_untrusted_text_is_escaped_and_clipped(self):
        self.work[0]['title'] = '<script>alert(1)</script>‮evil\x1b[2J' + 'y' * 300
        row = next(r for r in self.view('tasks')['rows'] if r['id'] == 'trial-1')
        self.assertEqual(row['title']['text'][:25], '<script>alert(1)</script>')
        self.assertIn('\\u202e', row['title']['text'])
        self.assertIn('\\u001b', row['title']['text'])
        self.assertNotIn('‮', row['title']['text'])
        self.assertLessEqual(len(row['title']['text']), t.TITLE_MAX)
        self.assertGreater(row['title']['omitted_chars'], 0)

    def test_an_unknown_view_is_refused(self):
        with self.assertRaises(ValueError):
            t.trace(self.snap(), 'everything')

    def test_recently_closed_means_fourteen_days_of_the_injected_clock(self):
        names = [row['id'] for row in self.view('tasks')['rows']]
        self.assertIn('trial-recent', names)
        self.assertNotIn('trial-old', names)
        self.work[5]['closed_at'] = stamp(14, hours=1)
        self.assertNotIn('trial-recent', [row['id'] for row in self.view('tasks')['rows']])


if __name__ == '__main__':
    unittest.main()
