"""Every corpus kind uses its real reader and an escaped data envelope."""
import hashlib
import tempfile
import unittest
from pathlib import Path

import test_context_find as fixture
import context_find as find


UPDATED = '2026-03-02T01:02:03Z'
DIRTY = 'orthogonal \x1b\x85\u202e\u2028' + '\x1b\u202e' * 20


class ReaderEnvelopeTests(unittest.TestCase):
    def own_case(self, cls):
        case = cls('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def visible_and_denied(self, kind, rows, **kwargs):
        answer = fixture.search(rows, args=['--kind', kind], **kwargs)
        narrower = [name for name in find.KINDS if name != kind]
        before = fixture.search([], args=['--kind', kind], kinds=narrower, **kwargs)
        after = fixture.search(rows, args=['--kind', kind], kinds=narrower, **kwargs)
        for field in ('total', 'items', 'coverage', 'cursor', 'stale'):
            self.assertEqual(before[field], after[field], (kind, field))
        self.assertEqual(0, after['total'])
        self.assertEqual(before['watermark']['generation'], after['watermark']['generation'])
        return answer

    def assert_data(self, kind, answer, native_body=None, updated=None):
        self.assertEqual(1, answer['total'], (kind, answer))
        item = answer['items'][0]
        self.assertEqual(kind, item['kind'])
        self.assertEqual('data', item['trust'])
        for field in ('title', 'excerpt'):
            text = item[field]['text']
            self.assertLessEqual(len(text), 200, (kind, field))
            self.assertNotRegex(text, '[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]')
            self.assertNotRegex(text, r'\\u[0-9a-fA-F]{0,3}$')
        rendered = find.format_text(answer)
        self.assertNotRegex(rendered, '[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e]')
        if native_body is not None:
            self.assertEqual(hashlib.sha256(native_body.encode()).hexdigest(), item['sha256'])
        if updated is not None:
            self.assertEqual(updated, item['updated_at'])
        return item

    def stamp(self, rows):
        for row in rows:
            for comment in row.get('comments') or []:
                comment['created_at'] = '2026-01-02T03:04:05Z'
                comment['updated_at'] = UPDATED

    def test_all_eight_kinds_use_escaped_data_and_source_metadata(self):
        import test_reference_records as refs
        import test_capability_records as caps
        import test_requirement_records as requirements
        import test_briefing as checkpoints
        import requirement_http
        import decision_attest

        task = fixture.row(title=DIRTY, text=DIRTY)
        self.assert_data('task', self.visible_and_denied('task', [task]), updated=task['updated_at'])
        ordinary = fixture.comment(DIRTY, updated_at=UPDATED)
        self.assert_data('comment', self.visible_and_denied('comment',
                         [fixture.row(text='', comments=[ordinary])]), DIRTY, UPDATED)

        ref = self.own_case(refs.ReferenceCase)
        ref.propose(title=DIRTY, statement=DIRTY, owner='person:refowner')
        self.stamp(ref.native.rows)
        item = self.assert_data('ref', self.visible_and_denied('ref', ref.native.rows,
                                operators=[refs.OPERATOR], project_path=ref.project),
                                ref.native.rows[0]['comments'][0]['text'], UPDATED)
        self.assertEqual('person:refowner', item['owner'])

        cap = self.own_case(caps.CapabilityCase)
        cap.propose(name=DIRTY, summary=DIRTY, owner='person:capowner')
        self.stamp(cap.native.rows)
        item = self.assert_data('cap', self.visible_and_denied('cap', cap.native.rows,
                                operators=[refs.OPERATOR], project_path=cap.project),
                                cap.native.rows[0]['comments'][0]['text'], UPDATED)
        self.assertEqual('person:capowner', item['owner'])

        req = self.own_case(requirements.RequirementRecordTests)
        req.apply(req.draft(title=DIRTY, description=DIRTY))
        self.stamp(req.native.rows)
        def served_requirements(rows, check):
            return requirement_http.read_snapshot(req.project, fixture.PROJECT, ['list'], rows,
                                                  req.native, [refs.OPERATOR], check_budget=check)
        selected = next(row for row in req.native.rows if requirement_http.is_requirement_row(row))
        before = list(req.native.writes())
        self.assert_data('requirement', self.visible_and_denied('requirement', req.native.rows,
                         project_path=req.project, requirement_reader=served_requirements),
                         selected['comments'][0]['text'], UPDATED)
        self.assertEqual(before, req.native.writes())

        rows = checkpoints.rows()
        payload = checkpoints.checkpoint(rows, summary=DIRTY)
        checkpoints.append_checkpoint(rows, 'current-checkpoint', payload)
        self.stamp(rows)
        self.assert_data('checkpoint', self.visible_and_denied('checkpoint', rows),
                         rows[0]['comments'][-1]['text'], UPDATED)

        rows = checkpoints.rows()
        payload = checkpoints.contribution('a' * 40)
        payload['summary'] = DIRTY
        checkpoints.add_review(rows, payload, 'current-contribution')
        self.stamp(rows)
        self.assert_data('review', self.visible_and_denied('review', rows),
                         rows[0]['comments'][-1]['text'], UPDATED)

        with tempfile.TemporaryDirectory() as root:
            decision = fixture.row(issue_type='decision', title=DIRTY, text=DIRTY)
            def served_decision(row):
                return decision_attest.read_answer(row, fixture.PROJECT, {}, [], Path(root))
            self.assert_data('decision', self.visible_and_denied('decision', [decision],
                             decision_reader=served_decision), updated=decision['updated_at'])

    def test_capability_supersession_uses_the_current_own_reader_state(self):
        import test_capability_records as caps
        cap = self.own_case(caps.RetireTests)
        cap.native.actor = caps.OPERATOR
        for key, name, operation in [('a.old', 'orthogonal old', 'old'), ('a.new', 'orthogonal new', 'new')]:
            payload = caps.entry(operation='draft', operation_id=operation, key=key, name=name,
                                 acceptance_state='accepted', acceptance=caps.acceptance())
            caps.cr.apply_native(payload, caps.OPERATOR, cap.native, cap.project,
                                 operator=True, operators=[caps.OPERATOR])
        cap.retire('a.old', 'a.new', 1)
        live = fixture.search(cap.native.rows, args=['--kind', 'cap'],
                              operators=[caps.OPERATOR], project_path=cap.project)
        selected = {item['pointer']: item for item in live['items']}
        self.assertEqual('superseded', selected['cap:a.old']['state'])
        self.assertEqual('cap:a.new', selected['cap:a.old']['superseded_by'])
        self.assertEqual(['cap:a.old'], selected['cap:a.new']['replaces'])
        inert = fixture.search(cap.native.rows, args=['--kind', 'cap'], operators=[], project_path=cap.project)
        selected = {item['pointer']: item for item in inert['items']}
        # This kind's own reader marks the unprovable retired ledger malformed;
        # it does not fall back to its formerly accepted content or retirement.
        self.assertNotIn('cap:a.old', selected)
        self.assertTrue(inert['total_is_lower_bound'])
        self.assertTrue(all('replaces' not in item and 'superseded_by' not in item
                            for item in selected.values()))


if __name__ == '__main__':
    unittest.main()
