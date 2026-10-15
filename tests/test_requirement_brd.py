"""Source wording stays inert and exact revision changes remain distinguishable."""
import copy
import unittest

from requirements import content_hash
from requirement_brd import compare, render


def record(identity='demo.1', revision=1, state='draft', title='Need', description='Wording', kind='requirement'):
    value = dict(id=identity, revision=revision, title=title, description=description, acceptance_state=state)
    if kind == 'requirement':
        value['key'] = identity
    value['sha256'] = content_hash(value)
    return value


def item(current, accepted=None, state='active', kind='requirement'):
    return dict(id=current['id'], kind=kind, current=current, accepted=accepted,
                pending_draft=current if current['acceptance_state'] == 'draft' else None,
                requirement_state=state, state=None, reason=None)


def view(*items):
    return dict(project='demo', governance={'mode': 'simple'}, items=list(items), decisions=[], questions=[])


class MarkdownTests(unittest.TestCase):
    def test_pending_requirement_and_narrative_each_warn_and_keep_exact_refs(self):
        accepted = record(state='accepted')
        pending = record(revision=2, description='New unaccepted wording')
        narrative = record('demo.2', kind='brd-section', title='Purpose')
        document = render(view(item(pending, accepted), item(narrative, kind='brd-section'))).decode()
        self.assertEqual(document.count('**Pending - not accepted**'), 2)
        self.assertIn('**Accepted wording**', document)
        for revision in (accepted, pending, narrative):
            self.assertIn(revision['sha256'], document)
        self.assertIn('Freezing this view does not accept a baseline', document)

    def test_hostile_markdown_html_controls_and_bidi_are_text(self):
        value = record(title='[click](javascript:alert(1))\n# heading',
                       description='<script>alert(1)</script>\n![image](https://bad.test)\n\x1b[2J\u202ehidden')
        document = render(view(item(value))).decode()
        self.assertNotIn('<script>', document)
        self.assertNotIn('![image](', document)
        self.assertNotIn('\x1b', document)
        self.assertNotIn('\u202e', document)
        self.assertIn('&lt;script&gt;', document)
        self.assertIn('u202e', document)
        self.assertIn('\\[click\\]', document)
        self.assertNotIn('\n# heading', document)

    def test_render_is_deterministic_lf_and_does_not_mutate_frozen_input(self):
        data = view(item(record('demo.2')), item(record('demo.1')))
        frozen = copy.deepcopy(data)
        first = render(data)
        data['items'].reverse()
        self.assertEqual(first, render(data))
        self.assertEqual(sorted(data['items'], key=lambda x: x['id']), sorted(frozen['items'], key=lambda x: x['id']))
        self.assertTrue(first.endswith(b'\n'))
        self.assertNotIn(b'\r', first)

    def test_tampered_selected_content_and_duplicate_ids_refuse(self):
        value = record()
        value['description'] = 'Rewritten without hash'
        with self.assertRaisesRegex(ValueError, 'does not match'):
            render(view(item(value)))
        good = item(record())
        with self.assertRaisesRegex(ValueError, 'repeats an ID'):
            render(view(good, good))

    def test_terminal_reason_and_acceptance_decision_survive_in_markdown(self):
        accepted = record(state='accepted')
        terminal = item(accepted, accepted, 'superseded')
        terminal['state'] = dict(sha256='a' * 64, superseded_by=dict(id='demo.2', revision=3, sha256='b' * 64))
        terminal['reason'] = dict(reason='Replaced by <new> wording')
        terminal['acceptance'] = dict(decision={'decision_id': 'demo.9'})
        document = render(view(terminal)).decode()
        self.assertIn('State: superseded', document)
        self.assertIn('Replaced by &lt;new&gt; wording', document)
        self.assertIn('Superseded by: demo', document)
        self.assertIn('a' * 64, document)
        self.assertIn('b' * 64, document)
        self.assertIn('Acceptance decision: demo', document)

    def test_wrong_item_kind_or_cross_id_revision_is_never_silently_omitted(self):
        wrong = item(record())
        wrong['kind'] = 'unknown-new-kind'
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            render(view(wrong))
        wrong = item(record())
        wrong['current'] = record('demo.2')
        with self.assertRaisesRegex(ValueError, 'another ID'):
            render(view(wrong))

    def test_empty_and_unreadable_are_explicit(self):
        self.assertIn(b'No requirements yet.', render(view()))
        bad = dict(id='demo.1', kind='unreadable', unreadable=True)
        self.assertIn(b'This record cannot be verified', render(view(bad)))


class ComparisonTests(unittest.TestCase):
    def test_pending_edit_does_not_become_accepted_and_has_both_exact_refs(self):
        accepted = record(state='accepted')
        old = view(item(accepted, accepted))
        pending = record(revision=2)
        result = compare(old, view(item(pending, accepted)))
        self.assertEqual(result['groups']['edited'], ['demo.1'])
        self.assertEqual(result['groups']['pending'], ['demo.1'])
        self.assertEqual(result['groups']['accepted'], [])
        self.assertEqual(result['changes'][0]['before']['current']['sha256'], accepted['sha256'])
        self.assertEqual(result['changes'][0]['after']['current']['sha256'], pending['sha256'])

    def test_acceptance_terminal_added_and_removed_are_distinct(self):
        draft = record()
        accepted = record(revision=2, state='accepted')
        old = view(item(draft), item(record('demo.2')))
        terminal = item(accepted, accepted, 'superseded')
        terminal['state'] = dict(sha256='f' * 64)
        new = view(terminal, item(record('demo.3')))
        result = compare(old, new)
        self.assertEqual(result['groups']['accepted'], ['demo.1'])
        self.assertEqual(result['groups']['superseded'], ['demo.1'])
        self.assertEqual(result['groups']['withdrawn'], [])
        self.assertEqual(result['groups']['removed'], ['demo.2'])
        self.assertEqual(result['groups']['added'], ['demo.3'])
        self.assertEqual(compare(new, new)['total'], 0)

    def test_comparison_ignores_clocks_and_refuses_unverified_or_wrong_project(self):
        data = view(item(record()))
        other = copy.deepcopy(data)
        data['at'], other['at'] = '2099-01-01', '1999-01-01'
        self.assertEqual(compare(data, other)['total'], 0)
        other['items'][0]['requirement_state'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            compare(data, other)
        other['project'] = 'other'
        with self.assertRaisesRegex(ValueError, 'same project'):
            compare(data, other)


if __name__ == '__main__':
    unittest.main()
