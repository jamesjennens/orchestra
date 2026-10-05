"""A first checkpoint from the brief alone, and every problem in one refusal (kittrial-5bb.113).

Seen with a real agent: the HTTP brief did not carry the activity cursor a checkpoint
needs, the record's shape was documented nowhere the agent could read, and each refusal
named one problem, so writing a first checkpoint took several round trips and the agent
gave up with its blocker recorded nowhere.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import briefing as b
import test_http_agents
import test_http_review_fixes as fixes
from test_briefing import PROJECT, TASK, checkpoint, rows


def refusal(payload, task=TASK):
    try:
        b.validate_checkpoint(payload, task)
    except ValueError as error:
        return str(error)
    return None


class OneProblemTests(unittest.TestCase):
    """With one problem the refusal is the sentence it has always been, byte for byte."""

    def test_each_single_fault_reads_exactly_as_before(self):
        good = checkpoint(rows())
        self.assertIsNone(refusal(good))
        cases = [
            ('not an object', ['x'], 'Invalid checkpoint: expected a JSON object'),
            ('an unknown field', dict(good, extra=1), 'Invalid checkpoint: unknown fields: extra'),
            ('a missing field', {k: v for k, v in good.items() if k != 'branch'}, 'Invalid checkpoint: missing fields: branch'),
            ('the schema version', dict(good, schema_version=2), 'Invalid checkpoint: schema_version must be integer 1'),
            ('a true schema version', dict(good, schema_version=True), 'Invalid checkpoint: schema_version must be integer 1'),
            ('another task', dict(good, task='other-task'), 'Checkpoint task mismatch'),
            ('a bad previous', dict(good, previous='not an id!'), 'Invalid task/item ID'),
            ('a long summary', dict(good, summary='x' * 1001), 'summary: 1001 characters, the limit is 1000'),
            ('an empty intent', dict(good, intent=''), None),
            ('a cursor that is not one', dict(good, activity_cursor='!!!'), 'Invalid cursor'),
            ('a cursor of another kind', dict(good, activity_cursor=b.token({'kind': 'history', 'task': TASK})),
             'Expected task activity cursor from brief/history'),
            ('items that are not a list', dict(good, open_items={}), 'open_items: expected a list of at most 100 items'),
            ('an item that is not an object', dict(good, open_items=['x']),
             'open_items[0]: expected an object with fields id, kind, source, text'),
            ('an item with a missing field', dict(good, open_items=[dict(id='a', kind='blocker', text='t')]),
             'open_items[0]: missing fields: source; allowed fields: id, kind, source, text'),
            ('an item kind', dict(good, open_items=[dict(id='a', kind='worry', text='t', source='s')]),
             'open_items[0].kind: expected one of blocker, correction, decision, dependency, question'),
            ('an item text', dict(good, open_items=[dict(id='a', kind='blocker', text='x' * 401, source='s')]),
             'open_items[0].text (id a): 401 characters, the limit is 400'),
            ('two items with one id', dict(good, open_items=[dict(id='a', kind='blocker', text='t', source='s')] * 2),
             'Duplicate item IDs'),
        ]
        for label, payload, expected in cases:
            with self.subTest(fault=label):
                said = refusal(payload)
                if expected is None:
                    self.assertIsNotNone(said)                    # whatever the text rule says, alone
                    self.assertNotIn('problems', said)
                else:
                    self.assertEqual(said, expected)
                self.assertEqual(b.split_problems(said), [said])


class ManyProblemsTests(unittest.TestCase):
    def test_five_faults_are_named_in_one_refusal_each_in_its_own_sentence(self):
        good = checkpoint(rows())
        bad = dict(good, schema_version=3, summary='x' * 1001, next_action='y' * 601, activity_cursor='!!!',
                   open_items=[dict(id='a', kind='worry', text='t', source='s')])
        said = refusal(bad)
        sentences = ['Invalid checkpoint: schema_version must be integer 1',
                     'summary: 1001 characters, the limit is 1000', 'next_action: 601 characters, the limit is 600',
                     'Invalid cursor',
                     'open_items[0].kind: expected one of blocker, correction, decision, dependency, question']
        self.assertEqual(said, 'Invalid checkpoint (5 problems): ' + ' '.join(
            '[%d] %s' % (number, sentence) for number, sentence in enumerate(sentences, 1)))
        for sentence in sentences:                        # a caller that matches on one still finds it
            self.assertIn(sentence, said)
        self.assertEqual(b.split_problems(said), sentences)
        self.assertEqual(b.checkpoint_problems(bad, TASK), sentences)

    def test_every_item_is_checked_not_only_the_first(self):
        good = checkpoint(rows())
        bad = dict(good, open_items=[dict(id='a', kind='blocker', text='t'),                     # missing source
                                     'not an object',
                                     dict(id='c', kind='worry', text='x' * 401, source='s' * 241),
                                     dict(id='d', kind='question', text='fine', source='fine')],
                   resolved=[dict(id='e', reason='', evidence='e'), dict(id='f', reason='r', evidence='e', extra=1)])
        problems = b.checkpoint_problems(bad, TASK)
        self.assertEqual(problems[:5], [
            'open_items[0]: missing fields: source; allowed fields: id, kind, source, text',
            'open_items[1]: expected an object with fields id, kind, source, text',
            'open_items[2].kind: expected one of blocker, correction, decision, dependency, question',
            'open_items[2].text (id c): 401 characters, the limit is 400',
            'open_items[2].source (id c): 241 characters, the limit is 240'])
        self.assertEqual(len(problems), 7)
        self.assertTrue(problems[5].startswith('resolved[0].reason (id e):'))
        self.assertEqual(problems[6], 'resolved[1]: unknown fields: extra; allowed fields: evidence, id, reason')
        self.assertEqual(b.split_problems(b.problems_message(problems)), problems)

    def test_missing_fields_do_not_hide_what_is_wrong_with_the_fields_that_are_there(self):
        good = checkpoint(rows())
        bad = {k: v for k, v in good.items() if k not in ('branch', 'resolved')}
        bad.update(summary='x' * 1001, extra=1)
        self.assertEqual(b.checkpoint_problems(bad, TASK),
                         ['Invalid checkpoint: unknown fields: extra; missing fields: branch, resolved',
                          'summary: 1001 characters, the limit is 1000'])

    def test_the_list_is_capped_and_says_how_many_more(self):
        good = checkpoint(rows())
        bad = dict(good, open_items=[dict(id='i%d' % n, kind='worry', text='t', source='s') for n in range(30)])
        said = refusal(bad)
        self.assertTrue(said.startswith('Invalid checkpoint (30 problems): [1] open_items[0].kind'))
        self.assertTrue(said.endswith(' (+10 more)'))
        self.assertIn('[20] open_items[19].kind', said)
        self.assertNotIn('[21]', said)
        self.assertEqual(len(b.split_problems(said)), 20)
        self.assertFalse(b.split_problems(said)[-1].endswith('more)'))

    def test_a_bracketed_number_inside_a_sentence_does_not_split_it(self):
        # A number that is not the next one in the list belongs to the sentence it is in.
        sentences = ['summary: said [7] times', 'Invalid cursor']
        self.assertEqual(b.split_problems(b.problems_message(sentences)), sentences)
        self.assertEqual(b.split_problems(''), [])
        self.assertEqual(b.split_problems(None), [])

    def test_a_hostile_value_cannot_make_the_refusal_long(self):
        good = checkpoint(rows())
        bad = dict(good, **{'k%04d' % n: 1 for n in range(500)})
        said = refusal(dict(bad, summary='x' * 5000))
        self.assertLess(len(said), 1500)

    def test_nothing_is_written_for_a_refused_record(self):
        data = rows()
        with self.assertRaises(ValueError):
            b.save_checkpoint(data, PROJECT, TASK, dict(checkpoint(data), summary='', intent=''), 'alice/session',
                              lambda argv: self.fail('write'))


class EndpointTests(fixes.EndpointCase):
    """Over HTTP on the endpoint backend: the brief gives what a checkpoint needs."""

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.project = self.create_project(self.admin, 'Alpha')
        user_id = self.create_account(self.admin, 'alex', 'alex-password-1')
        self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, user_id), {'role': 'contributor'},
                     token=self.admin)
        alex = self.login('alex', 'alex-password-1')[0]
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/alex/k',
                                                   'projects': [self.project]}, token=alex)
        self.agent = made.data['credential']['secret']
        self.task = self.create_task(self.admin, self.project, 'a task').data['id']
        self.base = '/v1/projects/%s/tasks/%s' % (self.project, self.task)
        self.assertEqual(200, self.request('POST', self.base + '/claim', {}, token=self.agent).status)

    def brief(self):
        answer = self.request('GET', self.base + '/brief', token=self.agent)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def test_an_agent_writes_a_first_checkpoint_from_the_brief_alone(self):
        brief = self.brief()
        history = self.request('GET', self.base + '/history?limit=5', token=self.agent).data
        self.assertTrue(brief['activity_cursor'])
        self.assertEqual(brief['activity_cursor'], history['activity_cursor'])
        template = brief['checkpoint_template']
        self.assertEqual((template['send_to'], template['method']), (self.base + '/checkpoints', 'POST'))
        self.assertEqual(template['required'], ['intent', 'acceptance', 'summary', 'next_action'])
        self.assertEqual(sorted(template['optional']), ['branch', 'open_items', 'resolved', 'source_commit'])
        self.assertEqual(template['open_item']['kind'], ['blocker', 'correction', 'decision', 'dependency', 'question'])
        self.assertEqual(template['limits']['summary'], '<= 1000 characters')
        body = dict(template['body'])
        self.assertEqual((body['previous'], body['activity_cursor']), (None, brief['activity_cursor']))
        # Fill the four texts and one blocker; leave out what there is nothing to say about.
        body.update(intent='do the task', acceptance='the checks pass', summary='stopped: no key',
                    next_action='wait for the key',
                    open_items=[{'id': 'key', 'kind': 'blocker', 'text': 'No key for the registry.',
                                 'source': 'the build log'}])
        for name in ('source_commit', 'branch', 'resolved'):
            del body[name]
        saved = self.request('POST', template['send_to'], body, token=self.agent)
        self.assertEqual(201, saved.status, saved.data)
        after = self.brief()
        self.assertEqual(after['checkpoint']['summary'], 'stopped: no key')
        self.assertEqual([item['id'] for item in after['checkpoint']['open_items']], ['key'])
        # The stored record is the canonical one: every field, the left-out ones empty.
        stored = [c for row in self.canonical_rows() if row['id'] == self.task for c in row['comments']
                  if c['text'].startswith(b.PREFIX)]
        record = json.loads(stored[-1]['text'][len(b.PREFIX):])
        b.validate_checkpoint(record, self.task)
        self.assertEqual((record['source_commit'], record['branch'], record['resolved']), ('', '', []))
        # The next template carries the new previous and a new cursor.
        self.assertEqual(after['checkpoint_template']['body']['previous'], after['checkpoint']['id'])
        self.assertNotEqual(after['activity_cursor'], brief['activity_cursor'])

    def test_a_record_with_five_faults_gets_one_refusal_that_lists_them(self):
        template = self.brief()['checkpoint_template']
        body = dict(template['body'], intent='i', acceptance='a', summary='x' * 1001, next_action='y' * 601,
                    schema_version=3, activity_cursor='!!!',
                    open_items=[{'id': 'a', 'kind': 'worry', 'text': 't', 'source': 's'}], surprise=True)
        answer = self.request('POST', template['send_to'], body, token=self.agent)
        self.assertEqual(422, answer.status, answer.data)
        error = answer.data['error']
        self.assertEqual(error['message'], 'Canonical command rejected the request')
        self.assertTrue(error['detail'].startswith('ValueError: Invalid checkpoint (5 problems): [1] '), error['detail'])
        self.assertEqual(error['problems'], [
            'Invalid checkpoint: schema_version must be integer 1',
            'summary: 1001 characters, the limit is 1000', 'next_action: 601 characters, the limit is 600',
            'Invalid cursor',
            'open_items[0].kind: expected one of blocker, correction, decision, dependency, question'])
        self.assertIsNone(self.brief()['checkpoint'])

    def test_one_fault_reads_as_it_always_did_with_the_list_beside_it(self):
        template = self.brief()['checkpoint_template']
        body = dict(template['body'], intent='i', acceptance='a', summary='x' * 1001, next_action='n')
        error = self.request('POST', template['send_to'], body, token=self.agent).data['error']
        self.assertEqual(error['detail'], 'ValueError: summary: 1001 characters, the limit is 1000')
        self.assertEqual(error['problems'], ['summary: 1001 characters, the limit is 1000'])

    def test_a_stale_cursor_is_still_its_own_refusal(self):
        template = self.brief()['checkpoint_template']
        body = dict(template['body'], intent='i', acceptance='a', summary='s', next_action='n')
        self.assertEqual(201, self.request('POST', template['send_to'], body, token=self.agent).status)
        again = self.request('POST', template['send_to'], dict(body, summary='second'), token=self.agent)
        self.assertEqual(422, again.status, again.data)
        self.assertIn('Stale previous checkpoint; read brief again', again.data['error']['detail'])


class InProcessTests(test_http_agents.AgentHarness):
    def test_the_brief_carries_the_template_and_the_optional_fields_may_be_left_out(self):
        admin = self.admin_token()
        project = self.create_project(admin, 'Alpha')
        task = self.request('POST', '/v1/projects/%s/tasks' % project, {'title': 'first'}, token=admin).data['id']
        base = '/v1/projects/%s/tasks/%s' % (project, task)
        self.request('POST', base + '/claim', token=admin)
        brief = self.request('GET', base + '/brief', token=admin).data
        self.assertIsNone(brief['activity_cursor'])
        template = brief['checkpoint_template']
        self.assertEqual(template['send_to'], base + '/checkpoints')
        body = dict(template['body'], intent='i', acceptance='a', summary='underway', next_action='n')
        for name in ('source_commit', 'branch', 'open_items', 'resolved'):
            del body[name]
        saved = self.request('POST', template['send_to'], body, token=admin)
        self.assertEqual(201, saved.status, saved.data)
        after = self.request('GET', base + '/brief', token=admin).data
        self.assertEqual(after['checkpoint_template']['body']['previous'], after['checkpoint']['id'])


if __name__ == '__main__':
    unittest.main()
