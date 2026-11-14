"""kittrial-5bb.560: ``web_lifecycle.approved_party`` asks the project's by-agent decision.

Pure: the party map and the decision are plain callables, no tracker and no web service. The
real stack is tests/test_agent_approval_integration_560.py.
"""
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import http_authority
import web_lifecycle

SENTENCE = 'The approval must be from another party than the author and current assignee'
PARTY = {'heron': {'cora'}, 'egret': {'cora'}, 'cora': {'cora'}, 'casey': {'casey'}, 'kite': {'bob'}}


def parties(name):
    return set(PARTY.get(name, {name} if name else set()))


def approved(author, approver='heron'):
    return dict(id='c1', commit='a' * 40, author=author, approver=approver, standing=True)


def decision(allowed, basis=None, refusal=None):
    return http_authority.ByAgentDecision(allowed, basis, refusal, None, None, False)


class Recorder:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, approver, author, assignee):
        self.calls.append((approver, author, assignee))
        return self.answer


class ApprovedPartyByAgent(unittest.TestCase):

    def refused(self, *args, **kwargs):
        with self.assertRaises(web_lifecycle.LifecycleError) as caught:
            web_lifecycle.approved_party(*args, **kwargs)
        return caught.exception.status, str(caught.exception)

    def test_no_callable_is_strict_as_before(self):
        for by_agent in (None, 'not callable', decision(True, 'agent')):
            self.assertEqual((409, SENTENCE), self.refused(approved('egret'), {'assignee': 'egret'}, parties,
                                                           by_agent=by_agent))
        self.assertEqual((409, SENTENCE), self.refused(approved('egret'), {'assignee': 'egret'}, parties))
        self.assertEqual(SENTENCE, web_lifecycle.OTHER_PARTY_REQUIRED)

    def test_an_allowed_decision_lets_the_meeting_parties_through(self):
        asked = Recorder(decision(True, 'agent'))
        self.assertEqual(['cora'], web_lifecycle.approved_party(approved('egret'), {'assignee': 'egret'}, parties,
                                                                by_agent=asked))
        self.assertEqual([('heron', 'egret', 'egret')], asked.calls)

    def test_every_refused_decision_keeps_the_sentence_and_status(self):
        for kind in http_authority.BY_AGENT_REFUSALS:
            asked = Recorder(decision(False, None, kind))
            self.assertEqual((409, SENTENCE), self.refused(approved('egret'), {'assignee': None}, parties,
                                                           by_agent=asked), kind)
            self.assertEqual([('heron', 'egret', None)], asked.calls)

    def test_only_a_true_allowed_passes(self):
        for answer in (None, object(), 'yes', dict(allowed=True), decision(1), decision('true')):
            self.assertEqual((409, SENTENCE), self.refused(approved('egret'), {}, parties,
                                                           by_agent=Recorder(answer)), answer)

    def test_the_assignee_alone_meeting_also_asks(self):
        asked = Recorder(decision(False, None, 'held-assignee'))
        self.assertEqual((409, SENTENCE), self.refused(approved('casey'), {'assignee': 'cora'}, parties,
                                                       by_agent=asked))
        self.assertEqual([('heron', 'casey', 'cora')], asked.calls)

    def test_independent_work_never_asks(self):
        asked = Recorder(decision(False, None, 'strict'))
        self.assertEqual(['cora'], web_lifecycle.approved_party(approved('casey'), {'assignee': 'kite'}, parties,
                                                                by_agent=asked))
        self.assertEqual([], asked.calls)

    def test_the_other_refusals_come_first_and_do_not_ask(self):
        asked = Recorder(decision(True, 'agent'))
        self.assertEqual((503, 'Configured web party ownership is unavailable'),
                         self.refused(approved('egret'), {}, None, by_agent=asked))
        self.assertEqual((409, SENTENCE), self.refused(approved('egret', approver=None), {},
                                                       lambda name: set(), by_agent=asked))
        self.assertEqual([], asked.calls)

    def test_approved_source_passes_the_callable_on(self):
        import inspect
        for function in (web_lifecycle.approved_source, web_lifecycle.read, web_lifecycle.integration_check,
                         web_lifecycle.apply):
            self.assertIn('by_agent', inspect.signature(function).parameters, function.__name__)
            self.assertIsNone(inspect.signature(function).parameters['by_agent'].default, function.__name__)


if __name__ == '__main__':
    unittest.main()
