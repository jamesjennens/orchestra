"""A reviewer's recommendation over HTTP, on both backends (kittrial-5bb.115)."""
import json
import secrets
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
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
        # An attribution label under the agent's own name is a different name; the person signed in is the same.
        labelled = self.recommend(sibling, task, contribution, commit, actor=made.data['agent']['id'] + '/reviewer')
        self.assertEqual(403, labelled.status, labelled.data)
        self.assertIn('must be independent of the author', labelled.data['error']['message'])
        review = self.review(self.owner, task)
        self.assertEqual((review['recommendation'], review['recommendations']), (None, []))
        # Another person may; and then that person's agent may not add a second one (kittrial-5bb.154).
        self.assertEqual(201, self.recommend(self.reviewer, task, contribution, commit).status)
        self.assertEqual(409, self.recommend(self.reviewer_agent, task, contribution, commit).status)
        self.assertEqual(len(self.review(self.owner, task)['recommendations']), 1)

    def test_a_refusal_says_which_rule_was_hit_and_unknown_fields_are_refused(self):
        """Review of the first delivery: an opaque 422, and fields dropped silently."""
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        for label, changes, words in (
                ('another commit', {'commit': 'e' * 40}, 'not the current contribution'),
                ('another contribution', {'contribution': 'nope-1'}, 'not the task'),
                ('another verdict', {'verdict': 'reject'}, 'verdict'),
                ('a long summary', {'summary': 'x' * 1201}, '1200')):
            with self.subTest(refused=label):
                sent = dict({'contribution': contribution, 'commit': commit}, **changes)
                answer = self.recommend(self.reviewer, task, sent.pop('contribution'), sent.pop('commit'), **sent)
                self.assertIn(answer.status, (409, 422), answer.data)
                message = answer.data['error']['message']
                self.assertIn(words, message)
                self.assertNotEqual(message, 'Canonical command rejected the request')
                self.assertNotIn('ValueError', message)
        # One refusal for every review operation (kittrial-5bb.110): a field the operation does
        # not take is named, at most five plain names, and never echoed when it is not one.
        for label, extra, shown in (('approved', {'approved': True}, 'approved'), ('a made-up field', {'surprise': 1}, 'surprise'),
                                    ('a name that is not an identifier', {'x\x1b[31m <b>': 1}, '<non-identifier name>'),
                                    ('many', {'f%d' % n: 1 for n in range(9)}, 'f0, f1, f2, f3, f4, (+4 more)')):
            with self.subTest(unknown=label):
                answer = self.recommend(self.reviewer, task, contribution, commit, **extra)
                self.assertEqual(422, answer.status, answer.data)
                self.assertEqual(answer.data['error']['message'],
                                 'Unsupported review payload field(s) for recommend: %s' % shown)
                self.assertNotIn('\x1b', json.dumps(answer.data, ensure_ascii=False))
        self.assertEqual(self.review(self.owner, task)['recommendations'], [])
        # The released client sends every optional field of every operation as null, and
        # `previous` for every operation: both are accepted and ignored for a recommendation.
        union = {name: None for name in ('previous', 'supersedes', 'follows', 'repository', 'base_commit', 'delivery',
                                         'request', 'resolutions', 'reviewer', 'reason', 'item', 'disposition')}
        self.assertEqual(201, self.recommend(self.reviewer, task, contribution, commit, **union).status)
        other, second = self.deliver('second', 'f' * 40)
        self.assertEqual(201, self.recommend(self.reviewer_agent, other, second, 'f' * 40, previous=second).status)
        self.assertEqual([len(self.review(self.owner, each)['recommendations']) for each in (task, other)], [1, 1])

    def test_the_queue_applies_the_same_person_rule_as_the_brief(self):
        """A recommendation by another agent of the AUTHOR is left out of the queue too, when author and assignee differ."""
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        self.assertEqual(201, self.recommend(self.reviewer_agent, task, contribution, commit).status)
        sibling = self.request('POST', '/v1/agents', {'name': 'Merlin', 'working_directory': '/home/carl/m',
                                                      'projects': [self.project]}, token=self.contributor)
        self.assertEqual(201, sibling.data and sibling.status, sibling.data)
        self.plant_recommendation(task, contribution, commit, sibling.data['agent']['id'])
        # Reassign the task away from the author: the assignee is now someone unrelated.
        self.reassign(task, self.reviewer_actor_for_reassignment())
        row = next(row for row in self.queue(self.owner) if row['id'] == task)
        self.assertNotIn(sibling.data['agent']['id'], row['recommended_by'])
        review = self.review(self.owner, task)
        self.assertNotIn(sibling.data['agent']['id'], [entry['author'] for entry in review['recommendations']])
        self.assertEqual(row['recommended_by'], [entry['author'] for entry in review['recommendations']])

    def test_a_reassignment_to_the_recommender_does_not_hide_the_recommendation(self):
        """The assignee rule is for the write; no reader, over SSH or HTTP, re-checks it."""
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        self.assertEqual(201, self.recommend(self.reviewer, task, contribution, commit).status)
        vera = self.review(self.owner, task)['recommendation']['author']      # the person who recommended
        self.reassign(task, vera)
        review = self.review(self.owner, task)
        self.assertEqual([entry['author'] for entry in review['recommendations']], [vera])
        self.assertEqual(review['recommendation']['author'], vera)
        row = next(row for row in self.queue(self.owner) if row['id'] == task)
        self.assertEqual((row['recommended'], row['recommended_by']), (True, [vera]))
        # Written now, by the assignee's own person, it is refused as before.
        again = self.recommend(self.reviewer, task, contribution, commit)
        self.assertEqual(403, again.status, again.data)

    def test_a_row_that_does_not_say_who_delivered_is_compared_with_the_assignee(self):
        """An endpoint older than this delivery sends no contribution_author (review 01a10c80)."""
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        sibling = self.request('POST', '/v1/agents', {'name': 'Merlin', 'working_directory': '/home/carl/m',
                                                      'projects': [self.project]}, token=self.contributor)
        self.plant_recommendation(task, contribution, commit, sibling.data['agent']['id'])
        read = {'items': [{'id': task, 'assignee': self.review(self.owner, task)['contribution']['author'],
                           'recommended_by': [sibling.data['agent']['id']], 'recommended': True,
                           'contribution_author': None, 'review_state': 'awaiting-review'}]}
        kept = self.handler_class()._independent_queue(read)['items'][0]
        self.assertEqual((kept['recommended_by'], kept['recommended']), ([], False))
        # With the author given, the author decides and the assignee is not compared.
        vera = self.reviewer_actor_for_reassignment()
        named = {'items': [dict(read['items'][0], assignee=vera, recommended_by=[vera], recommended=True,
                                contribution_author=read['items'][0]['assignee'])]}
        self.assertEqual(self.handler_class()._independent_queue(named)['items'][0]['recommended_by'], [vera])
        self.assertEqual(http_service.ApiHandler._read_parties('a', None), ['a'])
        self.assertEqual(http_service.ApiHandler._read_parties('a', ''), ['a'])
        self.assertEqual(http_service.ApiHandler._read_parties('a', 'b'), ['b'])

    ALREADY = (' has already recommended this contribution, and that recommendation stands until the contribution is '
               'revised or decided. To ask for changes instead, request changes: the recommendation then stops counting. '
               'A recommendation cannot be withdrawn.')

    def second_agent(self):
        made = self.request('POST', '/v1/agents', {'name': 'Plover', 'working_directory': '/home/rita/p',
                                                   'projects': [self.project]}, token=self.reviewer)
        self.assertEqual(201, made.status, made.data)
        self.plover_id = made.data['agent']['id']
        return made.data['credential']['secret']

    def test_one_standing_recommendation_from_each_person(self):
        """kittrial-5bb.154: a second one by the same agent, by its owner or by its owner's other agent is refused."""
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        plover = self.second_agent()
        self.assertEqual(201, self.recommend(self.reviewer_agent, task, contribution, commit).status)
        stored = self.review(self.owner, task)['recommendations']
        self.assertEqual([entry['author'] for entry in stored], [self.reviewer_actor])
        for label, token in (('the same agent', self.reviewer_agent), ('its owner', self.reviewer),
                             ('another agent of its owner', plover)):
            with self.subTest(second=label):
                answer = self.recommend(token, task, contribution, commit, summary='A second reading.')
                self.assertEqual(409, answer.status, answer.data)
                message = answer.data['error']['message']
                # It names the one of the caller's own party who recommended, and the two ways on.
                self.assertTrue(message.endswith(self.ALREADY), message)
                self.assertIn('Osprey', message[:-len(self.ALREADY)])
                self.assertEqual(answer.data['error']['detail'], {'recommended_by': self.reviewer_actor})
        # Under an attribution label the name is another one; the person signed in is the same.
        labelled = self.recommend(plover, task, contribution, commit, actor=self.plover_id + '/second-reading')
        self.assertEqual(409, labelled.status, labelled.data)
        self.assertTrue(labelled.data['error']['message'].endswith(self.ALREADY))
        self.assertEqual(self.review(self.owner, task)['recommendations'], stored)           # nothing was stored
        # The owner of the project is another person: hers is a second voice, and is stored.
        self.assertEqual(201, self.recommend(self.owner, task, contribution, commit).status)
        self.assertEqual(len(self.review(self.owner, task)['recommendations']), 2)

    def test_a_reviewer_who_is_not_the_owner_changes_their_mind_by_requesting_changes(self):
        """From recommending to asking for changes: the earlier recommendation stops counting, for every reader."""
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        self.assertEqual(201, self.recommend(self.reviewer_agent, task, contribution, commit).status)
        row = next(row for row in self.queue(self.owner) if row['id'] == task)
        self.assertEqual((row['recommended'], row['recommended_by']), (True, [self.reviewer_actor]))
        asked = self.ask_changes(self.reviewer_agent, task, contribution)
        self.assertEqual(201, asked.status, asked.data)
        review = self.review(self.owner, task)
        self.assertEqual((review['state'], review['recommendation'], review['recommendations']),
                         ('changes-requested', None, []))
        row = next(row for row in self.queue(self.owner) if row['id'] == task)
        self.assertEqual((row['review_state'], row['recommended'], row['recommended_by']), ('changes-requested', False, []))
        work = self.request('GET', '/v1/me/work', token=self.owner).data
        self.assertFalse(any(item.get('recommended') for group in work.values() if isinstance(group, list)
                             for item in group if isinstance(item, dict)))
        # While changes are requested nobody recommends; the sentence is the one for the state.
        self.assertIn(self.recommend(self.reviewer_agent, task, contribution, commit).status, (409, 422))

    def test_a_revised_contribution_may_be_recommended_again_by_the_same_person(self):
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        self.assertEqual(201, self.recommend(self.reviewer_agent, task, contribution, commit).status)
        revised = self.revise(task, contribution, 'd' * 40)
        self.assertEqual(self.review(self.owner, task)['recommendations'], [])
        again = self.recommend(self.reviewer, task, revised, 'd' * 40)                    # the agent's owner this time
        self.assertEqual(201, again.status, again.data)
        review = self.review(self.owner, task)
        self.assertEqual((len(review['recommendations']), review['recommendation']['contribution']), (1, revised))
        refused = self.recommend(self.reviewer_agent, task, revised, 'd' * 40)
        self.assertEqual(409, refused.status, refused.data)
        self.assertTrue(refused.data['error']['message'].endswith(self.ALREADY))

    def handler_class(self):
        """A handler bound to this test's service and backend, for calling its helpers directly."""
        return self.httpd.RequestHandlerClass.__new__(self.httpd.RequestHandlerClass)

    def check_the_scenario(self, task, other, contribution, commit, approve):
        """`task` has a contribution by the agent; `other` has one too, with no recommendation."""
        before = self.review(self.owner, task)
        self.assertEqual((before['state'], before['recommendation'], before['recommendations']),
                         ('awaiting-review', None, []))
        # Nobody recommends their own contribution: neither the agent nor (by the name rule) anyone else under its name.
        self.assertIn(self.recommend(self.agent, task, contribution, commit).status, (403, 422))
        # A viewer has no reviews capability.
        self.assertEqual(403, self.recommend(self.viewer, task, contribution, commit).status)
        # Refused before anybody has recommended: another contribution, another commit, another verdict, a long summary, hidden text.
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

    def plant_recommendation(self, task, contribution, commit, actor):
        stored = self.backend.state.setdefault('recommendations', {}).setdefault(task, [])
        records = self.backend.state['contributions'][task]
        stored.append({'id': 'rec_planted', 'task_id': task, 'kind': 'recommendation', 'contribution_id': contribution,
                       'commit': commit, 'verdict': 'approve', 'summary': 'planted', 'items': [], 'actor': actor,
                       'created_at': '2099-01-01T00:00:00Z', 'after': len(records)})

    def reassign(self, task, actor):
        self.backend._task(self.project, task)['assignee'] = actor

    def reviewer_actor_for_reassignment(self):
        return next(uid for uid, user in self.service.state['users'].items() if user.get('username') == 'vera')

    def ask_changes(self, token, task, contribution):
        return self.request('POST', self.base(task) + '/reviews',
                            {'operation': 'request-changes', 'contribution': contribution, 'items': ['Fix it']}, token=token)

    def revise(self, task, contribution, commit):
        made = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'contribute', 'commit': commit, 'base_commit': test_http_agents.BASE,
            'bundle_sha256': test_http_agents.BUNDLE, 'summary': 'revised'}, token=self.agent)
        self.assertEqual(201, made.status, made.data)
        return made.data['contribution']['id']

    def test_the_backend_refuses_the_same_actors_second_one_itself(self):
        """Behind the route's rule by person, the in-process write refuses the same actor by name, as canonically."""
        from http_service import HttpError
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        self.assertEqual(201, self.recommend(self.reviewer_agent, task, contribution, commit).status)
        stored = self.backend.get_task(self.project, task)
        records = self.backend.state['contributions'][task]
        payload = {'contribution': contribution, 'commit': commit, 'verdict': 'approve', 'summary': SUMMARY, 'items': []}
        with self.assertRaises(HttpError) as refused:
            self.backend._recommend(None, self.project, stored, records, dict(payload, actor=self.reviewer_actor))
        self.assertEqual((refused.exception.status, refused.exception.message),
                         (409, self.reviewer_actor + self.ALREADY))
        self.assertEqual(len(self.backend.state['recommendations'][task]), 1)

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

    def test_the_backend_applies_the_name_rule_itself(self):
        """Behind the route's rule by person, the in-process write still refuses the author by name."""
        from http_service import HttpError
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        author = self.review(self.owner, task)['contribution']['author']
        stored = self.backend.get_task(self.project, task)
        records = self.backend.state['contributions'][task]
        payload = {'contribution': contribution, 'commit': commit, 'verdict': 'approve', 'summary': SUMMARY, 'items': []}
        with self.assertRaises(HttpError) as refused:
            self.backend._recommend(None, self.project, stored, records, dict(payload, actor=author))
        self.assertEqual(refused.exception.status, 403)
        self.assertNotIn(task, self.backend.state.get('recommendations', {}))

    def test_a_stored_one_by_the_authors_own_person_is_not_shown(self):
        """The read applies the same rule: such a record can reach the task over SSH, where only names are compared."""
        commit = test_http_agents.COMMIT
        task, contribution = self.deliver('first', commit)
        self.assertEqual(201, self.recommend(self.reviewer_agent, task, contribution, commit).status)
        carl = next(uid for uid, user in self.service.state['users'].items() if user.get('username') == 'carl')
        stored = self.backend.state['recommendations'][task]
        stored.append(dict(stored[0], id='rec_owner', actor=carl, created_at='2099-01-01T00:00:00Z', summary='my own agent did well'))
        review = self.review(self.owner, task)
        # The newest is the owner's: it is dropped, and the newest INDEPENDENT one is shown in its place.
        self.assertEqual((review['recommendation']['author'], review['recommendation']['summary']),
                         (self.reviewer_actor, SUMMARY))
        self.assertNotIn('my own agent did well', json.dumps(review))
        self.assertEqual([entry['author'] for entry in review['recommendations']], [self.reviewer_actor])
        self.assertEqual(sorted(review['recommendations'][0]), ['at', 'author', 'author_name', 'id'])
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

    def _canonical(self, change):
        path = self.canonical_root / 'canonical.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        change(state)
        path.write_text(json.dumps(state), encoding='utf-8')

    def plant_recommendation(self, task, contribution, commit, actor):
        """A record written natively, as over SSH: only the name rule stood in its way."""
        from requirements import canonical_bytes
        payload = {'schema_version': 1, 'operation': 'recommend', 'operation_id': 'planted-1', 'task': task,
                   'contribution': contribution, 'commit': commit, 'verdict': 'approve', 'summary': 'planted',
                   'items': []}
        rec.validate(payload, task)

        def change(state):
            row = next(r for r in state['rows'] if r['id'] == task)
            state['comments'] = state.get('comments', 0) + 1
            row['comments'].append({'id': 'planted%d' % state['comments'], 'author': actor,
                                    'created_at': '2099-01-01T00:00:00Z',
                                    'text': rec.PREFIX + canonical_bytes(payload).decode()})
        self._canonical(change)

    def reassign(self, task, actor):
        def change(state):
            next(r for r in state['rows'] if r['id'] == task)['assignee'] = actor
        self._canonical(change)

    def reviewer_actor_for_reassignment(self):
        return next(uid for uid, user in self.service.state['users'].items() if user.get('username') == 'vera')

    def ask_changes(self, token, task, contribution):
        return self.request('POST', self.base(task) + '/reviews', {
            'operation': 'request-changes', 'schema_version': 1, 'operation_id': 'op-' + secrets.token_hex(6),
            'previous': self.review(self.owner, task)['latest_id'], 'contribution': contribution,
            'items': [{'id': 'fix', 'text': 'Fix it'}]}, token=token)

    def revise(self, task, contribution, commit):
        body = dict(fixes.CONTRIBUTION, commit=commit, operation='contribute', schema_version=1,
                    operation_id='op-' + secrets.token_hex(6), previous=self.review(self.owner, task)['latest_id'],
                    supersedes=contribution)
        made = self.request('POST', self.base(task) + '/reviews', body, token=self.agent)
        self.assertEqual(201, made.status, made.data)
        return made.data.get('comment_id')

    def test_the_canonical_write_refuses_the_same_actors_second_one_itself(self):
        """A recommendation written without the web service (over SSH) meets the same rule, by actor name."""
        commit = fixes.COMMIT
        task, contribution = self.deliver('first', commit)
        self.assertEqual(201, self.recommend(self.reviewer_agent, task, contribution, commit).status)
        rows = json.loads((self.canonical_root / 'canonical.json').read_text(encoding='utf-8'))['rows']
        payload = {'schema_version': 1, 'operation': 'recommend', 'operation_id': 'direct-1', 'task': task,
                   'contribution': contribution, 'commit': commit, 'verdict': 'approve', 'summary': 'again', 'items': []}
        with self.assertRaises(ValueError) as refused:
            rec.execute(rows, task, self.reviewer_actor, payload, lambda args: self.fail('nothing may be written'))
        self.assertEqual(str(refused.exception), self.reviewer_actor + self.ALREADY)

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
