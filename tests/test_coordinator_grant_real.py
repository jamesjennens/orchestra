"""The coordinator grant through the real stack (kittrial-5bb.209): the web service, the real
endpoint.py and a real bd. Skipped where there is no bd or no POSIX; the rules themselves are
in tests/test_coordinator_grant.py on the in-process backend.

What only this file shows: the endpoint authorizes the write a second time from the present
state, so an approval by a granted agent is carried out and written under the agent's own
name, and one by an agent that is not granted (or no longer) writes nothing.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_bd_label_aliases as rb
import test_claim_held as held
import test_http_review_fixes as fixes

OWN_PARTY_AGENT = ('This contribution was delivered by this agent\'s own account (its owner, one of that account\'s '
                   'agents, or a worker credential it issued). An agent never approves work of its own account: another '
                   'coordinator or owner of this project, or a superuser who did not deliver it, must approve it.')


def only_its_own_tests(cls):
    """Borrows the real stack of tests/test_claim_held.py and runs none of that file's tests again."""
    for base in (rb.RealBdLabelAliasTests, held.RealStackTests):
        for name in dir(base):
            if name.startswith('test_') and name not in cls.__dict__:
                setattr(cls, name, None)
    return cls


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@only_its_own_tests
class RealStackTests(held.RealStackTests):
    """alex owns the project, casey and drew contribute; cora is made a coordinator and has an agent."""

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        if 'cora' not in self.tokens:
            self.ids['cora'] = self.create_account(admin, 'cora', 'cora-password-1')
            self.tokens['cora'] = self.login('cora', 'cora-password-1')[0]
        added = self.request('PUT', '/v1/projects/pp/members/%s' % self.ids['cora'], {'role': 'coordinator'},
                             token=self.tokens['alex'])
        self.assertEqual(200, added.status, added.data)
        made = self.request('POST', '/v1/agents', {'name': 'Heron', 'working_directory': '/home/x/heron', 'projects': ['pp']},
                            token=self.tokens['cora'])
        self.assertEqual(201, made.status, made.data)
        self.agent, self.agent_id, self.agent_actor = (made.data['credential']['secret'], made.data['agent']['id'],
                                                       made.data['agent']['actor'])
        self.count = 0

    def grant(self, method='PUT'):
        return self.request(method, '/v1/projects/pp/agents/%s/coordinator' % self.agent_id, {} if method == 'PUT' else None,
                            token=self.tokens['alex'])

    def deliver(self, who):
        """A task claimed and delivered by ``who``; returns (task, the id of the contribution)."""
        self.count += 1
        task = self.new('work %d' % self.count)
        self.assertEqual(200, self.claim(who, task).status)
        made = self.request('POST', '%s/%s/reviews' % (self.tasks, task), dict(
            fixes.CONTRIBUTION, operation='contribute', schema_version=1, operation_id='op-deliver-%d' % self.count,
            previous=None), token=self.tokens[who])
        self.assertEqual(201, made.status, made.data)
        return task, self.review(task)['contribution']['id']

    def review(self, task):
        return self.request('GET', '%s/%s/brief' % (self.tasks, task), token=self.tokens['alex']).data['review']

    def approve(self, token, task, contribution):
        self.count += 1
        return self.request('POST', '%s/%s/reviews' % (self.tasks, task), dict(
            operation='approve', schema_version=1, operation_id='op-approve-%d' % self.count, previous=contribution,
            contribution=contribution, summary='Accepted'), token=token)

    def comments(self, task):
        found = json.loads(self.bd('comments', task, '--json').stdout)
        return [(entry.get('author'), entry.get('text') or '') for entry in found or []]

    def test_a_granted_agent_approves_another_accounts_work_and_bd_holds_it_under_its_name(self):
        task, contribution = self.deliver('casey')
        # Not granted: refused, and bd holds no approval.
        refused = self.approve(self.agent, task, contribution)
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual('awaiting-review', self.review(task)['state'])
        before = self.comments(task)
        self.assertEqual(200, self.grant().status)
        approved = self.approve(self.agent, task, contribution)
        self.assertEqual(201, approved.status, approved.data)
        self.assertEqual('awaiting-integration', self.review(task)['state'])
        written = self.comments(task)[len(before):]
        self.assertEqual([self.agent_actor], [author for author, _ in written], written)
        self.assertIn('"operation":"approve"', written[0][1].replace(' ', ''))

    def test_its_own_accounts_work_and_the_grant_removed(self):
        self.assertEqual(200, self.grant().status)
        # cora's own contribution: her agent is refused though the installation's setting is off.
        self.assertFalse(self.service.approval_by_another_party)
        own, mine = self.deliver('cora')
        refused = self.approve(self.agent, own, mine)
        self.assertEqual((403, OWN_PARTY_AGENT), (refused.status, held.message(refused)), refused.data)
        self.assertEqual('awaiting-review', self.review(own)['state'])
        # cora herself, signed in, approves it as before: the setting is off.
        self.assertEqual(201, self.approve(self.tokens['cora'], own, mine).status)
        # The grant removed: the next request is refused and bd is as it was.
        task, contribution = self.deliver('drew')
        before = self.comments(task)
        self.assertEqual(200, self.grant('DELETE').status)
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)
        self.assertEqual((before, 'awaiting-review'), (self.comments(task), self.review(task)['state']))


if __name__ == '__main__':
    unittest.main()
