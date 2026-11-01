"""Delegated decision rights: the record, its journal and the live check (kittrial-5bb.407).

Pure tests of delegations.py over an emulated native issue and a temporary project
directory: no bd, no endpoint. The HTTP and real-bd cases are in
test_web_records.NativeWebRecordsTests (run_delegation_suite).
"""
import json
import os
import signal
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]

import delegations as d

OWNER = 'usr_' + '1' * 16
OTHER_OWNER = 'usr_' + '2' * 16
COORD = 'usr_' + '3' * 16
AGENT = 'agent_' + 'a' * 16
SIBLING = 'agent_' + 'b' * 16
PROJECT = 'pp'
AT = ['2026-10-25T01:00:%02dZ' % n for n in range(60)]


def request(action='grant', decision='pp-dec', scope=('capabilities', 'references'), grantee='coordinator-grant',
            escalate='requirement changes and anything costing money', reason='The owner delegates acceptance.',
            title=None, operation_id='op-1'):
    return {'schema_version': 1, 'operation_id': operation_id, 'decision': decision, 'title': title,
            'action': action, 'right': 'record-acceptance', 'grantee': grantee,
            'scope': list(scope) if action != 'revoke' else [],
            'escalate': escalate if action != 'revoke' else '', 'reason': reason}


def state(owner_role='owner', disabled=False, grants=(AGENT, SIBLING)):
    return {'users': {OWNER: {'id': OWNER, 'disabled': disabled, 'display_name': 'Olive'},
                      OTHER_OWNER: {'id': OTHER_OWNER, 'display_name': 'Otto'},
                      COORD: {'id': COORD, 'display_name': 'Cora'}},
            'memberships': {PROJECT: {OWNER: owner_role, OTHER_OWNER: 'owner', COORD: 'coordinator'}},
            'agents': {AGENT: {'id': AGENT, 'name': 'Coordinator agent', 'owner': COORD, 'enabled': True},
                       SIBLING: {'id': SIBLING, 'name': 'Lane agent', 'owner': COORD, 'enabled': True}},
            'coordinator_grants': {PROJECT: {agent: {'granted_by': OWNER, 'granted_at': AT[0]} for agent in grants}}}


class Native:
    """An emulated native project: rows with comments, and the run closure the module uses."""

    def __init__(self, issue_type='decision', labels=()):
        self.rows = {'pp-dec': {'id': 'pp-dec', 'title': 'Delegate acceptance', 'issue_type': issue_type,
                                'labels': list(labels), 'comments': []}}
        self.seq = 0

    def run(self, argv):
        if argv[0] == 'list' and '--id' in argv:
            wanted = argv[argv.index('--id') + 1].split(',')
            return json.dumps([{k: v for k, v in self.rows[i].items() if k != 'comments'}
                               for i in wanted if i in self.rows])
        if argv[0] == 'show':
            ids = [token for token in argv[1:] if not token.startswith('--')]
            return json.dumps([self.rows[i] for i in ids if i in self.rows])
        if argv[0] == 'comments' and argv[1] == 'add':
            return json.dumps(self.comment(argv[2], argv[3], author=None))
        if argv[0] == 'create':
            self.seq += 1
            new = 'pp-new%d' % self.seq
            self.created_argv = list(argv)
            self.rows[new] = {'id': new, 'title': argv[argv.index('--title') + 1],
                              'issue_type': argv[argv.index('--type') + 1],
                              'labels': [], 'comments': []}
            return json.dumps(self.rows[new])
        if argv[0] == 'update' and '--add-label' in argv:
            self.rows[argv[1]]['labels'].append(argv[argv.index('--add-label') + 1])
            return json.dumps(self.rows[argv[1]])
        raise AssertionError('unexpected native call %r' % (argv,))

    def comment(self, issue, text, author):
        self.seq += 1
        if author is None:
            author = self.writer
        comment = {'id': '01a1%04d' % self.seq, 'author': author, 'text': text}
        self.rows[issue]['comments'].append(comment)
        return comment


class BlockingReadTimeout(Exception):
    pass


def bounded(call):
    previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(BlockingReadTimeout()))
    signal.setitimer(signal.ITIMER_REAL, 1)
    try:
        return call()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def make_fifo(case, path):
    if not hasattr(os, 'mkfifo'):
        case.skipTest('mkfifo is unavailable')
    os.mkfifo(path)


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / PROJECT
        self.project.mkdir()
        self.native = Native()

    def write(self, payload, writer=OWNER, via='session', at=AT[1]):
        self.native.writer = writer
        return d.apply(payload, project=PROJECT, journal=self.project, run=self.native.run, recorded_by=writer,
                       via=via, account=writer if via == 'session' else None, at=at)

    def row(self, issue='pp-dec'):
        return self.native.rows[issue]

    def check(self, agent=AGENT, kind='references', live=None, issue='pp-dec', operators=('ops',)):
        return d.check_acceptance(self.row(issue) if issue else None, project=PROJECT,
                                  state=live if live is not None else state(), journal=self.project,
                                  agent=agent, kind=kind, operators=list(operators))

    def refused(self, fragment, **kw):
        with self.assertRaises(d.DelegationRefusal) as caught:
            self.check(**kw)
        self.assertEqual(caught.exception.status, 403)
        self.assertIn(fragment.lower(), str(caught.exception).lower())

    # -- the request -------------------------------------------------------------------------
    def test_the_request_is_closed_and_one_line(self):
        d.validate_request(request())
        for broken in (dict(request(), extra=1), dict(request(), schema_version=2),
                       dict(request(), right='design-decisions'), dict(request(), grantee='lane:a'),
                       dict(request(), grantee=COORD), dict(request(), scope=['references', 'capabilities']),
                       dict(request(), scope=['tasks']), dict(request(), scope=[]),
                       dict(request(), escalate=''), dict(request(), escalate='two\nlines'),
                       dict(request(), reason=''), dict(request(), decision='bad id'),
                       request(action='revoke', scope=()) | {'scope': ['references']},
                       request(action='narrow', decision=None, title='x')):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                d.validate_request(broken)

    def test_a_grant_may_create_its_decision_and_only_a_grant(self):
        d.validate_request(request(decision=None, title='Delegate record acceptance'))
        with self.assertRaises(ValueError):
            d.validate_request(request(decision=None, title=None))
        with self.assertRaises(ValueError):
            d.validate_request(request(title='a title with an existing decision'))
        result = self.write(request(decision=None, title='Delegate record acceptance'))
        created = result['decision']
        self.assertEqual(self.native.rows[created]['issue_type'], 'decision')
        self.assertIn(d.LABEL, self.native.rows[created]['labels'])
        self.assertEqual(self.check(issue=created)['decision'], created)

    # -- the write ---------------------------------------------------------------------------
    def test_regular_file_journal_missing_damaged_and_valid_classifications(self):
        record = self.write(request())['record']
        path = self.project / d.JOURNAL_DIR / (record['sha256'] + '.json')
        self.assertTrue(d.journaled(self.project, record))
        path.write_text('{bad', encoding='utf-8')
        self.assertFalse(d.journaled(self.project, record))
        path.unlink()
        self.assertFalse(d.journaled(self.project, record))

    def test_fifo_journal_never_blocks_and_grants_nothing(self):
        record = self.write(request())['record']
        path = self.project / d.JOURNAL_DIR / (record['sha256'] + '.json')
        path.unlink()
        make_fifo(self, path)
        self.assertFalse(bounded(lambda: d.journaled(self.project, record)))

    def test_a_grant_writes_one_record_its_journal_entry_and_the_label(self):
        result = self.write(request())
        record = result['record']
        self.assertEqual((record['via'], record['account'], record['recorded_by']), ('session', OWNER, OWNER))
        self.assertTrue((self.project / d.JOURNAL_DIR / (record['sha256'] + '.json')).is_file())
        self.assertIn(d.LABEL, self.row()['labels'])
        self.assertEqual(len(self.row()['comments']), 1)
        delegation = self.check()
        self.assertEqual(delegation['scope'], ['capabilities', 'references'])

    def test_write_refusals(self):
        self.write(request())
        for payload, status in ((request(), 409),                                   # a second grant
                                (request(action='narrow', scope=('capabilities', 'references')), 409),
                                (request(action='narrow', grantee=AGENT, scope=('references',)), 409)):
            with self.subTest(payload=payload), self.assertRaises(d.DelegationRefusal) as caught:
                self.write(payload)
            self.assertEqual(caught.exception.status, status)
        self.native.rows['pp-task'] = {'id': 'pp-task', 'issue_type': 'task', 'labels': [], 'comments': []}
        with self.assertRaises(d.DelegationRefusal) as caught:
            self.write(request(decision='pp-task'))
        self.assertEqual(caught.exception.status, 422)
        with self.assertRaises(d.DelegationRefusal) as caught:
            self.write(request(decision='pp-missing'))
        self.assertEqual(caught.exception.status, 404)
        self.write(request(action='revoke', operation_id='op-r'), at=AT[2])
        with self.assertRaises(d.DelegationRefusal) as caught:
            self.write(request(action='revoke', operation_id='op-r2'), at=AT[3])
        self.assertEqual(caught.exception.status, 409)

    # -- the live check ----------------------------------------------------------------------
    def test_a_valid_delegation_admits_a_granted_agent(self):
        self.write(request())
        self.assertEqual(self.check(agent=AGENT)['grantee'], 'coordinator-grant')
        self.assertEqual(self.check(agent=SIBLING, kind='capabilities')['grantee'], 'coordinator-grant')

    def test_an_operator_written_delegation_counts_while_the_operator_is_listed(self):
        self.write(request(), writer='ops', via='operator')
        self.assertEqual(self.check(operators=('ops',))['via'], 'operator')
        self.refused('no longer a listed operator', operators=('someone-else',))

    def test_each_refusal_names_its_reason(self):
        self.refused('does not exist', issue=None)
        self.native.rows['pp-task'] = {'id': 'pp-task', 'issue_type': 'task', 'labels': [], 'comments': []}
        self.refused('not a decision issue', issue='pp-task')
        self.refused('no delegation is recorded')
        self.write(request(grantee=AGENT, scope=('references',)))
        self.refused('granted to %s, not to %s' % (AGENT, SIBLING), agent=SIBLING)
        self.refused('covers references, not capabilities', kind='capabilities')
        self.refused('holds no coordinator grant', live=state(grants=(SIBLING,)))
        self.refused('only references, capabilities and interfaces', kind='tasks')

    def test_the_agent_grant_must_be_in_effect_under_the_issuer_rule(self):
        # kittrial-5bb.410 merged into r3: a coordinator grant counts only while its issuer may
        # still give it. The delegation's own recorder (OTHER_OWNER) stays an owner throughout.
        self.write(request(), writer=OTHER_OWNER)
        live = state()
        live['memberships'][PROJECT][OWNER] = 'coordinator'          # the grants' issuer demoted
        self.refused('not in effect', live=live)
        items = d.listing([self.row()], PROJECT, live, self.project)
        self.assertEqual({a['agent']: (a['grant_in_effect'], a['covered']) for a in d.coverage(live, PROJECT, items)},
                         {AGENT: (False, False), SIBLING: (False, False)})
        live['memberships'][PROJECT][OWNER] = 'owner'                # re-promoted: in effect again
        self.assertTrue(self.check(live=live))
        self.assertTrue(all(a['covered'] for a in d.coverage(live, PROJECT, d.listing([self.row()], PROJECT, live,
                                                                                         self.project))))

    def test_the_grant_recorder_must_still_be_a_person_owner_at_use(self):
        self.write(request())
        self.refused('no longer a person owner', live=state(owner_role='coordinator'))
        self.refused('no longer a person owner', live=state(disabled=True))
        self.assertTrue(self.check(live=state()))

    def test_revocation_and_narrowing_take_effect_on_the_next_check(self):
        self.write(request())
        self.write(request(action='narrow', scope=('references',), escalate='capabilities too now',
                           operation_id='op-n'), at=AT[2])
        self.assertEqual(self.check()['escalate'], 'capabilities too now')
        self.refused('covers references, not capabilities', kind='capabilities')
        self.write(request(action='revoke', operation_id='op-r'), writer=OTHER_OWNER, at=AT[3])
        self.refused('was revoked')

    def test_a_revocation_by_a_since_demoted_owner_still_holds(self):
        self.write(request())
        self.write(request(action='revoke', operation_id='op-r'), writer=OTHER_OWNER, at=AT[2])
        live = state()
        live['memberships'][PROJECT][OTHER_OWNER] = 'contributor'
        self.refused('was revoked', live=live)

    # -- what grants nothing -----------------------------------------------------------------
    def test_an_operator_delegation_needs_that_exact_actor_listed(self):
        # Review r1, N2: another actor of the same principal does not keep it in force.
        self.write(request(), writer='ops', via='operator')
        self.assertEqual(self.check(operators=('ops',))['recorded_by'], 'ops')
        self.refused('no longer a listed operator', operators=('ops-session-2',))
        self.refused('no longer a listed operator', operators=())

    def test_a_title_cannot_read_as_a_flag(self):
        # Review r1, N3: refused before any native call, and passed as --title's value.
        for title in ('--type=task', '-x', '@attachment:0'):
            with self.subTest(title=title), self.assertRaises(ValueError):
                d.validate_request(request(decision=None, title=title))
        self.write(request(decision=None, title='Delegate record acceptance'))
        argv = self.native.created_argv
        self.assertEqual(argv[argv.index('--title') + 1], 'Delegate record acceptance')

    def test_a_narrowing_never_widens_the_scope(self):
        # Review r1, N5: even a planted, journaled "narrowing" to a wider scope changes nothing,
        # and a narrowing that is not a strict subset of the scope in force is ignored.
        self.write(request(scope=('references',)))
        wider = d.make_record(request(action='narrow', scope=('capabilities', 'references'), operation_id='op-w'),
                              project=PROJECT, recorded_by=OWNER, via='session', account=OWNER, at=AT[2])
        self.native.comment('pp-dec', d.body(wider), author=OWNER)
        d.publish(self.project, wider)
        self.assertEqual(self.check()['scope'], ['references'])
        self.refused('covers references, not capabilities', kind='capabilities')
        grant = {'scope': ['references']}
        self.assertEqual(d._scope_in_force([grant, {'action': 'narrow', 'scope': ['capabilities', 'references']}]),
                         ['references'])
        self.assertEqual(d._scope_in_force([{'scope': ['capabilities', 'references']},
                                            {'action': 'narrow', 'scope': ['capabilities']},
                                            {'action': 'narrow', 'scope': ['references']}]), ['capabilities'])

    def test_a_record_without_its_journal_entry_grants_nothing(self):
        result = self.write(request())
        (self.project / d.JOURNAL_DIR / (result['record']['sha256'] + '.json')).unlink()
        self.refused('no delegation is recorded')

    def test_lost_revoke_receipt_does_not_restore_grant(self):
        self.write(request())
        revoked = self.write(request(action='revoke', operation_id='revoke'), at=AT[2])
        self.refused('was revoked')
        (self.project / d.JOURNAL_DIR / (revoked['record']['sha256'] + '.json')).unlink()
        self.refused('unhonoured comment')
        self.assertFalse(d.listing([self.row()], PROJECT, state(), self.project, ['ops'])[0]['in_force'])

    def test_lost_narrow_receipt_does_not_restore_scope(self):
        self.write(request())
        narrowed = self.write(request(action='narrow', scope=('references',), operation_id='narrow'), at=AT[2])
        self.assertEqual(['references'], self.check()['scope'])
        (self.project / d.JOURNAL_DIR / (narrowed['record']['sha256'] + '.json')).unlink()
        self.refused('unhonoured comment', kind='capabilities')
        self.refused('unhonoured comment', kind='references')

    def test_crash_after_revoke_comment_fails_closed_and_same_decision_cannot_regrant(self):
        self.write(request())
        with mock.patch.object(d, 'publish', side_effect=OSError('interrupted')):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                self.write(request(action='revoke', operation_id='crash'), at=AT[2])
        self.refused('unhonoured comment')
        before = json.dumps(self.row(), sort_keys=True)
        for payload in (request(operation_id='another-grant'),
                        request(action='narrow', scope=('references',), operation_id='another-narrow'),
                        request(action='revoke', operation_id='another-revoke')):
            with self.assertRaisesRegex(d.DelegationRefusal, 'record a new decision'):
                self.write(payload, at=AT[3])
            self.assertEqual(before, json.dumps(self.row(), sort_keys=True))

    def test_malformed_or_wrong_project_newer_comment_refuses_without_mutation(self):
        self.write(request())
        later = d.make_record(request(action='revoke', operation_id='later'), project='other',
                              recorded_by=OWNER, via='session', account=OWNER, at=AT[2])
        for text in (d.body(later), d.PREFIX + '{', d.PREFIX + '{"at":"not-a-time"}'):
            with self.subTest(text=text):
                self.native.comment('pp-dec', text, author=OWNER)
                before = json.dumps(self.row(), sort_keys=True)
                self.refused('unhonoured comment')
                self.assertEqual(before, json.dumps(self.row(), sort_keys=True))
                self.row()['comments'].pop()

    def test_older_unhonoured_history_cannot_be_assumed_harmless(self):
        old = d.make_record(request(action='revoke', operation_id='old'), project=PROJECT,
                            recorded_by=OWNER, via='session', account=OWNER, at=AT[0])
        self.write(request(), at=AT[1])
        self.write(request(action='narrow', scope=('references',), operation_id='narrow'), at=AT[2])
        self.native.comment('pp-dec', d.body(old), author=OWNER)
        live, reason = d.current(self.row(), PROJECT, state(), self.project, ['ops'])
        self.assertIsNone(live)
        self.assertIn('unhonoured', reason)
        self.refused('unhonoured comment', kind='capabilities')
        self.assertEqual((True, False), d.reading(self.row(), PROJECT, self.project)[1:])

    def test_lost_older_grant_never_turns_an_ambiguous_history_into_authority(self):
        first = self.write(request())
        second = d.make_record(request(operation_id='second'), project=PROJECT, recorded_by=OWNER,
                               via='session', account=OWNER, at=AT[2])
        self.native.comment('pp-dec', d.body(second), author=OWNER)
        d.publish(self.project, second)
        self.refused('more than one delegation grant')
        (self.project / d.JOURNAL_DIR / (first['record']['sha256'] + '.json')).unlink()
        self.refused('unhonoured comment')

    def test_lost_older_revoke_never_revives_a_later_legacy_narrowing(self):
        self.write(request())
        revoked = self.write(request(action='revoke', operation_id='revoke'), at=AT[2])
        later = d.make_record(request(action='narrow', operation_id='legacy', scope=('references',)),
                              project=PROJECT, recorded_by=OWNER, via='session', account=OWNER, at=AT[3])
        self.native.comment('pp-dec', d.body(later), author=OWNER)
        d.publish(self.project, later)
        self.refused('was revoked')
        (self.project / d.JOURNAL_DIR / (revoked['record']['sha256'] + '.json')).unlink()
        self.refused('unhonoured comment')

    def test_tied_timestamp_native_id_and_reordered_comments_agree(self):
        self.write(request())
        revoked = self.write(request(action='revoke', operation_id='tied'), at=AT[1])
        (self.project / d.JOURNAL_DIR / (revoked['record']['sha256'] + '.json')).unlink()
        self.refused('unhonoured comment')
        self.row()['comments'].reverse()
        self.refused('unhonoured comment')
        # An unreadable ordering never becomes authority because export order changed.
        self.native.comment('pp-dec', d.PREFIX + '{}', author=OWNER)
        self.row()['comments'].reverse()
        self.refused('unhonoured comment')

    def test_equivalent_utc_spellings_use_comment_order_not_lexical_time(self):
        self.write(request(), at=AT[1])
        revoked = self.write(request(action='revoke', operation_id='utc-tie'),
                             at=AT[1].replace('Z', '+00:00'))
        self.refused('was revoked')
        (self.project / d.JOURNAL_DIR / (revoked['record']['sha256'] + '.json')).unlink()
        self.refused('unhonoured comment')
        self.row()['comments'].reverse()
        self.refused('unhonoured comment')

    def test_ambiguous_same_time_and_comment_id_never_restores_authority(self):
        self.write(request())
        last = self.row()['comments'][-1]
        bad = dict(last, text=last['text'].replace('"grant"', '"revoke"'))
        self.row()['comments'].append(bad)
        self.refused('unhonoured comment')

    def test_fresh_decision_recovery_does_not_lock_out_a_legitimate_agent(self):
        self.write(request())
        revoked = self.write(request(action='revoke', operation_id='lost'), at=AT[2])
        (self.project / d.JOURNAL_DIR / (revoked['record']['sha256'] + '.json')).unlink()
        self.refused('unhonoured comment')
        self.native.rows['pp-fresh'] = {'id': 'pp-fresh', 'issue_type': 'decision', 'labels': [], 'comments': []}
        self.write(request(decision='pp-fresh', operation_id='recovered'), at=AT[3])
        self.assertEqual(['capabilities', 'references'], self.check(issue='pp-fresh')['scope'])
        self.refused('unhonoured comment')

    def test_lost_grant_cannot_be_replaced_on_the_same_decision(self):
        granted = self.write(request())
        (self.project / d.JOURNAL_DIR / (granted['record']['sha256'] + '.json')).unlink()
        with self.assertRaisesRegex(d.DelegationRefusal, 'record a new decision'):
            self.write(request(operation_id='replacement'), at=AT[2])

    def test_a_planted_lookalike_grants_nothing(self):
        # A lane writes the exact body a person owner's record would have, under its own name
        # (the comment author is the native --actor label), with no journal entry: nothing.
        record = d.make_record(request(), project=PROJECT, recorded_by=OWNER, via='session', account=OWNER, at=AT[1])
        self.native.comment('pp-dec', d.body(record), author='lane-agent')
        self.refused('no delegation is recorded')
        # Even with a journal entry, a comment whose native author is not the recorder is not it.
        d.publish(self.project, record)
        self.refused('no delegation is recorded')
        # A record claiming an agent wrote it as a session cannot be built at all.
        with self.assertRaises(ValueError):
            d.make_record(request(), project=PROJECT, recorded_by=AGENT, via='session', account=AGENT, at=AT[1])

    def test_a_tampered_or_misplaced_record_grants_nothing(self):
        result = self.write(request())
        comment = self.row()['comments'][0]
        for text in (comment['text'].replace('"references"', '"references" '),
                     comment['text'].replace('coordinator-grant', AGENT)):
            with self.subTest(text=text[:40]):
                comment['text'] = text
                self.refused('no delegation is recorded')
        record = result['record']
        self.assertIsNone(d.parse_comment({'author': OWNER, 'text': d.body(record)}, project='other', decision='pp-dec'))
        self.assertIsNone(d.parse_comment({'author': OWNER, 'text': d.body(record)}, project=PROJECT, decision='pp-x'))

    def test_a_stored_delegation_with_a_legacy_invisible_reason_still_reads(self):
        record = d.make_record(request(), project=PROJECT, recorded_by=OWNER,
                               via='session', account=OWNER, at=AT[1])
        record['reason'] = 'Stored before the expanded rule\u061c'
        record['sha256'] = d.content_hash(record)
        comment = {'author': OWNER, 'text': d.body(record)}
        self.assertEqual(record, d.parse_comment(comment, project=PROJECT, decision='pp-dec'))
        with self.assertRaisesRegex(ValueError, 'plain text'):
            self.write(request(reason='New write\u061c'))
        self.assertEqual([], self.row()['comments'])

    def test_two_grants_on_one_decision_grant_nothing(self):
        self.write(request())
        record = d.make_record(request(operation_id='op-2', grantee=AGENT), project=PROJECT, recorded_by=OWNER,
                               via='session', account=OWNER, at=AT[2])
        self.native.comment('pp-dec', d.body(record), author=OWNER)
        d.publish(self.project, record)
        self.refused('more than one delegation grant')

    @unittest.skipIf(os.name == 'nt', 'symlinks need privileges on Windows')
    def test_a_symlinked_journal_is_not_trusted(self):
        result = self.write(request())
        journal = self.project / d.JOURNAL_DIR
        moved = Path(self.tmp.name) / 'elsewhere'
        journal.rename(moved)
        journal.symlink_to(moved, target_is_directory=True)
        self.refused('no delegation is recorded')
        self.assertFalse(d.journaled(self.project, result['record']))

    def test_the_journal_entry_validates_for_a_backup(self):
        record = self.write(request())['record']
        self.assertEqual(d.validate_entry(record, record['sha256'] + '.json'), record)
        with self.assertRaises(ValueError):
            d.validate_entry(record, '0' * 64 + '.json')
        # A field changed under the same hash is not the record the journal entry names.
        for changed in (dict(record, scope=['references']), dict(record, grantee=AGENT),
                        dict(record, account=OTHER_OWNER, recorded_by=OTHER_OWNER)):
            with self.subTest(changed=sorted(k for k in changed if changed[k] != record[k])):
                with self.assertRaises(ValueError):
                    d.validate_entry(changed, record['sha256'] + '.json')

    def test_listing_reports_in_force_and_not(self):
        self.write(request())
        self.native.rows['pp-old'] = {'id': 'pp-old', 'title': 'old', 'issue_type': 'decision', 'labels': [],
                                      'comments': []}
        self.native.writer = OWNER
        d.apply(request(decision='pp-old', operation_id='op-o'), project=PROJECT, journal=self.project,
                run=self.native.run, recorded_by=OWNER, via='session', account=OWNER, at=AT[2])
        d.apply(request(action='revoke', decision='pp-old', operation_id='op-or'), project=PROJECT,
                journal=self.project, run=self.native.run, recorded_by=OWNER, via='session', account=OWNER, at=AT[3])
        items = {item['decision']: item for item in d.listing(list(self.native.rows.values()), PROJECT, state(),
                                                              self.project)}
        self.assertTrue(items['pp-dec']['in_force'])
        self.assertFalse(items['pp-old']['in_force'])
        self.assertIn('revoked', items['pp-old']['reason'])


class ReservedTests(unittest.TestCase):
    """A raw comment cannot plant a delegation record and a contributor cannot set its label."""

    def test_every_version_of_the_prefix_and_the_label_are_reserved(self):
        import reserved_comments
        self.assertEqual(reserved_comments.DELEGATION_PREFIX, d.PREFIX)
        for text in ('Kind: delegation-v1\n{}', 'Kind: delegation-v2\n{}'):
            self.assertIsNotNone(reserved_comments.reserved_match(text), text)
        self.assertTrue(reserved_comments.reserved_label(d.LABEL))
        self.assertIsNone(reserved_comments.reserved_match('We discussed the delegation-v1 idea.'))


class RouteTests(unittest.TestCase):
    """Only the web service reaches the write action; the bound-key path is an operator's."""

    def setUp(self):
        try:
            import endpoint
        except ImportError:
            self.skipTest('endpoint.py needs fcntl')
        self.endpoint = endpoint

    def test_the_web_action_is_service_only_and_the_coordinator_command_needs_an_operator(self):
        from unittest import mock
        endpoint = self.endpoint
        self.assertIn('delegation', endpoint.SERVICE_ONLY_ACTIONS)
        self.assertIn('delegation-record', endpoint.COORDINATOR_COMMANDS)
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(endpoint, 'configured_operators', return_value=['ops']):
                endpoint.coordinator_refusal({'actor': 'ops', 'args': ['delegation-record']}, Path(root), 'person:ops')
                for member in ('lane-agent', AGENT, OWNER):
                    with self.assertRaises(ValueError):
                        endpoint.coordinator_refusal({'actor': member, 'args': ['delegation-record']},
                                                     Path(root), 'lane:a')
                with self.assertRaises(ValueError):
                    endpoint.coordinator_refusal({'actor': 'ops', 'args': ['delegation-record']}, Path(root), None)

    def test_the_coordinator_command_writes_through_the_endpoint(self):
        # Review r1, N1: the bound-key `coordinator delegation-record` write itself, end to end
        # through endpoint.execute (gate, lock, guarded write, the shared library call), over an
        # emulated native tracker.
        import subprocess
        from unittest import mock
        endpoint = self.endpoint
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'rt'
            project = root / 'projects' / 'alpha'
            (project / '.beads').mkdir(parents=True)
            (project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            (root / 'deployment.private.json').write_text(
                json.dumps({'operators': ['ops'], 'verifiers': [], 'password': 'x'}), encoding='utf-8')
            (project / '.sessions.json').write_text(
                json.dumps({'schema_version': 1, 'records': {}, 'owners': {'ops': 'person:ops', 'lane-agent': 'lane:a'}}),
                encoding='utf-8')
            native = Native()

            def run(argv, env=None, **kwargs):
                at = argv.index('--actor')
                native.writer = argv[at + 1]
                return subprocess.CompletedProcess(argv, 0, native.run(argv[at + 2:]), '')
            payload = request(decision='pp-dec', operation_id='op-coordinator-1')
            built = {'project': 'alpha', 'actor': 'ops', 'action': 'coordinator', 'operation_id': 'op-coordinator-1',
                     'args': ['delegation-record', '@attachment:0'],
                     'attachments': {'0': {'flag': '--file', 'text': json.dumps(payload)}}}
            with mock.patch.object(endpoint.native, 'run', side_effect=run):
                answer = endpoint.execute(root, built, key_principal='person:ops', key_projects=['alpha'])
            self.assertEqual(answer['returncode'], 0, answer)
            record = json.loads(answer['stdout'])['record']
            self.assertEqual((record['via'], record['recorded_by'], record['project']), ('operator', 'ops', 'alpha'))
            self.assertTrue(d.journaled(project, record))
            self.assertEqual(native.rows['pp-dec']['comments'][0]['author'], 'ops')
            # A lane behind its own bound key is refused before anything is written.
            lane = dict(built, actor='lane-agent', operation_id='op-coordinator-2')
            with self.assertRaises(ValueError):
                endpoint.execute(root, lane, key_principal='lane:a', key_projects=['alpha'])
            self.assertEqual(len(native.rows['pp-dec']['comments']), 1)

    def test_the_block_table_binds_each_operation(self):
        import http_authority
        table = http_authority.BLOCK_CAPABILITIES['delegation']
        self.assertEqual(table['list'], http_authority.CAP_READ)
        for operation in ('grant', 'narrow', 'revoke'):
            self.assertEqual(table[operation], http_authority.CAP_PROJECT_ADMIN)


class HostCommandTests(unittest.TestCase):
    """``admin.py delegation-record`` (operator, host journal) and ``delegation-check`` (read only)."""

    def setUp(self):
        import contextlib
        import io
        import types
        from unittest import mock
        self.mock, self.io, self.contextlib = mock, io, contextlib
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'projects' / PROJECT
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        fake = types.SimpleNamespace(flock=lambda *a, **k: None, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, LOCK_SH=1)
        patcher = mock.patch.dict(sys.modules, {'fcntl': fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        import admin
        self.admin = admin
        self.native = Native()
        self.calls = []

    def run_bd(self, root, project, argv):
        if argv[:1] == ['--actor']:
            self.native.writer = argv[1]
            argv = argv[2:]
        self.calls.append(argv)
        if argv[0] == 'list' and '--label' in argv:
            label = argv[argv.index('--label') + 1]
            return json.dumps([row for row in self.native.rows.values() if label in row['labels']])
        return self.native.run(argv)

    def command(self, *argv, operators=frozenset({'ops'}), live=None):
        out = self.io.StringIO()
        import http_authority
        with self.mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                self.mock.patch.object(self.admin, 'root_path', return_value=self.root), \
                self.mock.patch.object(self.admin, 'project_dir', return_value=self.project), \
                self.mock.patch.object(self.admin, 'operators', return_value=operators), \
                self.mock.patch.object(self.admin, 'run_bd', side_effect=self.run_bd), \
                self.mock.patch.object(self.admin, 'deployment_config_lock', self.contextlib.nullcontext), \
                self.mock.patch.object(http_authority, 'read_state', return_value=live or state()), \
                self.contextlib.redirect_stdout(out):
            self.admin.main()
        return json.loads(out.getvalue())

    def record(self, payload, actor='ops', operators=frozenset({'ops'})):
        path = self.root / 'payload.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return self.command('delegation-record', '--project', PROJECT, '--actor', actor, '--file', str(path),
                            operators=operators)

    def test_an_operator_records_a_delegation_with_its_journal_entry(self):
        answer = self.record(request())
        record = answer['record']
        self.assertEqual((record['via'], record['recorded_by'], record['account']), ('operator', 'ops', None))
        self.assertTrue((self.project / d.JOURNAL_DIR / (record['sha256'] + '.json')).is_file())
        self.assertEqual(self.native.rows['pp-dec']['comments'][0]['author'], 'ops')

    def test_an_actor_off_the_allowlist_records_nothing(self):
        with self.assertRaises(ValueError):
            self.record(request(), actor='lane-agent')
        self.assertEqual(self.native.rows['pp-dec']['comments'], [])
        self.assertFalse((self.project / d.JOURNAL_DIR).exists())

    def test_the_check_lists_uncovered_granted_agents_and_writes_nothing(self):
        state_file = self.root / 'state.json'
        state_file.write_text('{}', encoding='utf-8')
        answer = self.command('delegation-check', '--project', PROJECT, '--state', str(state_file))
        self.assertEqual(sorted(answer['uncovered']), sorted([AGENT, SIBLING]))
        self.record(request(grantee=AGENT, scope=('references',)))
        self.calls.clear()
        answer = self.command('delegation-check', '--project', PROJECT, '--state', str(state_file))
        self.assertEqual(answer['uncovered'], [SIBLING])
        self.assertEqual({a['agent']: a['by'] for a in answer['granted_agents']}, {AGENT: ['pp-dec'], SIBLING: []})
        self.assertFalse(any(call[0] in ('create', 'comments', 'update') for call in self.calls), self.calls)

    def test_the_backup_carries_and_validates_the_journal(self):
        record = self.record(request())['record']
        name = '.delegations/' + record['sha256'] + '.json'
        self.admin.validate_coordination_files({name: record})
        with self.assertRaises(ValueError):
            self.admin.validate_coordination_files({'.delegations/' + '0' * 64 + '.json': record})


class BackupTests(unittest.TestCase):
    """The coordination backup carries `.delegations/`, and a restore honours what it carried."""

    def setUp(self):
        import types
        from unittest.mock import patch
        self.patch = patch
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'projects' / 'source'
        self.destination = self.root / 'projects' / 'destination'
        self.source.mkdir(parents=True)
        self.destination.mkdir()
        (self.root / 'backups' / 'source').mkdir(parents=True)
        self.bundle = self.root / 'backups' / 'source.coordination.json'
        fake = types.SimpleNamespace(flock=lambda *a, **k: None, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, LOCK_SH=1)
        patcher = patch.dict(sys.modules, {'fcntl': fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        import admin
        self.admin = admin
        for name, value in (('backup_lifecycle_void_family', {}), ('restore_lifecycle_void_family', False)):
            patcher = patch.object(admin, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_restored_project_honours_the_delegation_and_a_missing_entry_voids_it(self):
        record = d.make_record(request(), project='destination', recorded_by=OWNER, via='session',
                               account=OWNER, at=AT[1])
        d.publish(self.source, record)
        with self.patch.object(self.admin, 'run_bd', return_value='synced'):
            self.admin.backup_project(self.root, 'source')
        sidecar = json.loads(self.bundle.read_text(encoding='utf-8'))
        self.assertIn(d.JOURNAL_DIR + '/' + record['sha256'] + '.json', sidecar['files'])
        self.admin.restore_coordination(self.root, 'source', 'destination')
        self.assertTrue(d.journaled(self.destination, record))
        row = {'id': 'pp-dec', 'issue_type': 'decision', 'labels': [],
               'comments': [{'id': '01a10001', 'author': OWNER, 'text': d.body(record)}]}
        live = state()
        live['memberships']['destination'] = live['memberships'][PROJECT]
        live['coordinator_grants']['destination'] = live['coordinator_grants'][PROJECT]
        self.assertEqual(d.check_acceptance(row, project='destination', state=live, journal=self.destination,
                                            agent=AGENT, kind='references')['decision'], 'pp-dec')
        (self.destination / d.JOURNAL_DIR / (record['sha256'] + '.json')).unlink()
        with self.assertRaises(d.DelegationRefusal):
            d.check_acceptance(row, project='destination', state=live, journal=self.destination,
                               agent=AGENT, kind='references')


if __name__ == '__main__':
    unittest.main()
