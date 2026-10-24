import json
import secrets
import unittest

import tests.test_coordinator_rules_endpoint as fixture


@unittest.skipIf(fixture.fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class ReviewRoundRealBdTests(fixture.RulesHelpers, unittest.TestCase):
    def test_rounds_through_endpoint_matches_review_without_native_writes(self):
        self.deployment(review_workflow_writes=True)
        task = self.task('Review rounds real-bd fixture', assignee='worker')
        made = json.loads(self.contribute(task, 'worker')['stdout'])
        contribution = made['comment_id']
        requested = self.post_review(task, 'reviewer', {
            'schema_version': 1, 'operation': 'request-changes',
            'operation_id': 'request-' + secrets.token_hex(8), 'task': task,
            'previous': contribution, 'contribution': contribution,
            'items': [{'id': 'fix', 'text': 'Exercise the rounds read'}],
        })
        request = json.loads(requested['stdout'])['comment_id']
        responded = self.post_review(task, 'worker', {
            'schema_version': 1, 'operation': 'respond',
            'operation_id': 'respond-' + secrets.token_hex(8), 'task': task,
            'previous': request, 'contribution': contribution,
            'resolutions': [{'request': request, 'item': 'fix', 'reason': 'Exercised',
                             'evidence': 'test:review-rounds-real-bd'}],
        })
        response = json.loads(responded['stdout'])['comment_id']
        approved = self.post_review(task, 'reviewer', {
            'schema_version': 1, 'operation': 'approve',
            'operation_id': 'approve-' + secrets.token_hex(8), 'task': task,
            'previous': response, 'contribution': contribution, 'summary': 'Approved',
        })
        approval = json.loads(approved['stdout'])['comment_id']

        before = self.rows()
        bare = json.loads(self.endpoint('review', 'worker', [task])['stdout'])
        rounds_reply = self.endpoint('review', 'worker', [task, '--rounds'])
        rounds = json.loads(rounds_reply['stdout'])
        after = self.rows()

        self.assertEqual(before, after)
        self.assertNotIn('server_time', rounds)
        self.assertEqual(rounds['rounds_total'], 1)
        self.assertEqual(rounds['coverage'], {'records': 4, 'malformed': 0, 'proved': True})
        round_ = rounds['rounds'][0]
        self.assertEqual(round_['contribution'], contribution)
        self.assertEqual(round_['outcome']['operation'], 'approve')
        self.assertEqual(round_['outcome']['record'], approval)
        self.assertEqual(round_['review_state'], bare['review_state'])
        self.assertEqual(round_['items'][0]['state'], 'answered')
        self.assertEqual(round_['items'][0]['evidence'], 'test:review-rounds-real-bd')

    def test_open_hold_is_carried_into_the_next_round(self):
        self.deployment(review_workflow_writes=True)
        task = self.task('Review rounds carried hold fixture', assignee='worker')
        first = json.loads(self.contribute(task, 'worker')['stdout'])['comment_id']
        attested = self.post_review(task, 'ops', {
            'schema_version': 1, 'operation': 'attest',
            'operation_id': 'attest-' + secrets.token_hex(8), 'task': task,
            'previous': first, 'contribution': first, 'verdict': 'verified',
            'commit': fixture.COMMIT, 'base_commit': fixture.BASE,
            'method': 'real-bd fixture attestation', 'reason': 'Fixture delivery checked',
            'sha256': 'f' * 64, 'bytes': 4096,
        })
        attestation = json.loads(attested['stdout'])['comment_id']
        held = self.post_review(task, 'ops', {
            'schema_version': 1, 'operation': 'hold',
            'operation_id': 'hold-' + secrets.token_hex(8), 'task': task,
            'previous': attestation, 'contribution': first, 'reason': 'Keep integration held',
        })
        hold = json.loads(held['stdout'])['comment_id']
        approved = self.post_review(task, 'reviewer', {
            'schema_version': 1, 'operation': 'approve',
            'operation_id': 'approve-first-' + secrets.token_hex(8), 'task': task,
            'previous': hold, 'contribution': first, 'summary': 'Approved under hold',
        })
        approval = json.loads(approved['stdout'])['comment_id']
        second = json.loads(self.contribute(
            task, 'worker', commit='c' * 40, previous=approval, supersedes=first,
        )['stdout'])['comment_id']
        self.approve(task, 'reviewer', second)

        bare = json.loads(self.endpoint('review', 'worker', [task])['stdout'])
        rounds = json.loads(self.endpoint('review', 'worker', [task, '--rounds'])['stdout'])
        self.assertEqual(bare['review_state'], 'held')
        self.assertEqual([round_['review_state'] for round_ in rounds['rounds']], ['held', 'held'])
        self.assertEqual([[hold['record'] for hold in round_['holds']]
                          for round_ in rounds['rounds']], [[hold], [hold]])
        self.assertIsNone(rounds['rounds'][0]['holds'][0]['released_by'])


if __name__ == '__main__':
    unittest.main()
