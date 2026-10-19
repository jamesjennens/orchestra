"""The coordinator's recorded hand-back (kittrial-5bb.343): payload, journal and applicability.

Pure-module tests. Every record is paired with a host journal entry in a disposable
project directory; the applicability matrix drives the ONE decision shared by work,
brief, review and wait, and the direction tests prove a withdrawal is the
coordinator's own ``superseded`` disposition rather than a second direction.
"""
import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import followon
import work
import briefing
import test_briefing

TASK = 'trial-task'
COORD = 'coordinator'
WORKER = 'alice/session'
COMMIT = 'a' * 40


def payload(text='Remaining scope: finish the release notes.', operation='record', **changes):
    if operation == 'record':
        value = {'schema_version': 1, 'operation': 'record', 'task': TASK,
                 'contribution': 'contrib-1', 'commit': COMMIT,
                 'integration': TASK + '.2', 'integration_order': 2,
                 'text': text, 'actor': COORD}
    else:
        value = {'schema_version': 1, 'operation': 'withdraw', 'task': TASK,
                 'target': 'fo1', 'reason': 'The coordinator kept the scope.',
                 'actor': COORD}
    value.update(changes)
    return value


def scopes(*extra):
    base = [{'scope': {'source_commit': COMMIT, 'integration_commit': 'b' * 40},
             'integrated': {'value': 'passed', 'event_id': TASK + '.2', 'order': 2},
             'order': 2, 'scope_token': 'token-1'}]
    base.extend(extra)
    return base


def review(contribution='contrib-1', fact='passed', state='integrated'):
    return {'review_state': state,
            'contribution': {'comment_id': contribution, 'commit': COMMIT},
            'integration': {'fact': fact, 'record_id': TASK + '.2', 'order': 2}}


def comment(cid, text, author=COORD, stamp='2026-10-19T04:00:00Z'):
    return {'id': cid, 'text': text, 'author': author, 'created_at': stamp}


def issue(comments, status='in_progress'):
    return {'id': TASK, 'issue_type': 'task', 'title': 'A task', 'status': status,
            'assignee': WORKER, 'labels': [], 'comments': comments}


def record_comment(cid='fo1', value=None):
    return comment(cid, followon.body(value or payload()))


class FollowonValidationTests(unittest.TestCase):
    def test_a_record_round_trips_through_its_canonical_body(self):
        value = payload()
        self.assertEqual(followon.parse_text(followon.body(value)), value)

    def test_a_byte_edit_is_not_canonical_or_loses_the_journal_binding(self):
        text = followon.body(payload())
        edited = text.replace('"text":', '"text": ', 1)
        with self.assertRaisesRegex(ValueError, 'not canonical JSON'):
            followon.parse_text(edited)
        # An edit INSIDE a JSON string is still canonical JSON; it is the journal's
        # payload digest that makes it a different record and therefore inert.
        within = text.replace('finish', 'alter', 1)
        self.assertNotEqual(within, text)
        self.assertIsInstance(followon.parse_text(within), dict)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        project = Path(temp.name)
        followon.publish(project, followon.journal_entry(payload(), 'fo1', COORD, 0))
        issue_row = issue([comment('fo1', within)])
        result = followon.evaluate(issue_row, review(), scopes(), project=project)
        self.assertFalse(result['applies'])
        self.assertEqual(result['block']['state'], 'ignored')

    def test_validation_refuses_bad_records_and_withdrawals(self):
        cases = [(dict(payload(), commit='nope'), 'commit'),
                 (dict(payload(), integration_order='2'), 'order'),
                 (dict(payload(), text='x' * (followon.TEXT_LIMIT + 1)), 'limited'),
                 (dict(payload(), text='   '), 'nonempty'),
                 (dict(payload(), actor='bad actor!'), 'actor'),
                 (dict(payload(), contribution=''), 'contribution'),
                 (dict(payload(), schema_version=2), 'schema_version'),
                 (dict(payload(), extra=1), 'does not take'),
                 (dict(payload(operation='withdraw'), reason=''), 'nonempty'),
                 (dict(payload(operation='withdraw'), target=''), 'follow-on record')]
        for value, said in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, said):
                    followon._payload(value)

    def test_a_request_can_never_supply_server_derived_facts(self):
        for name in ('contribution', 'commit', 'integration', 'integration_order', 'actor'):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, 'does not take'):
                    followon.request_payload(dict(
                        {'schema_version': 1, 'operation': 'record', 'task': TASK, 'text': 'x'},
                        **{name: 'y'}))
        with self.assertRaisesRegex(ValueError, 'does not take'):
            followon.request_payload({'schema_version': 1, 'operation': 'withdraw',
                                      'target': 'fo1', 'reason': 'r', 'targets': 'x'})

    def test_every_refusal_says_nothing_was_written(self):
        for call in (lambda: followon.request_payload(None),
                     lambda: followon.request_payload({}),
                     lambda: followon.request_payload({'schema_version': 1, 'operation': 'record',
                                                       'task': TASK}),
                     lambda: followon._payload(dict(payload(), text='x' * (followon.TEXT_LIMIT + 1)))):
            with self.subTest(call=call):
                with self.assertRaises(ValueError) as refused:
                    call()
                self.assertTrue(str(refused.exception).endswith('Nothing was written.'),
                                str(refused.exception))


class FollowonJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)

    def test_the_entry_name_is_the_canonical_payload_digest_and_the_path_is_checked(self):
        value = payload()
        entry = followon.journal_entry(value, 'fo1', COORD, 0)
        path = followon.publish(self.project, entry)
        self.assertEqual(path.name, entry['sha256'] + '.json')
        entries, invalid = followon.host_journal(self.project)
        self.assertEqual(entries['fo1'], entry)
        self.assertEqual(invalid, [])
        with self.assertRaisesRegex(ValueError, 'path mismatch'):
            followon.validate_entry(entry, '0' * 64 + '.json')
        broken = dict(entry, sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'hash does not match'):
            followon.validate_entry(broken)

    def test_a_missing_or_symlinked_journal_trusts_nothing(self):
        self.assertEqual(followon.host_journal(self.project), ({}, []))
        self.assertEqual(followon.host_journal(None), ({}, []))
        try:
            (self.project / followon.JOURNAL_DIR).symlink_to(self.temp.name, target_is_directory=True)
        except OSError as exc:
            self.skipTest('Symlink privilege unavailable: ' + str(exc))
        self.assertEqual(followon.host_journal(self.project), ({}, []))

    def test_an_invalid_entry_is_reported_without_failing_the_read(self):
        value = payload()
        entry = followon.journal_entry(value, 'fo1', COORD, 0)
        followon.publish(self.project, entry)
        (self.project / followon.JOURNAL_DIR / 'not-a-digest.json').write_text('{}', encoding='utf-8')
        entries, invalid = followon.host_journal(self.project)
        self.assertEqual(entries['fo1'], entry)
        self.assertEqual(invalid, [followon.JOURNAL_DIR + '/not-a-digest.json'])

    def test_a_record_without_its_entry_is_inert(self):
        result = followon.evaluate(issue([record_comment()]), review(), scopes(), project=self.project)
        self.assertFalse(result['applies'])
        self.assertEqual(result['block']['state'], 'ignored')
        self.assertIn('not a host-journaled hand-back', result['block']['why'])

    def test_a_record_with_its_entry_applies(self):
        entry = followon.journal_entry(payload(), 'fo1', COORD, 0)
        followon.publish(self.project, entry)
        result = followon.evaluate(issue([record_comment()]), review(), scopes(), project=self.project)
        self.assertTrue(result['applies'])
        self.assertEqual(result['block']['state'], 'open')
        self.assertEqual(result['block']['id'], 'fo1')
        self.assertEqual(result['block']['text'], payload()['text'])
        self.assertEqual(result['block']['actor'], COORD)


class FollowonApplicabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.entries = {}

    def journal(self, value, cid='fo1', actor=COORD, order=0):
        entry = followon.journal_entry(value, cid, actor, order)
        followon.publish(self.project, entry)
        self.entries[cid] = entry
        return entry

    def evaluate(self, comments, reviewed=None, evidence=None, status='in_progress'):
        return followon.evaluate(issue(comments, status=status), reviewed or review(),
                                 evidence if evidence is not None else scopes(), project=self.project)

    def test_a_checkpoint_disposition_ends_it(self):
        self.journal(payload())
        data = [dict(issue([record_comment()]))]
        digest = briefing.entry_digests(briefing.snapshot(data, 'trial', TASK))[TASK + '-cfo1']
        checkpoint = test_briefing.checkpoint(
            data, directions=[{'id': TASK + '-cfo1', 'digest': digest, 'state': 'resolved',
                               'note': 'Scope agreed', 'evidence': 'checkpoint note'}])
        data[0]['comments'].append(comment('cp1', briefing.PREFIX
                                           + json.dumps(checkpoint, sort_keys=True, separators=(',', ':')),
                                           author=WORKER))
        result = followon.evaluate(data[0], review(), scopes(), project=self.project)
        self.assertFalse(result['applies'])
        self.assertEqual(result['block']['state'], 'resolved')
        self.assertIn('checkpoint recorded', result['block']['why'])

    def test_acknowledgement_alone_does_not_end_it(self):
        self.journal(payload())
        data = [dict(issue([record_comment()]))]
        digest = briefing.entry_digests(briefing.snapshot(data, 'trial', TASK))[TASK + '-cfo1']
        checkpoint = test_briefing.checkpoint(
            data, directions=[{'id': TASK + '-cfo1', 'digest': digest, 'state': 'acknowledged'}])
        data[0]['comments'].append(comment('cp1', briefing.PREFIX
                                           + json.dumps(checkpoint, sort_keys=True, separators=(',', ':')),
                                           author=WORKER))
        self.assertTrue(followon.evaluate(data[0], review(), scopes(), project=self.project)['applies'])

    def test_a_resolution_with_a_stale_digest_does_not_end_it(self):
        self.journal(payload())
        data = [dict(issue([record_comment()]))]
        checkpoint = test_briefing.checkpoint(
            data, directions=[{'id': TASK + '-cfo1', 'digest': '0' * 64, 'state': 'resolved',
                               'note': 'Scope agreed', 'evidence': 'checkpoint note'}])
        data[0]['comments'].append(comment('cp1', briefing.PREFIX
                                           + json.dumps(checkpoint, sort_keys=True, separators=(',', ':')),
                                           author=WORKER))
        self.assertTrue(followon.evaluate(data[0], review(), scopes(), project=self.project)['applies'])

    def test_a_withdrawal_ends_it(self):
        self.journal(payload())
        self.journal(payload(operation='withdraw'), cid='fo2')
        value = payload(operation='withdraw')
        comments = [record_comment(), comment('fo2', followon.body(value))]
        result = self.evaluate(comments)
        self.assertFalse(result['applies'])
        self.assertEqual(result['block']['state'], 'withdrawn')
        self.assertIn('withdrew this hand-back', result['block']['why'])

    def test_a_new_contribution_ends_it(self):
        self.journal(payload())
        result = self.evaluate([record_comment()], reviewed=review(contribution='contrib-2'))
        self.assertFalse(result['applies'])
        self.assertEqual(result['block']['state'], 'ended')
        self.assertIn('newer contribution', result['block']['why'])

    def test_a_reverted_or_failed_integration_ends_it(self):
        self.journal(payload())
        for fact in ('reverted', 'failed', 'unknown'):
            with self.subTest(fact=fact):
                result = self.evaluate([record_comment()], reviewed=review(fact=fact))
                self.assertFalse(result['applies'])
                self.assertIn('not integrated', result['block']['why'])
        result = self.evaluate([record_comment()], reviewed=review(state='changes-requested'))
        self.assertFalse(result['applies'])

    def test_an_unknown_citation_ends_it(self):
        self.journal(payload(integration=TASK + '.9', integration_order=9))
        result = self.evaluate([record_comment(value=payload(integration=TASK + '.9',
                                                             integration_order=9))])
        self.assertFalse(result['applies'])
        self.assertEqual(result['block']['state'], 'ended')
        self.assertIn('citation cannot be re-verified', result['block']['why'])

    def test_a_later_attestation_for_the_same_commit_does_not_end_it(self):
        self.journal(payload())
        later = {'scope': {'source_commit': COMMIT, 'integration_commit': 'c' * 40},
                 'integrated': {'value': 'passed', 'event_id': TASK + '.5', 'order': 5},
                 'order': 5, 'scope_token': 'token-2'}
        result = self.evaluate([record_comment()], evidence=scopes(later))
        self.assertTrue(result['applies'])
        self.assertEqual(result['block']['state'], 'open')

    def test_a_newer_record_ends_the_older_one(self):
        self.journal(payload())
        newer = payload(text='Newer remaining scope.')
        self.journal(newer, cid='fo2', order=1)
        comments = [record_comment(), comment('fo2', followon.body(newer),
                                             stamp='2026-10-19T05:00:00Z')]
        result = self.evaluate(comments)
        self.assertTrue(result['applies'])
        self.assertEqual(result['block']['id'], 'fo2')
        self.assertEqual(result['newest']['comment_id'], 'fo2')

    def test_a_closed_task_ends_it(self):
        self.journal(payload())
        result = self.evaluate([record_comment()], status='closed')
        self.assertFalse(result['applies'])
        self.assertIn('task is closed', result['block']['why'])

    def test_an_inert_record_reads_as_an_ordinary_direction(self):
        data = [dict(issue([record_comment()]))]
        data[0]['assignee'] = WORKER
        page = briefing.direction_page(data, 'trial', TASK, provenance_writes=True)
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['items'][0]['id'], TASK + '-cfo1')

    def test_a_withdrawal_is_the_coordinators_superseded_disposition_not_a_direction(self):
        data = [dict(issue([record_comment()]), comments=[record_comment(),
                                                          comment('fo2', followon.body(payload(operation='withdraw')))])]
        page = briefing.direction_page(data, 'trial', TASK, provenance_writes=True)
        ids = [item['id'] for item in page['items']]
        self.assertIn(TASK + '-cfo1', ids)
        self.assertNotIn(TASK + '-cfo2', ids)


class FollowonMoveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        followon.publish(Path(self.temp.name), followon.journal_entry(payload(), 'fo1', COORD, 0))

    def test_bounded_text_is_escaped_once(self):
        value = '"quoted"\nsecond\tline\\tail\x07'
        self.assertEqual(followon.view_text(value),
                         '\\"quoted\\"\\nsecond\\tline\\\\tail\\u0007')
        self.assertLessEqual(len(followon.view_text('x' * 1000)), followon.TEXT_LIMIT)

    def test_the_next_sentence_quotes_the_text_and_says_when_the_checkpoint_route_cannot_be_written(self):
        block = followon.evaluate(issue([record_comment()]), review(), scopes(),
                                  project=Path(self.temp.name))['block']
        on = followon.next_text(block, checkpoint_available=True)
        off = followon.next_text(block, checkpoint_available=False)
        self.assertIn(payload()['text'], on)
        self.assertIn('checkpoint', on)
        self.assertIn('checkpoint_provenance_writes is off', off)

    def test_move_decision_makes_an_applying_handback_the_assignees_move(self):
        move, text, role = work.move_decision('integrated', TASK, True, assignee=WORKER)
        self.assertEqual((move, role), ('coordinator', 'coordinator'))
        # The .336 sentence stays the answer with no applying record, and deliberately
        # mentions that the coordinator can now record a hand-back (kittrial-5bb.343).
        self.assertIn('records what remains', text)
        self.assertIn('hand-back', text)
        block = followon.evaluate(issue([record_comment()]), review(), scopes(),
                                  project=Path(self.temp.name))['block']
        move, text, role = work.move_decision('integrated', TASK, True, assignee=WORKER, followon=block)
        self.assertEqual((move, role), ('you', 'assignee'))
        self.assertIn(payload()['text'], text)
        # Another caller's scan still names the role, never a false "you".
        move, text, role = work.move_decision('integrated', TASK, False, assignee=WORKER, followon=block)
        self.assertEqual((move, role), ('unknown', 'assignee'))

    def test_one_open_record_is_refused_before_any_write(self):
        self.journal = followon.host_journal(Path(self.temp.name))
        issue_row = issue([record_comment()])
        writes = []
        with patch.object(followon, '_projection', return_value=(review(), scopes())), \
                patch.object(followon, 'host_journal', return_value=self.journal):
            with self.assertRaisesRegex(ValueError, 'already has an open hand-back.*Nothing was written'):
                followon._record([], issue_row, TASK, 'Second hand-back', COORD,
                                 lambda args: writes.append(args), Path(self.temp.name), None)
        self.assertEqual(writes, [])


class FollowonBackupRestoreTests(unittest.TestCase):
    """The journal round-trips through admin backup/restore (coordinator addition 2)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'projects' / 'source'
        self.destination = self.root / 'projects' / 'destination'
        self.source.mkdir(parents=True)
        self.destination.mkdir()
        (self.root / 'backups' / 'source').mkdir(parents=True)
        self.bundle = self.root / 'backups' / 'source.coordination.json'
        fake = types.SimpleNamespace(flock=lambda *a, **k: None, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        self.patch_fcntl = patch.dict(sys.modules, {'fcntl': fake})
        self.patch_fcntl.start()
        self.addCleanup(self.patch_fcntl.stop)
        import admin
        for name, value in (('backup_lifecycle_void_family', {}),
                            ('restore_lifecycle_void_family', False)):
            patcher = patch.object(admin, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_restored_project_honours_the_hand_back_and_a_missing_entry_makes_it_inert(self):
        import admin
        entry = followon.journal_entry(payload(), 'fo1', COORD, 0)
        followon.publish(self.source, entry)
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'source')
        sidecar = json.loads(self.bundle.read_text(encoding='utf-8'))
        self.assertIn('.assignee-followons/' + entry['sha256'] + '.json', sidecar['files'])
        admin.restore_coordination(self.root, 'source', 'destination')
        restored = self.destination / followon.JOURNAL_DIR / (entry['sha256'] + '.json')
        self.assertTrue(restored.is_file())
        self.assertTrue(followon.evaluate(issue([record_comment()]), review(), scopes(),
                                          project=self.destination)['applies'])
        restored.unlink()
        inert = followon.evaluate(issue([record_comment()]), review(), scopes(),
                                  project=self.destination)
        self.assertFalse(inert['applies'])
        self.assertEqual(inert['block']['state'], 'ignored')

    def test_backup_and_restore_validate_the_entry(self):
        import admin
        entry = followon.journal_entry(payload(), 'fo1', COORD, 0)
        files = {'.assignee-followons/' + entry['sha256'] + '.json': entry}
        admin.validate_coordination_files(files)
        broken = copy.deepcopy(entry)
        broken['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'hash does not match'):
            admin.validate_coordination_files({'.assignee-followons/' + entry['sha256'] + '.json': broken})


if __name__ == '__main__':
    unittest.main()
