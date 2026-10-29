"""kittrial-5bb.431: a lane is told when a new revision leaves the previous request's items open.

Only a respond record answers a review item; a revision or a plain comment does not. The contribute answer
says so (a warning naming the request and item ids, beside the additive `open_items`), and the `work` row,
the brief and the HTTP next-actions answer name the open ids and the respond record until each is answered.
Advice only: the contribution is always recorded.
"""
import json
import secrets
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import briefing
import claim_limit
import review_state
import review_workflow as rw
import work
from requirements import canonical_bytes
from test_briefing import PROJECT, TASK, rows
import test_http_review_fixes as fixes
from test_http_web import TeamHarness, COMMIT as WEB_COMMIT

ALICE = 'alice/session'
CLIENT_WAY = 'a respond record (`review %s --file respond.json`, operation respond)' % TASK


def add(data, payload, cid, author):
    data[0].setdefault('comments', []).append(dict(id=cid, author=author, created_at='2026-09-16T00:00:00Z',
                                                   text=rw.PREFIX + canonical_bytes(payload).decode()))


def contribute(opid='send-1', previous=None, commit='a' * 40, supersedes=None):
    return dict(schema_version=1, operation='contribute', operation_id=opid, task=TASK, previous=previous,
                repository='ssh://git/example', commit=commit, base_commit='b' * 40,
                delivery=dict(kind='bundle', path='server:/rev.bundle', sha256='c' * 64),
                summary='Contribution delivered', supersedes=supersedes)


def request(opid, previous, ids, notes=()):
    items = [dict(id=i, text='Fix ' + i) for i in ids] + [dict(id=n, text='Note ' + n, severity='note') for n in notes]
    return dict(schema_version=1, operation='request-changes', operation_id=opid, task=TASK, previous=previous,
                contribution='delivery', items=items)


def respond(opid, previous, pairs, contribution='delivery'):
    return dict(schema_version=1, operation='respond', operation_id=opid, task=TASK, previous=previous,
                contribution=contribution,
                resolutions=[dict(request=r, item=i, reason='Done.', evidence='e' * 40) for r, i in pairs])


class Chain(unittest.TestCase):
    def setUp(self):
        self.data = rows()
        self.data[0]['comments'] = []
        self.data[0]['assignee'] = ALICE
        add(self.data, contribute(), 'delivery', ALICE)

    def native(self, cid):
        def run(args):
            if args[0] == 'export':
                return '\n'.join(json.dumps(r) for r in self.data)
            self.assertEqual(args[:3], ['comments', 'add', TASK])
            self.data[0]['comments'].append(dict(id=cid, text=args[3], author=ALICE, created_at='2026-09-16T01:00:00Z'))
            return json.dumps({'id': cid})
        return run

    def revise(self, previous, opid='send-2', cid='delivery-2'):
        payload = contribute(opid, previous, commit='e' * 40, supersedes='delivery')
        return rw.execute(self.data, TASK, ALICE, payload, self.native(cid), review_writes=True)

    def item(self):
        return work.queue(self.data, ALICE, ['--mine'])['items'][0]


class ContributeAnswerTests(Chain):
    def test_a_revision_with_open_items_warns_with_their_ids_and_is_still_recorded(self):
        add(self.data, request('review-1', 'delivery', ['a', 'b', 'c', 'd']), 'feedback', 'reviewer')
        add(self.data, respond('answer-1', 'feedback', [('feedback', 'a')]), 'answer', ALICE)
        answer = self.revise('answer')
        self.assertEqual(answer['open_items'], [dict(request='feedback', item=i, contribution='delivery') for i in 'bcd'])
        self.assertEqual(answer['warnings'], ['3 items of request feedback are still open (b, c, d): a new revision does '
                                              'not answer them; answer each with %s.' % CLIENT_WAY])
        self.assertEqual((answer['comment_id'], answer['review_state']), ('delivery-2', 'changes-requested'))
        self.assertEqual(self.data[0]['comments'][-1]['id'], 'delivery-2')

    def test_one_open_item_reads_in_the_singular(self):
        add(self.data, request('review-1', 'delivery', ['only']), 'feedback', 'reviewer')
        self.assertEqual(self.revise('feedback')['warnings'],
                         ['1 item of request feedback is still open (only): a new revision does not answer '
                          'them; answer each with %s.' % CLIENT_WAY])

    def test_a_revision_after_every_item_was_answered_does_not_warn(self):
        add(self.data, request('review-1', 'delivery', ['a', 'b'], notes=['n']), 'feedback', 'reviewer')
        add(self.data, respond('answer-1', 'feedback', [('feedback', 'a'), ('feedback', 'b')]), 'answer', ALICE)
        answer = self.revise('answer')
        # The note is still open, and never warns: it never blocks.
        self.assertEqual(answer['open_items'], [])
        self.assertNotIn('warnings', answer)
        self.assertEqual(answer['review_state'], 'awaiting-review')

    def test_each_request_gets_its_own_sentence(self):
        add(self.data, request('review-1', 'delivery', ['a', 'b']), 'feedback', 'reviewer')
        add(self.data, request('review-2', 'feedback', ['x']), 'feedback-2', 'reviewer')
        warnings = self.revise('feedback-2')['warnings']
        self.assertEqual(len(warnings), 2)
        self.assertTrue(warnings[0].startswith('2 items of request feedback are still open (a, b)'))
        self.assertTrue(warnings[1].startswith('1 item of request feedback-2 is still open (x)'))

    def test_an_exact_replay_answers_the_same_advice(self):
        add(self.data, request('review-1', 'delivery', ['a', 'b']), 'feedback', 'reviewer')
        first = self.revise('feedback')
        again = self.revise('feedback')
        self.assertTrue(again['reconciled'])
        self.assertEqual((again['open_items'], again['warnings']), (first['open_items'], first['warnings']))
        self.assertEqual(sum(1 for c in self.data[0]['comments'] if c['id'] == 'delivery-2'), 1)

    def test_other_operations_are_unchanged(self):
        add(self.data, request('review-1', 'delivery', ['a']), 'feedback', 'reviewer')
        answer = rw.execute(self.data, TASK, ALICE, respond('answer-1', 'feedback', [('feedback', 'a')]),
                            self.native('answer'), review_writes=True)
        self.assertNotIn('open_items', answer)
        self.assertNotIn('warnings', answer)


class WorkAndBriefTests(Chain):
    def test_the_row_and_the_brief_name_the_open_ids_until_each_is_answered(self):
        add(self.data, request('review-1', 'delivery', ['a', 'b']), 'feedback', 'reviewer')
        self.revise('feedback')
        named = 'Open: a, b of request feedback. Answer each with %s.' % CLIENT_WAY
        item = self.item()
        self.assertEqual(item['open_review_items'], [dict(request='feedback', item='a'), dict(request='feedback', item='b')])
        self.assertTrue(item['next'].endswith(' ' + named), item['next'])
        self.assertTrue(briefing.brief(self.data, PROJECT, TASK)['next_action'].endswith(' ' + named))
        add(self.data, respond('answer-1', 'delivery-2', [('feedback', 'a'), ('feedback', 'b')], 'delivery-2'),
            'answer', ALICE)
        item = self.item()
        self.assertEqual(item['open_review_items'], [])
        self.assertNotIn('Open:', item['next'])
        self.assertNotIn('Open:', briefing.brief(self.data, PROJECT, TASK)['next_action'])

    def test_more_than_five_open_items_are_counted_not_listed(self):
        add(self.data, request('review-1', 'delivery', ['i1', 'i2', 'i3', 'i4', 'i5', 'i6', 'i7']), 'feedback', 'reviewer')
        self.revise('feedback')
        self.assertIn('Open: i1, i2, i3, i4, i5 of request feedback and 2 more. Answer each with', self.item()['next'])

    def test_the_http_wording_names_the_route_never_the_client_command(self):
        items = [dict(request='feedback', item='a')]
        route = '/v1/projects/p/tasks/%s' % TASK
        self.assertEqual(review_state.open_items_sentence(items, TASK, route),
                         'Open: a of request feedback. Answer each with a respond record (POST `%s/reviews`, '
                         'operation respond).' % route)
        self.assertNotIn('review %s --file' % TASK, ' '.join(review_state.open_items_warnings(items, TASK, route)))


class EndpointOpenItemsTests(fixes.EndpointCase):
    """Over HTTP on the canonical binding (the strict stub runs the real review write)."""

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.project = self.create_project(admin, 'Alpha')
        self.people = {}
        for name, role in (('alex', 'contributor'), ('blair', 'owner')):
            user_id = self.create_account(admin, name, name + '-password-1')
            added = self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, user_id), {'role': role},
                                 token=admin)
            self.assertIn(added.status, (200, 201), added.data)
            self.people[name] = self.login(name, name + '-password-1')[0]
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/alex/kestrel',
                                                   'projects': [self.project], 'claim_limit': claim_limit.LIMIT_MAX},
                            token=self.people['alex'])
        self.assertEqual(201, made.status, made.data)
        self.secret = made.data['credential']['secret']
        self.task = self.create_task(self.people['blair'], self.project, 'task').data['id']
        self.base = '/v1/projects/%s/tasks/%s' % (self.project, self.task)
        self.assertEqual(200, self.request('POST', self.base + '/claim', {}, token=self.secret).status)

    def review(self, token, operation, key=None, **fields):
        body = dict({'operation': operation, 'schema_version': 1, 'operation_id': 'op-' + secrets.token_hex(6)}, **fields)
        answer = self.request('POST', self.base + '/reviews', body, token=token, key=key)
        self.assertEqual(201, answer.status, answer.data)
        return answer

    def test_a_contribute_over_http_with_open_items_answers_the_warning_and_ids(self):
        old = self.review(self.secret, 'contribute', previous=None, **fixes.CONTRIBUTION).data['comment_id']
        asked = self.review(self.people['blair'], 'request-changes', previous=old, contribution=old,
                            items=[{'id': 'fix', 'text': 'Fix it.'}, {'id': 'doc', 'text': 'Document it.'}])
        request_id = asked.data['comment_id']
        body = dict(fixes.CONTRIBUTION, commit='d' * 40, supersedes=old, previous=request_id,
                    operation_id='op-revision-2')
        new = self.review(self.secret, 'contribute', key='open-items-key-1', **body)
        self.assertEqual([(i['request'], i['item']) for i in new.data['open_items']],
                         [(request_id, 'fix'), (request_id, 'doc')])
        route = self.base
        self.assertEqual(new.data['warnings'],
                         ['2 items of request %s are still open (fix, doc): a new revision does not answer them; '
                          'answer each with a respond record (POST `%s/reviews`, operation respond).'
                          % (request_id, route)])
        self.assertNotIn('--file respond.json', json.dumps(new.data))
        # An exact retry answers the stored answer.
        again = self.request('POST', self.base + '/reviews', dict(body, operation='contribute', schema_version=1),
                             token=self.secret, key='open-items-key-1')
        self.assertEqual((again.data['open_items'], again.data['warnings']), (new.data['open_items'], new.data['warnings']))
        # The agent's next action names the ids and the route, never the client command.
        data = self.request('GET', '/v1/agents/me/next', token=self.secret).data
        action = next(x for x in data['next_actions'] if x['task'] == self.task)
        self.assertIn('Open: fix, doc of request %s. Answer each with a respond record (POST `%s/reviews`'
                      % (request_id, route), action['next'])
        self.assertNotIn('--file respond.json', action['next'])


class InProcessOpenItemsTests(TeamHarness):
    """Over HTTP on the in-process backend: the same answer, in the web's words."""

    def test_a_revision_with_open_items_warns_in_the_webs_words(self):
        self.claim(self.carl)
        first = self.contribute(self.carl)
        self.assertEqual(201, first.status, first.data)
        latest = self.request('GET', self.path('/tasks/%s/brief' % self.task), token=self.carl).data['review']['latest_id']
        asked = self.review(self.olive, 'request-changes', contribution=latest, previous=latest,
                            items=[{'id': 'item-1', 'text': 'Handle an empty page'}])
        self.assertEqual(201, asked.status, asked.data)
        revised = self.contribute(self.carl, commit='d' * 40)
        self.assertEqual(201, revised.status, revised.data)
        self.assertEqual([i['item'] for i in revised.data['open_items']], ['item-1'])
        self.assertEqual(len(revised.data['warnings']), 1)
        self.assertIn('POST `/v1/projects/%s/tasks/%s/reviews`' % (self.project, self.task), revised.data['warnings'][0])
        self.assertNotIn('--file respond.json', revised.data['warnings'][0])


if __name__ == '__main__':
    unittest.main()
