"""One newer-activity definition and the 'changes since your checkpoint' section (kittrial-5bb.354).

Pinned here:
- the marker (kittrial-5bb.351) also moves on a title, description or acceptance edit, and
  never on a claim, a status change or the owner's own comment;
- the released ``newer_activity`` boolean is ONE function that work rows and brief share;
- the section lists new, edited and late entries with author, time, entry id and own or
  other, clipped and terminal-safe, with coverage and omissions, and says so with no
  checkpoint;
- the changed fields come from the tracker's version history (``bd history``): field,
  when, current and previous value, who not recorded; when that cannot be read, a note;
- that read is read-only (shared lock, a read verb, ``--readonly``) and cached per
  tracker generation;
- the web routes (next, wait, the task read) carry the same fields on both backends.
"""
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import briefing as b
import field_history
import read_cache
import work
from test_briefing import PROJECT, STAMP, TASK, comment, rows, save_cp

COORD = 'coordinator/session'
FIELDS = ('newer_activity', 'newer_activity_marker', 'newer_activity_by_others', 'newer_activity_own',
          'newer_activity_coverage', 'unresolved_directions')


def queue_row(data):
    return work.queue(data, 'alice/session', ['--mine'])['items'][0]


def history_json(*commits):
    """A ``bd history --json`` answer in bd 1.2.2's shape, newest first."""
    return json.dumps([{'CommitHash': 'c%031d' % index, 'Committer': 'beads', 'CommitDate': at,
                        'Issue': dict({'id': TASK, 'status': 'in_progress'}, **values)}
                       for index, (at, values) in enumerate(commits)])


class TheMarkerCase(unittest.TestCase):
    """The marker moves on other-party entries and on the three fields, never on a claim."""

    def setUp(self):
        self.data = rows()
        save_cp(self.data, 'cp1', next_action='Wait for the coordinator')

    def marker(self, actor='alice/session'):
        return work.queue(self.data, actor, ['--mine'])['items'][0]['newer_activity_marker']

    def test_a_current_checkpoint_has_a_fields_marker(self):
        row = queue_row(self.data)
        self.assertEqual('current', row['newer_activity_coverage'])
        self.assertTrue(row['newer_activity_marker'].startswith('fields:'), row['newer_activity_marker'])
        self.assertEqual(len(row['newer_activity_marker'].split(':', 1)[1]), b.MARKER_DIGEST)

    def test_a_claim_or_a_status_change_does_not_move_it(self):
        first = self.marker()
        # Another agent claims the task: its row reads the same marker.
        self.data[0]['assignee'] = 'bob/session'
        self.assertEqual(first, self.marker('bob/session'))
        self.data[0]['assignee'] = 'alice/session'
        self.data[0]['status'] = 'blocked'
        self.data[0]['labels'] = ['priority:high']
        self.data[0]['notes'] = 'A note'
        self.assertEqual(first, self.marker())

    def test_two_description_edits_give_two_markers(self):
        first = self.marker()
        self.data[0]['description'] = 'Intent, revised once'
        second = self.marker()
        self.data[0]['description'] = 'Intent, revised twice'
        third = self.marker()
        self.assertEqual(3, len({first, second, third}))
        for name, value in (('title', 'A renamed task'), ('acceptance_criteria', 'Stricter checks')):
            before = self.marker()
            self.data[0][name] = value
            self.assertNotEqual(before, self.marker(), name)

    def test_an_own_comment_does_not_move_it_and_an_other_party_comment_does(self):
        first = self.marker()
        self.data[0]['comments'].append(comment('own', 'My own note', '2026-09-16T01:00:00Z'))
        self.assertEqual(first, self.marker())
        self.data[0]['comments'].append(comment('dir', 'A direction', '2026-09-16T02:00:00Z', author=COORD))
        moved = self.marker()
        self.assertTrue(moved.startswith(TASK + '-cdir:'), moved)
        # The same entry with an edited description moves it again.
        self.data[0]['description'] = 'Intent, revised'
        self.assertNotEqual(moved, self.marker())

    def test_the_agents_own_claim_comment_and_checkpoint(self):
        """A client wakes on a CHANGE of the marker: the agent's own acts move it at most once."""
        first = self.marker()
        # Its own claim (released, then claimed again by the same agent) and its own comment.
        self.data[0]['assignee'] = None
        self.data[0]['status'] = 'open'
        self.data[0]['assignee'] = 'alice/session'
        self.data[0]['status'] = 'in_progress'
        self.assertEqual(first, self.marker())
        self.data[0]['comments'].append(comment('own', 'Starting on it', '2026-09-16T01:00:00Z'))
        self.assertEqual(first, self.marker())
        # Its own checkpoint with nothing other-party newer: unchanged, no wake.
        save_cp(self.data, 'cp2', previous='cp1', next_action='Go on')
        self.assertEqual(first, self.marker())
        # After a coordinator comment, its own checkpoint changes the marker exactly once
        # (back to the fields marker), and the next reads are stable.
        self.data[0]['comments'].append(comment('dir', 'A direction', '2026-09-16T02:00:00Z', author=COORD))
        woke = self.marker()
        self.assertNotEqual(first, woke)
        save_cp(self.data, 'cp3', previous='cp2', next_action='Doing it')
        after = self.marker()
        self.assertNotEqual(woke, after)
        self.assertEqual(first, after)
        self.assertEqual(after, self.marker())

    def test_brief_carries_the_same_fields_as_the_queue_row(self):
        self.data[0]['comments'].append(comment('dir', 'A direction', '2026-09-16T02:00:00Z', author=COORD))
        self.data[0]['comments'].append(comment('own', 'My own note', '2026-09-16T03:00:00Z'))
        row = queue_row(self.data)
        brief = b.brief(self.data, PROJECT, TASK)
        self.assertEqual({name: row[name] for name in FIELDS}, brief['activity'])
        self.assertEqual(brief['newer']['newer_activity_marker'], row['newer_activity_marker'])
        self.assertEqual((1, 1), (row['newer_activity_by_others'], row['newer_activity_own']))

    def test_the_boolean_keeps_its_released_rule(self):
        # An other-party comment after the checkpoint sets it; an edit to an older one
        # (counted, and moving the marker) does not: older clients read what they read.
        data = rows()
        data[0]['comments'].append(comment('old', 'An earlier direction', '2026-09-14T00:00:00Z', author=COORD))
        save_cp(data, 'cp1', next_action='Go on')
        self.assertFalse(queue_row(data)['newer_activity'])
        data[0]['comments'][1]['text'] = 'An earlier direction, corrected'
        row = queue_row(data)
        self.assertFalse(row['newer_activity'])
        self.assertEqual(1, row['newer_activity_by_others'])
        data[0]['comments'].append(comment('dir', 'A new direction', '2026-09-16T02:00:00Z', author=COORD))
        self.assertTrue(queue_row(data)['newer_activity'])
        self.assertTrue(b.brief(data, PROJECT, TASK)['activity']['newer_activity'])

    def test_a_legacy_checkpoint_has_no_marker(self):
        from test_briefing import append_checkpoint, checkpoint
        data = rows()
        append_checkpoint(data, 'cp1', checkpoint(data))
        self.assertIsNone(queue_row(data)['newer_activity_marker'])
        data[0]['description'] = 'Changed'
        self.assertIsNone(queue_row(data)['newer_activity_marker'])


class TheSectionCase(unittest.TestCase):
    """The section's entries: new, edited and late, own and other, clipped, bounded."""

    def setUp(self):
        self.data = rows()
        self.data[0]['comments'].append(comment('old', 'An earlier direction', '2026-09-14T00:00:00Z', author=COORD))
        save_cp(self.data, 'cp1', next_action='Wait')

    def section(self, **kwargs):
        return b.brief(self.data, PROJECT, TASK, **kwargs)['changes_since_checkpoint']

    def test_a_current_checkpoint_lists_nothing(self):
        section = self.section()
        self.assertEqual(('current', 0, []), (section['coverage'], section['total'], section['entries']))
        self.assertEqual('cp1', section['checkpoint']['comment_id'])

    def test_new_edited_and_own_entries_appear_newest_first(self):
        self.data[0]['comments'].append(comment('dir', 'Please also do X', '2026-09-16T02:00:00Z', author=COORD))
        self.data[0]['comments'].append(comment('mine', 'Noted, doing X', '2026-09-16T03:00:00Z'))
        self.data[0]['comments'][1]['text'] = 'An earlier direction, corrected'
        section = self.section()
        self.assertEqual(3, section['total'])
        self.assertEqual((2, 1), (section['other_count'], section['own_count']))
        got = [(e['entry_id'].rsplit('-c', 1)[1], e['state'], e['own'], e['author']['text']) for e in section['entries']]
        self.assertEqual([('mine', 'new', True, 'alice/session'), ('dir', 'new', False, COORD),
                          ('old', 'edited', False, COORD)], got)
        self.assertEqual('Please also do X', section['entries'][1]['body']['text'])
        self.assertEqual('history %s --since-checkpoint' % TASK, section['history'])
        text = b.format_brief(b.brief(self.data, PROJECT, TASK))
        self.assertIn('Changes since your checkpoint (READ THESE FIRST): 3 entries, 2 by others, 1 own', text)
        self.assertIn('[comment, edited] by %s: An earlier direction, corrected' % COORD, text)

    def test_a_late_arrival_is_marked_late(self):
        self.data[0]['comments'].append(comment('late', 'Backdated', '2026-09-15T09:00:00Z', author=COORD))
        [entry] = self.section()['entries']
        self.assertEqual('late', entry['state'])

    def test_a_new_checkpoint_clears_it(self):
        self.data[0]['comments'].append(comment('dir', 'Please also do X', '2026-09-16T02:00:00Z', author=COORD))
        self.assertEqual(1, self.section()['total'])
        save_cp(self.data, 'cp2', next_action='Doing X')
        section = self.section()
        self.assertEqual(('current', 0), (section['coverage'], section['total']))
        self.assertEqual('cp2', section['checkpoint']['comment_id'])

    def test_no_checkpoint_says_so(self):
        # The JSON brief leaves both blocks out (it keeps its compact size); the text says
        # so, and the web task read builds the section's sentence (TheInProcessRoutes /
        # TheRoutesAgree cases and the endpoint's _no_checkpoint_section).
        brief = b.brief(rows(), PROJECT, TASK)
        self.assertNotIn('changes_since_checkpoint', brief)
        self.assertNotIn('activity', brief)
        import http_service
        section = http_service.EndpointBackend._no_checkpoint_section(TASK)
        self.assertIsNone(section['checkpoint'])
        self.assertEqual(b.SECTION_NO_CHECKPOINT, section['note'])
        self.assertIn('Changes since your checkpoint: ' + b.SECTION_NO_CHECKPOINT,
                      b.format_brief(b.brief(rows(), PROJECT, TASK)))

    def test_hostile_text_is_clipped_and_stays_text(self):
        hostile = '\x1b[31mRED\x1b]0;title\x07 ' + 'x' * 1000 + '‮'
        self.data[0]['comments'].append(comment('dir', hostile, '2026-09-16T02:00:00Z', author=COORD))
        [entry] = self.section()['entries']
        self.assertNotIn('\x1b', entry['body']['text'])
        self.assertNotIn('\x07', entry['body']['text'])
        self.assertIn('\\u001b', entry['body']['text'])
        self.assertEqual(b.SECTION_BODY_CHARS, len(entry['body']['text']))
        self.assertGreater(entry['body']['omitted_chars'], 0)
        self.assertNotIn('\x1b', b.format_brief(b.brief(self.data, PROJECT, TASK)))

    def test_it_is_bounded_and_counts_what_it_leaves_out(self):
        for index in range(30):
            self.data[0]['comments'].append(comment('d%02d' % index, 'y' * 400, '2026-09-16T%02d:00:00Z' % (index % 24),
                                                    author=COORD))
        section = self.section()
        self.assertEqual(30, section['total'])
        self.assertLessEqual(len(section['entries']), b.SECTION_ENTRIES_MAX)
        self.assertEqual(section['total'] - len(section['entries']), section['omitted'])
        spent = sum(b.encoded_size(e['body']['text']) for e in section['entries'])
        self.assertLessEqual(spent, b.SECTION_BUDGET)


class TheFieldsCase(unittest.TestCase):
    """Which of the three fields changed, from the tracker's version history."""

    def setUp(self):
        self.data = rows()
        save_cp(self.data, 'cp1', next_action='Wait')
        self.at = STAMP        # the checkpoint comment's time

    def history(self, *commits):
        return lambda task: history_json(*commits)

    def test_two_description_edits_name_the_field_and_its_previous_value(self):
        self.data[0]['description'] = 'Intent, revised twice'
        read = self.history(('2026-09-16T02:00:00.5Z', {'title': 'A task', 'description': 'Intent, revised twice',
                                                       'acceptance_criteria': 'Acceptance'}),
                            ('2026-09-16T01:00:00.2Z', {'title': 'A task', 'description': 'Intent, revised once',
                                                       'acceptance_criteria': 'Acceptance'}),
                            ('2026-09-15T10:00:00.9Z', {'title': 'A task', 'description': 'Intent',
                                                       'acceptance_criteria': 'Acceptance'}),
                            ('2026-09-15T09:00:00Z', {'title': 'A task', 'description': 'Intent',
                                                     'acceptance_criteria': 'Acceptance'}),
                            # An older value before the checkpoint is not "previous": the
                            # newest commit at or before the checkpoint is.
                            ('2026-09-14T09:00:00Z', {'title': 'A task', 'description': 'Draft intent',
                                                     'acceptance_criteria': 'Acceptance'}))
        section = b.brief(self.data, PROJECT, TASK, field_history=read)['changes_since_checkpoint']
        self.assertIsNone(section['fields_note'])
        [field] = section['fields']
        self.assertEqual(('description', None, 2, '2026-09-16T02:00:00Z'),
                         (field['field'], field['author'], field['edits'], field['changed_at'][:19] + 'Z'))
        self.assertEqual('Intent', field['previous']['text'])
        self.assertEqual('Intent, revised twice', field['current']['text'])
        self.assertEqual(field_history.WHO_NOTE, field['note'])
        text = b.format_brief(b.brief(self.data, PROJECT, TASK, field_history=read))
        self.assertIn('field description changed (who made the change is not recorded): now "Intent, revised twice"', text)
        self.assertIn('previous "Intent"', text)

    def test_a_claim_lists_no_field(self):
        self.data[0]['assignee'] = 'bob/session'
        read = self.history(('2026-09-16T02:00:00Z', {'title': 'A task', 'description': 'Intent',
                                                     'acceptance_criteria': 'Acceptance'}),
                            ('2026-09-15T09:00:00Z', {'title': 'A task', 'description': 'Intent',
                                                     'acceptance_criteria': 'Acceptance'}))
        section = b.brief(self.data, PROJECT, TASK, field_history=read)['changes_since_checkpoint']
        self.assertEqual(([], None), (section['fields'], section['fields_note']))

    def test_when_it_cannot_be_known_it_says_so(self):
        self.data[0]['title'] = 'Renamed'
        cases = {'no reader': None,
                 'unreadable': lambda task: 'not json',
                 'wrong shape': lambda task: json.dumps({'Issue': {}}),
                 'too short': self.history(('2026-09-16T02:00:00Z', {'title': 'Renamed'})),
                 'raises': mock.Mock(side_effect=ValueError('bd failed'))}
        for name, read in cases.items():
            with self.subTest(name):
                section = b.brief(self.data, PROJECT, TASK, field_history=read)['changes_since_checkpoint']
                self.assertEqual([], section['fields'])
                self.assertTrue(section['fields_note'].startswith('Which task fields changed could not be determined'),
                                section['fields_note'])

    def test_it_is_read_only_when_the_checkpoint_is_stale(self):
        read = mock.Mock(return_value=history_json())
        b.brief(self.data, PROJECT, TASK, field_history=read)
        read.assert_not_called()

    def test_values_are_clipped_and_terminal_safe(self):
        self.data[0]['description'] = '\x1b[2J' + 'z' * 500
        same = {'title': 'A task', 'acceptance_criteria': 'Acceptance'}
        read = self.history(('2026-09-16T02:00:00Z', dict(same, description=self.data[0]['description'])),
                            ('2026-09-15T09:00:00Z', dict(same, description='Intent')))
        [field] = b.brief(self.data, PROJECT, TASK, field_history=read)['changes_since_checkpoint']['fields']
        self.assertNotIn('\x1b', field['current']['text'])
        self.assertEqual(field_history.VALUE_CHARS, len(field['current']['text']))


class TheReadIsReadOnlyCase(unittest.TestCase):
    """The history read takes the lock shared, is a read verb, and carries --readonly."""

    def test_the_command(self):
        argv = field_history.command(TASK)
        self.assertEqual(['history', TASK, '--json', '--limit', str(field_history.HISTORY_LIMIT), '--readonly'], argv)
        from http_authority import is_mutating_invocation
        self.assertFalse(is_mutating_invocation(argv))
        if os.name != 'nt':
            import endpoint
            self.assertTrue(endpoint.coordination_lock_shared({'action': 'brief', 'args': [TASK]}))

    def test_the_cache_serves_one_generation_and_refuses_a_tampered_file(self):
        from test_http_service import unique_dir
        home = unique_dir('history-cache-')
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        project = home / 'projects' / 'trial'
        project.mkdir(parents=True)
        fetch = mock.Mock(side_effect=lambda: 'answer-%d' % fetch.call_count)
        with mock.patch.object(read_cache, 'project_generation', return_value='k' * 32):
            self.assertEqual('answer-1', read_cache.cached_task_history(home, project, TASK, fetch))
            self.assertEqual('answer-1', read_cache.cached_task_history(home, project, TASK, fetch))
            self.assertEqual(1, fetch.call_count)
            stored = project / read_cache.CACHE_DIR / read_cache._history_file(TASK)
            stored.write_bytes(stored.read_bytes()[:-1] + b'X')
            # A tampered file is refused and replaced by a fresh read, which is then served.
            self.assertEqual('answer-2', read_cache.cached_task_history(home, project, TASK, fetch))
            self.assertEqual('answer-2', read_cache.cached_task_history(home, project, TASK, fetch))
        with mock.patch.object(read_cache, 'project_generation', return_value='m' * 32):
            self.assertEqual('answer-3', read_cache.cached_task_history(home, project, TASK, fetch))
        with mock.patch.object(read_cache, 'project_generation', return_value=None):
            self.assertEqual('answer-4', read_cache.cached_task_history(home, project, TASK, fetch))


import claim_limit  # noqa: E402
import test_http_proposals  # noqa: E402
import test_http_review_fixes as fixes  # noqa: E402


class TheRoutesAgreeCase(fixes.EndpointCase):
    """next, wait and the task read carry the SAME fields, from the canonical endpoint."""

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.project = self.create_project(admin, 'Alpha')
        tokens = {}
        for name, role in (('alex', 'contributor'), ('blair', 'owner')):
            user_id = self.create_account(admin, name, name + '-password-1')
            added = self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, user_id), {'role': role},
                                 token=admin)
            self.assertIn(added.status, (200, 201), added.data)
            tokens[name] = self.login(name, name + '-password-1')[0]
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/alex/kestrel',
                                                   'projects': [self.project], 'claim_limit': claim_limit.LIMIT_MAX},
                            token=tokens['alex'])
        self.assertEqual(201, made.status, made.data)
        self.agent, self.secret = made.data['agent'], made.data['credential']['secret']
        self.task = self.create_task(tokens['blair'], self.project, 'the task').data['id']
        self.base = '/v1/projects/%s/tasks/%s' % (self.project, self.task)

    def native(self, actor):
        return test_http_proposals.stub_module().Canonical(self.canonical_root, self.project, actor=actor).run

    def checkpoint(self, previous=None):
        history = self.request('GET', self.base + '/history?limit=5', token=self.secret).data
        answer = self.request('POST', self.base + '/checkpoints', {
            'schema_version': 1, 'previous': previous, 'activity_cursor': history['activity_cursor'],
            'source_commit': '', 'branch': '', 'intent': 'do the task', 'acceptance': 'the checks pass',
            'summary': 'stopped', 'next_action': 'wait', 'open_items': [], 'resolved': []}, token=self.secret)
        self.assertEqual(201, answer.status, answer.data)

    def read(self):
        """The fields as next, wait and the task read each carry them."""
        nxt = self.request('GET', '/v1/agents/me/next', token=self.secret)
        self.assertEqual(200, nxt.status, nxt.data)
        action = next(x for x in nxt.data['next_actions'] if x['task'] == self.task)
        wait = self.request('GET', '/v1/agents/me/wait', token=self.secret)
        self.assertEqual(200, wait.status, wait.data)
        row = next(x for x in wait.data['items'] if x['task'] == self.task)
        page = self.request('GET', self.base + '/brief', token=self.secret)
        self.assertEqual(200, page.status, page.data)
        # unresolved_directions is left out of the comparison: the STUB's work path counts
        # directions with provenance writes on while its brief path reads the installation
        # switch (off here); the real endpoint passes the same switch to both, and the
        # canonical case above compares all six.
        same = [name for name in FIELDS if name != 'unresolved_directions']
        return ({name: action.get(name) for name in same}, {name: row.get(name) for name in same},
                {name: page.data['activity'].get(name) for name in same}, page.data['changes_since_checkpoint'])

    # The canonical stub is a child Python process: on Windows its piped output is UTF-8
    # only in UTF-8 mode, and a stale brief's text carries an em dash (as in
    # test_agent_attention's read-only test).
    @mock.patch.dict(os.environ, {'PYTHONUTF8': '1'})
    def test_every_route_carries_the_same_fields_and_the_section_names_the_comment(self):
        self.assertEqual(200, self.request('POST', self.base + '/claim', {}, token=self.secret).status)
        self.checkpoint()
        self.native('blair')(['comments', 'add', self.task, 'A direction after the checkpoint.'])
        on_next, on_wait, on_page, section = self.read()
        self.assertEqual(on_next, on_wait)
        self.assertEqual(on_next, on_page)
        self.assertTrue(on_next['newer_activity'])
        self.assertEqual(1, on_next['newer_activity_by_others'])
        [entry] = section['entries']
        self.assertEqual(('blair', False, 'A direction after the checkpoint.'),
                         (entry['author']['text'], entry['own'], entry['body']['text']))
        # The agent's own comment: counted as own on every route, and listed as own.
        self.native(self.agent['actor'])(['comments', 'add', self.task, 'Working on it.'])
        on_next, on_wait, on_page, section = self.read()
        self.assertEqual(on_next, on_wait)
        self.assertEqual(on_next, on_page)
        self.assertEqual((1, 1), (on_next['newer_activity_by_others'], on_next['newer_activity_own']))
        self.assertEqual([True, False], [e['own'] for e in section['entries']])
        # A new checkpoint clears it everywhere.
        page = self.request('GET', self.base + '/brief', token=self.secret).data
        self.checkpoint(previous=page['checkpoint']['id'])
        on_next, on_wait, on_page, section = self.read()
        self.assertEqual(on_next, on_page)
        self.assertEqual((0, 0, False), (on_next['newer_activity_by_others'], on_next['newer_activity_own'],
                                         on_next['newer_activity']))
        self.assertEqual(0, section['total'])


import test_http_agents  # noqa: E402


class TheInProcessRoutesAgreeCase(test_http_agents.AgentHarness):
    """The disposable backend: the task read's block IS its agent row's fields."""

    def test_next_and_the_task_read_carry_the_same_fields(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        _agent, secret, _ = self.agent_secret(alex, projects=[project], claim_limit=claim_limit.LIMIT_MAX)
        task = self.request('POST', '/v1/projects/%s/tasks' % project, {'title': 'the task'}, token=alex).data['id']
        base = '/v1/projects/%s/tasks/%s' % (project, task)
        self.assertEqual(200, self.request('POST', base + '/claim', token=secret).status)
        page = self.request('GET', base + '/brief', token=secret).data
        self.assertIsNone(page['activity'])
        self.assertEqual(b.SECTION_NO_CHECKPOINT, page['changes_since_checkpoint']['note'])
        made = self.request('POST', base + '/checkpoints', {
            'schema_version': 1, 'previous': None, 'activity_cursor': page.get('activity_cursor') or '',
            'source_commit': '', 'branch': '', 'intent': 'do the task', 'acceptance': 'the checks pass',
            'summary': 'stopped', 'next_action': 'wait', 'open_items': [], 'resolved': []}, token=secret)
        self.assertEqual(201, made.status, made.data)
        said = self.request('POST', base + '/comments', {'text': 'A direction.'}, token=alex)
        self.assertIn(said.status, (200, 201), said.data)
        action = next(x for x in self.request('GET', '/v1/agents/me/next', token=secret).data['next_actions']
                      if x['task'] == task)
        page = self.request('GET', base + '/brief', token=secret).data
        self.assertEqual({name: action.get(name) for name in FIELDS}, page['activity'])
        section = page['changes_since_checkpoint']
        self.assertEqual('unknown', section['coverage'])
        self.assertEqual(1, section['total'])
        self.assertIn('keeps no tracker history', section['fields_note'])


class ThePromptsCase(unittest.TestCase):
    """The worker prompt and the agent prompt say to read the section first after a wake."""

    def test_they_name_the_section(self):
        kit = Path(__file__).resolve().parents[1]
        worker = (kit / 'templates' / 'WORKER_PROMPT.md').read_text(encoding='utf-8')
        self.assertIn('After a wake, read `brief TASK` first', worker)
        self.assertIn('Changes since your checkpoint (READ THESE', worker)
        import agent_prompts
        source = Path(agent_prompts.__file__).read_text(encoding='utf-8')
        self.assertIn('changes_since_checkpoint first: ', source)
        self.assertIn('After a wake, read that brief', source)


class TheTaskPageCase(unittest.TestCase):
    """The task page shows the section, as text, and nothing without a checkpoint."""

    def test_the_page_lists_the_changes_as_text(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        kit = Path(__file__).resolve().parents[1]
        done = test_http_web.run_node_module(self, node, 'await import(process.argv[1])',
                                             (kit / 'tests' / 'web_changes_since_checkpoint.mjs').as_uri(),
                                             (kit / 'tests' / 'web_dom_shim.mjs').as_uri(),
                                             (kit / 'web' / 'js' / 'api.js').as_uri(),
                                             (kit / 'web' / 'js' / 'views' / 'task.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(2, len(seen['entries']), seen['entries'])
        self.assertIn('yours', seen['entries'][0])
        self.assertIn('Working on it', seen['entries'][0])
        self.assertIn('edited', seen['entries'][1])
        self.assertIn('(40 more characters)', seen['entries'][1])
        [field] = seen['fields']
        for part in ('Description changed', 'who made the change is not recorded', 'Now: New intent',
                     'Before: Old intent'):
            self.assertIn(part, field)
        self.assertTrue(seen['heading'])
        self.assertTrue(seen['omitted'])
        # The hostile body is shown as its text: no element was made from it.
        self.assertTrue(seen['hostileAsText'])
        self.assertEqual(0, seen['images'])
        self.assertFalse(seen['pwned'])
        self.assertEqual(1, seen['listedMarked'])
        self.assertEqual(1, seen['nothingMarked'])
        self.assertTrue(seen['nothingText'])
        self.assertEqual(0, seen['noCheckpoint'])


if __name__ == '__main__':
    unittest.main()
