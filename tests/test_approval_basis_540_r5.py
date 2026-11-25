"""kittrial-5bb.540 r5: the r4 review's items (request 01a21167), one class per item.

- crlf-one-break: a Windows line break is ONE break, so a single \\r\\n between the words is still the label (r3 refused
  these four forms; r4 took \\r\\n for a blank line), and \\r\\n\\r\\n is a blank line (review-540r4-neon.md MUST-1).
- contribute-bound: the gate's work is bounded: an overlong contribute summary skips it (the canonical write refuses
  it), and after each label only a bounded prefix is read (MUST-2).
- mutants: Neon's two survivors, NEON_R4_2_sep_drop_excl_quest and NEON_R4_3_dash_drop_lookahead (NOTE-2).
"""
import sys
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import test_agent_approval_credentials as cred
import test_http_agents
from test_approval_basis_540_r2 import gate

#: crlf-one-break: refused on r3 (f9c78c60), accepted on r4 (cb0dc00f); refused again.
CRLF_FORMS = {
    'the two words over one CRLF': 'Approval\r\nbasis: same-person-agent',
    'one CRLF and an indent': 'Approval\r\n   basis: same-person-agent',
    'Allowed by over one CRLF': 'Allowed\r\nby: same-person-agent',
    'the dash form over one CRLF': 'Approval\r\nbasis — same-person-agent',
}

#: Prose: a blank line written with CRLFs, and the other two sentence ends (NEON_R4_2_sep_drop_excl_quest).
CRLF_AND_SENTENCE_PROSE = {
    'a blank line of CRLFs': 'Ready for approval\r\n\r\nBasis: CI is green',
    'a question mark ends the sentence': 'Ready for approval? Basis: CI is green.',
    'an exclamation mark ends the sentence': 'Needs approval! Basis: all tests pass.',
}


class CrlfOneBreakTests(cred.PinBase):
    """crlf-one-break: refused 422 on approve, recommend and contribute; the CRLF blank line stays prose."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)

    def test_the_gate_refuses_one_crlf_and_accepts_a_crlf_blank_line(self):
        for name, text in CRLF_FORMS.items():
            with self.subTest(refused=name):
                self.assertIsNotNone(gate(text))
        for name, text in CRLF_AND_SENTENCE_PROSE.items():
            with self.subTest(prose=name):
                self.assertIsNone(gate(text))

    def test_each_crlf_form_is_refused_on_approve_recommend_and_contribute(self):
        task, cid = self.deliver(self.tokens['carl'])
        own = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'g'}, token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', self.base(own) + '/claim', {}, token=self.other).status)
        for number, (name, text) in enumerate(CRLF_FORMS.items()):
            with self.subTest(form=name, on='approve'):
                refused = self.request('POST', self.base(task) + '/reviews', {
                    'operation': 'approve', 'contribution': cid, 'summary': text}, token=self.agent)
                self.assertEqual(422, refused.status, refused.data)
            with self.subTest(form=name, on='recommend'):
                refused = self.request('POST', self.base(task) + '/reviews', {
                    'operation': 'recommend', 'schema_version': 1, 'operation_id': 'rec-c-%d' % number,
                    'contribution': cid, 'commit': test_http_agents.COMMIT, 'verdict': 'approve',
                    'summary': text, 'items': []}, token=self.agent)
                self.assertEqual(422, refused.status, refused.data)
            with self.subTest(form=name, on='contribute'):
                refused = self.request('POST', self.base(own) + '/reviews', {
                    'operation': 'contribute', 'commit': test_http_agents.COMMIT, 'base_commit': test_http_agents.BASE,
                    'bundle_sha256': test_http_agents.BUNDLE, 'summary': text}, token=self.other)
                self.assertEqual(422, refused.status, refused.data)
        self.assertEqual('awaiting-review', self.state_of(task))


class ContributeBoundTests(cred.PinBase):
    """contribute-bound: a body near MAX_BODY_BYTES answers at once; the gate's own work is bounded."""

    def test_a_contribute_body_near_the_limit_answers_quickly(self):
        own = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'g'}, token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', self.base(own) + '/claim', {}, token=self.other).status)
        summary = ('Allowed by: ' * 21500)[:http_service.MAX_BODY_BYTES - 2048]      # about 256 KB
        started = time.monotonic()
        refused = self.request('POST', self.base(own) + '/reviews', {
            'operation': 'contribute', 'commit': test_http_agents.COMMIT, 'base_commit': test_http_agents.BASE,
            'bundle_sha256': test_http_agents.BUNDLE, 'summary': summary}, token=self.other)
        elapsed = time.monotonic() - started
        self.assertEqual(422, refused.status, refused.data)
        self.assertIn('1200', refused.data['error']['message'])              # the canonical write's own refusal
        self.assertLess(elapsed, 10.0)                                        # r4: about 17 minutes

    def test_an_overlong_contribute_summary_never_reaches_the_gate(self):
        # R5_contribute_unbounded: the gate is skipped for a summary the canonical write refuses by its length.
        from unittest import mock
        own = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'g'}, token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', self.base(own) + '/claim', {}, token=self.other).status)
        real = http_service.ApiHandler._basis_text_in
        seen = []

        def spy(handler, summary):
            seen.append(len(summary) if isinstance(summary, str) else None)
            return real(handler, summary)
        body = {'operation': 'contribute', 'commit': test_http_agents.COMMIT, 'base_commit': test_http_agents.BASE,
                'bundle_sha256': test_http_agents.BUNDLE}
        limit = http_service.TEXT_LIMITS['summary']
        with mock.patch.object(http_service.ApiHandler, '_basis_text_in', spy):
            refused = self.request('POST', self.base(own) + '/reviews', dict(body, summary='x' * (limit + 1)),
                                   token=self.other)
            self.assertEqual(422, refused.status, refused.data)
            self.assertEqual([], seen)
            refused = self.request('POST', self.base(own) + '/reviews', dict(body, summary='Approval basis: same-person-agent'),
                                   token=self.other)
            self.assertEqual(422, refused.status, refused.data)
            self.assertEqual([33], seen)

    def test_the_gate_reads_a_bounded_prefix_after_each_label(self):
        handler = http_service.ApiHandler.__new__(http_service.ApiHandler)
        # r4 at 20,000 characters: about 6 s for the first two; the quoted mention's rest is bounded as well.
        for unit, length, bound in (('Allowed by: ', 20000, 3.0), ('Approval basis - ', 20000, 3.0),
                                    ("'Approval basis:' x ", 40000, 6.0)):
            with self.subTest(unit=unit):
                text = (unit * (length // len(unit) + 1))[:length]
                started = time.monotonic()
                handler._basis_text_in(text)
                self.assertLess(time.monotonic() - started, bound)
        self.assertEqual(2 * http_service.TEXT_LIMITS['summary'], handler.BASIS_REST_LIMIT)
        # The bounded prefix is the start of the whole normalised text.
        rest = '   — **Same**-Person  Agent, and then a great deal more' * 3
        self.assertEqual(handler._normalize_basis_text(rest)[:handler.BASIS_VALUE_PREFIX], handler._basis_value_prefix(rest))


class SurvivingMutantTests(unittest.TestCase):
    """mutants: Neon's NEON_R4_2 and NEON_R4_3."""

    def test_a_question_mark_and_an_exclamation_mark_end_a_sentence(self):
        # NEON_R4_2_sep_drop_excl_quest: [.!?] narrowed to [.].
        for name in ('a question mark ends the sentence', 'an exclamation mark ends the sentence'):
            with self.subTest(prose=name):
                self.assertIsNone(gate(CRLF_AND_SENTENCE_PROSE[name]))

    def test_a_later_dash_label_after_a_prose_one_is_found(self):
        # NEON_R4_3_dash_drop_lookahead: the dash label consuming the rest skips the second label.
        self.assertIsNotNone(gate('Approval basis — we talked it over. Approval basis — same-person-agent'))


if __name__ == '__main__':
    unittest.main()
