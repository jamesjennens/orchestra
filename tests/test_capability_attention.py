"""Capability attention in `work` and `brief` (kittrial-5bb.76; .60 section 8).

`attention.capability_index` is the agent attention shape: counts for everyone, items
only for an operator, computed from the export `work` already made. `brief` adds up to
3 accepted capabilities tagged like the task, after the other kinds.
"""
import calendar
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import briefing
import capability_records as cr
import http_service
import work
from test_capability_records import entry
from test_capability_verification import INTEGRATED, OPS, OTHER, VerificationCase
from test_reference_records import OPERATOR

KEY = 'review.structured-contribution'
MISSING = ('review_workflow.py::execute',)


class AttentionCase(VerificationCase):
    def accept_key(self, key, revision=1, operation_id=None):
        return self.accept(key, revision=revision, operation_id=operation_id or 'accept-' + key)

    def make(self, key, **extra):
        return self.propose(operation_id='make-' + key, key=key, name='Name of ' + key, aliases=[], **extra)

    def rows(self):
        self.sync()
        return [dict(row) for row in self.native.rows]

    def block(self, actor=OPERATOR, now=None, **options):
        return cr.work_attention(self.rows(), actor, OPS, 'demo', project=self.journal, now=now, **options)

    def days_later(self, days):
        return calendar.timegm(time.gmtime()) + days * 86400


class WorkAttentionTests(AttentionCase):
    def test_the_block_is_the_agent_attention_shape(self):
        block = self.block()
        shape = set(http_service.ApiHandler._agent_attention_view({}))
        self.assertTrue(shape | {'actions'} <= set(block))
        self.assertEqual(set(block) - shape - {'actions'}, {'items', 'next_offset'})
        self.assertEqual(block['counts'], {'conflicted': 0, 'drifted': 0, 'reported_only': 0, 'unverified_stale': 0,
                                           'alias_pending': 0, 'draft_pending': 1, 'malformed': 0, 'total': 1})
        self.assertEqual((block['state'], block['summary']), ('pending', '1 draft(s) waiting for acceptance.'))
        action = block['actions'][0]
        self.assertEqual(set(action), {'priority', 'kind', 'project', 'task', 'reason', 'links', 'label', 'token'})
        self.assertEqual((action['kind'], action['project'], action['task'], action['label']['text']),
                         ('capability-accept', 'demo', self.anchor(), 'capability get ' + KEY))

    def test_every_count_and_the_action_order(self):
        self.accept_key(KEY)                                   # accepted, no verification yet
        for key in ('a.drift', 'a.alias', 'a.report', 'a.fresh', 'a.verified'):
            self.make(key)
            self.accept_key(key)
        self.make('a.draft')                                   # a draft that waits
        self.verify(actor='bob', key='a.drift', missing=MISSING)
        self.native.actor = 'carol'
        cr.propose_alias('a.alias', 'a pending phrase', 'carol', self.run_native, OPS)
        self.verify(actor='bob', key='a.report')
        self.verify(actor=OPERATOR, operator=True, key='a.verified', commit=INTEGRATED)
        later = self.days_later(cr.UNVERIFIED_STALE_DAYS + 1)
        block = self.block(now=later)
        self.assertEqual(block['counts'], {'conflicted': 0, 'drifted': 1, 'reported_only': 1, 'unverified_stale': 4,
                                           'alias_pending': 1, 'draft_pending': 1, 'malformed': 0, 'total': 6})
        self.assertEqual(block['state'], 'drifted')
        kinds = [action['kind'] for action in block['actions']]
        self.assertEqual((kinds[0], sorted(kinds[1:3]), sorted(kinds[3:])),
                         ('capability-drift', ['capability-accept', 'capability-alias'],
                          ['capability-verify', 'capability-verify-report']))    # by priority, then task id
        self.assertEqual([(action['priority'], action['project'], action['task']) for action in block['actions']],
                         sorted((action['priority'], action['project'], action['task'])
                                for action in block['actions']))
        flags = {item['key']: item['flags'] for item in block['items']}
        self.assertEqual(flags, {'a.drift': ['drifted'], 'a.draft': ['draft_pending'],
                                 'a.alias': ['unverified_stale', 'alias_pending'],
                                 'a.report': ['unverified_stale', 'reported_only'],
                                 'a.fresh': ['unverified_stale'], KEY: ['unverified_stale']})
        self.assertEqual([item['key'] for item in block['items']][:1], ['a.drift'])
        # Within 30 days of acceptance, nothing is stale yet.
        self.assertEqual(self.block()['counts']['unverified_stale'], 0)

    def test_items_only_for_an_operator_and_paged(self):
        for key in ('a.one', 'a.two', 'a.three'):
            self.make(key)
        block = self.block(actor='bob')
        self.assertEqual((block['counts']['draft_pending'], block['items'], block['truncated'], block['next_offset']),
                         (4, [], True, None))
        self.assertIn('4 item(s) for operators', block['coverage'])
        page = self.block(limit=2)
        self.assertEqual((len(page['items']), page['next_offset'], page['truncated']), (2, 2, True))
        self.assertEqual(len(self.block(limit=2, offset=2)['items']), 2)
        item = page['items'][0]
        self.assertEqual(set(item), {'kind', 'key', 'task', 'state', 'verification', 'flags', 'owner',
                                     'aliases_pending', 'accepted_days', 'title'})
        self.assertEqual((item['kind'], item['title']['trust']), ('capability', 'draft'))

    def test_a_trusted_pass_at_an_integrated_commit_clears_the_drift(self):
        self.accept_key(KEY)
        self.verify(actor='bob', missing=MISSING)
        self.assertEqual(self.block()['counts']['drifted'], 1)
        self.verify(actor=OPERATOR, operator=True, commit=OTHER)            # not integrated: still drifted
        self.assertEqual(self.block()['counts']['drifted'], 1)
        self.verify(actor=OPERATOR, operator=True, commit=INTEGRATED)
        block = self.block(now=self.days_later(cr.UNVERIFIED_STALE_DAYS + 5))
        self.assertEqual((block['counts']['total'], block['state'], block['summary']),
                         (0, 'clear', 'No capability needs attention.'))

    def test_malformed_and_retired_entries(self):
        self.accept_key(KEY)
        self.make('a.broken')
        self.plant({'key': 'a.broken'} and self.payload(key='a.broken'), OPERATOR, True, OPERATOR)
        row = self.native.row(self.anchor('a.broken'))
        row['comments'].append({'id': 'c-bad', 'text': 'Kind: capability-entry-v1\n{"bad": 1}', 'author': 'x',
                                'created_at': '2026-10-01T12:00:00Z'})
        block = self.block()
        self.assertEqual((block['counts']['malformed'], block['state']), (1, 'malformed'))
        self.assertEqual(block['actions'][0]['kind'], 'capability-repair')

    def test_work_carries_the_block_and_reads_nothing_more(self):
        self.accept_key(KEY)
        self.verify(actor='bob', missing=MISSING)
        rows = self.rows()
        self.native.calls = []
        result = work.queue(rows, OPERATOR, [], operators=OPS, journal=self.journal, reference_attention=True)
        self.assertEqual(self.native.calls, [])     # no read and no write: computed from the rows work holds
        self.assertEqual(set(result['attention']), {'reference_review', 'reference_matches', 'proposal_queue',
                                                    'capability_index'})
        self.assertEqual(result['attention']['capability_index']['counts']['drifted'], 1)
        self.assertEqual(result['attention']['capability_index']['actions'][0]['project'],
                         Path(self.journal).name)
        paged = work.queue(rows, OPERATOR, ['--capability-limit', '1'], operators=OPS, journal=self.journal,
                           reference_attention=True)
        self.assertEqual(len(paged['attention']['capability_index']['items']), 1)
        with self.assertRaisesRegex(ValueError, '--capability-limit must be'):
            work.queue(rows, OPERATOR, ['--capability-limit', '0'], operators=OPS, journal=self.journal,
                       reference_attention=True)
        self.assertEqual(self.native.calls, [])


class BriefAttentionTests(AttentionCase):
    def test_tagged_accepted_capabilities_drifted_first_at_most_three(self):
        for key in ('t.a', 't.b', 't.c', 't.d'):
            self.make(key, tags=['charts'])
            self.accept_key(key)
        self.make('t.draft', tags=['charts'])                   # a draft is never selected
        self.make('t.other', tags=['other'])
        self.accept_key('t.other')
        self.verify(actor='bob', key='t.d', missing=MISSING)
        self.capability_rows.append(self.native.seed('task-9', labels=['charts']))
        rows = self.rows()
        brief = briefing.brief(rows, 'demo', 'task-9', operators=OPS, journal=self.journal)
        self.assertEqual([(item['kind'], item['key'], item['verification']) for item in brief['attention']],
                         [('capability', 't.d', 'drifted'), ('capability', 't.a', 'unverified'),
                          ('capability', 't.b', 'unverified')])
        self.assertEqual((brief['attention_total'], brief['attention_more']), (4, 1))
        item = brief['attention'][0]
        self.assertEqual((item['trust'], item['source'], item['title']['text']),
                         ('accepted', 'capability get t.d', '"Name of t.d"'))
        self.assertEqual(item['text'], 'Capability t.d is tagged for this task (verification: drifted).')
        self.assertIn('Capability [drifted, accepted]', briefing.format_brief(brief))
        untagged = briefing.brief(rows, 'demo', 'task-1', operators=OPS, journal=self.journal)
        self.assertEqual([item for item in untagged['attention'] if item['kind'] == 'capability'], [])


if __name__ == '__main__':
    unittest.main()
