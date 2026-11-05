"""kittrial-5bb.515, release A (readers): the approve record's additive ``approval_basis`` field.

The field is the server's statement of why an approval was let through (kittrial-5bb.438, ``agent_approval:
by-agent``). Release A teaches every reader to read and show it and every other writer to refuse it; nothing
writes it yet (release B, tests/test_approval_basis_writers.py). Each test below fails when its rule is broken.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import briefing
import review_workflow as w
import work

#: The last kit before the field existed (main at the start of kittrial-5bb.515).
BEFORE_THE_FIELD = 'c01d067'
TASK = 'proj-1'
SENTENCE = ('Approved by a coordinator agent of the same person as the agent that delivered it '
            '(project setting agent_approval: by-agent).')


def contribute():
    return {'schema_version': 1, 'operation': 'contribute', 'operation_id': 'op-1', 'task': TASK, 'previous': None,
            'repository': 'r', 'commit': 'a' * 40, 'base_commit': 'b' * 40, 'supersedes': None, 'summary': 'delivered',
            'delivery': {'kind': 'remote', 'remote': 'origin', 'branch': 'work'}}


def approve(**extra):
    return dict({'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-2', 'task': TASK, 'previous': '1',
                 'contribution': '1', 'summary': 'accepted'}, **extra)


def chain(approval):
    return w.projection([(contribute(), {'id': 1, 'author': 'worker', 'created_at': '2026-01-01T00:00:00Z'}),
                         (approval, {'id': 2, 'author': 'heron', 'created_at': '2026-01-02T00:00:00Z'})])


class OlderReader(unittest.TestCase):
    def test_a_kit_before_the_field_refuses_a_record_that_carries_it(self):
        """Why the rollout is two releases: the old validate keeps the approve field set exact."""
        git = shutil.which('git')
        shown = subprocess.run([git, '-C', str(KIT), 'show', BEFORE_THE_FIELD + ':review_workflow.py'],
                               capture_output=True, text=True) if git else None
        if shown is None or shown.returncode:
            print('NOTE: OlderReader was SKIPPED: %s is not readable from git here.' % BEFORE_THE_FIELD, file=sys.stderr)
            self.skipTest('the pre-field review_workflow.py is not readable from git')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'review_workflow_before.py'
            path.write_text(shown.stdout, encoding='utf-8')
            spec = importlib.util.spec_from_file_location('review_workflow_before', path)
            old = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(old)
        old.validate(approve(), TASK)                                     # the old form reads there
        with self.assertRaisesRegex(ValueError, 'Invalid review workflow fields'):
            old.validate(approve(approval_basis='same-person-agent'), TASK)


class NewReader(unittest.TestCase):
    def test_both_forms_read(self):
        w.validate(approve(), TASK)
        w.validate(approve(approval_basis='same-person-agent'), TASK)
        field, old = chain(approve(approval_basis='same-person-agent')), chain(approve(summary=SENTENCE))
        self.assertEqual('awaiting-integration', field['review_state'])
        self.assertEqual('awaiting-integration', old['review_state'])
        self.assertEqual(('same-person-agent', 'heron', '2'),
                         tuple(field['approval_basis'][key] for key in ('basis', 'author', 'comment_id')))
        self.assertEqual(w.APPROVAL_BASES['same-person-agent'], field['approval_basis']['text'])
        # The old form: the sentence is the approver's summary and is not read as a basis.
        self.assertNotIn('approval_basis', old)

    def test_each_value_reads_with_its_own_label(self):
        labels = {
            'same-person-agent': 'Allowed by: coordinator agent of the same person as the delivering agent '
                                 '(project setting agent_approval: by-agent; the audit entry holds who set it and when)',
            'same-person-worker-credential': 'Allowed by: coordinator agent of the same person as the delivering '
                                             'worker credential (project setting agent_approval: by-agent; the audit '
                                             'entry holds who set it and when)'}
        self.assertEqual(labels, w.APPROVAL_BASES)                  # exactly these two values (kittrial-5bb.519)
        for value, label in labels.items():
            with self.subTest(value=value):
                w.validate(approve(approval_basis=value), TASK)
                read = chain(approve(approval_basis=value))
                self.assertEqual((value, label), (read['approval_basis']['basis'], read['approval_basis']['text']))

    def test_a_basis_is_only_shown_for_a_standing_approval(self):
        new_contribution = dict(contribute(), operation_id='op-3', previous='2', supersedes='1')
        again = w.projection([(contribute(), {'id': 1, 'author': 'worker', 'created_at': '2026-01-01T00:00:00Z'}),
                              (approve(approval_basis='same-person-agent'),
                               {'id': 2, 'author': 'heron', 'created_at': '2026-01-02T00:00:00Z'}),
                              (new_contribution, {'id': 3, 'author': 'worker', 'created_at': '2026-01-03T00:00:00Z'})])
        self.assertEqual('awaiting-review', again['review_state'])
        self.assertNotIn('approval_basis', again)

    def test_the_field_is_a_closed_vocabulary(self):
        for bad in ('Same-Person-Agent', 'other', '', None, 7, ['same-person-agent'], {'x': 1},
                    'same-person-worker', 'worker-credential', 'same-person-worker-credential '):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, 'Invalid approval basis'):
                    w.validate(approve(approval_basis=bad), TASK)

    def test_the_field_belongs_to_approve_alone(self):
        other = dict(contribute(), approval_basis='same-person-agent')
        with self.assertRaisesRegex(ValueError, 'Invalid review workflow fields'):
            w.validate(other, TASK)

    def test_the_brief_names_it_on_a_line_of_its_own(self):
        review = chain(approve(approval_basis='same-person-agent'))
        result = {'task': TASK, 'title': {'text': 't', 'omitted_chars': 0}, 'owner': {'text': 'o', 'omitted_chars': 0},
                  'status': 'open', 'review': review, 'intent': {'text': 'i', 'omitted_chars': 0},
                  'acceptance': {'text': 'a', 'omitted_chars': 0}, 'current_position': 'p', 'next_action': 'n',
                  'checkpoint': None, 'newer': None, 'directions': None, 'resolved': None,
                  'unresolved': {'total': 0, 'items': [], 'next_offset': None}, 'dependencies': {}, 'lifecycle': {},
                  'lifecycle_scope': {}, 'activity_cursor': 'c', 'evidence': {}, 'warnings': []}
        lines = briefing.format_brief(result).splitlines()
        self.assertIn('Approval basis: ' + w.APPROVAL_BASES['same-person-agent'], lines)
        result['review'] = chain(approve(summary=SENTENCE))
        self.assertFalse([line for line in briefing.format_brief(result).splitlines()
                          if line.startswith('Approval basis:')])


class Advertised(unittest.TestCase):
    def test_the_review_help_lists_the_optional_fields_per_operation(self):
        fields = work.help_payload('review')['optional_fields']
        self.assertEqual(['approval_basis'], fields['approve'])
        self.assertEqual(['follows'], fields['contribute'])

    def test_a_kit_without_the_field_does_not_list_it(self):
        # kittrial-5bb.515 r2 (N5): the help is derived from OPTIONAL, so a kit whose approve allows no
        # approval_basis lists none, and a writer reading that help does not see it advertised.
        import unittest.mock
        import http_service
        self.assertTrue(http_service.approval_basis_listed({'optional_fields': w.help_optional_fields()}))
        with unittest.mock.patch.dict(w.OPTIONAL, {'approve': set()}):
            listed = w.help_optional_fields()
            self.assertNotIn('approval_basis', listed.get('approve', []))
            self.assertFalse(http_service.approval_basis_listed({'optional_fields': listed}))


class NobodyElseWrites(unittest.TestCase):
    def test_a_write_outside_the_service_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'approval_basis is set by the web service'):
            w.check_write_fields(approve(approval_basis='same-person-agent'))
        w.check_write_fields(approve(approval_basis='same-person-agent'), by_service=True)
        w.check_write_fields(approve())

    def test_the_host_review_path_refuses_it_before_anything_is_written(self):
        """review_workflow.execute without the service's verified authority (the host `review` action)."""
        rows = [{'id': TASK, 'status': 'open', 'issue_type': 'task', 'comments': []}]
        wrote = []
        with self.assertRaisesRegex(ValueError, 'approval_basis is set by the web service'):
            w.execute(rows, TASK, 'host-user', approve(approval_basis='same-person-agent'),
                      lambda argv: wrote.append(argv) or '[]')
        self.assertEqual([], wrote)

    def test_a_value_outside_the_vocabulary_is_refused_even_for_the_service(self):
        rows = [{'id': TASK, 'status': 'open', 'issue_type': 'task', 'comments': []}]
        with self.assertRaisesRegex(ValueError, 'Invalid approval basis'):
            w.execute(rows, TASK, 'svc', approve(approval_basis='anything'), lambda argv: '[]',
                      verified_web_authority=True)


class Page(unittest.TestCase):
    def test_the_page_shows_the_basis_apart_from_the_summary_and_an_old_sentence_is_plain_text(self):
        node = shutil.which('node')
        if not node:
            print('NOTE: Page.test_the_page_shows_the_basis was SKIPPED: node is not installed.', file=sys.stderr)
            self.skipTest('node is not installed')
        import test_http_web
        done = test_http_web.run_node_module(self, node, 'await import(process.argv[1])',
                                             (KIT / 'tests' / 'web_task_approval_basis.mjs').as_uri(),
                                             (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web' / 'js' / 'api.js').as_uri(),
                                             (KIT / 'web' / 'js' / 'views' / 'task.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        field, old = seen['withField'], seen['oldForm']
        self.assertEqual(1, len(field['lines']), field)
        self.assertTrue(field['lines'][0]['text'].startswith('Allowed by: coordinator agent of the same person'))
        self.assertEqual('same-person-agent', field['lines'][0]['basis'])
        # Apart from the summary: the line is not one of the summary paragraphs and they do not contain it.
        self.assertTrue(all('Allowed by' not in prose for prose in field['prose']), field['prose'])
        self.assertIn('Delivered the change', field['prose'])
        # The old form: the sentence is just text (in the delivery summary and in the history entry), no line.
        self.assertEqual([], old['lines'])
        self.assertIn(seen['sentence'], old['prose'])
        self.assertIn('accepted ' + seen['sentence'], old['text'])
        self.assertNotIn('Allowed by', old['text'])
        # The worker-credential value shows the label the server sends for it, on its own line.
        credential = seen['withCredential']
        self.assertEqual([{'text': w.APPROVAL_BASES['same-person-worker-credential'],
                           'basis': 'same-person-worker-credential'}], credential['lines'])
        self.assertTrue(all('Allowed by' not in prose for prose in credential['prose']), credential['prose'])


if __name__ == '__main__':
    unittest.main()
