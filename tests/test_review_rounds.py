import json
import unittest
from pathlib import Path

import tests.test_review_verification_holds as fixture
import review_rounds
import review_state
import review_workflow as workflow
import work


class ReviewRoundTests(fixture.ChainHarness):
    def read(self):
        return review_rounds.read(self.issue, operators=[fixture.OPERATOR])

    def request(self, contribution, items, summary=None):
        extra = {} if summary is None else {'summary': summary}
        return self.send(self.payload('request-changes', contribution=contribution,
                                      items=items, **extra), fixture.REVIEWER)

    def test_three_rounds_share_outcomes_and_carry_item_history(self):
        first = self.send(self.contribution())['comment_id']
        requested = self.request(first, [
            {'id': 'fix', 'text': 'Correct the edge case'},
            {'id': 'nit', 'text': 'Optional note', 'severity': 'note'},
        ], 'Review reasoning')['comment_id']
        second = self.send(self.contribution(commit=fixture.COMMIT_2))['comment_id']
        self.send(self.payload('respond', contribution=second, resolutions=[
            {'request': requested, 'item': 'fix', 'reason': 'Corrected',
             'evidence': 'commit:' + fixture.COMMIT_2},
        ]))
        settle = self.request(second, [{'id': 'settle', 'text': 'Confirm the result'}])['comment_id']
        self.send(self.payload('resolve-item', contribution=second, request=settle,
                               item='settle', reason='Confirmed', disposition='resolved'),
                  fixture.REVIEWER)
        third = self.send(self.contribution(commit='9' * 40))['comment_id']
        self.send(self.payload('approve', contribution=third, summary='Approved'), fixture.REVIEWER)

        result = self.read()
        self.assertEqual(result['rounds_total'], 3)
        self.assertEqual(result['rounds_omitted'], 0)
        self.assertEqual([r['contribution'] for r in result['rounds']], [third, second, first])
        self.assertEqual([r['round'] for r in result['rounds']], [3, 2, 1])
        self.assertTrue(result['coverage']['proved'])
        self.assertEqual(result['coverage'], {'records': 8, 'malformed': 0, 'proved': True})

        one, two, three = reversed(result['rounds'])
        self.assertEqual(one['outcome']['operation'], 'request-changes')
        self.assertEqual(one['next_contribution'], second)
        self.assertEqual(one['disposition'], 'superseded')
        self.assertEqual({item['id']: item['state'] for item in one['items']},
                         {'fix': 'superseded', 'nit': 'superseded'})
        self.assertEqual(two['next_contribution'], third)
        self.assertEqual({item['id']: item['state'] for item in two['items']},
                         {'fix': 'answered', 'nit': 'superseded', 'settle': 'settled'})
        fix = next(item for item in two['items'] if item['id'] == 'fix')
        self.assertEqual(fix['reason'], {'text': 'Corrected', 'omitted_chars': 0})
        self.assertEqual(fix['evidence'], 'commit:' + fixture.COMMIT_2)
        self.assertEqual(fix['by'], 'respond')
        self.assertEqual(three['outcome']['operation'], 'approve')
        self.assertEqual(three['items'][0]['severity'], 'note')
        self.assertEqual(three['items'][0]['state'], 'open')
        self.assertEqual(three['review_state'], 'awaiting-integration')
        self.assertEqual(three['review_state'], self.shared()['review_state'])

        ordered, voids, invalid, refused, positions, reverts, invalid_reverts = workflow.history(self.issue)
        prefix = []
        chronological = []
        for entry in ordered:
            if entry[0]['operation'] == 'contribute' and prefix:
                chronological.append(list(prefix))
            prefix.append(entry)
        chronological.append(prefix)
        for round_, records in zip(reversed(result['rounds']), chronological):
            raw = workflow.projection(records, voids, invalid, refused, positions,
                                      reverts, invalid_reverts)
            shared = review_state.effective(raw, [], raw['review_state'])
            self.assertEqual(round_['review_state'], shared['review_state'])

    def test_open_withdrawn_and_note_disposition_states(self):
        first = self.send(self.contribution())['comment_id']
        request = self.request(first, [
            {'id': 'open', 'text': 'Still open'},
            {'id': 'note', 'text': 'Downgrade this'},
        ])['comment_id']
        self.send(self.payload('resolve-item', contribution=first, request=request,
                               item='note', reason='Only advisory', disposition='note'),
                  fixture.REVIEWER)
        self.send(self.payload('withdraw', contribution=first, reason='Re-scoped',
                               disposition='withdrawn'))
        round_ = self.read()['rounds'][0]
        self.assertEqual(round_['disposition'], 'withdrawn')
        self.assertEqual(round_['disposition_reason']['text'], 'Re-scoped')
        self.assertEqual({item['id']: item['state'] for item in round_['items']},
                         {'open': 'withdrawn', 'note': 'withdrawn'})
        note = next(item for item in round_['items'] if item['id'] == 'note')
        self.assertEqual(note['severity'], 'note')
        self.assertEqual(note['disposition'], 'note')
        self.assertEqual(note['by'], 'resolve-item')

    def test_attestations_stay_on_their_round_and_holds_link_releases(self):
        contribution = self.send(self.contribution())['comment_id']
        attestation = self.send(self.attest(contribution, sha256=fixture.SHA_1, bytes=4096))['comment_id']
        hold = self.send(self.hold(contribution))['comment_id']
        release = self.send(self.release_hold(contribution, hold))['comment_id']
        round_ = self.read()['rounds'][0]
        self.assertEqual(round_['attestations'][0]['record'], attestation)
        self.assertEqual(round_['attestations'][0]['verdict'], 'verified')
        self.assertEqual(round_['holds'][0]['record'], hold)
        self.assertEqual(round_['holds'][0]['released_by'], release)

    def test_scoped_integration_is_shared_for_matching_and_other_commits(self):
        def scope(commit):
            return [{
                'scope': {'source_commit': commit, 'integration_commit': 'd' * 40,
                          'release_id': '', 'environment': 'development'},
                'scope_token': 'scope-' + commit[0], 'order': 1,
                'integrated': {'value': 'passed', 'event_id': 'integration-1', 'order': 1},
            }]

        first = self.approved()
        for commit, expected in ((fixture.COMMIT_1, 'integrated'),
                                 ('e' * 40, 'awaiting-integration')):
            with self.subTest(commit=commit):
                round_ = review_rounds.read(
                    self.issue, scope(commit), operators=[fixture.OPERATOR])['rounds'][0]
                self.assertEqual(round_['review_state'], expected)
                self.assertEqual(round_['workflow_state'], 'awaiting-integration')
                self.assertEqual(round_['integration']['matches_contribution'],
                                 commit == fixture.COMMIT_1)

        second = self.approved(commit=fixture.COMMIT_2)
        rounds = review_rounds.read(
            self.issue, scope(fixture.COMMIT_1), operators=[fixture.OPERATOR])['rounds']
        self.assertEqual([round_['contribution'] for round_ in rounds], [second, first])
        self.assertEqual([round_['review_state'] for round_ in rounds],
                         ['awaiting-integration', 'integrated'])
        self.assertEqual(rounds[0]['integration']['fact'], 'unknown')
        self.assertEqual(rounds[1]['integration']['fact'], 'passed')

    def test_open_holds_are_carried_into_each_later_round(self):
        first = self.send(self.contribution())['comment_id']
        self.send(self.attest(first, sha256=fixture.SHA_1, bytes=4096))
        hold = self.send(self.hold(first))['comment_id']
        self.send(self.payload('approve', contribution=first, summary='Approved'), fixture.REVIEWER)
        second = self.send(self.contribution(commit=fixture.COMMIT_2))['comment_id']
        self.send(self.payload('approve', contribution=second, summary='Approved again'),
                  fixture.REVIEWER)

        result = self.read()
        self.assertEqual([round_['review_state'] for round_ in result['rounds']], ['held', 'held'])
        self.assertEqual([[item['record'] for item in round_['holds']]
                          for round_ in result['rounds']], [[hold], [hold]])
        self.assertTrue(all(round_['holds'][0]['released_by'] is None
                            for round_ in result['rounds']))
        self.assertEqual(result['rounds'][0]['review_state'], self.shared()['review_state'])

    def test_releasing_a_carried_hold_does_not_rewrite_the_earlier_round(self):
        first = self.send(self.contribution())['comment_id']
        hold = self.send(self.hold(first))['comment_id']
        second = self.send(self.contribution(commit=fixture.COMMIT_2))['comment_id']
        release = self.send(self.release_hold(second, hold))['comment_id']

        rounds = self.read()['rounds']
        self.assertEqual(rounds[0]['holds'][0]['released_by'], release)
        self.assertEqual(rounds[0]['holds'][0]['release_reason']['text'],
                         'Hold cleared after re-verification')
        self.assertIsNone(rounds[1]['holds'][0]['released_by'])
        self.assertEqual(rounds[0]['review_state'], 'awaiting-review')
        self.assertEqual(rounds[1]['review_state'], 'awaiting-review')

    def test_follow_on_relation_marks_the_prior_round_followed(self):
        first = self.stored(self.contribution())
        self.stored(self.payload('approve', contribution=first, summary='Approved'),
                    fixture.REVIEWER)
        second = self.stored(self.contribution(
            commit=fixture.COMMIT_2, supersedes=None, follows=first))
        rounds = self.read()['rounds']
        self.assertEqual(rounds[1]['disposition'], 'followed')
        self.assertEqual(rounds[1]['next_contribution'], second)
        self.assertEqual(rounds[0]['follows'], first)

    def test_clipping_does_not_split_an_escape(self):
        hostile = ('x' * 199) + '\x1b' + 'tail'
        contribution = self.stored(self.contribution(summary=hostile))
        request = self.stored(self.payload('request-changes', contribution=contribution,
                                          items=[{'id': 'escape', 'text': hostile}]), fixture.REVIEWER)
        evidence = ('x' * 239) + '\x1b' + 'tail'
        self.stored(self.payload('respond', contribution=contribution, resolutions=[
            {'request': request, 'item': 'escape', 'reason': 'Checked', 'evidence': evidence},
        ]))
        round_ = self.read()['rounds'][0]
        self.assertEqual(round_['summary'], {'text': 'x' * 199, 'omitted_chars': 10})
        self.assertEqual(round_['items'][0]['text'],
                         {'text': 'x' * 199, 'omitted_chars': 10})
        self.assertEqual(round_['items'][0]['evidence'], 'x' * 239)
        self.assertEqual(round_['items'][0]['evidence_omitted_chars'], 10)
        self.assertNotIn('\\u001', round_['summary']['text'])
        exact = review_rounds._text('x' * 199 + '\\')
        self.assertEqual(exact, {'text': 'x' * 199 + '\\', 'omitted_chars': 0})

    def test_twenty_one_rounds_returns_the_newest_twenty(self):
        for number in range(21):
            self.send(self.contribution(commit='%040x' % (number + 1)))
        result = self.read()
        self.assertEqual(result['rounds_total'], 21)
        self.assertEqual(result['rounds_omitted'], 1)
        self.assertEqual(len(result['rounds']), 20)
        self.assertEqual(result['rounds'][0]['round'], 21)
        self.assertEqual(result['rounds'][-1]['round'], 2)

    def test_fifty_one_items_and_eleven_attestations_and_holds_are_bounded(self):
        contribution = self.send(self.contribution())['comment_id']
        for batch in range(2):
            request = self.request(contribution, [
                {'id': 'item-%d-%d' % (batch, index), 'text': 'Fix'} for index in range(20)
            ])['comment_id']
            self.send(self.payload('respond', contribution=contribution, resolutions=[
                {'request': request, 'item': 'item-%d-%d' % (batch, index),
                 'reason': 'Done', 'evidence': 'commit:' + fixture.COMMIT_1}
                for index in range(20)
            ]))
        self.request(contribution, [{'id': 'open-%d' % index, 'text': 'Open'}
                                    for index in range(11)])
        for _ in range(11):
            self.send(self.attest(contribution, sha256=fixture.SHA_1, bytes=4096))
            self.send(self.hold(contribution))
        round_ = self.read()['rounds'][0]
        self.assertEqual(len(round_['items']), 50)
        self.assertEqual(round_['items_omitted'], 1)
        self.assertEqual(len(round_['attestations']), 10)
        self.assertEqual(round_['attestations_omitted'], 1)
        self.assertEqual(len(round_['holds']), 10)
        self.assertEqual(round_['holds_omitted'], 1)

    def test_hostile_legacy_text_is_escaped_clipped_and_cannot_forge_lines(self):
        hostile = 'begin\nforged\x1b[2J\x85\u202e\u2028' + ('x' * 260)
        contribution_payload = self.contribution(repository=hostile, summary=hostile,
                                                 delivery={'kind': 'remote', 'remote': hostile,
                                                           'branch': hostile})
        contribution = self.stored(contribution_payload)
        request_payload = self.payload('request-changes', contribution=contribution,
                                       summary=hostile, items=[{'id': 'hostile', 'text': hostile}])
        request = self.stored(request_payload, fixture.REVIEWER)
        response_payload = self.payload('respond', contribution=contribution, resolutions=[
            {'request': request, 'item': 'hostile', 'reason': hostile, 'evidence': hostile}
        ])
        self.stored(response_payload)
        self.stored(self.attest(contribution, method=hostile[:190], reason=hostile), fixture.VERIFIER)
        self.stored(self.hold(contribution, reason=hostile), fixture.OPERATOR)

        result = self.read()
        round_ = result['rounds'][0]
        item = round_['items'][0]
        for excerpt in (round_['summary'], round_['repository'], round_['delivery']['remote'],
                        round_['delivery']['branch'], item['text'], item['summary'], item['reason'],
                        round_['attestations'][0]['method'], round_['attestations'][0]['reason'],
                        round_['holds'][0]['reason']):
            self.assertLessEqual(len(excerpt['text']), 200)
            self.assertGreater(excerpt['omitted_chars'], 0)
            for character in ('\x1b', '\x85', '\u202e', '\u2028', '\r'):
                self.assertNotIn(character, excerpt['text'])
        self.assertEqual(len(item['evidence']), 240)
        self.assertGreater(item['evidence_omitted_chars'], 0)
        self.assertNotIn('\nforged', result['text'])
        self.assertIn(' / forged', result['text'])
        for character in ('\x1b', '\x85', '\u202e', '\u2028', '\r'):
            self.assertNotIn(character, result['text'])

    def test_malformed_history_is_counted_and_uncertain_states_are_unknown(self):
        contribution = self.send(self.contribution())['comment_id']
        self.request(contribution, [{'id': 'fix', 'text': 'Fix'}])
        self.issue['comments'].append({'id': 'broken', 'text': workflow.PREFIX + '{',
                                       'author': fixture.REVIEWER,
                                       'created_at': '2026-09-16T00:00:00Z'})
        result = self.read()
        self.assertFalse(result['coverage']['proved'])
        self.assertEqual(result['coverage']['records'], 3)
        self.assertEqual(result['coverage']['malformed'], 1)
        self.assertEqual(result['rounds'][0]['review_state'], 'unknown')
        self.assertIsNone(result['rounds'][0]['outcome'])
        self.assertEqual(result['rounds'][0]['items'][0]['state'], 'unknown')
        self.assertTrue(result['warnings'])

    def test_semantically_invalid_history_uses_the_same_unknown_fallback(self):
        contribution = self.send(self.contribution())['comment_id']
        self.stored(self.payload('respond', contribution=contribution, resolutions=[
            {'request': 'missing', 'item': 'fix', 'reason': 'No such request',
             'evidence': 'test:missing'},
        ]))
        result = self.read()
        self.assertFalse(result['coverage']['proved'])
        self.assertEqual(result['coverage']['records'], 2)
        self.assertEqual(result['coverage']['malformed'], 1)
        self.assertEqual(result['rounds'][0]['review_state'], 'unknown')

    def test_empty_history_is_proved_and_empty(self):
        result = self.read()
        self.assertEqual(result['rounds'], [])
        self.assertEqual(result['coverage'], {'records': 0, 'malformed': 0, 'proved': True})


class ReviewRoundRouteTests(fixture.ChainHarness):
    def test_rounds_is_read_only_and_bare_review_and_file_write_are_unchanged(self):
        commands = []

        def run(argv):
            commands.append(argv)
            return self.run_native(argv)

        path = Path('.')
        bare_before = work.execute(path, fixture.AUTHOR, 'review', [fixture.TASK], {}, run)
        rounds = work.execute(path, fixture.AUTHOR, 'review', [fixture.TASK, '--rounds'], {}, run)
        bare_after = work.execute(path, fixture.AUTHOR, 'review', [fixture.TASK], {}, run)
        self.assertEqual(bare_before, bare_after)
        self.assertEqual(rounds['rounds'], [])
        self.assertTrue(all(argv == ['export', '--all'] for argv in commands))

        payload = self.contribution()
        made = work.execute(path, fixture.AUTHOR, 'review', [fixture.TASK, '@attachment:0'],
                            {'0': {'flag': '--file', 'text': json.dumps(payload)}}, run)
        self.assertEqual(made['review_state'], 'awaiting-review')
        self.assertEqual(work.execute(path, fixture.AUTHOR, 'review', [fixture.TASK], {}, run)
                         ['contribution']['comment_id'], made['comment_id'])
        self.assertEqual(work.execute(path, fixture.AUTHOR, 'review',
                                      [fixture.TASK, '--rounds', '--json'], {}, run)['rounds_total'], 1)
        with self.assertRaisesRegex(ValueError, 'Use review TASK'):
            work.execute(path, fixture.AUTHOR, 'review',
                         [fixture.TASK, '--rounds', '@attachment:0'],
                         {'0': {'flag': '--file', 'text': json.dumps(payload)}}, run)

    def test_help_and_standalone_client_agree_on_rounds_usage(self):
        import client
        help_payload = work.help_payload('review')
        self.assertEqual(help_payload['usage'], 'review TASK [--rounds | --file payload.json]')
        self.assertIn('--rounds', [option['flag'] for option in help_payload['options']])
        self.assertIn(help_payload['usage'], client.contributor_command_help('review'))
        self.assertEqual(help_payload['limits']['rounds / items per round'], '20 / 50')


if __name__ == '__main__':
    unittest.main()
