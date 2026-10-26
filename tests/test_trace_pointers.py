"""Traceability slice 2 (kittrial-5bb.417): the pointer kinds `req:` and `design:`.

TRACEABILITY_DESIGN.md §2.4 in the context index's one grammar (briefing.py): flags per
kind, resolved from caller-authorized rows on the server, never a refusal.
"""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing as b
import requirement_governance as governance
import requirement_http as http
import requirement_owner_records as owner
import requirement_records as rr
from export_requirements import REVISION_PREFIX
from requirements import canonical_bytes, content_hash
from test_http_web import TeamHarness
from test_briefing import rows, checkpoint, append_checkpoint, TASK, PROJECT
from test_reference_records import ReferenceCase, OPERATOR, acceptance
from test_requirement_records import Native
from test_requirement_owner_records import OwnerNative, ACCOUNT


def resolve(token, data, project=PROJECT, **kwargs):
    parsed = b.parse_context_pointers(token)
    assert len(parsed) == 1, (token, parsed)
    return b.ContextPointers(data, project, **kwargs).resolve(parsed[0])


def without_token(entry):
    entry = dict(entry)
    entry.pop('pointer')
    return entry


class GrammarTests(unittest.TestCase):
    def test_req_and_design_valid_forms_parse_in_the_one_grammar(self):
        text = 'see req:srv.backup@2 req:R01 design:server-redesign#plan-phase-2 design:a.b-c'
        parsed = b.parse_context_pointers(text)
        self.assertEqual([(p['kind'], p['value']) for p in parsed],
                         [('req', 'srv.backup@2'), ('req', 'R01'),
                          ('design', 'server-redesign#plan-phase-2'), ('design', 'a.b-c')])

    def test_malformed_req_and_design_tokens_are_prose(self):
        for text in ('req:a@', 'req:a@0', 'req:a@01', 'req:a@x', 'req:a@1@2', 'req:@2',
                     'req:a@1234567890', 'design:#x', 'design:A#x', 'design:a#b#c',
                     'design:a#', 'design:-a', 'design:a..b', 'design:a#' + 'x' * 121,
                     'Req:a', 'req:', 'design:'):
            with self.subTest(text=text):
                self.assertEqual(b.parse_context_pointers(text), [])
        self.assertEqual(len(b.parse_context_pointers('design:a#' + 'x' * 120)), 1)
        self.assertEqual(len(b.parse_context_pointers('req:a@123456789')), 1)

    def test_at_sign_stays_prose_for_every_older_kind(self):
        for kind in 'task comment ref cap item file room commit doc archive'.split():
            with self.subTest(kind=kind):
                self.assertEqual(b.parse_context_pointers(kind + ':a@1'), [])
                self.assertEqual(len(b.parse_context_pointers(kind + ':a1')), 1)


class HostRequirementTests(unittest.TestCase):
    """Contributor drafts and operator acceptances, as `requirement_records` writes them."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.native = Native()
        self.native.seed('job-1')
        self.operators = ['operator']
        self.draft()

    def write(self, actor, operator=False, **payload):
        self.native.actor = actor
        base = dict(schema_version=1, kind='requirement', title='R01: Intent', key='R01',
                    description='Statement.', acceptance_state='draft')
        base.update(payload)
        return rr.apply_native(base, actor, self.native, self.project, operator=operator,
                               operators=self.operators if operator else None)

    def draft(self):
        self.write('alice', operation_id='op-1', operation='draft', parent='job-1')

    def revise(self, revision, accepted=False):
        extra = dict(acceptance_state='accepted', acceptance=dict(
            owners=['owner-a'], approvers=['owner-a'], policy='any-owner',
            decision_id='decision-%d' % revision, evidence='review')) if accepted else {}
        self.write('operator' if accepted else 'alice', operator=accepted,
                   operation_id='op-%d' % revision, operation='revise', task='req-1',
                   revision=revision, description='Statement %d.' % revision, **extra)

    def resolve(self, token, data=None, **kwargs):
        kwargs.setdefault('operators', self.operators)
        return resolve(token, self.native.rows if data is None else data, **kwargs)

    def sha(self, revision):
        return self.native.record('req-1', revision)['sha256']

    def test_draft_only_requirement_is_live_with_the_draft_flag(self):
        answer = self.resolve('req:R01')
        self.assertEqual(answer['state'], 'live')
        self.assertEqual(answer['flags'], ['draft'])
        self.assertEqual(answer['reference'], {'id': 'req-1', 'revision': 1, 'sha256': self.sha(1)})
        self.assertEqual(answer['label']['text'], 'R01: Intent')
        self.assertEqual(answer['follow'], 'requirements get req-1')
        self.assertEqual(answer['trust'], 'data')

    def test_newest_accepted_revision_is_live_without_flags(self):
        self.revise(2, accepted=True)
        answer = self.resolve('req:R01@2')
        self.assertEqual((answer['state'], answer['flags']), ('live', []))
        self.assertEqual(answer['reference']['sha256'], self.sha(2))

    def test_older_accepted_revision_is_superseded(self):
        self.revise(2, accepted=True)
        self.revise(3, accepted=True)
        answer = self.resolve('req:R01@2')
        self.assertEqual((answer['state'], answer['flags']), ('superseded', ['superseded']))
        self.assertEqual(answer['reference']['revision'], 2)

    def test_older_draft_under_a_newer_acceptance_is_draft_and_superseded(self):
        self.revise(2, accepted=True)
        answer = self.resolve('req:R01@1')
        self.assertEqual((answer['state'], answer['flags']), ('superseded', ['draft', 'superseded']))

    def test_unknown_revision_and_unknown_key(self):
        answer = self.resolve('req:R01@9')
        self.assertEqual(answer, {'pointer': 'req:R01@9', 'kind': 'req', 'state': 'unavailable',
                                  'trust': 'data', 'flags': ['unknown-revision']})
        answer = self.resolve('req:R99')
        self.assertEqual(answer, {'pointer': 'req:R99', 'kind': 'req', 'state': 'unavailable',
                                  'trust': 'data', 'flags': ['unknown-key']})

    def test_acceptance_reads_exactly_as_the_requirements_read_does(self):
        # One acceptance authority: the pointer calls resolved_acceptance with the
        # brief's operator list, as requirement_http.read does; it never adds its own.
        self.revise(2, accepted=True)
        with patch.object(rr, 'resolved_acceptance', return_value=False) as check:
            answer = self.resolve('req:R01@2')
        self.assertEqual((answer['state'], answer['flags']), ('unavailable', []))
        self.assertNotIn('label', answer)
        self.assertTrue(all(call.args[2] == self.operators for call in check.call_args_list))

    def test_malformed_ledger_is_unavailable_not_unknown(self):
        row = self.native.row('req-1')
        record = dict(self.native.record('req-1', 1), description='Conflicting text')
        record['sha256'] = content_hash({k: v for k, v in record.items() if k != 'sha256'})
        row['comments'].append(dict(id='99', author='alice', created_at='2026-09-25T00:00:00Z',
                                    text=REVISION_PREFIX + canonical_bytes(record).decode('utf-8')))
        answer = self.resolve('req:R01')
        self.assertEqual((answer['state'], answer['flags']), ('unavailable', []))

    def test_conflicting_type_labels_are_unavailable(self):
        self.native.row('req-1')['labels'].append('brd-section')
        answer = self.resolve('req:R01')
        self.assertEqual((answer['state'], answer['flags']), ('unavailable', []))

    def test_a_row_whose_revisions_disagree_on_the_key_is_unavailable(self):
        row = self.native.row('req-1')
        record = dict(self.native.record('req-1', 1), revision=2, key='R02', description='Other key')
        record['sha256'] = content_hash({k: v for k, v in record.items() if k != 'sha256'})
        row['comments'].append(dict(id='98', author='alice', created_at='2026-09-25T00:00:00Z',
                                    text=REVISION_PREFIX + canonical_bytes(record).decode('utf-8')))
        for token in ('req:R01', 'req:R02'):
            answer = self.resolve(token)
            self.assertEqual((answer['state'], answer['flags']), ('unavailable', []), token)

    def test_a_revision_copied_onto_an_unlabelled_row_never_resolves(self):
        copy_row = copy.deepcopy(self.native.row('req-1'))
        copy_row['id'] = 'plain-1'
        copy_row['labels'] = []
        answer = self.resolve('req:R01', [copy_row])
        self.assertEqual((answer['state'], answer['flags']), ('unavailable', ['unknown-key']))

    def test_two_rows_claiming_one_key_are_unknown(self):
        twin = copy.deepcopy(self.native.row('req-1'))
        twin['id'] = 'req-twin'
        answer = self.resolve('req:R01', self.native.rows + [twin])
        self.assertEqual((answer['state'], answer['flags']), ('unavailable', ['unknown-key']))

    def test_another_projects_requirement_never_resolves(self):
        foreign = copy.deepcopy(self.native.rows)
        for row in foreign:
            row['project_id'] = 'other'
        answer = self.resolve('req:R01', foreign)
        self.assertEqual(answer, self.resolve('req:R01', []))
        self.assertEqual(answer['flags'], ['unknown-key'])
        # A project-qualified spelling is just a key this project does not have.
        self.assertEqual(self.resolve('req:other/R01')['flags'], ['unknown-key'])

    def test_hidden_equals_absent_byte_for_byte_apart_from_the_token(self):
        self.revise(2, accepted=True)
        for token in ('req:R01', 'req:R01@2', 'req:R01@9'):
            with self.subTest(token=token):
                hidden = self.resolve(token, can_read=lambda row: row['id'] != 'req-1')
                absent = self.resolve(token, [r for r in self.native.rows if r['id'] != 'req-1'])
                self.assertEqual(json.dumps(hidden, sort_keys=False), json.dumps(absent, sort_keys=False))
                self.assertEqual(hidden['flags'], ['unknown-key'])
                self.assertNotIn('R01: Intent', json.dumps(hidden))

    def test_requirement_title_is_untrusted_text(self):
        self.native.row('req-1')
        record_title = '\x1b‮<b>' + 'z' * 140
        self.write('alice', operation_id='op-t', operation='revise', task='req-1', revision=2,
                   title=record_title)
        answer = self.resolve('req:R01@2')
        self.assertEqual(answer['label'], b.clip(b.plain_text(record_title), 120))
        self.assertGreater(answer['label']['omitted_chars'], 0)


class OwnerRequirementTests(unittest.TestCase):
    """Owner decisions bind the project's governance history (requirement_http.read)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'alpha'
        self.path.mkdir()
        governance.initialize(self.path, 'alpha', ACCOUNT, 'creation-alpha')
        self.context = http.OwnerContext('alpha', ACCOUNT, governance.current(self.path, 'alpha'))
        self.native = OwnerNative()

    def accepted(self, key):
        draft = http.apply(self.path, self.context, 'create', dict(kind='requirement', parent='job-1',
            title=key, description='Immutable ' + key, key=key), 'create-' + key, self.native)
        return http.apply(self.path, self.context, 'accept', dict(expected_revision=draft['revision'],
            expected_sha256=draft['sha256']), 'accept-' + key, self.native, draft['id'])

    def terminal(self, item, action, operation, **extra):
        body = dict(expected_revision=item['revision'], expected_sha256=item['sha256'],
                    expected_state_sha256=None, reason='No longer needed.', **extra)
        return http.apply(self.path, self.context, action, body, operation, self.native, item['id'])

    def snapshot(self):
        return governance.snapshot(self.path, 'alpha')

    def resolve(self, token, **kwargs):
        return resolve(token, self.native.rows, project='alpha', **kwargs)

    def key(self, item):
        return rr.existing_revisions(self.native.row(item['id']))[item['revision']]['key']

    def test_owner_acceptance_is_live_only_with_a_matching_governance_snapshot(self):
        item = self.accepted('srv.backup')
        token = 'req:' + self.key(item)
        live = self.resolve(token, governance=self.snapshot())
        self.assertEqual((live['state'], live['flags']), ('live', []))
        self.assertEqual(live['reference']['id'], item['id'])
        missing = self.resolve(token)
        self.assertEqual((missing['state'], missing['flags']), ('unavailable', []))
        self.assertNotIn('label', missing)
        other = Path(self.path.parent) / 'beta'
        other.mkdir()
        governance.initialize(other, 'beta', ACCOUNT, 'creation-beta')
        wrong = self.resolve(token, governance=governance.snapshot(other, 'beta'))
        self.assertEqual((wrong['state'], wrong['flags']), ('unavailable', []))

    def test_current_means_the_newest_accepted_revision_not_a_newer_draft(self):
        item = self.accepted('srv.backup')
        http.apply(self.path, self.context, 'revise', {
            'expected_revision': item['revision'], 'expected_sha256': item['sha256'],
            'title': 'Changed', 'description': 'A new draft.'}, 'edit-1', self.native, task=item['id'])
        token = 'req:' + self.key(item)
        current = self.resolve(token, governance=self.snapshot())
        self.assertEqual(current['reference']['revision'], item['revision'])
        self.assertEqual((current['state'], current['flags']), ('live', []))
        newer = self.resolve(token + '@' + str(item['revision'] + 1), governance=self.snapshot())
        self.assertEqual((newer['state'], newer['flags']), ('live', ['draft']))

    def test_withdrawn_requirement_reads_superseded_with_the_withdrawn_flag(self):
        item = self.accepted('srv.backup')
        self.terminal(item, 'withdraw', 'withdraw-1')
        answer = self.resolve('req:' + self.key(item), governance=self.snapshot())
        self.assertEqual((answer['state'], answer['flags']), ('superseded', ['withdrawn']))
        self.assertNotEqual(answer['state'], 'live')

    def test_owner_superseded_requirement_reads_superseded(self):
        item = self.accepted('srv.backup')
        successor = self.accepted('srv.backup2')
        self.terminal(item, 'supersede', 'supersede-1',
                      successor={k: successor[k] for k in ('id', 'revision', 'sha256')})
        answer = self.resolve('req:' + self.key(item), governance=self.snapshot())
        self.assertEqual((answer['state'], answer['flags']), ('superseded', ['superseded']))
        later = self.resolve('req:' + self.key(successor), governance=self.snapshot())
        self.assertEqual((later['state'], later['flags']), ('live', []))

    def test_unverifiable_owner_state_is_unavailable_never_promoted(self):
        item = self.accepted('srv.backup')
        self.terminal(item, 'withdraw', 'withdraw-1')
        row = self.native.row(item['id'])
        row['comments'] = [c for c in row['comments'] if not c['text'].startswith(owner.REASON_PREFIX)]
        answer = self.resolve('req:' + self.key(item), governance=self.snapshot())
        self.assertEqual((answer['state'], answer['flags']), ('unavailable', []))
        self.assertNotIn('label', answer)
        # A terminal label with no state evidence is unverifiable too.
        row = self.native.row(self.accepted('srv.other')['id'])
        row['labels'].append('requirement:withdrawn')
        answer = self.resolve('req:srv.other', governance=self.snapshot())
        self.assertEqual((answer['state'], answer['flags']), ('unavailable', []))


    def test_terminal_state_bound_to_another_governance_is_unavailable(self):
        item = self.accepted('srv.backup')
        self.terminal(item, 'withdraw', 'withdraw-1')
        row = self.native.row(item['id'])
        comment = next(c for c in row['comments'] if c['text'].startswith(owner.STATE_PREFIX))
        reason = next(c for c in row['comments'] if c['text'].startswith(owner.REASON_PREFIX))
        state = owner.parse(comment['text'], state=True)
        changed = dict(state, governance=dict(state['governance'], sha256='0' * 64))
        changed['sha256'] = content_hash(changed)
        comment['text'] = owner.body(changed, state=True)
        why = owner.parse_reason(reason['text'])
        why = dict(why, state_sha256=changed['sha256'])
        why['sha256'] = content_hash(why)
        reason['text'] = owner.reason_body(why)
        self.assertIsNotNone(owner.state_ledger(row)[0])
        answer = self.resolve('req:' + self.key(item), governance=self.snapshot())
        self.assertEqual((answer['state'], answer['flags']), ('unavailable', []))


def design_document(scope):
    """A minimal design document for `design.<scope>` (kittrial-5bb.418: every new design entry carries one)."""
    return '<!-- design-v1\nscope: %s\nstatus: candidate\n-->\n# Design\n\n## Plan phase 2\n' % scope


class DesignPointerTests(ReferenceCase):
    """`design:SCOPE#section` against the reference entry `design.<scope>` (§2.2)."""

    def propose(self, **extra):
        key = extra.get('key', 'calendar.trading')
        if not key.startswith('design.'):
            return super().propose(**extra)
        import hashlib
        import reference_records
        from test_reference_records import entry
        text = design_document(key[len('design.'):])
        extra['authority'] = {'type': 'repo-document', 'path': 'docs/design/DESIGN.md', 'commit': 'a' * 40,
                              'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        self.native.actor = 'alice'
        return reference_records.apply_native(entry(**extra), 'alice', self.native, self.project, document=text)

    def resolve(self, token, data=None, **kwargs):
        return resolve(token, self.native.rows if data is None else data, operators=[OPERATOR], **kwargs)

    def test_unknown_scope_with_and_without_a_section(self):
        self.assertEqual(self.resolve('design:nothing', []),
                         {'pointer': 'design:nothing', 'kind': 'design', 'state': 'unavailable',
                          'trust': 'data', 'flags': ['unknown-scope']})
        answer = self.resolve('design:nothing#plan-phase-1', [])
        self.assertEqual(answer['flags'], ['unknown-scope'])
        self.assertEqual(answer['section'], {'anchor': 'plan-phase-1', 'state': 'unchecked'})

    def test_draft_entry_is_not_selected_then_accepted_entry_is_selected(self):
        self.propose(key='design.server-redesign')
        answer = self.resolve('design:server-redesign#plan-phase-2')
        self.assertEqual((answer['state'], answer['flags']), ('live', ['not-selected']))
        self.assertEqual(answer['follow'], 'ref get design.server-redesign')
        self.assertEqual(answer['section'], {'anchor': 'plan-phase-2', 'state': 'unchecked'})
        self.accept(1, self.sha(1, key='design.server-redesign'), key='design.server-redesign')
        answer = self.resolve('design:server-redesign#plan-phase-2')
        self.assertEqual((answer['state'], answer['flags']), ('live', []))
        self.assertEqual(answer['label']['text'], 'Trading calendar authority')
        self.assertEqual(answer['section']['state'], 'unchecked')
        whole = self.resolve('design:server-redesign')
        self.assertNotIn('section', whole)

    def test_retired_design_entry_reads_superseded(self):
        import reference_records
        self.propose(key='design.old')
        self.propose(key='design.new', operation_id='next-ref')
        self.native.actor = OPERATOR
        reference_records.apply_native(dict(schema_version=1, operation_id='retire-ref', operation='retire',
            key='design.old', revision=1, record_sha256=self.sha(1, key='design.old'), successor='design.new',
            acceptance_state='superseded', acceptance=acceptance()),
            OPERATOR, self.native, self.project, operator=True, operators=[OPERATOR], web_coordinator=True)
        answer = self.resolve('design:old#x')
        self.assertEqual((answer['state'], answer['flags']), ('superseded', []))

    def test_section_is_never_checked_before_slice_three(self):
        self.propose(key='design.server-redesign')
        for token in ('design:server-redesign#no-such-heading', 'design:missing#no-such-heading'):
            answer = self.resolve(token)
            self.assertEqual(answer['section']['state'], 'unchecked')
            self.assertNotIn('unknown-section', answer['flags'])

    def test_unreadable_or_ambiguous_design_entries_are_unknown_scope(self):
        import reference_records
        good = dict(key='design.x', state='accepted', record=dict(name='X', acceptance_state='accepted'))
        for views in ([dict(good, state=s) for s in ('malformed',)], [dict(good, state='conflicted')],
                      [dict(good, state='incomplete')], [dict(good, state='unsupported')], [good, good]):
            with patch.object(reference_records, 'catalog', return_value=(views, [])):
                answer = self.resolve('design:x', [])
            self.assertEqual((answer['state'], answer['flags']), ('unavailable', ['unknown-scope']), views)
        with patch.object(reference_records, 'catalog', return_value=([good], [])):
            self.assertEqual(self.resolve('design:x', [])['state'], 'live')

    def test_design_names_only_design_entries_never_another_reference(self):
        self.propose()  # the reference entry calendar.trading, not a design
        self.assertEqual(self.resolve('ref:calendar.trading')['state'], 'live')
        answer = self.resolve('design:calendar.trading')
        self.assertEqual((answer['state'], answer['flags']), ('unavailable', ['unknown-scope']))

    def test_hidden_equals_absent_and_another_project_never_resolves(self):
        self.propose(key='design.server-redesign')
        token = 'design:server-redesign#plan'
        hidden = self.resolve(token, can_read=lambda row: False)
        absent = self.resolve(token, [])
        self.assertEqual(json.dumps(hidden), json.dumps(absent))
        foreign = copy.deepcopy(self.native.rows)
        for row in foreign:
            row['project_id'] = 'other'
        self.assertEqual(json.dumps(self.resolve(token, foreign)), json.dumps(absent))


class SurfaceTests(unittest.TestCase):
    def brief(self, source, journal=None):
        data = rows()
        append_checkpoint(data, 'cp', checkpoint(data, open_items=[dict(id='x', kind='question', text='Q', source=source)]))
        return b.brief(data, PROJECT, TASK, journal=journal)

    def test_governance_is_read_only_when_a_req_token_is_present(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(governance, 'snapshot', return_value={}) as read:
            self.brief('task:' + TASK + ' design:x#y ref:a.b prose', journal=Path(directory))
            read.assert_not_called()
            answer = self.brief('see req:R01@2', journal=Path(directory))
            read.assert_called_once_with(Path(directory), PROJECT)
        pointer = answer['unresolved']['pointer_items']['x']['pointers'][0]
        self.assertEqual(pointer['flags'], ['unknown-key'])

    def test_unreadable_governance_leaves_owner_evidence_unverifiable_not_a_failure(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(governance, 'snapshot', side_effect=ValueError('broken')):
            answer = self.brief('req:R01', journal=Path(directory))
        self.assertEqual(answer['unresolved']['pointer_items']['x']['pointers'][0]['state'], 'unavailable')

    def test_text_brief_shows_flags_and_section(self):
        answer = self.brief('req:R01@2 design:server-redesign#plan-phase-2 task:' + TASK)
        rendered = b.format_brief(answer)
        self.assertIn('req:R01@2 [unavailable; trust=data] flags=unknown-key', rendered)
        self.assertIn('design:server-redesign#plan-phase-2 [unavailable; trust=data] flags=unknown-scope '
                      'section=plan-phase-2 unchecked', rendered)
        # The older kinds' lines are unchanged.
        self.assertIn('task:' + TASK + ' [live; trust=data] A task', rendered)

    def test_new_kinds_share_the_existing_budget_and_display_copies(self):
        raw = [dict(id=str(n), source=' '.join(['req:R1', 'design:a'] * 6)) for n in range(6)]
        answer = b.ContextPointers(rows(), PROJECT).item_pointers(raw, 'source')
        self.assertEqual(sum(p['state'] != 'unresolved' for i in answer.values() for p in i['pointers']), 50)
        groups = b.bound_context_pointer_display([answer])
        shown = [p for item in groups[0].values() for p in item['pointers'] if p['state'] != 'unresolved']
        self.assertTrue(all('flags' in p for p in shown))
        late = [p for item in groups[0].values() for p in item['pointers'] if p['state'] == 'unresolved']
        self.assertTrue(late and all(set(p) == {'pointer', 'kind', 'trust', 'state'} for p in late))


class TracePointerHttpTests(TeamHarness):
    def test_task_page_brief_shows_flags_and_section_and_leaks_nothing(self):
        self.claim(self.carl)
        foreign = self.create_task(self.otto, self.other, 'PRIVATE title').data['id']
        items = [dict(id='trace', kind='question', text='Q',
                      source='req:R01@2 design:server-redesign#plan-phase-2 task:' + foreign)]
        answer = self.request('POST', self.path('/tasks/' + self.task + '/checkpoints'),
                              dict(previous=None, summary='Trace', open_items=items), token=self.carl)
        self.assertEqual(answer.status, 201, answer.data)
        result = self.request('GET', self.path('/tasks/' + self.task + '/brief'), token=self.vera)
        self.assertEqual(result.status, 200, result.data)
        shown = result.data['checkpoint']['pointer_items']['trace']['pointers']
        self.assertEqual([(p['kind'], p['state'], p.get('flags')) for p in shown],
                         [('req', 'unavailable', ['unknown-key']), ('design', 'unavailable', ['unknown-scope']),
                          ('task', 'unavailable', None)])
        self.assertEqual(shown[1]['section'], {'anchor': 'plan-phase-2', 'state': 'unchecked'})
        self.assertEqual(result.data['checkpoint']['open_items'], items)
        self.assertNotIn('PRIVATE title', str(result.data))


class TracePointerWebTests(unittest.TestCase):
    def test_flags_and_section_render_as_text_nodes(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        root = Path(__file__).resolve().parents[1]
        code = '''await import(process.argv[1]);
const view=await import(process.argv[2]);
const el=view.contextPointers({pointers:[{pointer:'req:R01@2',kind:'req',state:'superseded',trust:'data',
 flags:['draft','superseded'],label:{text:'<img src=x onerror=evil()>'},follow:'requirements get req-1'},
 {pointer:'design:a#b',kind:'design',state:'unavailable',trust:'data',flags:['unknown-scope'],
 section:{anchor:'b',state:'unchecked'}},
 {pointer:'task:t',kind:'task',state:'live',trust:'data'}],pointers_omitted:0});
console.log(JSON.stringify({text:el.textContent,images:el.querySelectorAll('img').length,links:el.querySelectorAll('a').length}));'''
        done = test_http_web.run_node_module(self, node, code, (root / 'tests/web_dom_shim.mjs').as_uri(),
                                             (root / 'web/js/views/task.js').as_uri())
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertIn('flags: draft, superseded', result['text'])
        self.assertIn('flags: unknown-scope', result['text'])
        self.assertIn('section b unchecked', result['text'])
        self.assertIn('<img src=x onerror=evil()>', result['text'])
        self.assertEqual(result['text'].count('flags:'), 2)
        self.assertEqual((result['images'], result['links']), (0, 0))


if __name__ == '__main__':
    unittest.main()
