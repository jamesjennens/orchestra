"""A reviewer's recommendation over HTTP, on both backends (kittrial-5bb.115)."""
import json
import secrets
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import review_recommendations as rec
import test_http_agents
import test_http_review_fixes as fixes

SUMMARY = 'Read the diff and ran the tests; did not check the docs.'


class Shared:
    """The same scenario on either backend; a subclass supplies the fixtures."""

    def base(self, task):
        return '/v1/projects/%s/tasks/%s' % (self.project, task)

    def recommend(self, token, task, contribution, commit, **changes):
        body = {'operation': 'recommend', 'schema_version': 1, 'operation_id': 'rec-' + secrets.token_hex(6),
                'contribution': contribution, 'commit': commit, 'verdict': 'approve', 'summary': SUMMARY, 'items': []}
        body.update(changes)
        return self.request('POST', self.base(task) + '/reviews', body, token=token)

    def review(self, token, task):
        return self.request('GET', self.base(task) + '/brief', token=token).data['review']

    def queue(self, token):
        return self.request('GET', '/v1/projects/%s/queue' % self.project, token=token).data['items']

    def test_a_recommendation_must_come_from_another_person(self):
        """The delivering agent's owner, and that person's other agents, are not independent."""
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        made = self.request('POST', '/v1/agents', {'name': 'Merlin', 'working_directory': '/home/carl/m',
                                                   'projects': [self.project]}, token=self.contributor)
        self.assertEqual(201, made.status, made.data)
        sibling = made.data['credential']['secret']
        for label, token in (('the agent\'s owner', self.contributor), ('another agent of the same person', sibling),
                             ('the delivering agent', self.agent)):
            with self.subTest(refused=label):
                answer = self.recommend(token, task, contribution, commit)
                self.assertEqual(403, answer.status, answer.data)
                self.assertIn('must be independent of the author', answer.data['error']['message'])
        review = self.review(self.owner, task)
        self.assertEqual((review['recommendation'], review['recommendations']), (None, []))
        # Another person, and another person's agent, may.
        self.assertEqual(201, self.recommend(self.reviewer, task, contribution, commit).status)
        self.assertEqual(201, self.recommend(self.reviewer_agent, task, contribution, commit).status)
        self.assertEqual(len(self.review(self.owner, task)['recommendations']), 2)

    def check_the_scenario(self, task, other, contribution, commit, approve):
        """`task` has a contribution by the agent; `other` has one too, with no recommendation."""
        before = self.review(self.owner, task)
        self.assertEqual((before['state'], before['recommendation'], before['recommendations']),
                         ('awaiting-review', None, []))
        # Nobody recommends their own contribution: neither the agent nor (by the name rule) anyone else under its name.
        self.assertIn(self.recommend(self.agent, task, contribution, commit).status, (403, 422))
        # A viewer has no reviews capability.
        self.assertEqual(403, self.recommend(self.viewer, task, contribution, commit).status)
        # A reviewing agent of another member can recommend; it can never approve.
        made = self.recommend(self.reviewer_agent, task, contribution, commit,
                              items=[{'id': 'naming', 'text': 'Consider a clearer name.'}])
        self.assertEqual(201, made.status, made.data)
        self.assertEqual(403, approve(self.reviewer_agent, task, contribution).status)
        after = self.review(self.owner, task)
        self.assertEqual(after['state'], 'awaiting-review')                 # advice, not a decision
        self.assertEqual(after['latest_id'], before['latest_id'])           # the chain did not move
        recommendation = after['recommendation']
        self.assertEqual((recommendation['verdict'], recommendation['summary'], recommendation['contribution'],
                          recommendation['commit'], recommendation['items']),
                         ('approve', SUMMARY, contribution, commit, [{'id': 'naming', 'text': 'Consider a clearer name.'}]))
        self.assertEqual(recommendation['author'], self.reviewer_actor)
        self.assertEqual([entry['author'] for entry in after['recommendations']], [self.reviewer_actor])
        # Refusals: another contribution, another commit, another verdict, a long summary, hidden text.
        for label, changes, statuses in (
                ('another contribution', {'contribution': 'nope-1'}, (409, 422)),
                ('another commit', {'commit': 'e' * 40}, (409, 422)),
                ('another verdict', {'verdict': 'reject'}, (422,)),
                ('a long summary', {'summary': 'x' * 1201}, (422,)),
                ('hidden text', {'summary': 'fine‮'}, (422,))):
            with self.subTest(refused=label):
                sent = dict({'contribution': contribution, 'commit': commit}, **changes)
                self.assertIn(self.recommend(self.reviewer, task, sent.pop('contribution'), sent.pop('commit'),
                                             **sent).status, statuses)
        self.assertEqual(len(self.review(self.owner, task)['recommendations']), 1)
        # The queue marks it and puts it first among the contributions awaiting review.
        rows = [row for row in self.queue(self.owner) if row['review_state'] == 'awaiting-review']
        self.assertEqual([(row['id'], row['recommended'], row['recommended_by']) for row in rows],
                         [(task, True, [self.reviewer_actor]), (other, False, [])])
        work = self.request('GET', '/v1/me/work', token=self.owner).data
        self.assertEqual([row['id'] for row in work['to_review']][:2], [task, other])
        self.assertTrue(work['to_review'][0]['recommended'])
        # The owner approves with the contribution as `previous`: not stale, and the recommendation lapses.
        approved = approve(self.owner, task, contribution)
        self.assertEqual(201, approved.status, approved.data)
        final = self.review(self.owner, task)
        self.assertEqual((final['recommendation'], final['recommendations']), (None, []))
        self.assertIn(self.recommend(self.reviewer, task, contribution, commit).status, (409, 422))
        self.assertFalse(any(row.get('recommended') for row in self.queue(self.owner)))


class InProcessTests(Shared, test_http_agents.AgentHarness):
    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.project = self.create_project(admin, 'Alpha')
        tokens = {}
        for name, role in (('olive', 'owner'), ('carl', 'contributor'), ('rita', 'contributor'), ('vera', 'viewer')):
            user_id = self.create_account(admin, name, name + '-password-1')
            self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, user_id), {'role': role}, token=admin)
            tokens[name] = self.login(name, name + '-password-1')[0]
        self.owner, self.reviewer, self.viewer = tokens['olive'], tokens['rita'], tokens['vera']
        self.contributor = tokens['carl']
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/carl/k',
                                                   'projects': [self.project]}, token=tokens['carl'])
        self.agent = made.data['credential']['secret']
        made = self.request('POST', '/v1/agents', {'name': 'Osprey', 'working_directory': '/home/rita/o',
                                                   'projects': [self.project]}, token=tokens['rita'])
        self.reviewer_agent, self.reviewer_actor = made.data['credential']['secret'], made.data['agent']['id']

    def deliver(self, title, commit):
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': title}, token=self.owner).data['id']
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', token=self.agent).status)
        made = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'contribute', 'commit': commit, 'base_commit': test_http_agents.BASE,
            'bundle_sha256': test_http_agents.BUNDLE, 'summary': 'delivered'}, token=self.agent)
        self.assertEqual(201, made.status, made.data)
        return task, made.data['contribution']['id']

    def test_recommend_then_approve(self):
        # The task recommended is the one the owner's list shows LAST before any
        # recommendation, so that coming first afterwards is the rule's doing.
        made = {}
        for title, commit in (('first', test_http_agents.COMMIT), ('second', 'f' * 40)):
            task_id, contribution_id = self.deliver(title, commit)
            made[task_id] = (contribution_id, commit)
        before = [row['id'] for row in self.request('GET', '/v1/me/work', token=self.owner).data['to_review']]
        self.assertEqual(sorted(before), sorted(made))
        other, task = before
        contribution, commit = made[task]

        def approve(token, task_id, contribution_id):
            return self.request('POST', self.base(task_id) + '/reviews',
                                {'operation': 'approve', 'contribution': contribution_id, 'summary': 'accepted'},
                                token=token)
        self.check_the_scenario(task, other, contribution, commit, approve)

    def test_a_new_revision_and_a_later_request_clear_it(self):
        task, contribution = self.deliver('first', test_http_agents.COMMIT)
        self.assertEqual(201, self.recommend(self.reviewer_agent, task, contribution, test_http_agents.COMMIT).status)
        self.assertTrue(self.review(self.owner, task)['recommendation'])
        asked = self.request('POST', self.base(task) + '/reviews',
                             {'operation': 'request-changes', 'contribution': contribution, 'items': ['Fix it']},
                             token=self.owner)
        self.assertEqual(201, asked.status, asked.data)
        self.assertIsNone(self.review(self.owner, task)['recommendation'])
        revised = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'contribute', 'commit': 'd' * 40, 'base_commit': test_http_agents.BASE,
            'bundle_sha256': test_http_agents.BUNDLE, 'summary': 'revised'}, token=self.agent)
        self.assertEqual(201, revised.status, revised.data)
        self.assertIsNone(self.review(self.owner, task)['recommendation'])

    def test_a_stored_one_by_the_authors_own_person_is_not_shown(self):
        """The read applies the same rule: such a record can reach the task over SSH, where only names are compared."""
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        self.assertEqual(201, self.recommend(self.reviewer_agent, task, contribution, commit).status)
        carl = next(uid for uid, user in self.service.state['users'].items() if user.get('username') == 'carl')
        stored = self.backend.state['recommendations'][task]
        stored.append(dict(stored[0], id='rec_owner', actor=carl, created_at='2099-01-01T00:00:00Z', summary='my own agent did well'))
        review = self.review(self.owner, task)
        # The newest is the owner's: it is dropped, and the other reviewer is still named.
        self.assertIsNone(review['recommendation'])
        self.assertEqual([entry['author'] for entry in review['recommendations']], [self.reviewer_actor])
        row = next(row for row in self.queue(self.owner) if row['id'] == task)
        self.assertEqual((row['recommended'], row['recommended_by']), (True, [self.reviewer_actor]))
        del stored[0]
        row = next(row for row in self.queue(self.owner) if row['id'] == task)
        self.assertEqual((row['recommended'], row['recommended_by']), (False, []))
        self.assertEqual(self.review(self.owner, task)['recommendations'], [])

    def test_one_written_before_a_request_does_not_come_back_when_the_request_is_resolved(self):
        task, contribution = self.deliver('first', test_http_agents.COMMIT)
        self.assertEqual(201, self.recommend(self.reviewer_agent, task, contribution, test_http_agents.COMMIT).status)
        asked = self.request('POST', self.base(task) + '/reviews',
                             {'operation': 'request-changes', 'contribution': contribution, 'items': ['Fix it']},
                             token=self.owner)
        self.assertEqual(201, asked.status, asked.data)
        review = self.review(self.owner, task)
        answered = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'respond', 'contribution': contribution, 'previous': review['latest_id'],
            'resolutions': [{'request': r['request'], 'item': r['id'], 'reason': 'No change needed',
                             'evidence': 'the same revision'} for r in review['requests']]}, token=self.agent)
        self.assertEqual(201, answered.status, answered.data)
        review = self.review(self.owner, task)
        # The same contribution awaits review again; the advice given before the request is not shown.
        self.assertEqual((review['state'], review['contribution']['id'], review['recommendation'], review['recommendations']),
                         ('awaiting-review', contribution, None, []))
        self.assertFalse(any(row['recommended'] for row in self.queue(self.owner)))
        again = self.recommend(self.reviewer_agent, task, contribution, test_http_agents.COMMIT)
        self.assertEqual(201, again.status, again.data)
        self.assertEqual(len(self.review(self.owner, task)['recommendations']), 1)


class EndpointTests(Shared, fixes.EndpointCase):
    """The same over the strict canonical stub: the real review_recommendations and work."""

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.project = self.create_project(admin, 'Alpha')
        tokens = {}
        for name, role in (('olive', 'owner'), ('carl', 'contributor'), ('rita', 'contributor'), ('vera', 'viewer')):
            user_id = self.create_account(admin, name, name + '-password-1')
            added = self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, user_id), {'role': role},
                                 token=admin)
            self.assertIn(added.status, (200, 201), added.data)
            tokens[name] = self.login(name, name + '-password-1')[0]
        self.owner, self.reviewer, self.viewer = tokens['olive'], tokens['rita'], tokens['vera']
        self.contributor = tokens['carl']
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/carl/k',
                                                   'projects': [self.project]}, token=tokens['carl'])
        self.assertEqual(201, made.status, made.data)
        self.agent = made.data['credential']['secret']
        made = self.request('POST', '/v1/agents', {'name': 'Osprey', 'working_directory': '/home/rita/o',
                                                   'projects': [self.project]}, token=tokens['rita'])
        self.reviewer_agent = made.data['credential']['secret']
        self.reviewer_actor = self.request('GET', '/v1/agents/me', token=self.reviewer_agent).data['agent']['actor']

    def deliver(self, title, commit):
        task = self.create_task(self.owner, self.project, title).data['id']
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {}, token=self.agent).status)
        body = dict(fixes.CONTRIBUTION, commit=commit, operation='contribute', schema_version=1,
                    operation_id='op-' + secrets.token_hex(6), previous=None)
        made = self.request('POST', self.base(task) + '/reviews', body, token=self.agent)
        self.assertEqual(201, made.status, made.data)
        return task, made.data.get('comment_id')

    def test_recommend_then_approve(self):
        first, second = sorted(('first', 'second'))
        task, contribution = self.deliver(first, fixes.COMMIT)
        other, _ = self.deliver(second, 'f' * 40)

        def approve(token, task_id, contribution_id):
            return self.request('POST', self.base(task_id) + '/reviews', {
                'operation': 'approve', 'schema_version': 1, 'operation_id': 'op-' + secrets.token_hex(6),
                'previous': contribution_id, 'contribution': contribution_id, 'summary': 'accepted'}, token=token)
        if other < task:                       # the queue orders equal rows by id
            task, other, contribution = other, task, self.review(self.owner, other)['contribution']['id']
            commit = 'f' * 40
        else:
            commit = fixes.COMMIT
        self.check_the_scenario(task, other, contribution, commit, approve)
        # The stored record is the canonical one, beside the chain, written by the reviewing agent.
        rows = json.loads((self.canonical_root / 'canonical.json').read_text(encoding='utf-8'))['rows']
        row = next(r for r in rows if r['id'] == task)
        stored = [c for c in row['comments'] if c['text'].startswith(rec.PREFIX)]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['author'], self.reviewer_actor)
        record = json.loads(stored[0]['text'][len(rec.PREFIX):])
        rec.validate(record, task)
        self.assertNotIn('previous', record)

    def test_an_exact_retry_replays_and_writes_once(self):
        task, contribution = self.deliver('first', fixes.COMMIT)
        body = {'operation': 'recommend', 'schema_version': 1, 'operation_id': 'rec-retry-1', 'contribution': contribution,
                'commit': fixes.COMMIT, 'verdict': 'approve', 'summary': SUMMARY, 'items': []}
        first = self.request('POST', self.base(task) + '/reviews', body, token=self.reviewer, key='rec-key-0001')
        again = self.request('POST', self.base(task) + '/reviews', body, token=self.reviewer, key='rec-key-0001')
        self.assertEqual((201, first.data.get('comment_id')), (first.status, again.data.get('comment_id')))
        rows = json.loads((self.canonical_root / 'canonical.json').read_text(encoding='utf-8'))['rows']
        row = next(r for r in rows if r['id'] == task)
        self.assertEqual(len([c for c in row['comments'] if c['text'].startswith(rec.PREFIX)]), 1)


class WebPageTests(unittest.TestCase):
    """The page's request is the route's contract; the page itself is not run here."""

    def test_the_form_sends_the_operation_the_route_accepts(self):
        source = (KIT / 'web' / 'js' / 'views' / 'task.js').read_text(encoding='utf-8')
        start = source.index('function recommendForm(')
        form = source[start:source.index('\n}\n', start)]
        self.assertIn("operation: 'recommend', contribution: contribution.id, commit: contribution.commit, "
                      "verdict: 'approve', summary, items", form)
        self.assertNotIn('previous', form)                       # beside the chain: no `previous`
        self.assertIn('error.status !== 409', form)              # a newer revision is explained, not swallowed
        # An owner decides directly; the assignee and the author never see the form.
        self.assertIn("if (!owner && writer && !mine && c && c.author !== ctx.me.id && review.state === "
                      "'awaiting-review') reviewBody.append(recommendForm(", source)
        # The note is shown only while the contribution awaits review, and says who decides.
        self.assertIn("const advice = review.state === 'awaiting-review' ? review.recommendation : null;", source)
        self.assertIn('An owner still decides.', source)

    def test_the_lists_mark_a_recommended_row(self):
        for name in ('project.js', 'work.js'):
            source = (KIT / 'web' / 'js' / 'views' / name).read_text(encoding='utf-8')
            self.assertIn('t.recommended ?', source, name)
            self.assertIn("'Recommended')", source, name)

    def test_the_prototype_mock_answers_the_same_operation(self):
        source = (KIT / 'web' / 'js' / 'mock.js').read_text(encoding='utf-8')
        self.assertIn("if (b.operation === 'recommend') {", source)
        self.assertIn('Nobody recommends their own contribution', source)


if __name__ == '__main__':
    unittest.main()
