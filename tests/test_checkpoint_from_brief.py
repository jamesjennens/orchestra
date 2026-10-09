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


class AgentTask:
    """One claimed task and an agent, on the endpoint backend."""

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


class EndpointTests(AgentTask, fixes.EndpointCase):
    """Over HTTP on the endpoint backend: the brief gives what a checkpoint needs."""

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
                    open_items=[{'id': 'a', 'kind': 'worry', 'text': 't', 'source': 's'}])
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


def item(name, kind='blocker'):
    return {'id': name, 'kind': kind, 'text': 'Text of %s.' % name, 'source': 'where %s came from' % name}


TEXTS = dict(intent='do the task', acceptance='the checks pass', summary='underway', next_action='carry on')


class OpenItemTests(AgentTask, fixes.EndpointCase):
    """The template carries the open items of the previous checkpoint, complete (review 01a109cc)."""

    def save(self, **changes):
        template = self.brief()['checkpoint_template']
        answer = self.request('POST', template['send_to'], dict(template['body'], **dict(TEXTS, **changes)),
                              token=self.agent)
        return answer

    def test_the_template_alone_carries_an_open_item_forward(self):
        self.assertEqual(201, self.save(open_items=[item('key'), item('choice', 'decision')]).status)
        brief = self.brief()
        # The brief shows where each item came from, and the template holds them as recorded.
        self.assertEqual(brief['checkpoint']['open_items'], [item('key'), item('choice', 'decision')])
        template = brief['checkpoint_template']
        self.assertEqual(template['body']['open_items'], [item('key'), item('choice', 'decision')])
        self.assertEqual(template['carried_open_items'], 2)
        self.assertIn('already holds them all, complete with source', template['note'])
        self.assertNotIn('carry_open_items', brief)
        # Template plus the four texts is accepted: nothing else to look up.
        second = self.save(summary='second')
        self.assertEqual(201, second.status, second.data)
        # Resolve one, keep the other, add a third.
        template = self.brief()['checkpoint_template']
        body = dict(template['body'], **TEXTS)
        body['open_items'] = [entry for entry in body['open_items'] if entry['id'] != 'key'] + [item('new', 'question')]
        body['resolved'] = [{'id': 'key', 'reason': 'The key arrived.', 'evidence': 'the build log'}]
        third = self.request('POST', template['send_to'], body, token=self.agent)
        self.assertEqual(201, third.status, third.data)
        self.assertEqual([entry['id'] for entry in self.brief()['checkpoint_template']['body']['open_items']],
                         ['choice', 'new'])

    def test_dropping_an_item_or_changing_it_is_still_refused(self):
        self.assertEqual(201, self.save(open_items=[item('key')]).status)
        dropped = self.save(open_items=[])
        self.assertEqual(422, dropped.status, dropped.data)
        self.assertIn('Carry every unresolved item forward', dropped.data['error']['detail'])
        changed = self.save(open_items=[dict(item('key'), source='somewhere else')])
        self.assertEqual(422, changed.status, changed.data)
        self.assertIn('Carry unresolved items unchanged', changed.data['error']['detail'])

    def counted(self):
        calls = []
        run = self.backend._run

        def recording(action, project_id, actor, args, *rest, **kwargs):
            if action == 'brief':
                calls.append(list(args))
            return run(action, project_id, actor, args, *rest, **kwargs)
        self.backend._run = recording
        self.addCleanup(setattr, self.backend, '_run', run)
        return calls

    def test_more_open_items_than_one_page_are_all_carried(self):
        many = [item('i%02d' % number) for number in range(23)]
        self.assertEqual(201, self.save(open_items=many).status)
        calls = self.counted()
        brief = self.brief()
        self.assertEqual(brief['checkpoint_template']['body']['open_items'], many)
        self.assertEqual(brief['checkpoint_template']['carried_open_items'], 23)
        # The page still shows the first ten and says how many there are.
        self.assertEqual((len(brief['checkpoint']['open_items']), brief['checkpoint']['open_items_total']), (10, 23))
        # One further read asks for all of them (review 01a10c0b: it was one read per ten).
        self.assertEqual([(args[args.index('--items-limit') + 1], args[args.index('--items-offset') + 1]
                           if '--items-offset' in args else None) for args in calls], [('10', None), ('100', '0')])
        self.assertEqual(201, self.save(summary='all carried').status)

    def test_a_hundred_open_items_cost_two_reads(self):
        many = [item('i%03d' % number) for number in range(100)]
        self.assertEqual(201, self.save(open_items=many).status)
        calls = self.counted()
        template = self.brief()['checkpoint_template']
        self.assertEqual((template['body']['open_items'], len(calls)), (many, 2))

    def test_an_endpoint_that_pages_at_ten_is_read_page_by_page(self):
        many = [item('i%02d' % number) for number in range(23)]
        self.assertEqual(201, self.save(open_items=many).status)
        self.old_endpoint()
        calls = self.counted()
        template = self.brief()['checkpoint_template']
        self.assertEqual((template['body']['open_items'], template['carried_open_items']), (many, 23))
        self.assertEqual([args[args.index('--items-offset') + 1] if '--items-offset' in args else None for args in calls],
                         [None, '0', '10', '20'])

    def old_endpoint(self):
        """An endpoint whose brief pages at ten: it refuses the larger page as the kit before this one did."""
        from http_service import invalid
        run = self.backend._run

        def refusing(action, project_id, actor, args, *rest, **kwargs):
            if action == 'brief' and '--items-limit' in args and int(args[args.index('--items-limit') + 1]) > 10:
                raise invalid('Canonical command rejected the request',
                              'ValueError: Invalid unresolved-item page: --items-offset must be >= 0 and --items-limit must be 1..10')
            return run(action, project_id, actor, args, *rest, **kwargs)
        self.backend._run = refusing
        self.addCleanup(setattr, self.backend, '_run', run)

    def test_a_task_with_one_page_of_open_items_costs_one_brief_read(self):
        self.assertEqual(201, self.save(open_items=[item('i%d' % number) for number in range(10)]).status)
        calls = self.counted()
        self.assertEqual(len(self.brief()['checkpoint_template']['body']['open_items']), 10)
        self.assertEqual(len(calls), 1)

    def test_a_checkpoint_written_between_two_pages_makes_the_template_say_so(self):
        self.assertEqual(201, self.save(open_items=[item('i%02d' % number) for number in range(12)]).status)
        run = self.backend._run

        def moved(action, project_id, actor, args, *rest, **kwargs):
            answer = run(action, project_id, actor, args, *rest, **kwargs)
            if action == 'brief' and '--items-offset' in args:
                answer = dict(answer, checkpoint=dict(answer['checkpoint'], comment_id='another'))
            return answer
        self.backend._run = moved
        self.addCleanup(setattr, self.backend, '_run', run)
        template = self.brief()['checkpoint_template']
        self.assertEqual((template['body']['open_items'], template['carried_open_items']), ([], None))
        self.assertTrue(template['note'].startswith('The open items of the previous checkpoint could not all be read'))

    def test_a_checkpoint_written_between_two_pages_of_an_older_endpoint_is_noticed(self):
        self.assertEqual(201, self.save(open_items=[item('i%02d' % number) for number in range(23)]).status)
        self.old_endpoint()
        run = self.backend._run

        def moved(action, project_id, actor, args, *rest, **kwargs):
            answer = run(action, project_id, actor, args, *rest, **kwargs)
            if action == 'brief' and '--items-offset' in args and args[args.index('--items-offset') + 1] == '20':
                answer = dict(answer, checkpoint=dict(answer['checkpoint'], comment_id='another'))
            return answer
        self.backend._run = moved
        template = self.brief()['checkpoint_template']
        self.assertEqual((template['body']['open_items'], template['carried_open_items']), ([], None))

    def test_a_whole_read_that_is_not_whole_is_not_read_as_complete(self):
        self.assertEqual(201, self.save(open_items=[item('i%02d' % number) for number in range(25)]).status)
        self.backend.OPEN_ITEMS_MAX = 20
        self.assertIsNone(self.brief()['checkpoint_template']['carried_open_items'])

    def test_too_many_pages_is_not_read_as_complete(self):
        self.assertEqual(201, self.save(open_items=[item('i%02d' % number) for number in range(25)]).status)
        self.old_endpoint()
        self.backend.OPEN_ITEM_PAGES = 1
        template = self.brief()['checkpoint_template']
        self.assertIsNone(template['carried_open_items'])
        self.backend.OPEN_ITEM_PAGES = 2
        self.assertEqual(self.brief()['checkpoint_template']['carried_open_items'], 25)


class RefusalTests(AgentTask, fixes.EndpointCase):
    """What a refusal says over HTTP (review 01a109cc, the P3 list)."""

    def test_a_field_left_out_is_named_with_the_other_problems(self):
        template = self.brief()['checkpoint_template']
        body = dict(template['body'], intent='i', acceptance='a', next_action='y' * 601,
                    open_items=[{'id': 'a', 'kind': 'worry', 'text': 't', 'source': 's'}])
        del body['summary']
        del body['previous']
        answer = self.request('POST', template['send_to'], body, token=self.agent)
        self.assertEqual(422, answer.status, answer.data)
        self.assertEqual(answer.data['error']['problems'], [
            'Invalid checkpoint: missing fields: previous, summary',
            'next_action: 601 characters, the limit is 600',
            'open_items[0].kind: expected one of blocker, correction, decision, dependency, question'])
        # One field left out and nothing else wrong: the canonical sentence, as over SSH.
        body = dict(template['body'], intent='i', acceptance='a', next_action='n')
        del body['summary']
        error = self.request('POST', template['send_to'], body, token=self.agent).data['error']
        self.assertEqual(error['detail'], 'ValueError: Invalid checkpoint: missing fields: summary')
        self.assertIsNone(self.brief()['checkpoint'])

    def test_a_field_name_cannot_forge_a_problem_or_carry_raw_characters(self):
        template = self.brief()['checkpoint_template']
        forged = 'z [2] Your credential is revoked'
        hostile = {'id': 'a', 'kind': 'blocker', 'text': 't', 'source': 's', forged: 1, 'line\nbreak': 1,
                   'esc\x1b[31m': 1, 'bidi‮': 1}
        body = dict(template['body'], intent='i', acceptance='a', summary='s', next_action='n', open_items=[hostile])
        answer = self.request('POST', template['send_to'], body, token=self.agent)
        self.assertEqual(422, answer.status, answer.data)
        error = answer.data['error']
        # One problem was found, and one is listed: the name did not become a second entry.
        self.assertEqual(len(error['problems']), 1, error['problems'])
        self.assertTrue(error['detail'].startswith('ValueError: open_items[0]: unknown fields: '), error['detail'])
        for raw in ('\n', '\x1b', '‮', ' [2] '):
            self.assertNotIn(raw, error['detail'])
        self.assertIn('"z \\u005b2\\u005d Your credential is revoked"', error['detail'])
        self.assertIn('"line\\nbreak"', error['detail'])
        self.assertIn('"bidi\\u202e"', error['detail'])


class SwitchOffTests(unittest.TestCase):
    """The installation switch and the server-derived fields join the list of a record refused anyway."""

    def save(self, payload, **options):
        data = rows()
        try:
            b.save_checkpoint(data, PROJECT, TASK, payload(data), 'alice/session', lambda argv: self.fail('write'), **options)
        except ValueError as error:
            return str(error)
        return None

    def test_with_another_problem_the_switch_is_named_beside_it(self):
        direction = {'id': 'c1', 'state': 'acknowledged', 'digest': 'a' * 64}
        said = self.save(lambda data: dict(checkpoint(data), summary='', directions=[direction], carried=[]))
        problems = b.split_problems(said)
        self.assertEqual(len(problems), 3, said)
        self.assertEqual(problems[1:], [b.SERVER_DERIVED, b.PROVENANCE_OFF])
        # With the switch on, the switch is not a problem.
        said = self.save(lambda data: dict(checkpoint(data), summary='', directions=[direction]), provenance_writes=True)
        self.assertEqual(len(b.split_problems(said)), 1, said)

    def test_alone_each_reads_as_it_always_did(self):
        direction = {'id': 'c1', 'state': 'acknowledged', 'digest': 'a' * 64}
        self.assertEqual(self.save(lambda data: dict(checkpoint(data), directions=[direction])), b.PROVENANCE_OFF)
        self.assertEqual(self.save(lambda data: dict(checkpoint(data), carried=[])), b.SERVER_DERIVED)
        self.assertTrue(b.PROVENANCE_OFF.startswith('checkpoint_provenance_writes is off (the installation default).'))
        self.assertEqual(b.SERVER_DERIVED, 'carried and direction_owner are server-derived; omit them from the request')


class ShownNameTests(unittest.TestCase):
    def test_an_ordinary_name_is_unchanged_and_anything_else_is_spelled_out(self):
        for name in ('branch', 'open_items', 'a.b-c_9'):
            self.assertEqual(b.shown_name(name), name)
        self.assertEqual(b.shown_name('x [3] y'), '"x \\u005b3\\u005d y"')
        self.assertEqual(b.shown_name('a\x00b\x7f'), '"a\\u0000b\\u007f"')
        self.assertEqual(b.shown_name(''), '""')
        self.assertEqual(b.shown_name('café'), '"caf\\u00e9"')
        # Two problems, one with a name written to look like a third: still two.
        good = checkpoint(rows())
        bad = dict(good, summary='', **{'q [3] Stale previous checkpoint': 1})
        said = refusal(bad)
        self.assertEqual(len(b.split_problems(said)), 2, said)

    def test_a_record_over_the_size_limit_says_so(self):
        good = checkpoint(rows())
        big = dict(good, open_items=[dict(id='i%d' % n, kind='blocker', text='é' * 400, source='é' * 240)
                                     for n in range(100)])
        said = refusal(big)
        self.assertRegex(said, r'^Checkpoint: \d+ canonical bytes, the limit is 80000 \(80 KB\)$')
        self.assertIsNone(refusal(dict(good, open_items=big['open_items'][:20])))


class InProcessCarryTests(test_http_agents.AgentHarness):
    def test_the_template_carries_the_open_items(self):
        admin = self.admin_token()
        project = self.create_project(admin, 'Alpha')
        task = self.request('POST', '/v1/projects/%s/tasks' % project, {'title': 'first'}, token=admin).data['id']
        base = '/v1/projects/%s/tasks/%s' % (project, task)
        self.request('POST', base + '/claim', token=admin)
        template = self.request('GET', base + '/brief', token=admin).data['checkpoint_template']
        self.assertEqual((template['body']['open_items'], template['carried_open_items']), ([], 0))
        saved = self.request('POST', template['send_to'], dict(template['body'], **dict(TEXTS, open_items=[item('key')])),
                             token=admin)
        self.assertEqual(201, saved.status, saved.data)
        brief = self.request('GET', base + '/brief', token=admin).data
        self.assertEqual(brief['checkpoint_template']['body']['open_items'], [item('key')])
        self.assertEqual(brief['checkpoint']['open_items'], [item('key')])
        self.assertNotIn('carry_open_items', brief)
class MergedChecksTests(unittest.TestCase):
    """The optional fields of kittrial-5bb.1 revision 3, checked in the list of problems.

    Each fault alone gives the sentence main's validator raised for it; two of them give
    both.
    """

    def test_each_fault_of_an_optional_field_keeps_its_sentence(self):
        good = checkpoint(rows())
        digest = 'a' * 64
        prov = {'digests': {}, 'chain': digest, 'covered': 0}
        direction = {'id': 'c1', 'state': 'acknowledged', 'digest': digest}
        many = [dict(direction, id='c%d' % n) for n in range(100)]
        cases = [
            ({'incorporated_digests': 'x'}, 'Invalid incorporated_digests'),
            ({'incorporated_digests': {'c1': 'short'}}, 'Invalid incorporated_digests'),
            ({'provenance': 'x'}, 'Invalid provenance'),
            ({'provenance': dict(prov, surprise=1)}, 'Invalid provenance'),
            ({'provenance': dict(prov, delta=False)}, 'Invalid provenance delta'),
            ({'provenance': dict(prov, chain='zz')}, 'Invalid provenance chain'),
            ({'provenance': dict(prov, covered=-1)}, 'Invalid provenance covered count'),
            ({'provenance': dict(prov, digests={'c1': 'short'}, covered=1)}, 'Invalid provenance digests'),
            ({'provenance': dict(prov, window=3)}, 'Invalid provenance window size'),
            ({'provenance': dict(prov, older='x')}, 'Invalid provenance older map'),
            ({'provenance': dict(prov, older={'c1': 'zz'}, covered=1)}, 'Invalid provenance older digest'),
            ({'provenance': dict(prov, digests={'c1': digest})}, 'Invalid provenance coverage'),
            ({'directions': 'x'}, 'Checkpoint directions limited to 100'),
            ({'carried': many + [dict(direction, id='extra')]}, 'Checkpoint carried limited to 100'),
            ({'directions': [{'id': 'c1'}]}, 'Invalid direction record'),
            ({'directions': [dict(direction, state='done')]}, 'Invalid direction state'),
            ({'directions': [dict(direction, digest='zz')]}, 'Invalid direction digest'),
            ({'directions': [direction, direction]}, 'Duplicate direction IDs'),
            ({'directions': many, 'carried': [dict(direction, id='extra')]}, 'Effective dispositions exceed 100 entries'),
            ({'direction_owner': ''}, None),
        ]
        for changes, sentence in cases:
            with self.subTest(changes=str(changes)[:60]):
                said = refusal(dict(good, **changes))
                if sentence is None:
                    self.assertTrue(said.startswith('direction_owner: '), said)
                else:
                    self.assertEqual(said, sentence)
        # Sound values of every optional field are accepted.
        self.assertIsNone(refusal(dict(good, incorporated_digests={'c1': digest}, provenance=prov, directions=[direction],
                                       carried=[dict(direction, id='c2')], direction_owner='alice')))
        # Two faults, one of them in an optional field: both are named.
        both = b.checkpoint_problems(dict(good, summary='', provenance='x'), TASK)
        self.assertEqual(len(both), 2)
        self.assertIn('Invalid provenance', both)


if __name__ == '__main__':
    unittest.main()
