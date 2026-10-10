"""A worker credential does not write under a name that is somebody else's on the host (kittrial-5bb.184).

A worker credential writes under the actor namespace its issuer chose, and the tracker's
rows carry that name and nothing else. A project owner issued one named as the host
coordinator's registered session and with it created a task, claimed one, contributed,
answered the changes requested of the real coordinator on the coordinator's own task and
wrote its checkpoint. Only the shape of another account's or agent's id was refused.

The rule is ``actor_names.collision``. It is applied where a credential is issued (the
route asks the host through the backend) and where it is used (the endpoint, for every
request the web service sends with a descriptor), so a credential issued before the rule,
or a name that was put on a list afterwards, is refused when it writes.

kittrial-5bb.188 adds three things to the same rule and the same two places: a name the
project's tracker already holds as an author or assignee (item 1), a name that reads as
another (a leading or trailing dot or dash, an ``@host``; item 2), and the service's own
namespace as it was started (``--actor-namespace``; item 3). The in-process backend, which
has no endpoint, applies the same rule at use too (item 4).
"""
import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import actor_names
import http_authority
import http_service
import sessions
import test_http_review_fixes as fixes

SESSION_A = 'session-11111111-2222-3333-4444-555555555555'
SESSION_FREE = 'session-99999999-8888-7777-6666-555555555555'
HOST = {'sessions': [SESSION_A], 'operators': ['ops-lead', 'team/alice'], 'verifiers': ['verity']}
#: The four keys `admin.project_merge_slot_state` (and now `endpoint.tracker_actors`) require
#: before bd is run at all: without them bd falls back to an embedded database (rev-3 item 2).
SERVER_COORDINATES = {'dolt_server_host': '127.0.0.1', 'dolt_server_port': 13317,
                      'dolt_server_user': 'root', 'dolt_database': 'probe'}
#: (what it is, the namespace asked for, the rule it breaks)
TAKEN = (
    ('a registered session actor', SESSION_A, actor_names.SESSION),
    ('a label under it', SESSION_A + '/night', actor_names.SESSION),
    ('the same in capitals', SESSION_A.upper(), actor_names.SESSION),
    ('a name of that shape that nobody registered', SESSION_FREE, actor_names.SESSION_SHAPED),
    ('that shape with a label and capitals', SESSION_FREE.upper() + '/x', actor_names.SESSION_SHAPED),
    ('an operator', 'ops-lead', actor_names.OPERATOR),
    ('a label under an operator', 'ops-lead/night', actor_names.OPERATOR),
    ('an operator in other letters', 'OPS-Lead', actor_names.OPERATOR),
    ('the head of a listed label', 'team', actor_names.OPERATOR),
    ('another label under that head', 'team/bob', actor_names.OPERATOR),
    ('a verifier', 'verity', actor_names.VERIFIER),
    ('a label under a verifier', 'Verity/2', actor_names.VERIFIER),
    ("the service's namespace", 'http', actor_names.SERVICE),
    ("the service's read actor", 'http/read', actor_names.SERVICE),
    ("the service's namespace in capitals", 'HTTP', actor_names.SERVICE),
    # kittrial-5bb.188 item 2: names that read as another, which the review workflow reads as
    # authors other than their head. The route's own shape rule takes a name that starts with
    # a dot or a dash before the rule is asked (`_rotate` below pins those on the pure rule).
    ('a trailing dot', 'ops-lead.', actor_names.LOOKALIKE),
    ('a trailing dash', 'ops-lead-', actor_names.LOOKALIKE),
    ('an @host', 'ops-lead@desk', actor_names.LOOKALIKE),
)
#: Look-alikes the credential route refuses for their shape before the rule is consulted, so
#: they are pinned on ``actor_names.collision`` itself.
LOOKALIKE_HEADS = ('.ops-lead', '-ops-lead', '-ops-lead/night', 'ops-lead..', 'ops..lead-',
                   'ops-lead@', '@ops-lead')
#: The operator list itself takes no name with a slash (`recovery.identity`), so on a real host
#: the two rows about `team/alice` cannot arise; the rule is tested with them all the same.
REAL_TAKEN = tuple(row for row in TAKEN if not row[1].casefold().startswith('team'))
FREE = ('worker-a', 'worker-a/sub', 'ops-lead2', 'ops', 'verity.b', 'httpx', 'session-1', 'session', 'teams/alice',
        'session-11111111-2222-3333-4444-55555555555', 'xsession-11111111-2222-3333-4444-555555555555')
#: What the project's tracker holds in these tests (kittrial-5bb.188 item 1); `bd export --all`
#: answers rows, and ``actor_names.tracker_marks`` turns them into these.
TRACKER_ROWS = ({'id': 'probe-1', 'created_by': 'opus-worker-lane', 'created_at': '2026-10-06T00:00:00Z',
                 'updated_at': '2026-10-06T00:00:00Z'},
                {'id': 'probe-2', 'assignee': 'night-shift', 'created_at': '2026-10-06T01:00:00Z',
                 'updated_at': '2026-10-06T01:00:00Z',
                 'comments': [{'author': 'commenter', 'created_at': '2026-10-06T02:00:00Z'}]})


def message(response):
    return (response.data.get('error') or {}).get('message', '') if isinstance(response.data, dict) else ''


class RuleTests(unittest.TestCase):
    def test_what_is_somebody_elses(self):
        for label, name, rule in TAKEN:
            with self.subTest(name=label):
                self.assertEqual(actor_names.collision(name, **HOST), rule)

    def test_what_is_nobodys(self):
        for name in FREE:
            with self.subTest(name=name):
                self.assertIsNone(actor_names.collision(name, **HOST))
        # A credential without a namespace writes under its issuer's own account id.
        for nothing in (None, '', 7, ['ops-lead']):
            self.assertIsNone(actor_names.collision(nothing, **HOST))

    def test_with_no_host_only_the_shape_and_the_services_namespace_are_left(self):
        self.assertEqual(actor_names.collision(SESSION_A), actor_names.SESSION_SHAPED)
        self.assertEqual(actor_names.collision('http/read'), actor_names.SERVICE)
        self.assertIsNone(actor_names.collision('ops-lead'))
        # The service may have been started with another namespace; both are its own.
        self.assertEqual(actor_names.collision('web/read', service=('web', 'http')), actor_names.SERVICE)
        self.assertEqual(actor_names.collision('http', service=('web', 'http')), actor_names.SERVICE)
        self.assertIsNone(actor_names.collision('http', service=()))

    def test_names_that_are_not_names_on_a_list_take_nothing(self):
        self.assertIsNone(actor_names.collision('worker', sessions=[None, 7, ''], operators=[None, ''], verifiers=None))

    def test_the_head_is_what_the_review_workflow_compares(self):
        import review_workflow
        for name in ('Team/Alice', 'team', 'TEAM/bob/x', 'ops-lead'):
            self.assertEqual(actor_names.head(name), review_workflow.author_key(name))

    def test_inside_a_namespace(self):
        for actor, namespace, inside in (('w', 'w', True), ('w/a', 'w', True), ('w/a/b', 'w/a', True), ('w/a', 'w/', True),
                                         ('wa', 'w', False), ('w', 'w/a', False), ('W', 'w', False), ('w', '', False),
                                         ('w', None, False), (None, 'w', False)):
            with self.subTest(actor=actor, namespace=namespace):
                self.assertIs(actor_names.inside(actor, namespace), inside)

    def test_the_sentence_fits_what_the_service_hands_on_and_names_no_other_name(self):
        longest = 'a' * 64
        for rule in (actor_names.SESSION, actor_names.SESSION_SHAPED, actor_names.OPERATOR, actor_names.VERIFIER,
                     actor_names.SERVICE, actor_names.ROWS, actor_names.LOOKALIKE):
            said = actor_names.refusal(longest, rule)
            self.assertLessEqual(len(said), 200)
            self.assertTrue(said.endswith(actor_names.REVOKE))
        self.assertIn('that name', actor_names.refusal('x\ny', actor_names.OPERATOR))
        self.assertNotIn('\n', actor_names.refusal('x\ny', actor_names.OPERATOR))

    def test_a_name_that_reads_as_another_is_refused(self):
        """kittrial-5bb.188 item 2: no normal form is chosen; the look-alike is refused."""
        for name in LOOKALIKE_HEADS + tuple(row[1] for row in TAKEN if row[2] == actor_names.LOOKALIKE):
            with self.subTest(name=name):
                self.assertEqual(actor_names.LOOKALIKE, actor_names.collision(name))
                self.assertEqual(actor_names.LOOKALIKE, actor_names.collision(name, **HOST))

    def test_a_name_the_trackers_rows_already_hold_is_somebody_elses(self):
        """kittrial-5bb.188 item 1: a plain name that already writes rows is taken."""
        for name in ('opus-worker-lane', 'Opus-Worker-Lane', 'opus-worker-lane/x'):
            with self.subTest(name=name):
                self.assertEqual(actor_names.ROWS, actor_names.collision(name, authors=['opus-worker-lane']))
        self.assertIsNone(actor_names.collision('free-lane', authors=['opus-worker-lane']))
        # The host's lists and sessions are asked first, so their own rule is what is said.
        self.assertEqual(actor_names.OPERATOR,
                         actor_names.collision('ops-lead', authors=['ops-lead'], **HOST))
        self.assertEqual(actor_names.SESSION,
                         actor_names.collision(SESSION_A, authors=[SESSION_A], **HOST))
        # A look-alike is refused before any list is consulted.
        self.assertEqual(actor_names.LOOKALIKE,
                         actor_names.collision('ops-lead.', authors=['ops-lead.'], **HOST))

    def test_the_tracker_rows_are_read_as_authors_assignees_and_commenters(self):
        marks = actor_names.tracker_marks(TRACKER_ROWS)
        self.assertEqual({'opus-worker-lane', 'night-shift', 'commenter'},
                         actor_names.tracker_names(marks))
        # The boundary is the instant the credential was issued: a row older than that is
        # somebody else's, a row written after it (within the skew allowance) is its own.
        issued = '2026-10-06T00:30:00Z'
        self.assertEqual({'opus-worker-lane'}, actor_names.tracker_names(marks, before=issued))
        self.assertEqual(set(), actor_names.tracker_names(marks, before='2026-10-06T00:03:00Z'))
        self.assertEqual({'opus-worker-lane', 'night-shift'},
                         actor_names.tracker_names(marks, before='2026-10-06T02:05:00Z'))
        self.assertEqual(actor_names.tracker_names(marks, before=issued),
                         actor_names.tracker_names(marks, before=actor_names.instant(issued)))
        # A boundary that cannot be read at all is the strict reading: every name is taken.
        self.assertEqual({'opus-worker-lane', 'night-shift', 'commenter'},
                         actor_names.tracker_names(marks, before='not a time'))
        # A row with no readable time cannot be shown to be the credential's own, so it counts
        # as somebody else's; with no boundary at all every name is taken.
        undated = actor_names.tracker_marks([{'created_by': 'lane'}])
        self.assertEqual({'lane'}, actor_names.tracker_names(undated, '2026-10-06T00:00:00Z'))
        self.assertEqual({'lane'}, actor_names.tracker_names(undated))

    def test_an_instant_is_read_without_datetime_fromisoformat(self):
        """A host's Python 3.6 has no ``datetime.fromisoformat`` (kittrial-5bb.182 item 2)."""
        midnight = actor_names.instant('2026-10-06T00:00:00Z')
        for said in ('2026-10-06T00:00:00Z', '2026-10-06T00:00:00+00:00', '2026-10-06T01:00:00+01:00',
                     '2026-10-06 00:00:00', '2026-10-06T00:00:00.000000Z'):
            with self.subTest(said=said):
                self.assertEqual(midnight, actor_names.instant(said))
        self.assertEqual(midnight + 0.5, actor_names.instant('2026-10-06T00:00:00.500000Z'))
        self.assertEqual(midnight, actor_names.instant(midnight))
        for bad in ('', 'yesterday', '2026-13-06T00:00:00Z', None, [], True):
            with self.subTest(bad=bad):
                self.assertIsNone(actor_names.instant(bad))

    def test_rows_inside_an_earlier_same_name_credentials_lifetime_are_its_own(self):
        """kittrial-5bb.188 item 3: a renewal is not held by the rows its predecessor wrote."""
        marks = [('lane', actor_names.instant('2026-06-01T00:00:00Z'))]
        # Without a predecessor the older row is somebody else's.
        self.assertEqual({'lane'}, actor_names.tracker_names(marks, before='2026-10-01T00:00:00Z'))
        # Inside a live predecessor's own lifetime it is not.
        live = (actor_names.instant('2026-01-01T00:00:00Z'), None)
        self.assertEqual(set(), actor_names.tracker_names(marks, before='2026-10-01T00:00:00Z', own=[live]))
        # A revoked predecessor loans only up to its revocation.
        revoked = (actor_names.instant('2026-01-01T00:00:00Z'), actor_names.instant('2026-05-01T00:00:00Z'))
        self.assertEqual({'lane'}, actor_names.tracker_names(marks, before='2026-10-01T00:00:00Z', own=[revoked]))
        # The 300 s allowance is only for the START of a life (kittrial-5bb.188 revision-3 item 1):
        # a row 200 s before the window opened may be its own ...
        near = [('lane', actor_names.instant('2026-01-01T00:00:00Z') - 200)]
        self.assertEqual(set(), actor_names.tracker_names(near, before='2026-10-01T00:00:00Z', own=[live]))
        # ... and one 200 s AFTER the window closed is somebody else's, with no allowance at the end.
        after = [('lane', actor_names.instant('2026-05-01T00:00:00Z') + 200)]
        self.assertEqual({'lane'}, actor_names.tracker_names(after, before='2026-10-01T00:00:00Z', own=[revoked]))
        # A row with no readable time is never placed in a lifetime.
        self.assertEqual({'lane'}, actor_names.tracker_names([('lane', None)], before='2026-10-01T00:00:00Z',
                                                             own=[live]))

    def test_own_intervals_only_a_settled_same_owner_same_name_same_project_predecessor(self):
        """kittrial-5bb.188 item 3: exactly which earlier credential lends its lifetime."""
        credentials = {
            'a': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'Lane/x',
                  'created_at': '2026-01-01T00:00:00Z', 'revoked': False},
            'b': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'lane', 'created_at': '2026-02-01T00:00:00Z',
                  'revoked': True, 'revoked_at': '2026-03-01T00:00:00Z'},
            'other-owner': {'user_id': 'usr_b', 'project_id': 'probe', 'actor': 'lane',
                            'created_at': '2026-01-01T00:00:00Z'},
            'other-project': {'user_id': 'usr_a', 'project_id': 'elsewhere', 'actor': 'lane',
                              'created_at': '2026-01-01T00:00:00Z'},
            'refused': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'lane',
                        'created_at': '2026-01-01T00:00:00Z', 'actor_rows_refused': actor_names.ROWS},
            'unreadable': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'lane', 'created_at': 'not a time'},
            'self': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'lane',
                     'created_at': '2026-09-01T00:00:00Z'},
        }
        self.assertEqual([(actor_names.instant('2026-01-01T00:00:00Z'), None),
                          (actor_names.instant('2026-02-01T00:00:00Z'), actor_names.instant('2026-03-01T00:00:00Z'))],
                         sorted(actor_names.own_intervals(credentials, 'lane', 'usr_a', 'probe', exclude='self')))
        # A different owner, a different project or a different head lends nothing.
        self.assertEqual([], actor_names.own_intervals({'x': credentials['other-owner']}, 'lane', 'usr_a', 'probe'))
        self.assertEqual([], actor_names.own_intervals({'x': credentials['other-project']}, 'lane', 'usr_a', 'probe'))
        self.assertEqual([], actor_names.own_intervals({'a': credentials['a']}, 'other', 'usr_a', 'probe'))
        # A refused or unreadable predecessor fails closed: it lends nothing.
        self.assertEqual([], actor_names.own_intervals({'x': credentials['refused']}, 'lane', 'usr_a', 'probe'))
        self.assertEqual([], actor_names.own_intervals({'x': credentials['unreadable']}, 'lane', 'usr_a', 'probe'))
        # A revoked record with no readable revocation time fails closed too.
        stale = dict(credentials['b'], revoked_at=None)
        self.assertEqual([], actor_names.own_intervals({'x': stale}, 'lane', 'usr_a', 'probe'))

    def test_own_intervals_end_at_the_earlier_of_revocation_and_expiry(self):
        """kittrial-5bb.188 revision-3 item 1: an expired predecessor stops lending its name."""
        start = actor_names.instant('2026-01-01T00:00:00Z')
        expiry = actor_names.instant('2026-02-01T00:00:00Z')
        expired = {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'lane',
                   'created_at': '2026-01-01T00:00:00Z', 'expires_at': '2026-02-01T00:00:00Z'}
        # Never revoked: the lifetime still ends at the expiry, not never.
        self.assertEqual([(start, expiry)], actor_names.own_intervals({'x': expired}, 'lane', 'usr_a', 'probe'))
        # Revoked before the expiry: the revocation ends it.
        early = dict(expired, revoked=True, revoked_at='2026-01-15T00:00:00Z')
        self.assertEqual([(start, actor_names.instant('2026-01-15T00:00:00Z'))],
                         actor_names.own_intervals({'x': early}, 'lane', 'usr_a', 'probe'))
        # Revoked after the expiry: the expiry is the earlier end.
        late = dict(expired, revoked=True, revoked_at='2026-03-01T00:00:00Z')
        self.assertEqual([(start, expiry)], actor_names.own_intervals({'x': late}, 'lane', 'usr_a', 'probe'))
        # An expiry that is present but unreadable is not an open lifetime: fail closed.
        broken = dict(expired, expires_at='not a time')
        self.assertEqual([], actor_names.own_intervals({'x': broken}, 'lane', 'usr_a', 'probe'))


class DenialTests(unittest.TestCase):
    """``descriptor_actor_denial``: the endpoint's refusal at use, on the descriptor alone."""

    def setUp(self):
        self.tmp = fixes.unique_dir('cred-actor-')
        self.addCleanup(fixes.shutil.rmtree, self.tmp, ignore_errors=True)
        state = fixes.authority_state(project='probe')
        state['credentials'] = {
            # Issued under the rule: the name was free at issue, so no write of its own reads
            # the tracker again (kittrial-5bb.188 item 1).
            'cred_w': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'worker-a', 'revoked': False,
                       'actor_rows_checked': True},
            'cred_ops': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'ops-lead', 'revoked': False,
                         'actor_rows_checked': True},
            'cred_none': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': None, 'revoked': False,
                          'actor_rows_checked': True},
            'cred_agent': {'user_id': 'usr_a', 'agent_id': 'agent_0123456789abcdef', 'actor': 'worker-a', 'revoked': False},
            # Issued before the rule: no mark, and it was issued at this instant.
            'cred_old': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'opus-worker-lane', 'revoked': False,
                         'created_at': '2026-10-07T00:00:00Z'},
            # The same shape under a neutral name, for the tests added after the review of
            # revision 2 that do not need the legacy name (kittrial-5bb.202 rev-3 item 5).
            'cred_plain': {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'held-name', 'revoked': False,
                           'created_at': '2026-10-07T00:00:00Z'},
        }
        (self.tmp / 'authority.json').write_text(json.dumps(state), encoding='utf-8')
        self.config = http_authority.AuthorityConfig(str(self.tmp / 'authority.json'))
        self.asked = 0
        #: What this "host" holds as tracker rows, older than the credential at `self.before`.
        self.authors = ['opus-worker-lane']
        self.before = None

    def reserved(self, rows=False, own=()):
        self.asked += 1
        found = dict(HOST)
        if rows:
            self.before = rows
            self.own = own
            found['authors'] = list(self.authors)
        return found

    def denial(self, actor, credential=None, config='live', authority='given'):
        descriptor = {'via': 'credential' if credential else 'session', 'user_id': 'usr_a', 'session_hash': 'sess_a',
                      'project': 'probe', 'capability': 'tasks.write'}
        if credential:
            descriptor['credential_id'] = credential
        request = {'project': 'probe', 'actor': actor, 'action': 'bd', 'args': ['create', 'x']}
        if authority == 'given':
            request['authority'] = descriptor
        return http_authority.descriptor_actor_denial(request, self.config if config == 'live' else None, self.reserved)

    def refused(self, answer, words):
        self.assertIsNotNone(answer)
        self.assertEqual((126, 403), (answer['returncode'], answer.get('authority_status')), answer)
        self.assertIn(words, answer['stderr'])

    def test_a_credential_writes_inside_a_namespace_that_is_nobodys(self):
        for actor in ('worker-a', 'worker-a/sub'):
            self.assertIsNone(self.denial(actor, 'cred_w'))
        self.assertEqual(self.asked, 2)
        self.assertIsNone(self.before, 'a credential issued under the rule read the tracker')

    def test_a_credential_under_a_taken_name_is_refused_with_what_to_do(self):
        for actor in ('ops-lead', 'ops-lead/night'):
            answer = self.denial(actor, 'cred_ops')
            self.refused(answer, 'A worker credential cannot write as ops-lead: that is a name on the operator list. '
                                 'Revoke it and issue one under another name.')
            self.assertNotIn('team/alice', answer['stderr'])
            self.assertNotIn('verity', answer['stderr'])

    def test_a_look_alike_is_refused_with_nothing_run(self):
        state = json.loads((self.tmp / 'authority.json').read_text(encoding='utf-8'))
        state['credentials']['cred_dot'] = {'user_id': 'usr_a', 'project_id': 'probe', 'actor': 'ops-lead.',
                                            'revoked': False, 'actor_rows_checked': True}
        (self.tmp / 'authority.json').write_text(json.dumps(state), encoding='utf-8')
        for actor in ('ops-lead.', 'ops-lead-', 'ops-lead@desk'):
            with self.subTest(actor=actor):
                state['credentials']['cred_dot']['actor'] = actor
                (self.tmp / 'authority.json').write_text(json.dumps(state), encoding='utf-8')
                self.refused(self.denial(actor, 'cred_dot'),
                             'that is a name that reads as another. Revoke it and issue one under another name.')

    def test_a_credential_issued_before_the_rule_is_judged_by_the_trackers_rows(self):
        """kittrial-5bb.188 item 1: the reviewer's legacy `opus-worker-lane` credential stops."""
        self.refused(self.denial('opus-worker-lane', 'cred_old'),
                     'A worker credential cannot write as opus-worker-lane: that is a name this project\'s '
                     'tracker already holds. Revoke it and issue one under another name.')
        # It said so from the rows older than the credential, which is what it asked for.
        self.assertEqual('2026-10-07T00:00:00Z', self.before)

    def test_an_old_credential_whose_rows_are_its_own_goes_on_writing(self):
        self.authors = []                      # nothing older than it: the rows are its own
        self.assertIsNone(self.denial('opus-worker-lane', 'cred_old'))
        self.assertEqual('2026-10-07T00:00:00Z', self.before)

    def test_a_name_outside_the_credentials_namespace_is_refused_and_the_host_is_not_asked(self):
        for actor, credential in (('ops-lead', 'cred_w'), ('worker-ab', 'cred_w'), ('worker', 'cred_w'), ('Worker-a', 'cred_w'),
                                  (SESSION_A, 'cred_w'), ('worker-a', 'cred_none'), ('anything', 'cred_missing')):
            with self.subTest(actor=actor, credential=credential):
                self.refused(self.denial(actor, credential), 'descriptor names')
        self.assertEqual(self.asked, 0)

    def test_an_account_and_an_agent_write_under_their_own_id_only(self):
        # A signed-in account (no credential in the descriptor): the hole kittrial-5bb.181 closed in the routes.
        for actor in (SESSION_A, 'ops-lead', 'worker-a', 'nobody', 'http/read'):
            with self.subTest(actor=actor):
                self.refused(self.denial(actor), 'The actor is not the account or agent')
        self.refused(self.denial('worker-a', 'cred_agent'), 'The actor is not the account or agent')
        # A session descriptor that carries a credential id is still a session.
        request = {'project': 'probe', 'actor': 'worker-a', 'action': 'bd', 'args': [],
                   'authority': {'via': 'session', 'user_id': 'usr_a', 'session_hash': 'sess_a', 'credential_id': 'cred_w'}}
        self.refused(http_authority.descriptor_actor_denial(request, self.config, self.reserved), 'The actor is not the account')
        self.assertEqual(self.asked, 0)

    def test_what_is_not_judged_here(self):
        # The account itself; a web-shaped name (http_actor_denial judges it); the SSH path
        # (no configuration); a request without a descriptor (the service's own reads).
        self.assertIsNone(self.denial('usr_a'))
        self.assertIsNone(self.denial('usr_0123456789abcdef'))
        self.assertIsNone(self.denial('agent_0123456789abcdef/x', 'cred_agent'))
        self.assertIsNone(self.denial('ops-lead', 'cred_ops', config=None))
        self.assertIsNone(self.denial('http/read', authority=None))
        self.assertIsNone(self.denial(SESSION_A, authority=None))
        self.assertEqual(self.asked, 0)

    def test_a_state_that_cannot_be_read_refuses(self):
        (self.tmp / 'authority.json').write_text('not json', encoding='utf-8')
        answer = self.denial('worker-a', 'cred_w')
        self.assertEqual(126, answer['returncode'], answer)

    def test_an_empty_export_is_a_tracker_fault_not_an_open_rule(self):
        """kittrial-5bb.188 item 1: an export that answers no rows is not "nothing is taken"."""
        def unreadable(rows=False, own=()):
            if rows:
                raise actor_names.TrackerUnreadable()
            return dict(HOST)
        answer = http_authority.descriptor_actor_denial(
            {'project': 'probe', 'actor': 'opus-worker-lane', 'action': 'bd', 'args': ['create', 'x'],
             'authority': {'via': 'credential', 'user_id': 'usr_a', 'credential_id': 'cred_old',
                           'session_hash': 'sess_a', 'project': 'probe', 'capability': 'tasks.write'}},
            self.config, unreadable)
        self.assertEqual((2, 'tracker'), (answer['returncode'], answer.get('fault')), answer)
        self.assertNotEqual(126, answer['returncode'])

    def test_a_tracker_without_the_merge_slot_is_its_own_fault_naming_merge_create(self):
        """kittrial-5bb.202 item 1: rows without the slot are not the transient no-rows fault."""
        def slotless(rows=False, own=()):
            if rows:
                raise actor_names.TrackerMergeSlotMissing()
            return dict(HOST)
        answer = http_authority.descriptor_actor_denial(
            {'project': 'probe', 'actor': 'held-name', 'action': 'bd', 'args': ['create', 'x'],
             'authority': {'via': 'credential', 'user_id': 'usr_a', 'credential_id': 'cred_plain',
                           'session_hash': 'sess_a', 'project': 'probe', 'capability': 'tasks.write'}},
            self.config, slotless)
        self.assertEqual((2, 'merge-slot'), (answer['returncode'], answer.get('fault')), answer)
        self.assertIn('merge-create', answer['stderr'])
        self.assertNotIn('try again shortly', answer['stderr'])
        self.assertNotEqual(126, answer['returncode'])

    def test_a_settled_credential_is_not_read_against_the_tracker_again(self):
        """Items 4 and 5: checked, waived and refused marks each answer without an export."""
        state = json.loads((self.tmp / 'authority.json').read_text(encoding='utf-8'))
        state['credentials']['cred_marked'] = {'user_id': 'usr_a', 'project_id': 'probe',
                                               'actor': 'opus-worker-lane', 'revoked': False,
                                               'actor_rows_checked': True}
        state['credentials']['cred_waived'] = {'user_id': 'usr_a', 'project_id': 'probe',
                                               'actor': 'opus-worker-lane', 'revoked': False,
                                               'actor_waived': {'by': 'usr_a', 'reason': 'the lane owns it',
                                                                'at': '2026-10-07T00:00:00Z'}}
        (self.tmp / 'authority.json').write_text(json.dumps(state), encoding='utf-8')
        self.assertIsNone(self.denial('opus-worker-lane/x', 'cred_marked'))
        self.assertIsNone(self.denial('opus-worker-lane/y', 'cred_waived'))
        self.assertIsNone(self.before, 'a settled credential read the tracker')

    def test_a_refused_mark_refuses_without_a_read(self):
        """Item 5: the outcome kept on a credential is honoured before any export."""
        state = json.loads((self.tmp / 'authority.json').read_text(encoding='utf-8'))
        state['credentials']['cred_stopped'] = {'user_id': 'usr_a', 'project_id': 'probe',
                                                'actor': 'opus-worker-lane', 'revoked': False,
                                                'actor_rows_refused': actor_names.ROWS}
        (self.tmp / 'authority.json').write_text(json.dumps(state), encoding='utf-8')
        self.refused(self.denial('opus-worker-lane', 'cred_stopped'), actor_names.ROWS)
        self.assertIsNone(self.before, 'a refused credential read the tracker')


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = fixes.unique_dir('cred-registry-')
        self.addCleanup(fixes.shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_registered_actors_are_read_and_a_damaged_registry_is_not_read_as_empty(self):
        self.assertEqual(sessions.registered_actors(self.tmp), [])
        record = {'request_id': '11111111-2222-3333-4444-555555555555', 'actor': SESSION_A, 'name': 'Coordinator',
                  'created_at': '2026-10-07T00:00:00+00:00'}
        file = self.tmp / '.sessions.json'
        file.write_text(json.dumps({'schema_version': 1, 'records': {record['request_id']: record}}), encoding='utf-8')
        before = file.stat().st_mtime_ns
        self.assertEqual(sessions.registered_actors(self.tmp), [SESSION_A])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ['.sessions.json'])
        self.assertEqual(file.stat().st_mtime_ns, before)
        for damaged in ('', 'not json', '[]', '{"schema_version": 1}', '{"schema_version": 2, "records": {}}', b'\xff\xfe'):
            with self.subTest(damaged=damaged):
                file.write_bytes(damaged if isinstance(damaged, bytes) else damaged.encode('utf-8'))
                with self.assertRaises(ValueError):
                    sessions.registered_actors(self.tmp)


class HostNames:
    """What the stub endpoint's host "has": written where the stub reads it."""

    def host(self, **names):
        (self.canonical_root / 'reserved-actors.json').write_text(json.dumps(dict(HOST, **names)), encoding='utf-8')

    def project(self):
        admin = self.admin_token()
        self.alex, self.pid = self.setup_project()
        self.casey_id = self.create_account(admin, 'casey', 'casey-password-1')
        self.request('PUT', '/v1/projects/%s/members/%s' % (self.pid, self.casey_id), {'role': 'contributor'}, token=self.alex)
        self.casey = self.login('casey', 'casey-password-1')[0]
        self.credentials = '/v1/projects/%s/worker-credentials' % self.pid
        self.tasks = '/v1/projects/%s/tasks' % self.pid

    def listed(self):
        answer = self.request('GET', self.credentials + '?limit=100', token=self.alex)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data['items']

    def listed_without_asking(self):
        """The service's own credential records, without the list route's host read."""
        return [c for c in self.service.state['credentials'].values() if c.get('project_id') == self.pid]


class IssueCase(HostNames, fixes.EndpointCase):
    def test_a_name_that_is_somebody_elses_is_refused_and_nothing_is_kept(self):
        self.project()
        self.host()
        for number, (label, name, rule) in enumerate(TAKEN):
            with self.subTest(name=label):
                key = 'issue-key-%04d' % number
                refused = self.request('POST', self.credentials, {'label': 'w', 'actor': name}, token=self.alex, key=key)
                self.assertEqual(422, refused.status, refused.data)
                self.assertEqual('A worker credential cannot write as %s: that is %s. Choose another name.' % (name, rule),
                                 message(refused))
                self.assertEqual({'actor': name, 'rule': rule}, refused.data['error']['detail'])
                self.assertNotIn('secret', json.dumps(refused.data))
                # Nothing was kept, the key included: the same key issues one under a free name.
                self.assertEqual([], [item for item in self.listed() if item['actor'] == name])
                issued = self.request('POST', self.credentials, {'label': 'w', 'actor': 'free-%d' % number}, token=self.alex, key=key)
                self.assertEqual(201, issued.status, issued.data)
        self.assertEqual(len(TAKEN), len(self.listed()))

    def test_the_refusal_names_the_rule_and_none_of_the_hosts_names(self):
        self.project()
        self.host()
        refused = self.request('POST', self.credentials, {'actor': 'team'}, token=self.alex)
        self.assertEqual(422, refused.status)
        for name in ('alice', 'ops-lead', 'verity', SESSION_A):
            self.assertNotIn(name, json.dumps(refused.data))

    def test_the_superuser_waiver_does_not_cross_an_unreadable_row(self):
        """kittrial-5bb.243 r2 review item 3: `allow_actor` waives the ROW rule only -- the
        tracker is still read to bound the rows, and a row that cannot be read still
        refuses the issue. A mutant that lets the waiver skip the read must fail here."""
        self.project()
        self.host(tracker='unreadable', unreadable_rows=[{'id': 'probe-deep'}])
        admin = self.admin_token()
        refused = self.request('POST', self.credentials,
                               {'actor': 'opus-worker-lane', 'allow_actor': True,
                                'allow_actor_reason': 'the lane owns this name'}, token=admin, key='waiver-1')
        self.assertEqual(503, refused.status, refused.data)
        self.assertEqual('unreadable_rows', refused.data['error']['code'])
        self.assertIn('probe-deep', message(refused))
        self.assertIn('second try', message(refused))
        self.assertEqual([], [c for c in self.listed_without_asking() if c.get('actor') == 'opus-worker-lane'])
        # And the key is not spent: with the tracker readable again the same key issues.
        self.host(authors=['opus-worker-lane'])
        allowed = self.request('POST', self.credentials,
                               {'actor': 'opus-worker-lane', 'allow_actor': True,
                                'allow_actor_reason': 'the lane owns this name'}, token=admin, key='waiver-1')
        self.assertEqual(201, allowed.status, allowed.data)

    def test_a_name_the_trackers_rows_already_hold_is_refused_and_a_superuser_may_allow_it(self):
        """kittrial-5bb.188 items 1 and 4 at issue: the reviewer's `opus-worker-lane` case."""
        self.project()
        self.host(authors=['opus-worker-lane'])
        refused = self.request('POST', self.credentials, {'actor': 'opus-worker-lane'}, token=self.alex, key='rows-name-1')
        self.assertEqual(422, refused.status, refused.data)
        self.assertEqual({'actor': 'opus-worker-lane', 'rule': actor_names.ROWS}, refused.data['error']['detail'])
        self.assertIn(actor_names.ROWS, message(refused))
        self.assertEqual([], [c for c in self.listed_without_asking() if c.get('actor') == 'opus-worker-lane'])
        # The owner (not a superuser) may not override it, and the key is not spent.
        denied = self.request('POST', self.credentials,
                              {'actor': 'opus-worker-lane', 'allow_actor': True,
                               'allow_actor_reason': 'the lane owns this name'}, token=self.alex, key='rows-name-1')
        self.assertEqual(403, denied.status, denied.data)
        self.assertEqual([], [c for c in self.listed_without_asking() if c.get('actor') == 'opus-worker-lane'])
        # A superuser must SAY WHY the waiver is used; a reason with no waiver is refused too.
        admin = self.admin_token()
        silent = self.request('POST', self.credentials,
                              {'actor': 'opus-worker-lane', 'allow_actor': True}, token=admin)
        self.assertEqual(422, silent.status, silent.data)
        self.assertIn('allow_actor_reason', message(silent))
        orphan = self.request('POST', self.credentials,
                              {'actor': 'opus-worker-lane', 'allow_actor_reason': 'no waiver'}, token=admin)
        self.assertEqual(422, orphan.status, orphan.data)
        # A superuser may, the reason is kept, and it is its OWN mark -- never actor_rows_checked.
        allowed = self.request('POST', self.credentials,
                               {'actor': 'opus-worker-lane', 'allow_actor': True,
                                'allow_actor_reason': 'the lane owns this name'}, token=admin, key='rows-name-1')
        self.assertEqual(201, allowed.status, allowed.data)
        credential_id = allowed.data['credential']['id']
        record = self.service.state['credentials'][credential_id]
        self.assertEqual('the lane owns this name', record['actor_waived']['reason'])
        self.assertEqual(record['user_id'], record['actor_waived']['by'])
        self.assertTrue(record['actor_waived']['at'])
        self.assertFalse(record.get('actor_rows_checked'))
        self.assertEqual(record['actor_waived'], allowed.data['credential']['actor_waived'])
        # The audit entry says a waiver was used, by whom and why.
        audit = self.request('GET', '/v1/projects/%s/audit?limit=100' % self.pid, token=self.alex).data['items']
        waiver_entry = [e for e in audit if e['action'] == 'credentials.issue' and e.get('reason')
                        and 'waiver used' in e['reason']][-1]
        self.assertIn('the lane owns this name', waiver_entry['reason'])
        self.assertEqual(record['user_id'], waiver_entry['user_id'])
        # The owner's list shows it as allowed, by NAME on DATE, and not as colliding.
        item = [c for c in self.listed() if c['id'] == credential_id][0]
        self.assertIsNone(item['actor_refused'])
        self.assertIn('allowed by root-admin on', item['actor_allowed'])
        self.assertEqual('the lane owns this name', item['actor_waived']['reason'])
        # It writes: the waiver is not undone at use.
        secret = allowed.data['credential']['secret']
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'allowed'}, token=secret).status)
        # The allowance covers the row rule only, never the host's own rules.
        for name, rule in (('ops-lead', actor_names.OPERATOR), (SESSION_A, actor_names.SESSION), ('http', actor_names.SERVICE)):
            with self.subTest(name=name):
                other = self.request('POST', self.credentials,
                                     {'actor': name, 'allow_actor': True, 'allow_actor_reason': 'why not'}, token=admin)
                self.assertEqual(422, other.status, other.data)
                self.assertEqual(rule, other.data['error']['detail']['rule'])
        # `allow_actor` is a field the route takes from a superuser and refuses from anyone else.
        strange = self.request('POST', self.credentials, {'actor': 'plain-lane', 'allow_actor': 'yes'}, token=admin)
        self.assertEqual(422, strange.status, strange.data)
        self.assertEqual(201, self.request('POST', self.credentials, {'actor': 'plain-lane'}, token=admin).status)

    def test_a_waiver_reason_with_hidden_characters_or_over_500_is_refused(self):
        """kittrial-5bb.188 revision-3 item 3(2): the reason passes the kit's plain-text rule
        (the one `guidance` applies), and its 500-character bound is pinned (reviewer mutant W2)."""
        self.project()
        self.host(authors=['opus-worker-lane'])
        admin = self.admin_token()
        for label, reason in (('a bidi override', 'the lane\u202e owns this'),
                              ('a control character', 'the lane\x1b[31m owns this'),
                              ('a zero-width space', 'the lane\u200b owns this'),
                              ('a tag character', 'the lane\U000E0041 owns this')):
            with self.subTest(bad=label):
                refused = self.request('POST', self.credentials,
                                       {'actor': 'opus-worker-lane', 'allow_actor': True,
                                        'allow_actor_reason': reason}, token=admin)
                self.assertEqual(422, refused.status, refused.data)
                self.assertIn('plain text', message(refused))
        too_long = self.request('POST', self.credentials,
                                {'actor': 'opus-worker-lane', 'allow_actor': True,
                                 'allow_actor_reason': 'x' * 501}, token=admin)
        self.assertEqual(422, too_long.status, too_long.data)
        self.assertIn('at most 500 characters', message(too_long))
        # None of them was kept, and the bound itself is allowed through.
        self.assertEqual([], [c for c in self.listed_without_asking() if c.get('actor') == 'opus-worker-lane'])
        allowed = self.request('POST', self.credentials,
                               {'actor': 'opus-worker-lane', 'allow_actor': True,
                                'allow_actor_reason': 'x' * 500}, token=admin, key='waiver-500')
        self.assertEqual(201, allowed.status, allowed.data)
        self.assertEqual('x' * 500, allowed.data['credential']['actor_waived']['reason'])

    def test_a_name_the_tracker_does_not_hold_is_issued_and_the_rows_were_read(self):
        self.project()
        with mock.patch.object(self.backend, 'actor_standing', wraps=self.backend.actor_standing) as asked:
            issued = self.request('POST', self.credentials, {'actor': 'fresh-lane'}, token=self.alex)
            self.assertEqual(201, issued.status, issued.data)
            self.assertEqual(1, asked.call_count)
            self.assertIs(True, asked.call_args.kwargs['rows'])
            self.assertEqual([], asked.call_args.kwargs['own'])

    def test_a_renewal_under_its_own_name_is_not_held_by_the_predecessors_rows(self):
        """kittrial-5bb.188 item 3: the same owner renews the name its own credential wrote as."""
        self.project()
        self.host()
        issued = self.request('POST', self.credentials, {'actor': 'renew-lane'}, token=self.alex)
        self.assertEqual(201, issued.status, issued.data)
        start = '2026-01-01T00:00:00Z'
        with self.service.store.lock:
            record = self.service.state['credentials'][issued.data['credential']['id']]
            record['created_at'] = start
            record['issued_raw'] = actor_names.instant(start)
            self.service.store.save()
        # A row the predecessor wrote inside its own lifetime is not held against the renewal.
        self.host(author_rows=[{'name': 'renew-lane', 'when': '2026-06-01T00:00:00Z'}])
        again = self.request('POST', self.credentials, {'actor': 'renew-lane'}, token=self.alex)
        self.assertEqual(201, again.status, again.data)
        # A row older than the predecessor is somebody else's: the renewal is refused.
        self.host(author_rows=[{'name': 'renew-lane', 'when': '2025-01-01T00:00:00Z'}])
        refused = self.request('POST', self.credentials, {'actor': 'renew-lane'}, token=self.alex)
        self.assertEqual(422, refused.status, refused.data)
        self.assertEqual(actor_names.ROWS, refused.data['error']['detail']['rule'])

    def test_an_expired_predecessor_does_not_lend_its_name_for_ever(self):
        """kittrial-5bb.188 revision-3 item 1: a lifetime ends at the earlier of revocation and expiry."""
        self.project()
        self.host()
        issued = self.request('POST', self.credentials, {'actor': 'expired-lane'}, token=self.alex)
        self.assertEqual(201, issued.status, issued.data)
        with self.service.store.lock:
            record = self.service.state['credentials'][issued.data['credential']['id']]
            record['created_at'] = '2026-01-01T00:00:00Z'
            record['issued_raw'] = actor_names.instant('2026-01-01T00:00:00Z')
            record['expires_at'] = '2026-02-01T00:00:00Z'      # expired, and never revoked
            self.service.store.save()
        # A row the predecessor wrote inside its own lifetime is still its own.
        self.host(author_rows=[{'name': 'expired-lane', 'when': '2026-01-15T00:00:00Z'}])
        self.assertEqual(201, self.request('POST', self.credentials,
                                           {'actor': 'expired-lane'}, token=self.alex).status)
        # A host lane's row after the expiry is somebody else's: the renewal is refused.
        self.host(author_rows=[{'name': 'expired-lane', 'when': '2026-03-01T00:00:00Z'}])
        refused = self.request('POST', self.credentials, {'actor': 'expired-lane'}, token=self.alex)
        self.assertEqual(422, refused.status, refused.data)
        self.assertEqual(actor_names.ROWS, refused.data['error']['detail']['rule'])

    def test_a_lane_row_after_a_lifetime_ends_is_never_the_predecessors_own(self):
        """kittrial-5bb.188 revision-3 item 1: the 300 s allowance is only for the start of a life."""
        self.project()
        self.host()
        issued = self.request('POST', self.credentials, {'actor': 'skew-lane'}, token=self.alex)
        self.assertEqual(201, issued.status, issued.data)
        start = actor_names.instant('2026-01-01T00:00:00Z')
        end = actor_names.instant('2026-02-01T00:00:00Z')
        with self.service.store.lock:
            record = self.service.state['credentials'][issued.data['credential']['id']]
            record['created_at'] = '2026-01-01T00:00:00Z'
            record['issued_raw'] = start
            record['revoked'] = True
            record['revoked_at'] = '2026-02-01T00:00:00Z'
            self.service.store.save()
        # 200 s after the revocation: inside the old allowance, and no longer its own.
        self.host(author_rows=[{'name': 'skew-lane', 'when': end + 200}])
        refused = self.request('POST', self.credentials, {'actor': 'skew-lane'}, token=self.alex)
        self.assertEqual(422, refused.status, refused.data)
        self.assertEqual(actor_names.ROWS, refused.data['error']['detail']['rule'])
        # 200 s after the start, still inside the life: the start allowance and the window both hold.
        self.host(author_rows=[{'name': 'skew-lane', 'when': start + 200}])
        self.assertEqual(201, self.request('POST', self.credentials,
                                           {'actor': 'skew-lane'}, token=self.alex).status)

    def test_a_renewal_by_another_owner_is_still_refused(self):
        """kittrial-5bb.188 item 3: the exemption is the SAME owner's, and fails closed otherwise."""
        self.project()
        self.host()
        issued = self.request('POST', self.credentials, {'actor': 'renew-lane'}, token=self.alex)
        self.assertEqual(201, issued.status, issued.data)
        start = '2026-01-01T00:00:00Z'
        with self.service.store.lock:
            record = self.service.state['credentials'][issued.data['credential']['id']]
            record['user_id'] = 'usr_somebody_else'          # not the issuer asking now
            record['created_at'] = start
            record['issued_raw'] = actor_names.instant(start)
            self.service.store.save()
        self.host(author_rows=[{'name': 'renew-lane', 'when': '2026-06-01T00:00:00Z'}])
        refused = self.request('POST', self.credentials, {'actor': 'renew-lane'}, token=self.alex)
        self.assertEqual(422, refused.status, refused.data)
        self.assertEqual(actor_names.ROWS, refused.data['error']['detail']['rule'])

    def test_an_export_that_answers_no_rows_issues_nothing(self):
        """kittrial-5bb.188 item 1 at issue: nothing was changed, and the key stays free.
        Since kittrial-5bb.243 the unreadable-row shape is its own 503 (unreadable_rows);
        nothing is issued and the key stays free exactly the same."""
        self.project()
        self.host(tracker='unreadable')
        refused = self.request('POST', self.credentials, {'actor': 'lane-empty'}, token=self.alex, key='empty-key-1')
        self.assertEqual(503, refused.status, refused.data)
        self.assertEqual('unreadable_rows', refused.data['error']['code'])
        self.assertIn('second try', message(refused))
        self.assertEqual([], self.listed_without_asking())
        self.host()
        self.assertEqual(201, self.request('POST', self.credentials, {'actor': 'lane-empty'},
                                           token=self.alex, key='empty-key-1').status)

    def test_a_name_that_is_nobodys_is_issued_and_writes(self):
        self.project()
        self.host()
        for name in FREE:
            with self.subTest(name=name):
                issued = self.request('POST', self.credentials, {'label': 'w', 'actor': name}, token=self.alex)
                self.assertEqual(201, issued.status, issued.data)
                made = self.request('POST', self.tasks, {'title': 'by ' + name}, token=issued.data['credential']['secret'])
                self.assertEqual(201, made.status, made.data)
                self.assertEqual([name], [row.get('created_by') for row in self.canonical_rows() if row['title'] == 'by ' + name])
        self.assertEqual([None] * len(FREE), [item['actor_refused'] for item in self.listed()])

    def test_one_without_a_name_and_one_under_the_issuers_own_id_are_issued_and_the_host_is_not_asked_for_the_first(self):
        self.project()
        self.host()
        with mock.patch.object(self.backend, 'actor_standing', wraps=self.backend.actor_standing) as asked:
            issued = self.request('POST', self.credentials, {'label': 'w'}, token=self.alex)
            self.assertEqual(201, issued.status, issued.data)
            self.assertEqual(0, asked.call_count)
        own = self.request('GET', '/v1/sessions/current', token=self.alex).data['user']['id']
        self.assertEqual(201, self.request('POST', self.credentials, {'actor': own}, token=self.alex).status)
        self.assertEqual(422, self.request('POST', self.credentials, {'actor': self.casey_id}, token=self.alex).status)

    def test_a_service_started_with_another_namespace_keeps_that_one_too(self):
        self.project()
        self.host()
        with mock.patch.object(self.backend, 'actor_namespace', 'web'):
            for name in ('web', 'web/read', 'WEB/x', 'http/read'):
                with self.subTest(name=name):
                    refused = self.request('POST', self.credentials, {'actor': name}, token=self.alex)
                    self.assertEqual(422, refused.status, refused.data)
                    self.assertIn(actor_names.SERVICE, message(refused))
            self.assertEqual(201, self.request('POST', self.credentials, {'actor': 'webmaster'}, token=self.alex).status)

    def test_only_who_may_issue_one_is_told_whether_a_name_is_taken(self):
        """A member who may not issue a credential is refused as that, whatever the name: no probing of the lists."""
        self.project()
        self.host()
        with mock.patch.object(self.backend, 'actor_standing', wraps=self.backend.actor_standing) as asked:
            for name in ('ops-lead', 'worker-a'):
                refused = self.request('POST', self.credentials, {'actor': name}, token=self.casey)
                self.assertEqual(403, refused.status, refused.data)
                self.assertNotIn('operator', json.dumps(refused.data))
            outsider = self.login_new('drew')
            for name in ('ops-lead', 'worker-a'):
                self.assertEqual(404, self.request('POST', self.credentials, {'actor': name}, token=outsider).status)
            self.assertEqual(0, asked.call_count)

    def login_new(self, name):
        self.create_account(self.admin_token(), name, name + '-password-1')
        return self.login(name, name + '-password-1')[0]

    def test_a_name_with_a_bad_shape_is_refused_as_before_and_the_host_is_not_asked(self):
        self.project()
        with mock.patch.object(self.backend, 'actor_standing', wraps=self.backend.actor_standing) as asked:
            for name in ('-x', 'a b', 'x' * 65, 7, ''):
                with self.subTest(name=name):
                    refused = self.request('POST', self.credentials, {'actor': name}, token=self.alex)
                    self.assertEqual(422, refused.status, refused.data)
                    self.assertIn('Invalid credential actor namespace', message(refused))
            self.assertEqual(0, asked.call_count)

    def test_when_the_host_cannot_say_nothing_is_issued(self):
        self.project()
        (self.canonical_root / 'reserved-actors.json').write_text('not json', encoding='utf-8')
        refused = self.request('POST', self.credentials, {'actor': 'worker-a'}, token=self.alex, key='cannot-say-1')
        self.assertGreaterEqual(refused.status, 400, refused.data)
        self.assertEqual([], self.listed_without_asking())
        self.host()
        self.assertEqual(201, self.request('POST', self.credentials, {'actor': 'worker-a'}, token=self.alex, key='cannot-say-1').status)

    def test_an_answer_of_the_host_that_is_not_one_issues_nothing(self):
        self.project()
        self.host()
        real = self.backend._run

        def odd(action, *args, **kwargs):
            return ['not', 'an', 'answer'] if action == 'actor-standing' else real(action, *args, **kwargs)
        with mock.patch.object(self.backend, '_run', odd):
            for name in ('ops-lead', 'worker-a'):
                refused = self.request('POST', self.credentials, {'actor': name}, token=self.alex)
                self.assertEqual(503, refused.status, refused.data)
        self.assertEqual([], self.listed_without_asking())


class UseCase(HostNames, fixes.EndpointCase):
    """A credential that has such a name already: issued before the rule, or the name was listed afterwards."""

    def old_credential(self, name, checked=None):
        self.host(sessions=[], operators=[], verifiers=[])
        issued = self.request('POST', self.credentials, {'label': 'old', 'actor': name}, token=self.alex)
        if issued.status != 201:
            # The service's own namespace is refused whatever the host has: plant the record as an old kit left it.
            issued = self.request('POST', self.credentials, {'label': 'old', 'actor': 'placeholder-name'}, token=self.alex)
            self.assertEqual(201, issued.status, issued.data)
            with self.service.store.lock:
                self.service.state['credentials'][issued.data['credential']['id']]['actor'] = name
                self.service.store.save()
        if checked is not None:
            # As a kit before kittrial-5bb.188 left it: no `actor_rows_checked` mark.
            with self.service.store.lock:
                self.service.state['credentials'][issued.data['credential']['id']]['actor_rows_checked'] = checked
                self.service.store.save()
        self.host()
        return issued.data['credential']

    def test_every_write_is_refused_and_nothing_is_written_while_reads_go_on(self):
        self.project()
        task = self.create_task(self.alex, self.pid, 'a task').data['id']
        path = self.tasks + '/' + task
        for number, (label, name, rule) in enumerate(TAKEN):
            with self.subTest(name=label):
                secret = self.old_credential(name)['secret']
                rows = json.dumps(self.canonical_rows(), sort_keys=True)
                said = 'A worker credential cannot write as %s: that is %s. Revoke it and issue one under another name.' % (name, rule)
                checkpoint = {'intent': 'i', 'acceptance': 'a', 'summary': 's', 'next_action': 'n', 'previous': None,
                              'activity_cursor': 'x'}
                contribute = dict(fixes.CONTRIBUTION, operation='contribute', schema_version=1, operation_id='op-%d' % number,
                                  previous=None)
                for what, method, where, body in (('create', 'POST', self.tasks, {'title': 'forged'}),
                                                  ('change', 'PATCH', path, {'title': 'forged'}),
                                                  ('claim', 'POST', path + '/claim', {}),
                                                  ('checkpoint', 'POST', path + '/checkpoints', checkpoint),
                                                  ('review', 'POST', path + '/reviews', contribute)):
                    with self.subTest(write=what):
                        refused = self.request(method, where, body, token=secret, key='use-%d-%s' % (number, what))
                        self.assertEqual(403, refused.status, (what, refused.data))
                        self.assertEqual(said, message(refused))
                self.assertEqual(rows, json.dumps(self.canonical_rows(), sort_keys=True))
                # It still reads; and its owner is told in the list.
                self.assertEqual(200, self.request('GET', self.tasks, token=secret).status)
                mine = [item for item in self.listed() if item['actor'] == name]
                self.assertEqual([said], [item['actor_refused'] for item in mine])
        self.assertNotIn('unknown', [e['outcome'] for e in self.request(
            'GET', '/v1/projects/%s/audit?limit=100' % self.pid, token=self.alex).data['items']])

    def test_a_label_under_a_taken_namespace_is_refused_too(self):
        self.project()
        secret = self.old_credential('ops-lead')['secret']
        refused = self.request('POST', self.tasks, {'title': 'forged', 'actor': 'ops-lead/night'}, token=secret)
        self.assertEqual(403, refused.status, refused.data)
        self.assertIn('that is a name on the operator list', message(refused))
        self.assertEqual([], [row for row in self.canonical_rows() if row['title'] == 'forged'])

    def test_a_credential_under_a_free_name_goes_on_writing_and_one_whose_name_is_listed_later_stops(self):
        self.project()
        self.host()
        issued = self.request('POST', self.credentials, {'actor': 'night-shift'}, token=self.alex).data['credential']
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'one'}, token=issued['secret']).status)
        self.host(operators=['ops-lead', 'night-shift'])                # `operators add night-shift`, afterwards
        refused = self.request('POST', self.tasks, {'title': 'two'}, token=issued['secret'])
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual(['one'], [row['title'] for row in self.canonical_rows()])
        self.host()
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'three'}, token=issued['secret']).status)

    def test_a_revoked_one_is_not_marked_and_a_listing_that_names_none_does_not_ask_the_host(self):
        self.project()
        credential = self.old_credential('ops-lead')
        self.assertEqual([True], [bool(item['actor_refused']) for item in self.listed()])
        revoked = self.request('POST', '%s/%s/revoke' % (self.credentials, credential['id']), {}, token=self.alex)
        self.assertIn(revoked.status, (200, 204), revoked.data)
        with mock.patch.object(self.backend, 'actor_standing', wraps=self.backend.actor_standing) as asked:
            self.assertEqual([None], [item['actor_refused'] for item in self.listed()])
            self.assertEqual(0, asked.call_count)

    def test_a_credential_that_predates_the_row_rule_stops_where_the_tracker_holds_the_name(self):
        """kittrial-5bb.188 item 1 at use: the rows decide, bounded by the credential's issue."""
        self.project()
        # Two records as an older kit left them: issued under a free name, then renamed, and
        # with no `actor_rows_checked` mark.
        legacy = self.old_credential('opus-worker-lane', checked=False)
        honest = self.old_credential('night-shift', checked=False)
        self.host(author_rows=[{'name': 'opus-worker-lane',
                                'when': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 86400))},
                               {'name': 'night-shift',
                                'when': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 600))}])
        said = 'A worker credential cannot write as opus-worker-lane: that is %s. Revoke it and issue one under another name.' \
            % actor_names.ROWS
        task = self.create_task(self.alex, self.pid, 'a task').data['id']
        for what, method, where, body in (('create', 'POST', self.tasks, {'title': 'forged'}),
                                          ('claim', 'POST', self.tasks + '/' + task + '/claim', {})):
            with self.subTest(write=what):
                refused = self.request(method, where, body, token=legacy['secret'], key='rows-%s' % what)
                self.assertEqual(403, refused.status, (what, refused.data))
                self.assertEqual(said, message(refused))
        self.assertEqual([], [row for row in self.canonical_rows() if row['title'] == 'forged'])
        # The same kind of record under a name whose rows came after it was issued goes on
        # writing: a credential is not refused for its own rows.
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'mine'}, token=honest['secret']).status)
        self.assertIn('mine', [row['title'] for row in self.canonical_rows()])
        # And a credential issued now under that name is refused at issue: the rows are there.
        refused = self.request('POST', self.credentials, {'actor': 'opus-worker-lane'}, token=self.alex)
        self.assertEqual(422, refused.status, refused.data)
        self.assertEqual(actor_names.ROWS, refused.data['error']['detail']['rule'])

    def test_a_credential_issued_under_the_rule_reads_no_tracker_at_use(self):
        self.project()
        self.host()
        issued = self.request('POST', self.credentials, {'actor': 'night-shift'}, token=self.alex).data['credential']
        with mock.patch.object(self.backend, '_run', wraps=self.backend._run) as asked:
            self.assertEqual(201, self.request('POST', self.tasks, {'title': 'one'}, token=issued['secret']).status)
            self.assertEqual([], [call.args[0] for call in asked.call_args_list if call.args and call.args[0] == 'actor-standing'])

    def test_a_signed_in_account_and_an_agent_are_untouched(self):
        self.project()
        self.host()
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'by the owner'}, token=self.alex).status)
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'by a member'}, token=self.casey).status)
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/casey/kestrel',
                                                  'projects': [self.pid]}, token=self.casey)
        self.assertEqual(201, made.status, made.data)
        agent = made.data['credential']['secret']
        self.assertEqual(201, self.request('POST', self.tasks, {'title': 'by an agent'}, token=agent).status)

    def counted(self, seen):
        real = self.backend.actor_standing

        def counted(project_id, names, rows=False, own=()):
            seen.append(bool(rows))
            return real(project_id, names, rows=rows, own=own)
        return counted

    def test_an_unmarked_credential_is_judged_by_the_rows_once_and_the_outcome_kept(self):
        """kittrial-5bb.188 item 5: one export at the first write, and none after it."""
        self.project()
        legacy = self.old_credential('slow-lane', checked=False)
        self.host(author_rows=[{'name': 'slow-lane', 'when': '2026-01-01T00:00:00Z'}])
        seen = []
        with mock.patch.object(self.backend, 'actor_standing', self.counted(seen)):
            refused = self.request('POST', self.tasks, {'title': 'first'}, token=legacy['secret'])
            self.assertEqual(403, refused.status, refused.data)
            self.assertEqual([False, True], seen)       # the cheap rule, then ONE row read
            record = self.service.state['credentials'][legacy['id']]
            self.assertEqual(actor_names.ROWS, record.get('actor_rows_refused'))
            self.assertFalse(record.get('actor_rows_checked'))
            again = self.request('POST', self.tasks, {'title': 'second'}, token=legacy['secret'])
            self.assertEqual(403, again.status, again.data)
            self.assertEqual([False, True], seen)       # nothing read again
        self.assertEqual([], [row for row in self.canonical_rows() if row['title'] in ('first', 'second')])
        # The owner's list reads the kept mark, so it shows the refusal without asking the host
        # for the rows again (kittrial-5bb.188 revision-3 item 3(3)).
        mine = [item for item in self.listed() if item['id'] == legacy['id']][0]
        self.assertEqual(actor_names.refusal('slow-lane', actor_names.ROWS), mine['actor_refused'])

    def test_the_settle_honours_an_earlier_same_name_lifetimes_own_rows(self):
        """kittrial-5bb.188 revision-3 item 3(4), reviewer mutant J5: the settle that keeps the
        outcome also passes the earlier own-lifetimes, so a row inside a predecessor's life is
        not held against an unjudged credential whose only excuse is that lifetime."""
        self.project()
        first = self.old_credential('settle-lane', checked=True)
        with self.service.store.lock:
            record = self.service.state['credentials'][first['id']]
            record['created_at'] = '2026-01-01T00:00:00Z'
            record['issued_raw'] = actor_names.instant('2026-01-01T00:00:00Z')
            self.service.store.save()
        legacy = self.old_credential('settle-lane', checked=False)
        self.host(author_rows=[{'name': 'settle-lane', 'when': '2026-06-01T00:00:00Z'}])
        seen = []
        with mock.patch.object(self.backend, 'actor_standing', self.counted(seen)):
            made = self.request('POST', self.tasks, {'title': 'mine'}, token=legacy['secret'])
            self.assertEqual(201, made.status, made.data)
            self.assertEqual([False, True], seen)       # the cheap rule, then ONE row read
        record = self.service.state['credentials'][legacy['id']]
        self.assertTrue(record.get('actor_rows_checked'))
        self.assertNotIn('actor_rows_refused', record)

    def test_an_unmarked_credential_whose_own_rows_are_newer_is_checked_once_and_writes_after(self):
        """kittrial-5bb.188 item 5: the honest pre-upgrade credential pays the read once."""
        self.project()
        legacy = self.old_credential('honest-old', checked=False)
        when = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 600))
        self.host(author_rows=[{'name': 'honest-old', 'when': when}])
        seen = []
        with mock.patch.object(self.backend, 'actor_standing', self.counted(seen)):
            made = self.request('POST', self.tasks, {'title': 'mine'}, token=legacy['secret'])
            self.assertEqual(201, made.status, made.data)
            self.assertEqual([False, True], seen)
            record = self.service.state['credentials'][legacy['id']]
            self.assertTrue(record.get('actor_rows_checked'))
            self.assertNotIn('actor_rows_refused', record)
            # A later write reads no tracker, even one that cannot be read at all.
            self.host(tracker='unreadable')
            self.assertEqual(201, self.request('POST', self.tasks, {'title': 'again'}, token=legacy['secret']).status)
            self.assertEqual([False, True], seen)

    def test_an_unmarked_credential_is_not_let_through_when_the_tracker_cannot_be_read(self):
        """kittrial-5bb.188 item 1 at use: a host fault, nothing written, no mark kept."""
        self.project()
        legacy = self.old_credential('lane-empty', checked=False)
        self.host(tracker='unreadable')
        refused = self.request('POST', self.tasks, {'title': 'nope'}, token=legacy['secret'])
        self.assertEqual(503, refused.status, refused.data)
        self.assertEqual([], [row for row in self.canonical_rows() if row['title'] == 'nope'])
        record = self.service.state['credentials'][legacy['id']]
        self.assertFalse(record.get('actor_rows_checked'))
        self.assertNotIn('actor_rows_refused', record)

    def test_every_broken_tracker_answer_is_unavailable_and_changes_nothing(self):
        """kittrial-5bb.188 revision-3 item 3(1): a cut line, a nonzero bd and words that are not
        rows are host faults answered 503, not 422, at issue and at use. A tracker without its
        merge slot is 503 too, but its own, non-transient sentence naming merge-create
        (kittrial-5bb.202 item 1); a readable tracker with NO rows at all (a plain `bd init`) is
        the same non-transient answer since rev-2, not the transient one."""
        self.project()
        for mode in ('unreadable', 'cut', 'exit1', 'words', 'no-slot', 'empty'):
            with self.subTest(tracker=mode):
                self.host(tracker=mode)
                refused = self.request('POST', self.credentials, {'actor': 'lane-%s' % mode},
                                       token=self.alex, key='bad-tracker-%s' % mode)
                self.assertEqual(503, refused.status, refused.data)
                said = message(refused)
                if mode in ('no-slot', 'empty'):
                    self.assertIn('merge-create', said)
                    self.assertNotIn('try again shortly', said)
                    self.assertEqual('merge_slot_missing', refused.data['error']['code'])
                elif mode == 'unreadable':
                    # kittrial-5bb.243: a row that cannot be read is its own non-transient
                    # answer, naming the repair after a second try.
                    self.assertIn('second try', said)
                    self.assertNotIn('try again shortly', said)
                    self.assertEqual('unreadable_rows', refused.data['error']['code'])
                else:
                    self.assertIn('Nothing was changed', said)
                    self.assertEqual('unavailable', refused.data['error']['code'])
        self.assertEqual([], self.listed_without_asking())
        # At use, an unjudged credential's first write is the same host fault, with no mark.
        legacy = self.old_credential('lane-old', checked=False)
        self.host(tracker='cut')
        refused = self.request('POST', self.tasks, {'title': 'nope'}, token=legacy['secret'])
        self.assertEqual(503, refused.status, refused.data)
        record = self.service.state['credentials'][legacy['id']]
        self.assertFalse(record.get('actor_rows_checked'))
        self.assertNotIn('actor_rows_refused', record)

    def test_the_kept_outcome_cannot_be_sent_in_a_request(self):
        """kittrial-5bb.188 item 5: the keeping is not reachable from a request."""
        self.project()
        self.host()
        for body in ({'actor': 'lane-c', 'actor_rows_checked': True},
                     {'actor': 'lane-d', 'actor_rows_refused': actor_names.ROWS},
                     {'actor': 'lane-e', 'actor_waived': {'by': 'usr_a', 'reason': 'x'}}):
            refused = self.request('POST', self.credentials, body, token=self.alex)
            self.assertEqual(422, refused.status, refused.data)
            self.assertEqual([], [c for c in self.listed_without_asking() if c.get('actor') == body['actor']])
        issued = self.request('POST', self.credentials, {'actor': 'lane-f'}, token=self.alex).data['credential']
        for method in ('PATCH', 'PUT'):
            self.assertEqual(404, self.request(method, '%s/%s' % (self.credentials, issued['id']),
                                               {'actor_rows_checked': True}, token=self.alex).status)
        record = self.service.state['credentials'][issued['id']]
        self.assertTrue(record.get('actor_rows_checked'))
        self.assertNotIn('actor_waived', record)
        self.assertNotIn('actor_rows_refused', record)


class InProcessCase(fixes.Harness):
    """Without a host there are no lists: the shape of a session actor, a look-alike and the
    service's namespace are left, and the same rule is applied at use (kittrial-5bb.188 item 4)."""

    def project_with_alex(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        pid = self.create_project(alex, 'Alpha')
        return alex, '/v1/projects/%s/worker-credentials' % pid, '/v1/projects/%s/tasks' % pid

    def test_issue(self):
        alex, path, _ = self.project_with_alex()
        for name, rule in ((SESSION_A, actor_names.SESSION_SHAPED), ('http/read', actor_names.SERVICE)):
            refused = self.request('POST', path, {'actor': name}, token=alex)
            self.assertEqual(422, refused.status, refused.data)
            self.assertIn(rule, message(refused))
        for name in ('ops-lead', 'worker-a'):
            self.assertEqual(201, self.request('POST', path, {'actor': name}, token=alex).status)
        listed = self.request('GET', path, token=alex).data['items']
        self.assertEqual([None, None], [item['actor_refused'] for item in listed])

    def test_an_earlier_credential_under_a_refused_name_stops_writing(self):
        """There is no endpoint here, so an old credential under a refused name wrote on."""
        alex, path, tasks = self.project_with_alex()
        issued = self.request('POST', path, {'actor': 'legacy-lane'}, token=alex)
        self.assertEqual(201, issued.status, issued.data)
        secret = issued.data['credential']['secret']
        credential_id = issued.data['credential']['id']
        self.assertEqual(201, self.request('POST', tasks, {'title': 'before'}, token=secret).status)
        for name, rule in ((SESSION_A, actor_names.SESSION_SHAPED), ('http', actor_names.SERVICE),
                           ('lane.', actor_names.LOOKALIKE)):
            with self.subTest(name=name):
                with self.service.store.lock:                       # as an older kit left the record
                    self.service.state['credentials'][credential_id]['actor'] = name
                    self.service.store.save()
                refused = self.request('POST', tasks, {'title': 'after'}, token=secret)
                self.assertEqual(403, refused.status, refused.data)
                self.assertIn(rule, message(refused))
        self.assertEqual(['before'], [item['title'] for item in self.request('GET', tasks, token=alex).data['items']])

    def test_a_service_started_with_another_namespace_keeps_that_one_at_use_too(self):
        alex, path, tasks = self.project_with_alex()
        issued = self.request('POST', path, {'actor': 'legacy-lane'}, token=alex).data['credential']
        self.backend.actor_namespace = 'web'
        for name in ('web', 'http'):
            with self.subTest(name=name):
                with self.service.store.lock:
                    self.service.state['credentials'][issued['id']]['actor'] = name
                    self.service.store.save()
                refused = self.request('POST', tasks, {'title': 'x'}, token=issued['secret'])
                self.assertEqual(403, refused.status, refused.data)
                self.assertIn(actor_names.SERVICE, message(refused))


@unittest.skipUnless(os.name == 'posix', 'endpoint.py and admin.py import fcntl; POSIX only')
class RealEndpointCase(unittest.TestCase):
    """The endpoint itself, with its own registry and its own lists."""

    def setUp(self):
        import importlib.util
        self.tmp = fixes.unique_dir('cred-endpoint-')
        self.addCleanup(fixes.shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / 'root'
        self.project = self.root / 'projects' / 'probe'
        (self.root / 'bin').mkdir(parents=True)
        (self.project / '.beads').mkdir(parents=True)
        # Real server coordinates: `endpoint.tracker_actors` refuses to run bd without them
        # (kittrial-5bb.202 rev-3 item 2, F2: bd would fall back to an embedded database), and
        # this fixture's `bd` is a stub, so any coordinates that satisfy the guard do.
        (self.project / '.beads' / 'metadata.json').write_text(json.dumps(SERVER_COORDINATES),
                                                               encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(json.dumps(
            {'password': 'probe', 'operators': ['ops-lead'], 'verifiers': HOST['verifiers']}), encoding='utf-8')
        record = {'request_id': '11111111-2222-3333-4444-555555555555', 'actor': SESSION_A, 'name': 'Coordinator',
                  'created_at': '2026-10-07T00:00:00+00:00'}
        (self.project / '.sessions.json').write_text(json.dumps(
            {'schema_version': 1, 'records': {record['request_id']: record}}), encoding='utf-8')
        self.marker = self.tmp / 'bd-ran'
        # The default tracker answer is a whole one: a row plus the project's merge slot
        # (kittrial-5bb.188 revision-3 item 3(1)), so the free-name writes below still run.
        self.set_bd([{'id': 'probe-1', 'title': 'x'}])
        spec = importlib.util.spec_from_file_location('real_endpoint_184', str(KIT / 'endpoint.py'))
        self.endpoint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.endpoint)
        self.state = fixes.authority_state(project='probe')
        self.config_path = self.tmp / 'authority.json'
        self.config = http_authority.AuthorityConfig(str(self.config_path))

    def credential(self, name, rows_checked=None, created_at=None):
        self.state['credentials'] = {'cred_x': {
            'id': 'cred_x', 'user_id': 'usr_a', 'project_id': 'probe', 'actor': name, 'revoked': False,
            'scopes': ['tasks', 'checkpoints', 'reviews', 'feedback'], 'expires_at': time.time() + 3600}}
        if rows_checked is not None:
            self.state['credentials']['cred_x']['actor_rows_checked'] = rows_checked
        if created_at is not None:
            self.state['credentials']['cred_x']['created_at'] = created_at
        self.config_path.write_text(json.dumps(self.state), encoding='utf-8')
        return {'via': 'credential', 'user_id': 'usr_a', 'credential_id': 'cred_x', 'project': 'probe',
                'capability': 'tasks.write', 'now': time.time() + 5}

    def set_bd(self, rows, slot=True):
        """What this host's ``bd export --all`` answers: the rows the tracker holds.

        A whole tracker always carries the project's merge slot, so the stub answers it too
        (kittrial-5bb.188 revision-3 item 3(1)); ``slot=False`` plants rows without it, and
        ``set_bd_raw`` writes the shell script itself for an answer that is not rows at all.
        """
        rows = list(rows)
        if slot:
            rows.append({'id': 'probe-merge-slot', 'issue_type': 'merge-slot', 'title': 'the merge slot',
                         'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z'})
        lines = '\n'.join('echo %s' % json.dumps(json.dumps(row)) for row in rows)
        self.set_bd_raw('#!/bin/sh\necho ran >> %s\n%s\nexit 0\n' % (self.marker, lines))

    def set_bd_raw(self, script):
        """The exact shell script this host's ``bd`` runs, for an answer that is not a tracker."""
        bd = self.root / 'bin' / 'bd'
        bd.write_text(script, encoding='utf-8')
        bd.chmod(0o755)

    def write(self, actor, descriptor, number):
        request = {'project': 'probe', 'actor': actor, 'action': 'bd', 'args': ['create', 'x'],
                   'operation_id': 'op-184-%s' % number, 'authority': descriptor}
        return self.endpoint.execute(self.root, request, authority_config=self.config, require_authority=True)

    def run_main(self, request, argv_extra=()):
        """One request through the endpoint's own CLI, argparse wiring included.

        ``execute`` is called directly everywhere else; this is the seam the reviewers'
        ``--service-namespace`` mutant (U6) lives at, so it goes through ``main``.
        """
        out = io.StringIO()
        argv = ['endpoint.py', '--root', str(self.root)] + list(argv_extra)
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.endpoint.main()
        return json.loads(out.getvalue())

    def test_the_host_says_which_names_are_taken_and_never_which_names_it_has(self):
        names = [name for _, name, _ in REAL_TAKEN] + list(FREE)
        answer = self.endpoint.execute(self.root, {'project': 'probe', 'actor': 'http/read', 'action': 'actor-standing',
                                                   'args': names})
        self.assertEqual(0, answer['returncode'], answer)
        found = json.loads(answer['stdout'])['names']
        self.assertEqual({name: rule for _, name, rule in REAL_TAKEN}, {name: found[name] for _, name, _ in REAL_TAKEN})
        self.assertEqual([None] * len(FREE), [found[name] for name in FREE])
        # Only the names that were asked about are in the answer, as often as they were asked.
        for mine in ('ops-lead', 'verity', SESSION_A):
            self.assertEqual(answer['stdout'].count(mine), sum(name.count(mine) for name in names))
        self.assertFalse(self.marker.exists(), 'the tracker was asked')
        for bad in ([], ['x'] * 201, ['x', 7], [''], ['x' * 97]):
            with self.subTest(bad=str(bad)[:30]), self.assertRaises(ValueError):
                self.endpoint.execute(self.root, {'project': 'probe', 'actor': 'http/read', 'action': 'actor-standing', 'args': bad})

    def test_a_write_under_a_taken_name_is_refused_before_anything_runs(self):
        for number, (label, name, rule) in enumerate(REAL_TAKEN):
            with self.subTest(name=label):
                refused = self.write(name, self.credential(name), number)
                self.assertEqual((126, 403), (refused['returncode'], refused.get('authority_status')), refused)
                self.assertEqual('A worker credential cannot write as %s: that is %s. Revoke it and issue one under another name.\n'
                                 % (name, rule), refused['stderr'])
        self.assertFalse(self.marker.exists(), 'the native command ran')
        self.assertFalse((self.project / '.http-operations.sqlite3').exists(), 'an operation identity was reserved')

    def test_a_write_under_a_free_name_runs_and_one_outside_the_namespace_does_not(self):
        done = self.write('worker-a/sub', self.credential('worker-a'), 'free')
        self.assertEqual(0, done['returncode'], done)
        self.assertTrue(self.marker.exists())
        self.marker.unlink()
        for number, actor in enumerate(('worker-b', 'ops-lead', SESSION_A, 'worker')):
            refused = self.write(actor, self.credential('worker-a'), 'out-%d' % number)
            self.assertEqual((126, 403), (refused['returncode'], refused.get('authority_status')), refused)
        refused = self.write('ops-lead', fixes.authority_descriptor('usr_a', 'sess_a', project='probe'), 'session')
        self.assertEqual((126, 403), (refused['returncode'], refused.get('authority_status')), refused)
        self.assertFalse(self.marker.exists(), 'the native command ran')

    def test_the_ssh_path_is_as_it_was(self):
        """No launch configuration: the coordinator itself, and anybody, writes under the name it gives."""
        for actor in (SESSION_A, 'ops-lead', 'worker-a'):
            done = self.endpoint.execute(self.root, {'project': 'probe', 'actor': actor, 'action': 'bd', 'args': ['create', 'x']})
            self.assertEqual(0, done['returncode'], done)

    def test_a_plain_name_the_tracker_holds_is_refused_at_use(self):
        """kittrial-5bb.188 item 1 on the endpoint: the reviewer's `opus-worker-lane` credential."""
        self.set_bd([{'id': 'probe-1', 'created_by': 'opus-worker-lane',
                      'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z'}])
        refused = self.write('opus-worker-lane', self.credential('opus-worker-lane', rows_checked=False,
                                                                 created_at='2026-10-07T00:00:00Z'), 'rows')
        self.assertEqual((126, 403), (refused['returncode'], refused.get('authority_status')), refused)
        self.assertEqual('A worker credential cannot write as opus-worker-lane: that is %s. Revoke it and issue one '
                         'under another name.\n' % actor_names.ROWS, refused['stderr'])
        self.assertFalse((self.project / '.http-operations.sqlite3').exists(), 'an operation identity was reserved')
        # A credential issued under the rule carries the mark and is not judged by the rows again:
        # every row under the name from now on is its own.
        self.marker.unlink()
        done = self.write('opus-worker-lane', self.credential('opus-worker-lane', rows_checked=True), 'marked')
        self.assertEqual(0, done['returncode'], done)
        self.assertTrue(self.marker.exists())

    def test_actor_standing_reads_the_rows_only_when_it_is_asked_for_them(self):
        self.set_bd([{'id': 'probe-1', 'assignee': 'opus-worker-lane', 'created_at': '2026-01-01T00:00:00Z',
                      'updated_at': '2026-01-01T00:00:00Z'}])
        plain = self.endpoint.execute(self.root, {'project': 'probe', 'actor': 'http/read',
                                                  'action': 'actor-standing', 'args': ['opus-worker-lane', 'worker-a']},
                                      authority_config=self.config)
        self.assertEqual({'opus-worker-lane': None, 'worker-a': None}, json.loads(plain['stdout'])['names'])
        self.assertFalse(self.marker.exists(), 'the tracker was asked without being asked')
        asked = self.endpoint.execute(self.root, {'project': 'probe', 'actor': 'http/read', 'action': 'actor-standing',
                                                  'args': ['opus-worker-lane', 'worker-a'], 'tracker': True},
                                      authority_config=self.config)
        self.assertEqual({'opus-worker-lane': actor_names.ROWS, 'worker-a': None},
                         json.loads(asked['stdout'])['names'])
        self.assertTrue(self.marker.exists())

    def test_an_empty_tracker_answers_the_missing_slot_not_the_transient_fault(self):
        """kittrial-5bb.202 rev-2, the empty-project decision: a readable tracker with NO rows at
        all (a plain `bd init`, whose `bd export --all` exits 0 and prints nothing) is not "the
        tracker holds no names" and not the transient host fault either. The read succeeded and
        what is absent is the merge-slot row, so the answer is the non-transient merge-slot
        fault naming merge-create, at use and through the endpoint CLI. A tracker that cannot be
        read keeps the transient answer (the cut/exit1/words shapes in the test below)."""
        self.set_bd([], slot=False)
        with self.assertRaises(actor_names.TrackerMergeSlotMissing):
            self.endpoint.tracker_actors(self.root, self.project)
        # At use, the descriptor path answers the non-transient fault, not "nothing is taken".
        answer = self.write('opus-worker-lane', self.credential('opus-worker-lane', rows_checked=False,
                                                                created_at='2026-10-07T00:00:00Z'), 'empty')
        self.assertEqual((2, 'merge-slot'), (answer['returncode'], answer.get('fault')), answer)
        self.assertIn('merge-create', answer['stderr'])
        self.assertNotIn('try again shortly', answer['stderr'])
        self.assertFalse((self.project / '.http-operations.sqlite3').exists(), 'an operation identity was reserved')
        # The service's own read of the rows answers the same marked fault, through the endpoint CLI.
        told = self.run_main({'project': 'probe', 'actor': 'http/read', 'action': 'actor-standing',
                              'args': ['opus-worker-lane'], 'tracker': True},
                             ['--authority-store', str(self.config_path)])
        self.assertEqual((2, 'merge-slot'), (told['returncode'], told.get('fault')), told)

    def test_a_project_with_no_server_coordinates_is_the_transient_fault(self):
        """kittrial-5bb.202 rev-3 item 2 (F2): `tracker_actors` now applies the same guard
        `admin.project_merge_slot_state` does.

        Without the Dolt server coordinates bd falls back to an embedded database, exits 0 and
        prints nothing, which revision 2 read as the empty-project answer, `merge_slot_missing`,
        and advised a merge-create that then fails (the reviewer's g4/g5). It is the host fault:
        `TrackerUnreadable`, answered 503 `unavailable` with the transient sentence, as on main.
        """
        (self.project / '.beads' / 'metadata.json').write_text(json.dumps(
            {'dolt_server_host': '127.0.0.1', 'dolt_server_port': 13317, 'dolt_server_user': 'root'}),
            encoding='utf-8')                      # the database name is not recorded
        with self.assertRaises(actor_names.TrackerUnreadable):
            self.endpoint.tracker_actors(self.root, self.project)
        answer = self.write('worker-a', self.credential('worker-a', rows_checked=False,
                                                        created_at='2026-10-07T00:00:00Z'), 'coords')
        self.assertEqual((2, 'tracker'), (answer['returncode'], answer.get('fault')), answer)
        self.assertNotEqual('merge-slot', answer.get('fault'))
        self.assertFalse(self.marker.exists(), 'bd ran without the server coordinates')
        # And through the endpoint's own CLI, the same mark (the service answers fault 'tracker'
        # with its 503 `unavailable` sentence, exactly as on main; the merge-slot mark, which
        # revision 2 gave this shape, is the one that must not appear).
        told = self.run_main({'project': 'probe', 'actor': 'http/read', 'action': 'actor-standing',
                              'args': ['worker-a'], 'tracker': True},
                             ['--authority-store', str(self.config_path)])
        self.assertEqual((2, 'tracker'), (told['returncode'], told.get('fault')), told)
        self.assertNotEqual('merge-slot', told.get('fault'))

    def test_the_real_endpoint_marks_the_missing_slot_on_its_own_cli(self):
        """kittrial-5bb.202 review `one-test-through-the-real-route`: mutant M2, endpoint.py's
        `main` marking the missing slot as `tracker` instead of `merge-slot`, must fail here.

        The stub-backed HTTP tests cannot catch it: ``tools/strict_canonical_endpoint.py``
        carries its own copy of the mark. This drives the endpoint the service really launches,
        ``endpoint.py main()``, for the shape the reviewer's M2 attacks (rows that came back
        without the merge-slot row) and for the empty tracker.
        """
        self.set_bd([{'id': 'probe-1', 'created_by': 'held-name',
                      'created_at': '2026-01-01T00:00:00Z'}], slot=False)
        answered = self.run_main({'project': 'probe', 'actor': 'http/read', 'action': 'actor-standing',
                                  'args': ['held-name'], 'tracker': True},
                                 ['--authority-store', str(self.config_path)])
        self.assertEqual((2, 'merge-slot'), (answered['returncode'], answered.get('fault')), answered)
        self.assertIn('merge-create', answered['stderr'])
        self.assertNotIn('try again shortly', answered['stderr'])
        # And the use path through the CLI: the same mark, not UNREAD's.
        descriptor = self.credential('worker-a', rows_checked=False, created_at='2026-10-07T00:00:00Z')
        self.set_bd([], slot=False)
        written = self.run_main({'project': 'probe', 'actor': 'worker-a', 'action': 'bd',
                                 'args': ['create', 'x'], 'operation_id': 'op-cli-merge-slot',
                                 'authority': descriptor},
                                ['--authority-store', str(self.config_path), '--require-authority'])
        self.assertEqual((2, 'merge-slot'), (written['returncode'], written.get('fault')), written)

    def test_an_answer_without_the_merge_slot_or_not_rows_at_all_is_a_host_fault(self):
        """kittrial-5bb.188 revision-3 item 3(1): a whole read requires the merge slot, and an
        answer that is a cut line, a nonzero bd or words that are not rows is a host fault.
        kittrial-5bb.202 item 1: rows that came back without the slot row are their own,
        non-transient fault naming the merge-create repair.
        kittrial-5bb.221 revision 2: the cut-line and not-rows fixtures carry the merge slot
        too, so they pin the unreadable row (and the slot requirement), not only its absence."""
        row = json.dumps(json.dumps({'id': 'probe-1', 'created_by': 'held-name',
                                     'created_at': '2026-01-01T00:00:00Z'}))
        slot = json.dumps(json.dumps({'id': 'probe-merge-slot', 'issue_type': 'merge-slot',
                                      'title': 'the merge slot', 'created_at': '2026-01-01T00:00:00Z'}))
        shapes = (
            ('rows but no merge slot', '#!/bin/sh\necho %s\nexit 0\n' % row,
             actor_names.TrackerMergeSlotMissing, 'merge-slot'),
            ('the last line cut short', '#!/bin/sh\necho %s\necho %s\necho \'{"id": "probe-2", "created_\'\nexit 0\n'
                                        % (row, slot),
             actor_names.TrackerRowsUnreadable, 'unreadable-rows'),
            ('exit 1 with an error line', '#!/bin/sh\necho "bd: the database is locked" >&2\nexit 1\n',
             actor_names.TrackerUnreadable, 'tracker'),
            ('words that are not rows', '#!/bin/sh\necho %s\necho "not a row at all"\nexit 0\n' % row,
             actor_names.TrackerRowsUnreadable, 'unreadable-rows'),
            # kittrial-5bb.202 rev-3 item 4 (F5): JSON that PARSES but is not rows at all. The
            # export succeeded and said nothing about this project's tracker, so it is the
            # transient host fault, never the missing-slot answer. Mutant N8 (`if False:` in
            # place of the guard) answers merge_slot_missing for these and must fail here.
            ('JSON null, not rows', '#!/bin/sh\necho null\nexit 0\n',
             actor_names.TrackerUnreadable, 'tracker'),
            ('JSON number, not rows', '#!/bin/sh\necho 1\nexit 0\n',
             actor_names.TrackerUnreadable, 'tracker'),
            ('JSON array of non-rows', '#!/bin/sh\necho \'[1, 2]\'\nexit 0\n',
             actor_names.TrackerUnreadable, 'tracker'),
        )
        for number, (label, script, fault_class, fault) in enumerate(shapes):
            with self.subTest(answer=label):
                self.set_bd_raw(script)
                with self.assertRaises(fault_class):
                    self.endpoint.tracker_actors(self.root, self.project)
                answer = self.write('worker-a', self.credential('worker-a', rows_checked=False,
                                                                created_at='2026-10-07T00:00:00Z'), 'bad-%d' % number)
                self.assertEqual((2, fault), (answer['returncode'], answer.get('fault')), answer)
                if fault == 'merge-slot':
                    self.assertIn('merge-create', answer['stderr'])
                    self.assertNotIn('try again shortly', answer['stderr'])
                self.assertFalse((self.project / '.http-operations.sqlite3').exists(),
                                 'an operation identity was reserved')

    def _whole_tracker_script(self, *extra_lines, slot=True):
        """A tracker answer as lines: one healthy row, any extra lines, the merge slot."""
        healthy = json.dumps({'id': 'probe-1', 'created_by': 'opus-worker-lane',
                              'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z'})
        lines = [healthy] + list(extra_lines)
        if slot:
            lines.append(json.dumps({'id': 'probe-merge-slot', 'title': 'the merge slot',
                                     'issue_type': 'merge-slot', 'created_at': '2026-01-01T00:00:00Z',
                                     'updated_at': '2026-01-01T00:00:00Z'}))
        script = '\n'.join('echo %s' % json.dumps(line) for line in lines)
        self.set_bd_raw('#!/bin/sh\necho ran >> %s\n%s\nexit 0\n' % (self.marker, script))

    def test_a_line_that_is_not_an_object_beside_real_rows_refuses(self):
        """kittrial-5bb.243 item N4: a JSON line that is a number, a string or a list holding
        a real row, each beside healthy rows and the merge slot, refuses the whole read. The
        list is the mutant that mattered: on the release before kittrial-5bb.221 r2, the
        names inside such a list were silently free (a non-dict row is not a mark to the
        name reader), so nothing pinned that they refuse."""
        shapes = (
            ('a number', '2026'),
            ('a string', '"just a string"'),
            ('a list holding a real row',
             '[{"id": "probe-in-a-list", "created_by": "hidden-name", "created_at": "2026-01-01T00:00:00Z"}]'),
        )
        for label, line in shapes:
            with self.subTest(line=label):
                self._whole_tracker_script(line)
                with self.assertRaises(actor_names.TrackerRowsUnreadable):
                    self.endpoint.tracker_actors(self.root, self.project)
                answer = self.write('worker-a', self.credential('worker-a', rows_checked=False,
                                                                created_at='2026-10-07T00:00:00Z'),
                                    'non-object-%s' % label.split()[0])
                self.assertEqual((2, 'unreadable-rows'), (answer['returncode'], answer.get('fault')), answer)
                # The sentence names the repair, never "try again shortly".
                self.assertNotIn('try again shortly', answer['stderr'])
                self.assertIn('--unset-metadata', answer['stderr'])

    def test_the_unreadable_row_ids_are_bounded_and_checked(self):
        """kittrial-5bb.243 r2 review item 1: a thousand unreadable rows name five ids and
        count the rest, an id not of the tracker's shape is never printed, and the web
        answer is built from the fault's fields, not from a stderr tail a row shaped."""
        thousand = ['{"id": "probe-%d", "created_by": "x", "metadata": %s}' % (i, '[' * 900 + ']' * 900)
                    for i in range(1000)]
        hostile = ('{"id": "' + 'x' * 500 + '\\n\\r\\u202e", "created_by": "y", "metadata": '
                   + '[' * 900 + ']' * 900 + '}')
        with self.subTest(answer='a thousand rows'):
            self._whole_tracker_script(*thousand)
            raised = None
            try:
                self.endpoint.tracker_actors(self.root, self.project)
            except actor_names.TrackerRowsUnreadable as error:
                raised = error
            self.assertIsNotNone(raised)
            self.assertEqual(5, len(raised.ids))
            self.assertLess(len(str(raised)), 600)
            self.assertIn('and 995 more', str(raised))
            answer = self.write('worker-a', self.credential('worker-a', rows_checked=False,
                                                            created_at='2026-10-07T00:00:00Z'), 'thousand')
            self.assertEqual((2, 'unreadable-rows'), (answer['returncode'], answer.get('fault')), answer)
            self.assertEqual(5, len(answer['unreadable_rows']))
            self.assertLess(len(answer['stderr']), 600)
        with self.subTest(answer='a hostile id'):
            self._whole_tracker_script(hostile)
            raised = None
            try:
                self.endpoint.tracker_actors(self.root, self.project)
            except actor_names.TrackerRowsUnreadable as error:
                raised = error
            self.assertIsNotNone(raised)
            self.assertIsNone(raised.ids)               # not of the tracker's id shape: counted, not printed
            self.assertNotIn('x' * 50, str(raised))
            import http_service
            failure = http_service.EndpointBackend._rows_unreadable(['x' * 500 + '\\n' + '\\u202e'])
            self.assertNotIn('x' * 50, failure.message)  # built from fields, never a stderr tail
            self.assertEqual('unreadable_rows', failure.code)

    def test_one_row_nested_65_levels_is_read_and_its_names_count(self):
        """kittrial-5bb.221: the 64-level comment guard refused the whole project's read on
        one row nested 65 levels. The row bound of kittrial-5bb.141 (750) applies: this row
        parses normally, its own author counts beside the healthy ones, and the rule that
        depends on the read works again."""
        import record_json
        deep = ('{"id": "probe-deep", "created_by": "deep-author", "created_at": "2026-01-01T00:00:00Z",'
                ' "updated_at": "2026-01-01T00:00:00Z", "metadata": ' + '{"a":' * 64 + '1' + '}' * 64 + '}')
        self.assertEqual(65, record_json.nesting(deep))       # what the comment guard refused
        self._whole_tracker_script(deep)
        self.assertEqual({'opus-worker-lane', 'deep-author'},
                         set(self.endpoint.tracker_actors(self.root, self.project)))
        # The healthy row's name is still held against a credential at use ...
        refused = self.write('opus-worker-lane', self.credential('opus-worker-lane', rows_checked=False,
                                                                 created_at='2026-10-07T00:00:00Z'), 'deep-65')
        self.assertEqual((126, 403), (refused['returncode'], refused.get('authority_status')), refused)
        # ... and a free name writes, where this row used to fault the whole project.
        self.marker.unlink()
        done = self.write('worker-a/sub', self.credential('worker-a'), 'deep-65-free')
        self.assertEqual(0, done['returncode'], done)
        self.assertTrue(self.marker.exists())

    def test_any_unreadable_row_refuses_the_whole_read_as_main(self):
        """kittrial-5bb.221 revision 2, review item 1: a row past the bound is marked, and
        ANY marked row refuses the whole read -- the unreadable row may be the row that
        holds the name, and a name the tracker might hold must not become issuable through
        a worker credential. A credential under the marked row's own name gets the host
        fault, never a pass."""
        import record_json
        deeper = ('{"id": "probe-deeper", "created_by": "deep-author", "created_at": "2026-01-01T00:00:00Z",'
                  ' "updated_at": "2026-01-01T00:00:00Z", "metadata": ' + '{"a":' * 750 + '1' + '}' * 750 + '}')
        self.assertEqual(record_json.ROW_NESTING_MAX + 1,
                         record_json.nesting(deeper, record_json.ROW_NESTING_MAX))
        self._whole_tracker_script(deeper)
        with self.assertRaises(actor_names.TrackerUnreadable):
            self.endpoint.tracker_actors(self.root, self.project)
        # The write path answers the unreadable-rows fault for every name: the taken name
        # is not refused (it cannot be read), and a free name does not pass either. Since
        # kittrial-5bb.243 N7 the answer carries the row id and the operator repair, never
        # the bare fault's "try again shortly".
        for number, name in enumerate(('opus-worker-lane', 'worker-a/sub', 'deep-author')):
            answer = self.write(name, self.credential(name.split('/')[0], rows_checked=False,
                                                       created_at='2026-10-07T00:00:00Z'), 'deep-751-%d' % number)
            self.assertEqual((2, 'unreadable-rows'), (answer['returncode'], answer.get('fault')), answer)
            self.assertIn('probe-deeper', answer['stderr'])
            self.assertIn('--unset-metadata', answer['stderr'])
        self.assertFalse((self.project / '.http-operations.sqlite3').exists(),
                         'an operation identity was reserved')
        # The issue path (actor-standing with rows) answers the same fault, with the row id.
        told = self.run_main({'project': 'probe', 'actor': 'http/read', 'action': 'actor-standing',
                              'args': ['opus-worker-lane'], 'tracker': True},
                             ['--authority-store', str(self.config_path)])
        self.assertEqual((2, 'unreadable-rows'), (told['returncode'], told.get('fault')), told)
        self.assertIn('probe-deeper', told['stderr'])

    def test_non_row_answers_with_exit_zero_refuse_even_with_the_slot(self):
        """kittrial-5bb.221 revision 2, review item 2: with bd exiting 0, answers that are
        not whole trackers still refuse -- a line cut mid-row, a row replaced by an error
        sentence, every line cut, a 5,000-digit number (over the interpreter's integer
        digit limit), and a junk line whose recoverable id says merge-slot: a marked row
        never proves the slot. Each fixture carries healthy rows; all but the last carry
        the slot too."""
        shapes = (
            ('the last line cut mid-row', lambda: self._whole_tracker_script('{"id": "probe-2", "created_')),
            ('a row replaced by an error sentence',
             lambda: self._whole_tracker_script('bd: the database is locked, try again')),
            ('every row cut',
             lambda: self.set_bd_raw('#!/bin/sh\necho ran >> %s\necho \'{"id": "probe-1", "created_by": "op\'\n'
                                     'echo \'{"id": "probe-merge-slot", "issue_t\'\nexit 0\n' % self.marker)),
            ('a row with a 5,000-digit number',
             lambda: self._whole_tracker_script('{"id": "probe-huge", "created_by": "huge-author", "n": ' + '9' * 5000 + '}')),
            ('a junk line whose id says merge-slot, and no real slot',
             lambda: self._whole_tracker_script(
                 '{"id": "probe-merge-slot", "created_by": "forged", "metadata": ' + '[' * 900 + ']' * 900 + '}',
                 slot=False)),
        )
        for label, make, fault in [(l, m, 'unreadable-rows') for l, m in shapes
                                   if l != 'every row cut'] + [('every row cut',
                                   dict(shapes)['every row cut'], 'tracker')]:
            with self.subTest(answer=label):
                make()
                with self.assertRaises(actor_names.TrackerUnreadable):   # the bare fault or its subclass
                    self.endpoint.tracker_actors(self.root, self.project)
                answer = self.write('worker-a', self.credential('worker-a', rows_checked=False,
                                                                created_at='2026-10-07T00:00:00Z'),
                                    'shape-%s' % label[:8].replace(' ', '-'))
                self.assertEqual((2, fault), (answer['returncode'], answer.get('fault')), answer)

    def test_the_endpoint_cli_carries_the_service_namespace_it_was_launched_with(self):
        """kittrial-5bb.188 item 6: mutant U6 (main drops --service-namespace) must fail here."""
        descriptor = self.credential('web/read', rows_checked=True)
        answer = self.run_main({'project': 'probe', 'actor': 'web/read', 'action': 'bd', 'args': ['create', 'x'],
                                'operation_id': 'op-cli-web', 'authority': descriptor},
                               ['--authority-store', str(self.config_path), '--require-authority',
                                '--service-namespace', 'web'])
        self.assertEqual((126, 403), (answer['returncode'], answer.get('authority_status')), answer)
        self.assertIn(actor_names.SERVICE, answer['stderr'])
        self.assertFalse(self.marker.exists(), 'the native command ran')

    def test_only_the_service_may_make_the_endpoint_read_the_tracker(self):
        """kittrial-5bb.188 item 6: `actor-standing` with rows is the service's, not any caller's."""
        self.set_bd([{'id': 'probe-1', 'created_by': 'opus-worker-lane', 'created_at': '2026-01-01T00:00:00Z'}])
        request = {'project': 'probe', 'actor': 'http/read', 'action': 'actor-standing',
                   'args': ['opus-worker-lane'], 'tracker': True}
        answer = self.run_main(request)                       # no authority store: the SSH path
        self.assertEqual(2, answer['returncode'], answer)
        self.assertIn('web service only', answer['stderr'])
        self.assertFalse(self.marker.exists(), 'a caller without the service read the tracker')
        told = self.run_main(request, ['--authority-store', str(self.config_path)])
        self.assertEqual(0, told['returncode'], told)
        self.assertEqual({'opus-worker-lane': actor_names.ROWS}, json.loads(told['stdout'])['names'])
        self.assertTrue(self.marker.exists())

    def test_the_credential_actors_command_line_carries_the_service_namespace(self):
        """kittrial-5bb.188 item 6: mutant L2 (the CLI drops --service-namespace) must fail here."""
        import admin
        rows = [{'id': 'probe-1', 'created_by': 'web/read', 'created_at': '2026-11-01T00:00:00Z',
                 'updated_at': '2026-11-01T00:00:00Z'}]      # newer than the credential: not held
        path = self.tmp / 'state.json'
        path.write_text(json.dumps({'users': {}, 'credentials': {
            'cred_w': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'w', 'actor': 'web/read',
                       'revoked': False, 'created_at': '2026-10-01T00:00:00Z'}}}), encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(admin, 'run_bd', return_value='\n'.join(json.dumps(row) for row in rows)), \
                mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'credential-actors',
                                                '--state', str(path), '--service-namespace', 'web']), \
                redirect_stdout(out), redirect_stderr(err):
            admin.main()
        item = [c for c in json.loads(out.getvalue())['credentials'] if c['credential'] == 'cred_w'][0]
        self.assertEqual(actor_names.SERVICE, item['collides'])

    def test_the_service_namespace_it_was_launched_with_is_judged_at_use(self):
        """kittrial-5bb.188 item 3: `--actor-namespace web` at issue, and now at use too."""
        for namespace in (None, 'web'):
            with self.subTest(namespace=namespace):
                self.config = http_authority.AuthorityConfig(str(self.config_path), None, namespace)
                descriptor = self.credential('web/read', rows_checked=False, created_at='2026-10-07T00:00:00Z')
                answer = self.write('web/read', descriptor, 'web-%s' % namespace)
                if namespace is None:
                    self.assertEqual(0, answer['returncode'], answer)
                    self.marker.unlink()
                else:
                    self.assertEqual((126, 403), (answer['returncode'], answer.get('authority_status')), answer)
                    self.assertIn(actor_names.SERVICE, answer['stderr'])

    def test_a_registry_that_cannot_be_read_refuses_the_credential_and_not_the_account(self):
        (self.project / '.sessions.json').write_text('not json', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.write('worker-a', self.credential('worker-a'), 'damaged')
        self.assertFalse(self.marker.exists())

    def test_the_host_command_lists_them_and_changes_nothing(self):
        import admin
        state = {'users': {'usr_a': {'id': 'usr_a', 'username': 'alex'}}, 'credentials': {
            'cred_1': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'one', 'actor': 'ops-lead/night', 'revoked': False,
                       'created_at': '2026-10-01T00:00:00Z', 'last_used': 1791000000.0, 'expires_at': 1793000000.0},
            'cred_2': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'two', 'actor': 'worker-a', 'revoked': False},
            'cred_3': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'three', 'actor': SESSION_A, 'revoked': True},
            'cred_4': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'no name', 'actor': None, 'revoked': False},
            'cred_5': {'user_id': 'usr_a', 'agent_id': 'agent_0123456789abcdef', 'actor': 'ops-lead', 'revoked': False},
            'cred_6': {'user_id': 'usr_gone', 'project_id': 'elsewhere', 'label': 'six', 'actor': 'verity', 'revoked': False}}}
        path = self.tmp / 'state.json'
        path.write_text(json.dumps(state), encoding='utf-8')
        rows = [{'id': 'probe-1', 'assignee': 'Worker-A/sub', 'created_by': 'usr_a', 'comments': [{'author': 'ops-lead'}]}]
        before = {p: p.stat().st_mtime_ns for p in list(self.root.rglob('*')) + [path] if p.is_file()}
        with mock.patch.object(admin, 'run_bd', return_value='\n'.join(json.dumps(row) for row in rows)):
            report = admin.credential_actors(self.root, path)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in list(self.root.rglob('*')) + [path] if p.is_file()})
        found = {item['credential']: item for item in report['credentials']}
        self.assertEqual(sorted(found), ['cred_1', 'cred_2', 'cred_3', 'cred_6'])
        self.assertEqual((4, 3), (report['worker_credentials_with_a_name'], report['colliding_and_not_revoked']))
        self.assertEqual((actor_names.OPERATOR, True, True, 'alex', '2026-10-03T04:00:00Z'),
                         tuple(found['cred_1'][key] for key in ('collides', 'refused_when_it_writes', 'tracker_rows',
                                                                'issued_by_username', 'last_used')))
        # A plain name the tracker holds, with no issuance stamp to bound the rows: refused when it writes.
        self.assertEqual((actor_names.ROWS, True, True),
                         tuple(found['cred_2'][key] for key in ('collides', 'refused_when_it_writes', 'tracker_rows')))
        self.assertEqual((actor_names.SESSION, True), (found['cred_3']['collides'], found['cred_3']['revoked']))
        # A project that is not on this host: the lists still apply; its tracker cannot be read, and that is said.
        self.assertEqual((actor_names.VERIFIER, False, None, None),
                         tuple(found['cred_6'][key] for key in ('collides', 'project_on_host', 'tracker_rows', 'issued_by_username')))
        # The service namespace the command was told about is judged too (kittrial-5bb.188 item 3):
        # without `--service-namespace web`, `web/read` is not marked, which is the review's finding.
        state['credentials']['cred_7'] = {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'seven',
                                          'actor': 'web/read', 'revoked': False,
                                          'created_at': '2026-10-01T00:00:00Z'}
        path.write_text(json.dumps(state), encoding='utf-8')
        with mock.patch.object(admin, 'run_bd', return_value=''):
            told = {item['credential']: item['collides']
                    for item in admin.credential_actors(self.root, path, 'web')['credentials']}
            untold = {item['credential']: item['collides']
                      for item in admin.credential_actors(self.root, path)['credentials']}
        self.assertEqual(actor_names.SERVICE, told['cred_7'])
        self.assertIsNone(untold['cred_7'])
        state['credentials']['cred_7']['actor'] = 'http/read'
        path.write_text(json.dumps(state), encoding='utf-8')
        with mock.patch.object(admin, 'run_bd', return_value=''):
            both = {item['credential']: item['collides']
                    for item in admin.credential_actors(self.root, path, 'web')['credentials']}
        self.assertEqual(actor_names.SERVICE, both['cred_7'])
        for missing in (self.tmp / 'nothing.json', self.root):
            with self.assertRaises(ValueError):
                admin.credential_actors(self.root, missing)
        path.write_text('[]', encoding='utf-8')
        with self.assertRaises(ValueError):
            admin.credential_actors(self.root, path)

    def test_an_empty_export_is_tracker_rows_null_and_not_false(self):
        """kittrial-5bb.202 rev-3 item 3(a) (review F3): main's answer, restored.

        ``credential-actors`` is a read-only listing, and an operator reading
        ``tracker_rows: false`` would take the tracker as read and the name as free. Revision 2
        made an empty export mean "no names"; the docstring always said null ("the tracker was
        not read"), and this read does not run the merge-slot decision
        ``endpoint.tracker_actors`` runs. This test holds the answer so mutant N3's shape (an
        empty export read as a whole tracker) can never come back unnoticed.
        """
        import admin
        path = self.tmp / 'empty-export-state.json'
        path.write_text(json.dumps({'users': {}, 'credentials': {
            'cred_e': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'empty', 'actor': 'worker-el',
                       'revoked': False, 'created_at': '2026-10-01T00:00:00Z'}}}), encoding='utf-8')
        for answer in ('', '\n', 'null', '1'):
            with self.subTest(export=repr(answer)):
                with mock.patch.object(admin, 'run_bd', return_value=answer):
                    report = admin.credential_actors(self.root, path)
                item = [c for c in report['credentials'] if c['credential'] == 'cred_e'][0]
                self.assertIsNone(item['tracker_rows'], (answer, item))
                self.assertIsNone(item['collides'], (answer, item))
        # A whole tracker that holds no row under the name is the other answer: false, a read.
        with mock.patch.object(admin, 'run_bd', return_value=json.dumps(
                {'id': 'probe-merge-slot', 'issue_type': 'merge-slot'})):
            report = admin.credential_actors(self.root, path)
        item = [c for c in report['credentials'] if c['credential'] == 'cred_e'][0]
        self.assertIs(item['tracker_rows'], False, item)

    def test_the_listing_reads_rows_through_the_row_bound(self):
        """kittrial-5bb.221 revision 2: credential-actors read the export with a bare
        json.loads per line. Now rows nested to 750 read normally (their names count), and
        an export with ANY unreadable row is one the tracker could not be read from:
        tracker_rows null for every credential of the project, with the unreadable row ids
        named, never "no such rows"."""
        import admin
        path = self.tmp / 'state.json'
        path.write_text(json.dumps({'users': {}, 'credentials': {
            'cred_free': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'free', 'actor': 'worker-a',
                          'revoked': False, 'created_at': '2026-10-01T00:00:00Z'},
            'cred_rows': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'rows', 'actor': 'opus-worker-lane',
                          'revoked': False, 'created_at': '2026-10-07T00:00:00Z'},
            'cred_deep': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'deep', 'actor': 'mid-author',
                          'revoked': False, 'created_at': '2026-10-07T00:00:00Z'}}}), encoding='utf-8')
        healthy = json.dumps({'id': 'probe-1', 'created_by': 'opus-worker-lane',
                              'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z'})
        deep65 = ('{"id": "probe-deep", "assignee": "mid-author", "created_at": "2026-01-01T00:00:00Z",'
                  ' "updated_at": "2026-01-01T00:00:00Z", "metadata": ' + '{"a":' * 64 + '1' + '}' * 64 + '}')
        deeper = ('{"id": "probe-deeper", "created_by": "deep-author", "created_at": "2026-01-01T00:00:00Z",'
                  ' "updated_at": "2026-01-01T00:00:00Z", "metadata": ' + '{"a":' * 750 + '1' + '}' * 750 + '}')
        # A row nested 65 levels reads normally: the healthy name and the deep row's name both count.
        with mock.patch.object(admin, 'run_bd', return_value='\n'.join((healthy, deep65)) + '\n'):
            found = {item['credential']: item for item in admin.credential_actors(self.root, path)['credentials']}
        self.assertEqual((True, actor_names.ROWS, True),
                         tuple(found['cred_rows'][key] for key in ('tracker_rows', 'collides',
                                                                  'refused_when_it_writes')))
        self.assertEqual((False, None, False),
                         tuple(found['cred_free'][key] for key in ('tracker_rows', 'collides',
                                                                 'refused_when_it_writes')))
        self.assertEqual((True, actor_names.ROWS, True),
                         tuple(found['cred_deep'][key] for key in ('tracker_rows', 'collides',
                                                                 'refused_when_it_writes')))
        self.assertNotIn('unreadable_rows', found['cred_rows'])
        # One unreadable row: the tracker could not be read -- null, not false, for every
        # credential of the project, with the row id named so an operator sees it.
        with mock.patch.object(admin, 'run_bd', return_value='\n'.join((healthy, deeper)) + '\n'):
            found = {item['credential']: item for item in admin.credential_actors(self.root, path)['credentials']}
        for identifier in ('cred_free', 'cred_rows', 'cred_deep'):
            with self.subTest(credential=identifier):
                self.assertIsNone(found[identifier]['tracker_rows'])
                self.assertEqual(['probe-deeper'], found[identifier]['unreadable_rows'])
                # kittrial-5bb.243 item N5: beside a null, "not read", never a False that
                # reads as "it may write" -- the name may be anybody's.
                self.assertIsNone(found[identifier]['refused_when_it_writes'])

    def test_the_host_command_shows_a_waived_name_as_allowed(self):
        """kittrial-5bb.188 item 4: a waived credential is allowed, not colliding/refused."""
        import admin
        path = self.tmp / 'waived-state.json'
        path.write_text(json.dumps({'users': {'usr_a': {'id': 'usr_a', 'username': 'root-admin'}},
                                    'credentials': {
            'cred_w': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'waived',
                       'actor': 'opus-worker-lane', 'revoked': False, 'created_at': '2026-10-01T00:00:00Z',
                       'actor_waived': {'by': 'usr_a', 'reason': 'the lane owns it',
                                        'at': '2026-10-07T00:00:00Z'}}}}), encoding='utf-8')
        rows = [{'id': 'probe-1', 'created_by': 'opus-worker-lane', 'created_at': '2026-01-01T00:00:00Z'}]
        with mock.patch.object(admin, 'run_bd', return_value='\n'.join(json.dumps(row) for row in rows)):
            report = admin.credential_actors(self.root, path)
        item = [c for c in report['credentials'] if c['credential'] == 'cred_w'][0]
        self.assertIsNone(item['collides'])
        self.assertFalse(item['refused_when_it_writes'])
        self.assertTrue(item['waived'])
        self.assertEqual('allowed by root-admin on 2026-10-07T00:00:00Z', item['actor_allowed'])
        self.assertEqual('the lane owns it', item['waived_reason'])
        self.assertEqual(0, report['colliding_and_not_revoked'])

    def test_a_cheap_rule_refusal_stays_true_beside_an_unread_tracker(self):
        """kittrial-5bb.243 r3 review item 3(a), pinning r2 item 2(a): a name the service's
        own namespace (or any cheap rule) refuses is refused at every write without reading
        any tracker, so beside an unread tracker collides still says the rule,
        refused_when_it_writes stays true, and the credential is counted in the refused
        sentence only -- never also as could-not-be-judged."""
        import admin
        path = self.tmp / 'state.json'
        path.write_text(json.dumps({'users': {}, 'credentials': {
            'cred_web': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'web', 'actor': 'http',
                         'revoked': False, 'created_at': '2026-10-01T00:00:00Z'},
            'cred_free': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'free', 'actor': 'worker-a',
                          'revoked': False, 'created_at': '2026-10-01T00:00:00Z'}}}), encoding='utf-8')
        deeper = ('{"id": "probe-deeper", "created_by": "deep-author", "created_at": "2026-01-01T00:00:00Z",'
                  ' "updated_at": "2026-01-01T00:00:00Z", "metadata": ' + '{"a":' * 750 + '1' + '}' * 750 + '}')
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(admin, 'run_bd', return_value=deeper + '\n'), \
                mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'credential-actors',
                                                '--state', str(path), '--service-namespace', 'web']), \
                redirect_stdout(out), redirect_stderr(err):
            admin.main()
        report = json.loads(out.getvalue())
        found = {item['credential']: item for item in report['credentials']}
        self.assertEqual((actor_names.SERVICE, True),
                         (found['cred_web']['collides'], found['cred_web']['refused_when_it_writes']))
        self.assertIsNone(found['cred_web']['tracker_rows'])
        self.assertIsNone(found['cred_free']['collides'])
        self.assertIsNone(found['cred_free']['refused_when_it_writes'])
        # Only the truly-unjudged name is counted; the refused one is not also among them.
        self.assertEqual(1, report['could_not_be_judged'])
        self.assertIn('1 worker credential(s) could not be judged', err.getvalue())
        self.assertIn('probe: it holds 1 unreadable row(s) (probe-deeper)', err.getvalue())
        self.assertEqual(1, err.getvalue().count('unreadable row(s)'))   # once per project, not per credential
        # And the count reached stdout, as a field of the printed report.
        self.assertIn('"could_not_be_judged": 1', out.getvalue())

    def test_the_exception_prints_no_id_that_is_not_of_the_trackers_shape(self):
        """kittrial-5bb.243 r3 review item 3(b): the row reader drops a hostile id early, so
        the exception's own shape check is pinned directly -- a mutant removing it must
        fail here."""
        hostile = 'x' * 500 + '\n\r\u202e'
        built = actor_names.TrackerRowsUnreadable([hostile, 'probe-2'])
        self.assertEqual(('probe-2',), built.ids)          # the shaped one is named
        self.assertEqual(2, built.total)                   # the hostile one is still counted
        self.assertNotIn('x' * 10, str(built))             # and never printed
        only_hostile = actor_names.TrackerRowsUnreadable([hostile])
        self.assertIsNone(only_hostile.ids)
        self.assertEqual(1, only_hostile.total)
        self.assertNotIn('x' * 10, str(only_hostile))
        self.assertIn('a row that cannot be read', str(only_hostile))

    def test_the_web_answer_says_and_k_more_with_the_right_k(self):
        """kittrial-5bb.247 (M19): a thousand unreadable rows answer with five named ids and
        'and 995 more' in the message -- a mutant that drops the K or the total must fail."""
        import http_service
        ids = ['probe-%d' % number for number in range(1000)]
        with self.assertRaises(http_service.HttpError) as failed:
            http_service.EndpointBackend._checked(
                {'returncode': 2, 'stdout': '', 'stderr': '', 'fault': 'unreadable-rows',
                 'unreadable_rows': ids[:5], 'unreadable_total': 1000},
                'actor-standing', reading=True)
        self.assertIn('and 995 more', failed.exception.message)
        self.assertEqual(1000, failed.exception.unreadable_total)
        # Fewer unnamed than the cap names nothing further; K is right at the edge too.
        with self.assertRaises(http_service.HttpError) as edge:
            http_service.EndpointBackend._checked(
                {'returncode': 2, 'stdout': '', 'stderr': '', 'fault': 'unreadable-rows',
                 'unreadable_rows': ids[:5], 'unreadable_total': 6},
                'actor-standing', reading=True)
        self.assertIn('and 1 more', edge.exception.message)
        self.assertEqual(6, edge.exception.unreadable_total)

    def test_the_endpoint_envelope_carries_the_total(self):
        """kittrial-5bb.247 (M21): the endpoint's fault envelope carries unreadable_total
        beside the ids, so the service can say 'and K more' from fields."""
        deeper = ('{"id": "probe-a", "created_by": "x", "created_at": "2026-01-01T00:00:00Z",'
                  ' "metadata": ' + '{"a":' * 750 + '1' + '}' * 750 + '}')
        deeper_too = ('{"id": "probe-b", "created_by": "y", "created_at": "2026-01-01T00:00:00Z",'
                      ' "metadata": ' + '{"a":' * 750 + '1' + '}' * 750 + '}')
        self._whole_tracker_script(deeper, deeper_too)
        answer = self.write('worker-a', self.credential('worker-a', rows_checked=False,
                                                        created_at='2026-10-07T00:00:00Z'), 'total-field')
        self.assertEqual((2, 'unreadable-rows'), (answer['returncode'], answer.get('fault')), answer)
        self.assertEqual(2, answer['unreadable_total'])
        self.assertEqual(['probe-a', 'probe-b'], answer['unreadable_rows'])

    def test_a_bd_that_failed_is_not_said_to_have_answered_nothing(self):
        """kittrial-5bb.247 (M22): the three non-answers are told apart -- bd exiting nonzero
        is 'bd failed (exit not 0)', never 'bd answered nothing' (a mutant that lumps them
        must fail here)."""
        import admin
        path = self.tmp / 'state.json'
        path.write_text(json.dumps({'users': {}, 'credentials': {
            'cred_w': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'w', 'actor': 'worker-a',
                       'revoked': False, 'created_at': '2026-10-01T00:00:00Z'}}}), encoding='utf-8')
        import subprocess as real_subprocess
        failure = real_subprocess.CalledProcessError(1, 'bd')
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(admin, 'run_bd', side_effect=failure), \
                mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'credential-actors',
                                                '--state', str(path)]), \
                redirect_stdout(out), redirect_stderr(err):
            admin.main()
        report = json.loads(out.getvalue())
        self.assertEqual('bd failed (exit not 0)', report['credentials'][0]['tracker_not_read'])
        self.assertIn('bd failed (exit not 0)', err.getvalue())
        self.assertNotIn('answered nothing', err.getvalue())
        self.assertEqual(1, report['could_not_be_judged'])

    def test_the_command_line_also_counts_what_could_not_be_judged(self):
        """kittrial-5bb.243 item N5: the closing sentence names the credentials that could
        not be judged at all, with the unreadable row ids, beside the colliding count."""
        import admin
        path = self.tmp / 'state.json'
        path.write_text(json.dumps({'users': {}, 'credentials': {
            'cred_w': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'one', 'actor': 'worker-a',
                       'revoked': False, 'created_at': '2026-10-01T00:00:00Z'}}}), encoding='utf-8')
        healthy = json.dumps({'id': 'probe-1', 'created_by': 'opus-worker-lane',
                              'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z'})
        deeper = ('{"id": "probe-deeper", "created_by": "deep-author", "created_at": "2026-01-01T00:00:00Z",'
                  ' "updated_at": "2026-01-01T00:00:00Z", "metadata": ' + '{"a":' * 750 + '1' + '}' * 750 + '}')
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(admin, 'run_bd', return_value='\n'.join((healthy, deeper)) + '\n'), \
                mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'credential-actors',
                                                '--state', str(path)]), \
                redirect_stdout(out), redirect_stderr(err):
            admin.main()
        self.assertIn('1 worker credential(s) could not be judged', err.getvalue())
        self.assertIn('probe: it holds 1 unreadable row(s) (probe-deeper)', err.getvalue())
        self.assertEqual(1, err.getvalue().count('unreadable row(s)'))   # once per project, not per credential
        self.assertIn('--unset-metadata', err.getvalue())
        self.assertNotIn('could not be judged', out.getvalue())

    def test_the_command_line_prints_the_list_and_says_how_many(self):
        import admin
        path = self.tmp / 'state.json'
        path.write_text(json.dumps({'users': {}, 'credentials': {
            'cred_1': {'user_id': 'usr_a', 'project_id': 'probe', 'label': 'one', 'actor': 'verity', 'revoked': False}}}), encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(admin, 'run_bd', return_value=''), mock.patch.object(
                sys, 'argv', ['admin.py', '--root', str(self.root), 'credential-actors', '--state', str(path)]), \
                redirect_stdout(out), redirect_stderr(err):
            admin.main()
        self.assertEqual(1, json.loads(out.getvalue())['colliding_and_not_revoked'])
        self.assertIn('1 worker credential(s) write under a name', err.getvalue())
        self.assertIn('Nothing was changed', err.getvalue())


class TrackerFaultTests(unittest.TestCase):
    """kittrial-5bb.188 item 1: the endpoint's no-rows fault is a host fault, never a 422."""

    def test_the_tracker_fault_is_unavailable_and_nothing_was_changed(self):
        for reading in (True, False):
            with self.subTest(reading=reading):
                with self.assertRaises(http_service.HttpError) as failed:
                    http_service.EndpointBackend._checked(
                        {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: no rows\n', 'fault': 'tracker'},
                        'actor-standing', reading=reading)
                error = failed.exception
                self.assertEqual((503, 'unavailable', http_service.EndpointBackend.UNREAD, True),
                                 (error.status, error.code, error.message,
                                  getattr(error, 'nothing_done', False)))

    def test_the_missing_slot_is_its_own_error_and_says_merge_create(self):
        """kittrial-5bb.202 item 1: not UNREAD's transient sentence, and its own code."""
        for reading in (True, False):
            with self.subTest(reading=reading):
                with self.assertRaises(http_service.HttpError) as failed:
                    http_service.EndpointBackend._checked(
                        {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: no slot\n', 'fault': 'merge-slot'},
                        'actor-standing', reading=reading)
                error = failed.exception
                self.assertEqual((503, 'merge_slot_missing', http_service.EndpointBackend.MERGE_SLOT_MISSING, True),
                                 (error.status, error.code, error.message,
                                  getattr(error, 'nothing_done', False)))
                self.assertIn('merge-create', error.message)
                self.assertNotEqual(http_service.EndpointBackend.UNREAD, error.message)
                self.assertNotEqual('unavailable', error.code)

    def test_unreadable_rows_are_their_own_error_and_name_the_row(self):
        """kittrial-5bb.243 item N7: an export read with one unreadable row answers 503 with
        its own code and the endpoint's sentence naming the row id and the repair, never
        UNREAD's "try again shortly" -- at issue and at use alike (reading or not)."""
        sentence = ("TrackerRowsUnreadable: The project's tracker holds unreadable row(s) pp-9, so it was "
                    "not read as a whole tracker: the unreadable row may be the row that holds a name. "
                    "An operator must repair the row (for deeply nested metadata: bd update ID "
                    "--unset-metadata KEY) if a second try gives the same answer, then try again.\n")
        for reading in (True, False):
            with self.subTest(reading=reading):
                with self.assertRaises(http_service.HttpError) as failed:
                    http_service.EndpointBackend._checked(
                        {'returncode': 2, 'stdout': '', 'stderr': sentence, 'fault': 'unreadable-rows',
                         'unreadable_rows': ['pp-9']}, 'actor-standing', reading=reading)
                error = failed.exception
                self.assertEqual((503, True), (error.status, getattr(error, 'nothing_done', False)))
                self.assertEqual('unreadable_rows', error.code)
                self.assertIn('pp-9', error.message)
                self.assertIn('--unset-metadata', error.message)
                self.assertIn('second try', error.message)
                self.assertNotIn('try again shortly', error.message)
                self.assertNotIn('re-enter the text', error.message)
                self.assertNotEqual(http_service.EndpointBackend.UNREAD, error.message)
                self.assertNotEqual('unavailable', error.code)
        # The message is built from the fault's fields: the ids ride as a field, and a
        # stderr tail shaped like a row's body never becomes the message (r2 review item 1).
        with self.assertRaises(http_service.HttpError) as failed:
            http_service.EndpointBackend._checked(
                {'returncode': 2, 'stdout': '', 'stderr': '', 'fault': 'unreadable-rows',
                 'unreadable_rows': ['pp-9']}, 'actor-standing', reading=True)
        self.assertEqual((503, 'unreadable_rows'), (failed.exception.status, failed.exception.code))
        self.assertIn('pp-9', failed.exception.message)
        self.assertEqual(['pp-9'], failed.exception.unreadable_rows)
        # With no ids at all, the fixed one answers, still its own code.
        with self.assertRaises(http_service.HttpError) as failed:
            http_service.EndpointBackend._checked(
                {'returncode': 2, 'stdout': '', 'stderr': '', 'fault': 'unreadable-rows'},
                'actor-standing', reading=True)
        self.assertEqual((503, 'unreadable_rows', http_service.EndpointBackend.ROWS_UNREADABLE),
                         (failed.exception.status, failed.exception.code, failed.exception.message))

    def test_an_ordinary_code_two_reading_refusal_is_still_422(self):
        """The endpoint's own guard refusals stay 422 on the reading path (main's rule)."""
        with self.assertRaises(http_service.HttpError) as failed:
            http_service.EndpointBackend._checked(
                {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: Refusing update\n'},
                'actor-standing', reading=True)
        self.assertEqual(422, failed.exception.status)


class LaunchWiringTests(unittest.TestCase):
    """kittrial-5bb.188 item 6: the --service-namespace launch wiring mutant S5 must fail here."""

    def backend(self, namespace='web2'):
        service = mock.Mock()
        service.store.path = os.path.join(os.sep, 'tmp', 'state.json')
        return http_service.EndpointBackend(sys.executable, 'endpoint.py', os.path.join(os.sep, 'root'),
                                            service=service, actor_namespace=namespace)

    def test_the_service_hands_its_namespace_to_the_endpoint_launch(self):
        backend = self.backend()
        captured = {}

        def fake_run(argv, **kwargs):
            captured['argv'] = list(argv)
            return subprocess.CompletedProcess(argv, 0,
                                               json.dumps({'returncode': 0, 'stdout': '{}\n', 'stderr': ''}), '')
        with mock.patch.object(subprocess, 'run', fake_run):
            backend._ask('actor-standing', 'probe', 'web2/read', ['worker'], None, None, None, None,
                         False, None, False, None)
        self.assertIn('--service-namespace', captured['argv'])
        self.assertEqual('web2', captured['argv'][captured['argv'].index('--service-namespace') + 1])


try:
    import test_bd_label_aliases as rb
except Exception:                                            # the real-bd fixture imports endpoint too
    rb = None


@unittest.skipIf(rb is None or rb.endpoint is None, 'real bd tracker read needs POSIX endpoint imports')
@unittest.skipIf(rb is not None and (rb.BD is None or not rb.BD.with_name('dolt').is_file()),
                 'no real bd/dolt pair (set ORCHESTRA_BD_BIN to a bd with a sibling dolt)')
class RealBdTrackerActorTests(unittest.TestCase):
    """kittrial-5bb.221 on a real pinned bd, in the fixture of test_bd_label_aliases.

    One tracker row planted exactly 65 levels deep (bd update --metadata embeds the value
    structurally in the exported row; on bd 1.2.2 the row line nests the value's depth plus
    the row and the metadata wrapper) sits beside healthy rows and the merge slot: the read
    answers its names, the issue path (`actor-standing` with rows, what the web service asks
    to issue a credential) holds the names, and a write under a held name is refused while a
    free name writes. A row planted exactly 751 levels deep is marked, and the whole read
    refuses as on main -- at issue and at first use.

    The fixture's machinery (its server, its scratch runtime, its signal handling) is
    borrowed method by method: the class is not a subclass, so its own test methods do not
    run a second time here.
    """
    BORROWED = ('PASSWORD', 'READY_TRIES', 'READY_PAUSE', 'bd', '_free_port',
                '_stop_server', '_signalled', '_watch_signals', '_await_server', '_set_root_password')

    @classmethod
    def setUpClass(cls):
        for name in cls.BORROWED:
            attr = rb.RealBdLabelAliasTests.__dict__[name]
            if isinstance(attr, classmethod):          # re-wrapped, so cls.<name>() binds the class again
                attr = classmethod(attr.__func__)
            setattr(cls, name, attr)
        rb.RealBdLabelAliasTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        rb.RealBdLabelAliasTests.tearDownClass.__func__(cls)

    def created(self, title, *args):
        result = self.bd('create', title, '--json', *args)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)['id']

    def setUp(self):
        import http_authority
        self.endpoint = rb.endpoint
        self.config_path = self.root / 'authority.json'
        self.state = fixes.authority_state(project='pp')
        self.config = http_authority.AuthorityConfig(str(self.config_path))

    def credential(self, name, rows_checked=None):
        self.state['credentials'] = {'cred_x': {
            'id': 'cred_x', 'user_id': 'usr_a', 'project_id': 'pp', 'actor': name, 'revoked': False,
            'scopes': ['tasks', 'checkpoints', 'reviews', 'feedback'], 'expires_at': time.time() + 3600}}
        if rows_checked is not None:
            self.state['credentials']['cred_x']['actor_rows_checked'] = rows_checked
        self.config_path.write_text(json.dumps(self.state), encoding='utf-8')
        return {'via': 'credential', 'user_id': 'usr_a', 'credential_id': 'cred_x', 'project': 'pp',
                'capability': 'tasks.write', 'now': time.time() + 5}

    def write(self, actor, descriptor, number):
        return self.endpoint.execute(self.root, {'project': 'pp', 'actor': actor, 'action': 'bd',
                                                 'args': ['create', 'x'], 'operation_id': 'op-221-%s' % number,
                                                 'authority': descriptor},
                                     authority_config=self.config, require_authority=True)

    def standing(self, names):
        """One actor-standing request through the endpoint's own CLI, exactly as the web
        service's issue path makes it (the shape of RealEndpointCase.run_main)."""
        out = io.StringIO()
        argv = ['endpoint.py', '--root', str(self.root), '--authority-store', str(self.config_path)]
        request = {'project': 'pp', 'actor': 'http/read', 'action': 'actor-standing',
                   'args': names, 'tracker': True}
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.endpoint.main()
        return json.loads(out.getvalue())

    def row_line(self, wanted):
        export = self.bd('export', '--all')
        self.assertEqual(0, export.returncode, export.stderr)
        for line in export.stdout.splitlines():
            if line.strip() and '"%s"' % wanted in line:
                return line
        self.fail('no row %s in the export' % wanted)

    def depth_exactly(self, line, expected):
        """Whether a row line nests exactly ``expected`` levels.

        nesting(text, bound) answers "past this bound or not": 0 when the text cannot
        exceed it. Exactly N is "not past N" and "past N-1" together."""
        import record_json
        return (record_json.nesting(line, expected) == 0
                and record_json.nesting(line, expected - 1) == expected)

    def test_rows_65_and_751_levels_read_and_refuse(self):
        import record_json
        self.created('the merge slot', '--id', 'pp-merge-slot')
        self.created('healthy', '--assignee', 'vic')
        deep = self.created('deep', '--assignee', 'deep-author')
        # The value's depth plus the row and the metadata wrapper is the row line's depth.
        shallow = '{"deep":' + '{"a":' * 63 + '1' + '}' * 63 + '}'
        self.assertEqual(0, self.bd('update', deep, '--metadata', shallow).returncode)
        line = self.row_line(deep)
        self.assertTrue(self.depth_exactly(line, 65), 'row line not exactly 65 levels: %r' % line[:120])
        # A whole tracker with one row nested 65 levels: the read answers its names (the
        # fixture's own actor wrote the rows, so it is held too) ...
        names = set(rb.endpoint.tracker_actors(self.root, self.project))
        self.assertIn('vic', names)
        self.assertIn('deep-author', names)
        # ... the issue path holds the asked-for names ...
        told = json.loads(self.standing(['vic', 'worker-a'])['stdout'])['names']
        self.assertEqual({'vic': actor_names.ROWS}, {name: rule for name, rule in told.items() if rule})
        # ... a write under the held name is refused, and a free name writes through the service.
        refused = self.write('vic', self.credential('vic', rows_checked=False), 'held')
        self.assertEqual((126, 403), (refused['returncode'], refused.get('authority_status')), refused)
        done = self.write('worker-a/sub', self.credential('worker-a'), 'free')
        self.assertEqual(0, done['returncode'], done)
        # A row nested exactly 751 levels is marked, and the whole read refuses, at issue and use.
        past = '{"deep":' + '{"a":' * 749 + '1' + '}' * 749 + '}'
        self.assertEqual(0, self.bd('update', deep, '--metadata', past).returncode)
        self.assertTrue(self.depth_exactly(self.row_line(deep), 751), 'row line not exactly 751 levels')
        with self.assertRaises(actor_names.TrackerUnreadable):
            rb.endpoint.tracker_actors(self.root, self.project)
        told = self.standing(['vic'])
        self.assertEqual((2, 'unreadable-rows'), (told['returncode'], told.get('fault')), told)
        self.assertIn(deep, told['stderr'])              # the sentence names the unreadable row
        fault = self.write('vic', self.credential('vic', rows_checked=False), 'unreadable')
        self.assertEqual((2, 'unreadable-rows'), (fault['returncode'], fault.get('fault')), fault)
        self.assertNotIn('try again shortly', fault['stderr'])


if __name__ == '__main__':
    unittest.main()
