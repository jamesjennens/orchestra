"""Review work in an agent's attention: ``review-recommended`` and ``to-review`` (kittrial-5bb.115).

An agent that may review is shown the contributions it could itself recommend; an agent
whose owner can approve is told when one is recommended and ready for that owner. Both
kinds come from the review-queue snapshot the attention read already has.
"""
import secrets
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_http_agents
import test_http_review_fixes as fixes

SUMMARY = 'Read the diff and ran the tests; did not check the docs.'


def kinds(data):
    return [(action['kind'], action['task']) for action in data['next_actions']]


class Shared:
    """One scenario on either backend. Olive owns the project; Carl and Rita contribute."""

    def people(self):
        admin = self.admin_token()
        self.project = self.create_project(admin, 'Alpha')
        self.tokens, self.ids, self.agents, self.agent_ids = {}, {}, {}, {}
        for name, role in (('olive', 'owner'), ('carl', 'contributor'), ('rita', 'contributor')):
            self.ids[name] = self.create_account(admin, name, name + '-password-1')
            added = self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, self.ids[name]), {'role': role},
                                 token=admin)
            self.assertIn(added.status, (200, 201), added.data)
            self.tokens[name] = self.login(name, name + '-password-1')[0]
        for owner, name in (('carl', 'Kestrel'), ('carl', 'Merlin'), ('rita', 'Osprey'), ('olive', 'Heron')):
            self.add_agent(owner, name)

    def add_agent(self, owner, name, **extra):
        body = dict({'name': name, 'working_directory': '/home/%s/%s' % (owner, name.lower()),
                     'projects': [self.project]}, **extra)
        made = self.request('POST', '/v1/agents', body, token=self.tokens[owner])
        self.assertEqual(201, made.status, made.data)
        self.agents[name], self.agent_ids[name] = made.data['credential']['secret'], made.data['agent']['id']

    def base(self, task):
        return '/v1/projects/%s/tasks/%s' % (self.project, task)

    def next(self, name):
        answer = self.request('GET', '/v1/agents/me/next', token=self.agents[name])
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def recommend(self, token, task, contribution, commit):
        return self.request('POST', self.base(task) + '/reviews', {
            'operation': 'recommend', 'schema_version': 1, 'operation_id': 'rec-' + secrets.token_hex(6),
            'contribution': contribution, 'commit': commit, 'verdict': 'approve', 'summary': SUMMARY, 'items': []},
            token=token)

    def scenario(self):
        """Two deliveries by Kestrel (Carl's agent) and one unclaimed task."""
        self.people()
        first, c1 = self.deliver('first', 'a' * 40)
        second, c2 = self.deliver('second', 'f' * 40)
        spare = self.new_task('spare')
        return sorted(((first, c1, 'a' * 40), (second, c2, 'f' * 40))), spare

    def test_who_is_shown_what_and_in_which_order(self):
        (one, two), spare = self.scenario()
        both = sorted([one[0], two[0]])
        # A reviewing agent of another person: both deliveries to review, before claimable work.
        osprey = self.next('Osprey')
        self.assertEqual(kinds(osprey), [('to-review', both[0]), ('to-review', both[1]), ('claimable-task', spare)])
        self.assertEqual((osprey['attention']['counts']['to_review'], osprey['attention']['counts']['review_recommended']),
                         (2, 0))
        action = osprey['next_actions'][0]
        self.assertEqual((action['priority'], action['who'], action['review_state'], action['recommended_by']),
                         (4, 'agent', 'awaiting-review', []))
        self.assertTrue(action['contribution'] and action['commit'])
        self.assertIn('record a recommendation or request changes', action['reason'])
        # The state does not change for review work; the summary says there is some.
        self.assertEqual(osprey['attention']['state'], 'idle')
        self.assertIn('2 contribution(s) to review', osprey['attention']['summary'])
        # The delivering agent, and its owner's other agent, are never offered them.
        kestrel = self.next('Kestrel')
        self.assertEqual([kind for kind, _ in kinds(kestrel)], ['claimable-task', 'awaiting-review', 'awaiting-review'])
        self.assertEqual((kestrel['attention']['counts']['to_review'], kestrel['attention']['counts']['review_recommended']),
                         (0, 0))
        self.assertEqual(kinds(self.next('Merlin')), [('claimable-task', spare)])
        self.assertNotIn('to review', self.next('Merlin')['attention']['summary'])
        # The owner's agent may review too.
        self.assertEqual([kind for kind, _ in kinds(self.next('Heron'))], ['to-review', 'to-review', 'claimable-task'])

    def test_a_recommendation_moves_it_for_the_owners_agent_and_drops_it_for_the_recommender(self):
        (one, two), spare = self.scenario()
        self.assertEqual(201, self.recommend(self.agents['Osprey'], *one).status)
        # Osprey has recommended it and its owner cannot approve: nothing more for it there.
        osprey = self.next('Osprey')
        self.assertEqual(kinds(osprey), [('to-review', two[0]), ('claimable-task', spare)])
        self.assertEqual(osprey['attention']['counts']['to_review'], 1)
        # Heron's owner can approve: recommended first, then the one still to review, then claimable.
        heron = self.next('Heron')
        self.assertEqual(kinds(heron), [('review-recommended', one[0]), ('to-review', two[0]), ('claimable-task', spare)])
        action = heron['next_actions'][0]
        self.assertEqual((action['priority'], action['who'], action['recommended_by']),
                         (4, 'owner', [self.agent_ids['Osprey']]))
        self.assertIn('Tell your owner it is ready to approve; an agent cannot approve', action['reason'])
        self.assertEqual((heron['attention']['counts']['review_recommended'], heron['attention']['counts']['to_review']),
                         (1, 1))
        self.assertEqual(heron['attention']['state'], 'idle')
        self.assertIn('1 contribution(s) recommended for approval: tell the owner; 1 contribution(s) to review.',
                      heron['attention']['summary'])
        # The owner's agent list shows the same numbers.
        listed = {a['name']: a for a in self.request('GET', '/v1/agents', token=self.tokens['olive']).data['items']}
        self.assertEqual(listed['Heron']['attention']['counts']['review_recommended'], 1)
        self.assertIn('recommended for approval', listed['Heron']['attention']['summary'])
        # Kestrel and Merlin still see none of it.
        for name in ('Kestrel', 'Merlin'):
            self.assertNotIn('review-recommended', [kind for kind, _ in kinds(self.next(name))])
            self.assertNotIn('to-review', [kind for kind, _ in kinds(self.next(name))])
        # The owner approves: it leaves every reviewer's list.
        approved = self.approve(self.tokens['olive'], one[0], one[1])
        self.assertEqual(201, approved.status, approved.data)
        self.assertEqual(kinds(self.next('Heron')), [('to-review', two[0]), ('claimable-task', spare)])
        self.assertEqual(kinds(self.next('Osprey')), [('to-review', two[0]), ('claimable-task', spare)])

    def test_the_copied_prompt_names_the_same_review_work(self):
        (one, two), spare = self.scenario()

        def prompt(name):
            data = self.request('GET', '/v1/me/work', token=self.tokens[name]).data
            return data['agent_prompts'][0]['text']
        heading = 'Contributions you could review:'
        rita = prompt('rita')
        self.assertIn(heading, rita)
        for task in (one[0], two[0]):
            self.assertIn('- task %s ' % task, rita.split(heading)[1])
        # The person who delivered them is not asked to review them.
        self.assertNotIn(heading, prompt('carl'))
        # Once Rita's agent has recommended one, Rita's prompt no longer lists it there,
        # and the owner's review line says it is recommended.
        self.assertEqual(201, self.recommend(self.agents['Osprey'], *one).status)
        after = prompt('rita').split(heading)[1]
        self.assertNotIn('- task %s ' % one[0], after)
        self.assertIn('- task %s ' % two[0], after)
        owner = prompt('olive')
        self.assertNotIn(heading, owner)
        line = next(line for line in owner.splitlines() if line.startswith('- task %s ' % one[0]))
        self.assertIn('; recommended by 1 reviewer(s)', line)

    def offered(self, name):
        return sorted(task for kind, task in kinds(self.next(name)) if kind in ('to-review', 'review-recommended'))

    def test_both_the_assignee_and_the_author_are_compared(self):
        """Each half of the independence check, alone (review 01a10c80)."""
        (one, two), spare = self.scenario()
        # Kestrel (Carl's) delivered `one`; the task is then reassigned to Osprey (Rita's).
        self.change(one[0], assignee=self.agent_ids['Osprey'])
        # The author's person is not offered it although it is no longer the assignee ...
        self.assertNotIn(one[0], self.offered('Kestrel'))
        self.assertNotIn(one[0], self.offered('Merlin'))
        # ... and the assignee's person is not offered it although someone else delivered it.
        self.assertNotIn(one[0], self.offered('Osprey'))
        # A third person's agent is.
        self.assertIn(one[0], self.offered('Heron'))
        # The other delivery is untouched: Osprey may review it, Kestrel may not.
        self.assertIn(two[0], self.offered('Osprey'))
        self.assertNotIn(two[0], self.offered('Kestrel'))

    def test_a_closed_task_is_not_offered_for_review(self):
        (one, two), spare = self.scenario()
        self.change(one[0], status='closed')
        self.assertEqual(self.offered('Osprey'), [two[0]])
        self.assertEqual(self.next('Osprey')['attention']['counts']['to_review'], 1)

    def test_a_revoked_or_expired_credential_does_not_count_for_the_reviews_capability(self):
        self.scenario()
        standing = self.service.agent_review_standing
        agent = self.agent_ids['Osprey']
        self.assertEqual(standing(agent, self.project), (True, False))
        mine = [c for c in self.service.state['credentials'].values() if c.get('agent_id') == agent]
        self.assertEqual(len(mine), 1)
        mine[0]['revoked'] = True
        self.assertEqual(standing(agent, self.project), (False, False))
        mine[0]['revoked'] = False
        mine[0]['expires_at'] = 1
        self.assertEqual(standing(agent, self.project), (False, False))

    def test_review_work_comes_after_the_agents_own_actionable_work(self):
        (one, two), spare = self.scenario()
        claimed = self.request('POST', self.base(spare) + '/claim', {}, token=self.agents['Osprey'])
        self.assertEqual(200, claimed.status, claimed.data)
        self.assertEqual(kinds(self.next('Osprey')),
                         [('in-progress', spare)] + [('to-review', task) for task in sorted([one[0], two[0]])])

    def test_an_agent_without_the_reviews_scope_is_shown_no_review_work(self):
        (one, two), spare = self.scenario()
        self.add_agent('rita', 'Wren', scopes=['tasks', 'checkpoints'])
        wren = self.next('Wren')
        self.assertEqual(kinds(wren), [('claimable-task', spare)])
        self.assertEqual((wren['attention']['counts']['to_review'], wren['attention']['counts']['review_recommended']),
                         (0, 0))
        self.assertNotIn('to review', wren['attention']['summary'])

    def test_an_agent_not_granted_the_project_is_shown_nothing_of_it(self):
        self.scenario()
        made = self.request('POST', '/v1/agents', {'name': 'Stray', 'working_directory': '/home/rita/stray',
                                                   'projects': []}, token=self.tokens['rita'])
        self.assertEqual(201, made.status, made.data)
        answer = self.request('GET', '/v1/agents/me/next', token=made.data['credential']['secret']).data
        self.assertEqual(answer['next_actions'], [])
        self.assertEqual(answer['attention']['counts']['to_review'], 0)


class InProcessTests(Shared, test_http_agents.AgentHarness):
    def change(self, task, **fields):
        self.backend._task(self.project, task).update(fields)

    def test_the_cap_is_on_the_whole_list_not_on_each_project(self):
        """Three projects of deliveries list twenty review actions in all, and claimable work stays (review 01a10c80)."""
        self.people()
        admin = self.admin_token()
        projects = [self.project] + [self.create_project(admin, name) for name in ('Beta', 'Gamma')]
        for project in projects[1:]:
            for name, role in (('olive', 'owner'), ('carl', 'contributor'), ('rita', 'contributor')):
                self.request('PUT', '/v1/projects/%s/members/%s' % (project, self.ids[name]), {'role': role}, token=admin)
        made = self.request('POST', '/v1/agents', {'name': 'Kite', 'working_directory': '/home/carl/kite',
                                                   'projects': projects}, token=self.tokens['carl'])
        wide = self.request('POST', '/v1/agents', {'name': 'Wide', 'working_directory': '/home/rita/wide',
                                                   'projects': projects}, token=self.tokens['rita'])
        self.assertEqual((201, 201), (made.status, wide.status))
        kite, reviewer = made.data['credential']['secret'], wide.data['credential']['secret']
        for index, project in enumerate(projects):
            base = '/v1/projects/%s/tasks' % project
            for number in range(12):
                task = self.request('POST', base, {'title': 'd %d' % number}, token=self.tokens['olive']).data['id']
                self.assertEqual(200, self.request('POST', '%s/%s/claim' % (base, task), token=kite).status)
                sent = self.request('POST', '%s/%s/reviews' % (base, task), {
                    'operation': 'contribute', 'commit': '%040x' % (index * 100 + number + 1),
                    'base_commit': test_http_agents.BASE, 'bundle_sha256': test_http_agents.BUNDLE, 'summary': 'd'}, token=kite)
                self.assertEqual(201, sent.status, sent.data)
            self.request('POST', base, {'title': 'spare'}, token=self.tokens['olive'])
        answer = self.request('GET', '/v1/agents/me/next', token=reviewer).data
        listed = [kind for kind, _ in kinds(answer)]
        self.assertEqual(listed.count('to-review'), 20)
        self.assertEqual(answer['attention']['counts']['to_review'], 36)
        self.assertTrue(answer['attention']['truncated'])
        self.assertEqual(listed.count('claimable-task'), 3)
        self.assertEqual(listed[:20], ['to-review'] * 20)
    def new_task(self, title):
        return self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': title},
                            token=self.tokens['olive']).data['id']

    def deliver(self, title, commit, agent='Kestrel'):
        task = self.new_task(title)
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', token=self.agents[agent]).status)
        made = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'contribute', 'commit': commit, 'base_commit': test_http_agents.BASE,
            'bundle_sha256': test_http_agents.BUNDLE, 'summary': 'delivered'}, token=self.agents[agent])
        self.assertEqual(201, made.status, made.data)
        return task, made.data['contribution']['id']

    def approve(self, token, task, contribution):
        return self.request('POST', self.base(task) + '/reviews',
                            {'operation': 'approve', 'contribution': contribution, 'summary': 'accepted'}, token=token)

    def test_the_list_is_capped_and_the_counts_are_exact(self):
        self.people()
        for index in range(23):
            self.deliver('task %02d' % index, '%040x' % (index + 1))
        osprey = self.next('Osprey')
        listed = [kind for kind, _ in kinds(osprey)]
        self.assertEqual((listed.count('to-review'), osprey['attention']['counts']['to_review']), (20, 23))
        self.assertTrue(osprey['attention']['truncated'])
        # The cap keeps what is ready for the owner: a recommended delivery is listed first
        # even when its task sorts last.
        last, contribution = self.deliver('task zz', '%040x' % 99)
        self.assertEqual(201, self.recommend(self.agents['Osprey'], last, contribution, '%040x' % 99).status)
        heron = self.next('Heron')
        self.assertEqual(kinds(heron)[0], ('review-recommended', last))
        self.assertEqual(len([kind for kind, _ in kinds(heron) if kind in ('review-recommended', 'to-review')]), 20)
        self.assertEqual((heron['attention']['counts']['review_recommended'], heron['attention']['counts']['to_review']),
                         (1, 23))

    def test_standing_is_per_project_and_follows_the_live_grant(self):
        self.people()
        admin = self.admin_token()
        other = self.create_project(admin, 'Beta')
        # Heron's owner owns Beta too; Heron itself is not granted it.
        added = self.request('PUT', '/v1/projects/%s/members/%s' % (other, self.ids['olive']), {'role': 'owner'}, token=admin)
        self.assertIn(added.status, (200, 201), added.data)
        standing = self.service.agent_review_standing
        self.assertEqual(standing(self.agent_ids['Heron'], self.project), (True, True))
        self.assertEqual(standing(self.agent_ids['Osprey'], self.project), (True, False))
        # A project the agent is not granted, whatever its owner's role there.
        self.assertEqual(standing(self.agent_ids['Heron'], other), (False, False))
        self.assertEqual(standing('no-such-agent', self.project), (False, False))


class EndpointTests(Shared, fixes.EndpointCase):
    """The same over the strict canonical stub: the kit's own work view and review rules."""

    def change(self, task, **fields):
        import json
        path = self.canonical_root / 'canonical.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        next(row for row in state['rows'] if row['id'] == task).update(fields)
        path.write_text(json.dumps(state), encoding='utf-8')

    def older_endpoint(self):
        """An endpoint from before this delivery: its work rows do not say who delivered."""
        run = self.backend._run

        def older(action, project_id, actor, args, *rest, **kwargs):
            answer = run(action, project_id, actor, args, *rest, **kwargs)
            if action == 'work' and isinstance(answer, dict):
                answer = dict(answer, items=[{k: v for k, v in row.items() if k != 'contribution_author'}
                                             for row in answer.get('items') or []])
            return answer
        self.backend._run = older
        self.addCleanup(setattr, self.backend, '_run', run)

    def test_over_an_older_endpoint_nobody_is_offered_their_own_delivery(self):
        (one, two), spare = self.scenario()
        self.assertEqual(201, self.recommend(self.agents['Osprey'], *one).status)
        self.older_endpoint()
        for name in ('Kestrel', 'Merlin'):
            self.assertEqual(self.offered(name), [], name)
        self.assertEqual(self.offered('Osprey'), [two[0]])
        # The queue compares with the assignee when the row names no author: a recommendation by
        # the delivering agent's own person is not shown.
        self.change(one[0], assignee=self.agent_ids['Osprey'])
        queue = self.request('GET', '/v1/projects/%s/queue' % self.project, token=self.tokens['olive']).data['items']
        row = next(row for row in queue if row['id'] == one[0])
        self.assertEqual((row['recommended_by'], row['recommended']), ([], False))

    def new_task(self, title):
        return self.create_task(self.tokens['olive'], self.project, title).data['id']

    def deliver(self, title, commit, agent='Kestrel'):
        task = self.new_task(title)
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {}, token=self.agents[agent]).status)
        body = dict(fixes.CONTRIBUTION, commit=commit, operation='contribute', schema_version=1,
                    operation_id='op-' + secrets.token_hex(6), previous=None)
        made = self.request('POST', self.base(task) + '/reviews', body, token=self.agents[agent])
        self.assertEqual(201, made.status, made.data)
        return task, made.data.get('comment_id')

    def approve(self, token, task, contribution):
        return self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve', 'schema_version': 1, 'operation_id': 'op-' + secrets.token_hex(6),
            'previous': contribution, 'contribution': contribution, 'summary': 'accepted'}, token=token)

    def test_review_work_costs_no_read_beyond_the_one_work_snapshot(self):
        (one, two), spare = self.scenario()
        self.assertEqual(201, self.recommend(self.agents['Osprey'], *one).status)
        calls = []
        run = self.backend._run

        def recording(action, project_id, actor, args, *rest, **kwargs):
            calls.append((action, list(args)))
            return run(action, project_id, actor, args, *rest, **kwargs)
        self.backend._run = recording
        heron = self.next('Heron')
        self.assertEqual([kind for kind, _ in kinds(heron)], ['review-recommended', 'to-review', 'claimable-task'])
        self.assertEqual(calls, [('work', ['--limit', '100', '--offset', '0', '--json'])])


if __name__ == '__main__':
    unittest.main()
