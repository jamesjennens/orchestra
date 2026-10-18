"""The one assignment-time display-name -> actor-id rule (kittrial-5bb.333)."""
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT)]

import assignee_names

AGENT = 'agent_5848a46b42198e52'
SESSION = 'session-11111111-1111-1111-1111-111111111111'
OTHER = 'session-22222222-2222-2222-2222-222222222222'


def roster():
    return [{'actor': AGENT, 'name': 'jj-vscode-1', 'kind': 'agent'},
            {'actor': SESSION, 'name': 'ds-worker-1', 'kind': 'session'},
            {'actor': 'alice', 'name': 'Alice Example', 'kind': 'account'}]


class IndexTests(unittest.TestCase):
    def test_index_names_and_actors(self):
        index = assignee_names.index(roster())
        self.assertEqual('jj-vscode-1', index['by_actor'][AGENT])
        self.assertEqual([AGENT], index['by_name']['jj-vscode-1'])
        self.assertEqual([SESSION], index['by_name']['ds-worker-1'])

    def test_a_name_that_is_a_known_actor_id_is_not_an_alias(self):
        # Actor ids known to have acted (a legacy/SSH actor) win over any display name that
        # spells them: this is what stops P1/P5 from renaming an existing actor.
        index = assignee_names.index(roster() + [{'actor': 'bob', 'name': AGENT, 'kind': 'account'}],
                                     actors=['w1'])
        self.assertNotIn(AGENT, index['by_name'])
        self.assertIn('w1', index['by_actor'])
        self.assertIsNone(index['by_actor']['w1'])

    def test_malformed_entries_are_ignored(self):
        index = assignee_names.index([None, {}, {'actor': ''}, {'name': 'x'},
                                      {'actor': 'ok', 'name': ''}])
        self.assertEqual({'ok': None}, index['by_actor'])
        self.assertEqual({}, index['by_name'])


class ResolveTests(unittest.TestCase):
    def test_exact_actor_passes_unchanged(self):
        picked = assignee_names.resolve(AGENT, roster())
        self.assertFalse(picked['resolved'])
        self.assertEqual(AGENT, picked['actor'])
        self.assertIsNone(picked['sentence'])

    def test_a_known_actor_that_has_acted_is_exact_even_when_a_session_has_that_name(self):
        # P5: `w1` is a stored assignee. A session later registered under the name `w1`
        # must never be selected for it.
        index = assignee_names.index(roster() + [{'actor': OTHER, 'name': 'w1', 'kind': 'session'}],
                                     actors=['w1'])
        picked = assignee_names.resolve('w1', index)
        self.assertFalse(picked['resolved'])
        self.assertEqual('w1', picked['actor'])

    def test_unique_display_name_resolves_and_says_so(self):
        picked = assignee_names.resolve('jj-vscode-1', roster())
        self.assertTrue(picked['resolved'])
        self.assertEqual(AGENT, picked['actor'])
        self.assertEqual('jj-vscode-1', picked['name'])
        self.assertIn('jj-vscode-1', picked['sentence'])
        self.assertIn(AGENT, picked['sentence'])

    def test_matching_is_exact_not_case_insensitive(self):
        # The M2 mutant (case-insensitive matching). A case, whitespace or hyphen variant is
        # a different name and must be refused, not resolved.
        for variant in ('JJ-VSCODE-1', 'jj-vscode-1 ', 'jj‐vscode-1', ' jj-vscode-1'):
            with self.subTest(variant=variant):
                with self.assertRaises(assignee_names.NameRefusal):
                    assignee_names.resolve(variant, roster())

    def test_unknown_name_is_refused_with_one_sentence(self):
        with self.assertRaises(assignee_names.NameRefusal) as caught:
            assignee_names.resolve('nobody-at-all', roster())
        text = str(caught.exception)
        self.assertIn('nobody-at-all', text)
        self.assertIn('actor id', text)
        self.assertEqual(1, text.count('.'))

    def test_ambiguous_name_is_refused_naming_both_actors(self):
        entries = roster() + [{'actor': OTHER, 'name': 'jj-vscode-1', 'kind': 'agent'}]
        with self.assertRaises(assignee_names.NameRefusal) as caught:
            assignee_names.resolve('jj-vscode-1', entries)
        text = str(caught.exception)
        self.assertIn(AGENT, text)
        self.assertIn(OTHER, text)
        self.assertEqual(1, text.count('.'))

    def test_empty_value_is_refused(self):
        for value in ('', None):
            with self.assertRaises(assignee_names.NameRefusal):
                assignee_names.resolve(value, roster())


class LabelTests(unittest.TestCase):
    def test_label_maps_an_actor_to_name_and_never_a_name_to_an_actor(self):
        self.assertEqual('jj-vscode-1 (%s)' % AGENT,
                         assignee_names.label(AGENT, roster()))
        # A raw display name is NOT resolved by the label path: it is returned unchanged, so
        # a label can never decide an owner.
        self.assertEqual('jj-vscode-1', assignee_names.label('jj-vscode-1', roster()))
        self.assertIsNone(assignee_names.label(None, roster()))
        self.assertEqual('w1', assignee_names.label('w1', roster()))

    def test_display_name_is_actor_to_name_only(self):
        self.assertEqual('jj-vscode-1', assignee_names.display_name(AGENT, roster()))
        self.assertEqual('jj-vscode-1', assignee_names.display_name('jj-vscode-1', roster()))
        self.assertEqual('unknown-name', assignee_names.display_name('unknown-name', roster()))


if __name__ == '__main__':
    unittest.main()
