"""Traceability slice 3, part 1 (kittrial-5bb.418): design documents on the server.

TRACEABILITY_DESIGN.md §5.1 steps 1-3: a design.<scope> entry's repo-document authority, the
upload with its bounds, the bytes in the task-files store, the server-derived design-index-v1
record, and acceptance re-checking bytes, sha256, index and scope. One named test per rule.
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import design_documents as dd
import reference_records as rr
import reserved_comments
import task_files
from test_reference_records import ReferenceCase, entry, acceptance, OPERATOR

KIT = Path(__file__).resolve().parents[1]
SCOPE = 'server-redesign'
KEY = 'design.' + SCOPE
BODY = ('# Server redesign\n\nThe intro cites req:srv.backup@2 and design:other-design#x .\n\n'
        '## Plan phase 1\n\n- contract: the new API\n- test oracle: api-smoke\n- epic: proj-41\n'
        'req:srv.migration@1\n')


def document(scope=SCOPE, body=BODY, status='candidate'):
    return ('<!-- design-v1\nscope: %s\nstatus: %s   # the author\'s claim\nrequirements: [srv.backup@2]\n'
            'decisions-open: [proj-41]\n-->\n' % (scope, status)) + body


def authority(text, path='docs/design/SERVER_REDESIGN.md', commit='a' * 40):
    return {'type': 'repo-document', 'path': path, 'commit': commit,
            'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}


class DesignCase(ReferenceCase):
    def propose_design(self, text=None, key=KEY, operation_id='design-1', limits=None, **extra):
        text = document() if text is None else text
        extra.setdefault('authority', authority(text))
        self.native.actor = 'alice'
        return rr.apply_native(entry(key=key, operation_id=operation_id, **extra), 'alice', self.native,
                               self.project, document=text, document_limits=limits)

    def revise_design(self, text, revision, operation_id='design-2', **extra):
        return self.propose_design(text, operation='revise', operation_id=operation_id, revision=revision,
                                   expected_sha256=self.sha(revision - 1, key=KEY), **extra)

    def accept_design(self, revision, **extra):
        return self.accept(revision, self.sha(revision, key=KEY), key=KEY,
                           operation_id='design-accept-%d' % revision, **extra)

    def anchor(self, key=KEY):
        return rr.anchor_for(rr.read_key_rows(self.native, key), key)[0]

    def indexes(self, text=None, key=KEY):
        return dd.indexes_for(self.anchor(key), key, authority(document() if text is None else text))

    def blobs(self):
        base = task_files.folder(self.project)
        return sorted(name for name, _ in task_files._blob_names(base)) if base is not None else []

    def writes(self):
        return len(self.native.writes())

    def nothing_written(self, before_writes, before_blobs):
        self.assertEqual(self.writes(), before_writes)
        self.assertEqual(self.blobs(), before_blobs)


class UploadTests(DesignCase):
    def test_propose_stores_the_bytes_journals_them_on_the_anchor_and_writes_one_index(self):
        text = document()
        outcome = self.propose_design(text)
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        self.assertEqual(outcome['document']['sha256'], digest)
        self.assertEqual(self.blobs(), [digest])
        self.assertEqual(dd.read_document(self.project, digest), text.encode('utf-8'))
        entries = [entry for _, entry in task_files.all_entries(self.project)]
        self.assertEqual([(e['task'], e['direction'], e['sha256']) for e in entries],
                         [(outcome['native_id'], 'design', digest)])
        self.assertEqual(len(self.indexes()), 1)
        self.assertIn(digest, task_files.referenced(self.project))

    def test_the_index_is_derived_by_the_server_from_the_bytes(self):
        self.propose_design()
        index = self.indexes()[0]
        self.assertEqual(index['front'], {'scope': SCOPE, 'status': 'candidate', 'requirements': ['srv.backup@2'],
                                          'decisions_open': ['proj-41'], 'alternatives': [], 'supersedes': []})
        self.assertEqual([(h['anchor'], h['level']) for h in index['headings']],
                         [('server-redesign', 1), ('plan-phase-1', 2)])
        self.assertEqual(index['headings'][0]['tokens'], ['req:srv.backup@2', 'design:other-design#x'])
        self.assertEqual(index['headings'][1]['tokens'], ['req:srv.migration@1'])
        self.assertEqual(index['plan_phases'], [{'anchor': 'plan-phase-1', 'contract': ['the new API'],
                                                 'test_oracles': ['api-smoke'], 'requirements': ['req:srv.migration@1'],
                                                 'epic': 'proj-41'}])
        self.assertEqual(index, dd.derive_index(document(), KEY, index['path'], index['commit']))

    def test_a_document_that_is_not_the_one_the_authority_names_is_refused_and_nothing_is_written(self):
        writes, blobs = self.writes(), self.blobs()
        with self.assertRaisesRegex(ValueError, 'not the one authority.sha256 names'):
            self.propose_design(document(), authority=authority(document(body='# Other\n')))
        self.nothing_written(writes, blobs)

    def test_each_bound_is_refused_before_anything_is_written(self):
        many = ''.join('## Heading %d\n' % n for n in range(dd.HEADINGS_MAX + 1))
        tokens = ' '.join('req:k%d' % n for n in range(dd.TOKENS_MAX + 1))
        long_tokens = '\n'.join('# H%d\n' % n + ' '.join('req:%s%d' % ('k' * 180, m) for m in range(5))
                                for n in range(120))
        cases = {
            'over 1 MiB': document(body='x' * (dd.DOCUMENT_MAX_BYTES + 1)),
            'NUL': document(body='a\x00b'),
            'not UTF-8 (a lone surrogate)': document(body='\ud800'),
            'headings': document(body=many),
            'tokens': document(body='# One\n' + tokens),
            'index too large': document(body=long_tokens),
            'no front block': '# Just prose\n',
            'scope of another key': document(scope='other-design'),
            'status': document(status='chosen'),
            'unknown front field': document().replace('status:', 'owner: x\nstatus:'),
            'a field twice': document().replace('status: candidate', 'status: candidate\nstatus: selected'),
        }
        for name, text in cases.items():
            with self.subTest(name):
                writes, blobs = self.writes(), self.blobs()
                try:
                    auth = authority(text)
                except UnicodeEncodeError:
                    auth = dict(authority(document()), sha256='0' * 64)
                with self.assertRaises(ValueError):
                    self.propose_design(text, authority=auth)
                self.nothing_written(writes, blobs)

    def test_the_front_block_is_required_where_the_document_opens(self):
        for text in ('# Just prose\n', '# Title\n' + document()):
            with self.assertRaisesRegex(ValueError, 'must open with a <!-- design-v1'):
                dd.front_block(text)
        self.assertEqual(dd.front_block('﻿\r\n' + document())['scope'], SCOPE)

    def test_text_that_is_not_utf8_is_refused_by_the_bytes_check_itself(self):
        for text in ('\ud800', 'a\udfffb'):
            with self.assertRaisesRegex(ValueError, 'valid UTF-8'):
                dd.document_bytes(text)

    def test_a_forged_repo_document_record_on_another_key_never_parses(self):
        self.propose_design()
        body = next(c['text'] for c in self.anchor()['comments'] if c['text'].startswith(rr.ENTRY_V3_PREFIX))
        record = json.loads(body[len(rr.ENTRY_V3_PREFIX):])
        record['key'] = 'calendar.trading'
        record.pop('sha256')
        record['sha256'] = rr.content_hash(record)
        forged = rr.ENTRY_V3_PREFIX + rr.canonical_bytes(record).decode('utf-8')
        self.assertIsNone(rr.parse_entry(forged))

    def test_at_most_one_document_travels_with_a_write(self):
        attachments = {'0': {'flag': '--file', 'text': json.dumps(entry(key=KEY))},
                       '1': {'flag': '--document', 'text': 'a'}, '2': {'flag': '--document', 'text': 'b'}}
        with self.assertRaisesRegex(ValueError, 'at most one --document'):
            rr.write_payload_and_document(['propose', '@attachment:0', '@attachment:1', '@attachment:2'], attachments)

    def test_the_bounds_themselves_are_allowed(self):
        exact = document(body='')
        exact = exact + 'x' * (dd.DOCUMENT_MAX_BYTES - len(exact.encode('utf-8')))
        self.assertEqual(len(dd.document_bytes(exact)), dd.DOCUMENT_MAX_BYTES)
        with self.assertRaisesRegex(ValueError, 'the limit is'):
            dd.document_bytes(exact + 'x')
        headings = ''.join('## H%d\n' % n for n in range(dd.HEADINGS_MAX))
        self.assertEqual(len(dd.derive_index(document(body=headings), KEY, 'a.md', 'a' * 40)['headings']),
                         dd.HEADINGS_MAX)
        tokens = '# One\n' + ' '.join('req:k%d' % n for n in range(dd.TOKENS_MAX))
        self.assertEqual(len(dd.derive_index(document(body=tokens), KEY, 'a.md', 'a' * 40)['headings'][0]['tokens']),
                         dd.TOKENS_MAX)

    def test_design_keys_take_a_repo_document_and_only_design_keys_do(self):
        with self.assertRaisesRegex(ValueError, 'repo-document'):
            self.propose(key=KEY)                     # a repo-path authority on a design key
        text = document()
        with self.assertRaisesRegex(ValueError, 'design.<scope>'):
            self.propose_design(text, key='calendar.trading')

    def test_the_document_and_its_authority_go_together(self):
        text = document()
        self.native.actor = 'alice'
        with self.assertRaisesRegex(ValueError, 'written with its document'):
            rr.apply_native(entry(key=KEY, authority=authority(text)), 'alice', self.native, self.project)
        with self.assertRaisesRegex(ValueError, 'only a design.<scope> propose or revise'):
            rr.apply_native(entry(), 'alice', self.native, self.project, document=text)

    def test_a_direct_accepted_design_revision_is_refused(self):
        text = document()
        self.native.actor = OPERATOR
        payload = entry(key=KEY, authority=authority(text), operation='draft', acceptance_state='accepted',
                        acceptance=acceptance(), revision=..., expected_sha256=...)
        with self.assertRaisesRegex(ValueError, 'proposed and then accepted'):
            rr.apply_native(payload, OPERATOR, self.native, self.project, operator=True, operators=[OPERATOR],
                            document=text)

    def test_repository_path_rules(self):
        text = document()
        for path in ('../x.md', '/abs/x.md', 'a\\b.md', 'C:/x.md', '~/x.md', 'a//b.md', './a.md', 'a/../b.md', ''):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, 'authority.path'):
                    self.propose_design(text, authority=authority(text, path=path))
        for bad, why in (({'commit': 'abc'}, 'authority.commit'), ({'sha256': 'A' * 64}, 'authority.sha256 must be'),
                         ({'sha256': 'a' * 63}, 'authority.sha256 must be')):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, why):
                    self.propose_design(text, authority=dict(authority(text), **bad))
        without = authority(text)
        del without['commit']
        with self.assertRaisesRegex(ValueError, 'pinned commit'):
            self.propose_design(text, authority=without)

    def test_crlf_and_a_byte_order_mark_are_kept_exactly(self):
        text = '\ufeff' + document().replace('\n', '\r\n')
        outcome = self.propose_design(text)
        self.assertEqual(dd.read_document(self.project, outcome['document']['sha256']), text.encode('utf-8'))
        self.assertEqual(len(self.indexes(text)), 1)

    def test_a_retry_after_the_index_write_failed_finishes_it(self):
        self.native.fail_comment_prefix = dd.INDEX_PREFIX
        with self.assertRaises(ValueError):
            self.propose_design()
        self.assertEqual(self.indexes(), [])
        self.native.fail_comment_prefix = None
        outcome = self.propose_design()
        self.assertTrue(outcome['reconciled'] or outcome['created'] is False)
        self.assertEqual(len(self.indexes()), 1)
        self.assertEqual(len(rr.existing_revisions(self.anchor())), 1)

    def test_a_revision_never_changes_the_accepted_one(self):
        first = document()
        self.propose_design(first)
        self.accept_design(1)
        accepted_index = self.indexes(first)
        second = document(body='# Another design\n\n## Plan phase 9\n')
        self.revise_design(second, 3)
        row = self.anchor()
        view = rr.KIND.entry_view(row, [OPERATOR])
        self.assertEqual(view['record']['authority'], authority(first))
        self.assertEqual(self.indexes(first), accepted_index)
        self.assertEqual(len(self.indexes(second)), 1)
        self.assertEqual(dd.read_document(self.project, authority(first)['sha256']), first.encode('utf-8'))

    def test_one_document_in_two_revisions_is_one_blob_one_entry_one_index(self):
        text = document()
        self.propose_design(text)
        self.revise_design(text, 2, statement='Same document, a new statement.')
        self.assertEqual(len(self.blobs()), 1)
        self.assertEqual(len(task_files.all_entries(self.project)), 1)
        self.assertEqual(len(self.indexes()), 1)

    def test_the_design_document_count_and_the_project_bytes_are_bounded(self):
        self.propose_design(limits={'max_design_documents': 1})
        writes, blobs = self.writes(), self.blobs()
        with self.assertRaisesRegex(ValueError, 'max_design_documents'):
            self.revise_design(document(body='# Second\n'), 2, limits={'max_design_documents': 1})
        self.nothing_written(writes, blobs)
        with self.assertRaisesRegex(ValueError, 'max_project_bytes'):
            self.revise_design(document(body='# Third\n'), 2, limits={'max_project_bytes': 10})
        self.nothing_written(writes, blobs)

    def test_a_refusal_whose_text_says_uncertain_still_removes_the_bytes(self):
        # Review b1: refusals echo caller text, so 'uncertain' in a decision id or key must not keep a blob.
        for extra in ({'decisions': ['zz-uncertain']}, {'key': 'design.uncertain', 'expected_sha256': '0' * 64,
                                                        'operation': 'revise', 'revision': 2}):
            with self.subTest(extra=extra):
                key = extra.get('key', KEY)
                text = document(scope=key[len('design.'):])
                with self.assertRaises(ValueError) as caught:
                    self.propose_design(text, key=key, operation_id='orphan-' + key, **{k: v for k, v in extra.items()
                                                                                      if k != 'key'})
                self.assertIn('uncertain', str(caught.exception))
                self.assertNotIsInstance(caught.exception, rr.core.UncertainOutcome)
                self.assertEqual(self.blobs(), [])
                self.assertEqual(task_files.project_bytes(self.project), 0)

    def test_a_genuinely_uncertain_write_keeps_the_bytes_for_its_retry(self):
        self.native.fail_comment_prefix = rr.ENTRY_V3_PREFIX
        with self.assertRaises(rr.core.UncertainOutcome):
            self.propose_design()
        self.assertEqual(self.blobs(), [authority(document())['sha256']])
        self.native.fail_comment_prefix = None
        self.propose_design()
        self.assertEqual(self.blobs(), [authority(document())['sha256']])
        self.assertEqual(len(self.indexes()), 1)

    def test_a_document_in_another_project_never_serves_this_one(self):
        # The plan's cross-project row: the bytes, the journal and the index live in one project's folder and
        # anchor; another project with the same rows but its own (empty) store cannot accept, page or count it.
        self.propose_design()
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(other), True)
        record = rr.existing_revisions(self.anchor())[1]
        token = rr._PROJECT.set(other)
        try:
            with self.assertRaisesRegex(ValueError, 'cannot be read'):
                rr.document_acceptance_rules(record, self.native)
        finally:
            rr._PROJECT.reset(token)
        self.assertEqual(task_files.project_bytes(other), 0)
        self.assertEqual(task_files.all_entries(other), [])
        foreign = [dict(row, project_id='other') for row in self.native.rows]
        self.assertEqual(dd.indexes_for(next((r for r in foreign if r['id'] == self.anchor()['id'])), KEY,
                                        authority(document())), self.indexes())   # rows carry no project rule
        import briefing
        pointer = briefing.parse_context_pointers('design:%s#plan-phase-1' % SCOPE)[0]
        answer = briefing.ContextPointers(foreign, 'trial', operators=[OPERATOR]).resolve(pointer)
        self.assertEqual(answer['flags'], ['unknown-scope'])

    def test_a_repo_document_path_is_one_plain_line(self):
        # Review r1 (control characters) and r2 b2 (bidi, zero-width, separators, tags): the plain-text rule
        # task-file names follow, applied to repo-document paths only.
        text = document()
        for code in (0x0A, 0x00, 0x09, 0x85, 0x7F, 0x202E, 0x202A, 0x2067, 0x2069, 0x061C, 0x200E, 0x200B,
                     0x2028, 0xFEFF, 0xE0041):
            path = 'docs/a' + chr(code) + 'b.md'
            with self.subTest(code=hex(code)):
                writes, blobs = self.writes(), self.blobs()
                with self.assertRaisesRegex(ValueError, 'authority.path'):
                    self.propose_design(text, authority=authority(text, path=path))
                self.nothing_written(writes, blobs)

    def test_a_stored_repo_document_record_with_an_invisible_path_never_parses(self):
        # The read side of b2: plain_text runs inside valid_authority, so a record carrying such a path (planted,
        # or written before this rule) is not a readable revision.
        self.propose_design()
        body = next(c['text'] for c in self.anchor()['comments'] if c['text'].startswith(rr.ENTRY_V3_PREFIX))
        record = json.loads(body[len(rr.ENTRY_V3_PREFIX):])
        for code in (0x202E, 0x200B):
            changed = dict(record, authority=dict(record['authority'], path='docs/a' + chr(code) + 'b.md'))
            changed.pop('sha256')
            changed['sha256'] = rr.content_hash(changed)
            self.assertIsNone(rr.parse_entry(rr.ENTRY_V3_PREFIX + rr.canonical_bytes(changed).decode('utf-8')))

    def test_a_repo_path_authority_keeps_its_earlier_rule(self):
        # Only repo-document paths are tightened: a repo-path entry is re-validated on every read.
        rr.valid_authority({'type': 'repo-path', 'path': 'docs/a' + chr(0x202E) + 'b.md', 'commit': 'a' * 40})

    def test_a_second_request_for_the_same_document_never_deletes_a_blob_it_did_not_create(self):
        # Review r2 (N3, the reviewer's SharedBlobProbe): an uncertain write keeps its unjournaled blob for its
        # retry; another request that stores the same bytes (created False) and is refused must not remove it.
        self.native.fail_comment_prefix = rr.ENTRY_V3_PREFIX
        with self.assertRaises(rr.core.UncertainOutcome):
            self.propose_design()
        self.native.fail_comment_prefix = None
        digest = authority(document())['sha256']
        self.assertEqual(self.blobs(), [digest])
        self.assertEqual(task_files.all_entries(self.project), [])
        with self.assertRaises(ValueError):
            self.propose_design(operation_id='second', decisions=['no-such-decision'])
        self.assertEqual(self.blobs(), [digest])
        self.propose_design()
        self.assertEqual(len(self.indexes()), 1)

    def test_a_refused_revision_removes_the_bytes_it_stored(self):
        self.propose_design()
        blobs = self.blobs()
        with self.assertRaises(ValueError):
            self.propose_design(document(body='# New\n'), operation='revise', operation_id='stale', revision=2,
                                expected_sha256='0' * 64)
        self.assertEqual(self.blobs(), blobs)


class AcceptanceTests(DesignCase):
    def setUp(self):
        super().setUp()
        self.text = document()
        self.digest = authority(self.text)['sha256']
        self.propose_design(self.text)

    def blob(self):
        return task_files.blob_path(self.project, self.digest)

    def test_acceptance_checks_the_stored_bytes_and_the_index_then_accepts(self):
        outcome = self.accept_design(1)
        self.assertEqual(outcome['state'], 'accepted')

    def test_missing_bytes_refuse_acceptance(self):
        os.remove(str(self.blob()))
        with self.assertRaisesRegex(ValueError, 'cannot be read'):
            self.accept_design(1)

    def test_changed_bytes_refuse_acceptance(self):
        os.chmod(str(self.blob()), 0o600)
        self.blob().write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'does not match its sha256'):
            self.accept_design(1)

    def test_a_missing_index_refuses_acceptance(self):
        row = self.native.row(self.anchor()['id'])
        row['comments'] = [c for c in row['comments'] if not c['text'].startswith(dd.INDEX_PREFIX)]
        with self.assertRaisesRegex(ValueError, 'no index'):
            self.accept_design(1)

    def test_a_planted_index_that_differs_refuses_acceptance(self):
        index = copy.deepcopy(self.indexes()[0])
        index['headings'][0]['title'] = 'Planted'
        index.pop('sha256')
        index['sha256'] = dd.content_hash(index)
        self.native.add_comment(self.anchor()['id'], dd.index_body(index), author='mallory')
        self.assertEqual(len(self.indexes()), 2)
        with self.assertRaisesRegex(ValueError, 'not the one derived'):
            self.accept_design(1)

    def test_a_malformed_lookalike_index_is_never_counted(self):
        index = copy.deepcopy(self.indexes()[0])
        index['headings'][0]['title'] = 'Planted'
        body = dd.index_body(index).replace('"key"', ' "key"', 1)      # not canonical, stale hash
        self.native.add_comment(self.anchor()['id'], body, author='mallory')
        self.assertEqual(len(self.indexes()), 1)
        self.assertEqual(self.accept_design(1)['state'], 'accepted')

    def test_an_index_for_the_same_bytes_at_another_path_or_commit_is_not_this_documents(self):
        # Review G2: the index binds the document's path and commit too.
        index = copy.deepcopy(self.indexes()[0])
        for change in ({'path': 'docs/other.md'}, {'commit': 'c' * 40}):
            planted = dict(index, headings=[], **change)
            planted.pop('sha256')
            planted['sha256'] = dd.content_hash(planted)
            self.native.add_comment(self.anchor()['id'], dd.index_body(planted), author='mallory')
        self.assertEqual(len(self.indexes()), 1)
        self.assertEqual(self.accept_design(1)['state'], 'accepted')

    def test_an_identical_second_index_does_not_refuse(self):
        self.native.add_comment(self.anchor()['id'], dd.index_body(self.indexes()[0]))
        self.assertEqual(self.accept_design(1)['state'], 'accepted')

    def test_acceptance_without_the_project_folder_is_refused(self):
        record = rr.existing_revisions(self.anchor())[1]
        with self.assertRaisesRegex(ValueError, 'only where its stored bytes can be read'):
            rr.document_acceptance_rules(record, self.native)

    def test_batch_acceptance_runs_the_same_check(self):
        os.remove(str(self.blob()))
        self.native.actor = OPERATOR
        payload = {'schema_version': 1, 'operation_id': 'batch-1', 'acceptance_state': 'accepted',
                   'acceptance': acceptance(),
                   'items': [{'key': KEY, 'revision': 1, 'record_sha256': self.sha(1, key=KEY)}]}
        result = rr.apply_batch(payload, OPERATOR, self.native, self.project, operators=[OPERATOR])
        self.assertEqual(result['items'][0]['result'], 'refused', result)
        self.assertIn('cannot be read', result['items'][0]['reason'])

    def test_a_raw_comment_with_the_index_prefix_is_refused_in_every_version(self):
        body = dd.index_body(self.indexes()[0])
        for text in (body, body.replace('design-index-v1', 'design-index-v2'), '\ufeff' + body):
            with self.subTest(text=text[:30]):
                with self.assertRaises(ValueError):
                    reserved_comments.check_raw_request(['comments', 'add', 'ref-1', text], {}, actor='alice',
                                                        task='ref-1')

    def test_an_index_record_is_hidden_like_every_record_comment(self):
        self.assertTrue(reserved_comments.is_record_comment(dd.index_body(self.indexes()[0])))


class StoreTests(DesignCase):
    def test_purge_keeps_design_documents_and_backup_and_verify_count_them(self):
        outcome = self.propose_design()
        digest = outcome['document']['sha256']
        rows = {outcome['native_id']: {'status': 'closed', 'closed_at': '2001-01-01T00:00:00Z'}}
        plan = task_files.purge_plan(self.project, '2100-01-01', rows)
        self.assertNotIn(digest, [item.get('sha256') for item in plan['remove']])
        self.assertEqual(plan['marks'], [])
        target = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(target), True)
        summary = task_files.backup_folder(self.project, target / 'backup')
        self.assertEqual(summary['files'], 1)
        self.assertEqual(summary['problems'], [])
        report = task_files.verify(self.project)
        self.assertEqual(report['missing'], [])
        self.assertEqual(report['orphans'], [])

    def test_no_file_route_lists_serves_or_withdraws_a_design_document(self):
        # Review: the SSH file list showed the design entry on the anchor; HTTP refuses anchors.
        outcome = self.propose_design()
        anchor, digest = outcome['native_id'], outcome['document']['sha256']
        self.assertEqual(task_files.listing(self.project, anchor, True), [])
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.download_target(self.project, anchor, digest)
        self.assertEqual(caught.exception.status, 404)
        payload = {'schema_version': 1, 'operation': 'coordinator-withdraw', 'task': anchor, 'sha256': digest,
                   'reason': 'remove it'}
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.apply(payload, OPERATOR, self.native, self.project, lambda: '2026-10-26T00:00:00Z')
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(dd.read_document(self.project, digest), document().encode('utf-8'))

    def test_an_upload_can_never_name_the_design_direction(self):
        with self.assertRaises(task_files.Refusal):
            task_files.check_direction(task_files.DESIGN)


class OlderKitTests(DesignCase):
    """An older kit reads a repo-document revision `unsupported`, never malformed or as a repo-path (Q1)."""

    def test_the_previous_release_reads_a_repo_document_entry_as_unsupported(self):
        previous = os.environ.get('ORCHESTRA_POINTER_PREVIOUS_KIT')
        if not previous:
            self.skipTest('ORCHESTRA_POINTER_PREVIOUS_KIT not supplied; the previous release is not exercised')
        self.propose_design()
        row = self.anchor()
        script = '''import json,sys
import reference_records as rr
row=json.loads(sys.stdin.read())
body=next(c['text'] for c in row['comments'] if c['text'].startswith('Kind: reference-entry-v3'))
assert rr.parse_entry(body) is None
assert rr.unsupported_reason(body,row)
view=rr.KIND.entry_view(row,[])
assert view['state']=='unsupported',view
print('older kit reads unsupported')
'''
        done = subprocess.run([sys.executable, '-c', script], cwd=previous, input=json.dumps(row), text=True,
                              capture_output=True, encoding='utf-8')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('older kit reads unsupported', done.stdout)

    def test_the_previous_release_never_purges_a_design_document_and_says_its_backup_skipped_it(self):
        previous = os.environ.get('ORCHESTRA_POINTER_PREVIOUS_KIT')
        if not previous:
            self.skipTest('ORCHESTRA_POINTER_PREVIOUS_KIT not supplied; the previous release is not exercised')
        outcome = self.propose_design()
        script = '''import sys,tempfile
from pathlib import Path
import task_files
project=Path(sys.argv[1])
try:
    task_files.purge_plan(project,'2100-01-01',{sys.argv[2]:{'status':'closed','closed_at':'2001-01-01T00:00:00Z'}})
except task_files.Refusal as error:
    print('purge refused:',error)
else:
    raise SystemExit('the older purge ran')
summary=task_files.backup_folder(project,Path(tempfile.mkdtemp())/'b')
assert summary['problems'],summary
print('backup problems:',len(summary['problems']))
assert task_files.verify(project)['unreadable']
'''
        done = subprocess.run([sys.executable, '-c', script, str(self.project), outcome['native_id']], cwd=previous,
                              text=True, capture_output=True, encoding='utf-8')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('purge refused', done.stdout)
        self.assertIn('backup problems: 1', done.stdout)
        self.assertEqual(dd.read_document(self.project, outcome['document']['sha256']), document().encode('utf-8'))

    def test_this_kit_would_read_an_unknown_later_type_unsupported_too(self):
        self.propose_design()
        body = next(c['text'] for c in self.anchor()['comments'] if c['text'].startswith(rr.ENTRY_V3_PREFIX))
        record = json.loads(body[len(rr.ENTRY_V3_PREFIX):])
        record['authority'] = dict(record['authority'], type='repo-later')
        record.pop('sha256')
        record['sha256'] = rr.content_hash(record)
        later = rr.ENTRY_V3_PREFIX + rr.canonical_bytes(record).decode('utf-8')
        self.assertIsNone(rr.parse_entry(later))
        self.assertIn('newer than this kit', rr.unsupported_reason(later))


@unittest.skipIf(shutil.which('git') is None, 'git is not installed')
class DocumentFromGitTests(unittest.TestCase):
    """Both clients read the document from git's own object at the pinned commit (exact bytes)."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.repo), True)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'core.autocrlf', 'false')
        (self.repo / 'docs').mkdir()
        self.text = '\ufeff' + document().replace('\n', '\r\n')
        (self.repo / 'docs' / 'D.md').write_bytes(self.text.encode('utf-8'))
        (self.repo / 'docs' / 'bin.md').write_bytes(b'\xff\xfe not utf-8')
        self.git('add', '.')
        blob = self.git('hash-object', '-w', '--stdin', input=b'docs/D.md').strip()
        self.git('update-index', '--add', '--cacheinfo', '120000,%s,docs/link.md' % blob)
        self.git('update-index', '--add', '--cacheinfo', '160000,%s,docs/sub' % ('1' * 40))
        self.git('commit', '-q', '-m', 'docs')
        self.commit = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args, input=None):
        done = subprocess.run(['git', '-C', str(self.repo), *args], input=input, capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.decode('ascii', 'replace')

    def readers(self):
        import client
        import http_client
        return (('client', client._document_from_git), ('http_client', http_client._document_from_git))

    def entry(self, path, **authority):
        return {'key': KEY, 'authority': dict({'type': 'repo-document', 'path': path, 'commit': self.commit},
                                              **authority)}

    def test_the_exact_bytes_and_their_sha256(self):
        for name, read in self.readers():
            with self.subTest(name):
                entry_, text = read(self.entry('docs/D.md'), str(self.repo))
                self.assertEqual(text.encode('utf-8'), self.text.encode('utf-8'))
                self.assertEqual(entry_['authority']['sha256'], hashlib.sha256(self.text.encode('utf-8')).hexdigest())

    def test_a_symlink_a_submodule_a_tree_a_missing_path_and_non_utf8_are_refused(self):
        for name, read in self.readers():
            for path, why in (('docs/link.md', 'not a regular file'), ('docs/sub', 'not a regular file'),
                              ('docs', 'not a regular file'), ('docs/none.md', 'holds no file'),
                              ('docs/bin.md', 'not UTF-8')):
                with self.subTest(name=name, path=path):
                    with self.assertRaisesRegex(ValueError, why):
                        read(self.entry(path), str(self.repo))

    def test_a_given_sha256_must_match(self):
        for name, read in self.readers():
            with self.subTest(name):
                with self.assertRaisesRegex(ValueError, 'is not the sha256'):
                    read(self.entry('docs/D.md', sha256='0' * 64), str(self.repo))

    def test_the_client_sends_the_document_as_an_exact_second_attachment(self):
        import client
        entry_file = self.repo / 'entry.json'
        entry_file.write_text(json.dumps(self.entry('docs/D.md')), encoding='utf-8')
        args, text, temporary = client._design_document_args(
            ['propose', '--file', str(entry_file), '--document-from-git', '--repo', str(self.repo)])
        self.addCleanup(lambda: Path(temporary).unlink(missing_ok=True))
        wire = json.loads(client._wire('example', 'alice/session1', args, 'ref', None, text))
        self.assertEqual(wire['args'], ['propose', '--file', '@attachment:0', '@attachment:1'][:1] + ['@attachment:0',
                                                                                                    '@attachment:1'])
        self.assertEqual(wire['attachments']['1'], {'flag': '--document', 'text': self.text})
        sent = json.loads(wire['attachments']['0']['text'])
        self.assertEqual(sent['authority']['sha256'], hashlib.sha256(self.text.encode('utf-8')).hexdigest())
        payload, document_text = rr.write_payload_and_document(wire['args'], wire['attachments'])
        self.assertEqual(document_text.encode('utf-8'), self.text.encode('utf-8'))
        self.assertEqual(payload['operation'], 'propose')


class WebDocumentReaderTests(unittest.TestCase):
    def test_the_form_hashes_the_exact_bytes_and_refuses_non_utf8(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        code = '''await import(process.argv[1]);
const view=await import(process.argv[2]);
const text='\\ufeff<!-- design-v1\\r\\nscope: s\\r\\nstatus: candidate\\r\\n-->\\r\\n# T\\r\\n';
const bytes=new TextEncoder().encode(text);
const ok=await view.readDesignDocument({arrayBuffer: async()=>bytes.buffer});
let refused=null;
try{await view.readDesignDocument({arrayBuffer: async()=>new Uint8Array([0xff,0xfe,0x41]).buffer});}catch(e){refused=e.message;}
console.log(JSON.stringify({same: ok.text===text, sha256: ok.sha256, refused}));'''
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/records.js').as_uri())
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout.strip().splitlines()[-1])
        text = '\ufeff<!-- design-v1\r\nscope: s\r\nstatus: candidate\r\n-->\r\n# T\r\n'
        self.assertTrue(result['same'])
        self.assertEqual(result['sha256'], hashlib.sha256(text.encode('utf-8')).hexdigest())
        self.assertIn('UTF-8', result['refused'])


# -- part 2: verification (Q4), the design page, trace --verify ---------------------------------

VERIFIER = 'verifier-1'


class VerificationTests(DesignCase):
    """§5.1 step 4 and the coordinator's Q4: mirror capabilities; an untrusted drift says so."""

    def setUp(self):
        super().setUp()
        self.text = document()
        self.propose_design(self.text)
        self.accept_design(1)
        self.auth = authority(self.text)

    def report(self, result='fail', observed='0' * 64, reason=None, **extra):
        payload = {'schema_version': 1, 'key': KEY, 'doc_sha256': self.auth['sha256'], 'path': self.auth['path'],
                   'commit': self.auth['commit'], 'result': result,
                   'observed_sha256': self.auth['sha256'] if result == 'pass' else observed,
                   'reason': reason or ('match' if result == 'pass' else 'mismatch')}
        payload.update(extra)
        return payload

    def file(self, payload, actor='alice', route='endpoint', verifiers=(VERIFIER,)):
        self.native.actor = actor
        return rr.write_verification(payload, actor, self.native, operators=[OPERATOR], verifiers=list(verifiers),
                                     route=route)

    def state(self, verifiers=(VERIFIER,)):
        return rr.get(rr.read_key_rows(self.native, KEY), KEY, [OPERATOR], verifiers=list(verifiers))['verification']

    def test_an_accepted_document_reads_unverified_until_a_trusted_check(self):
        self.assertEqual(self.state()['state'], 'unverified')

    def test_an_untrusted_failing_report_opens_drift_marked_untrusted(self):
        self.file(self.report())
        state = self.state()
        self.assertEqual((state['state'], state['trusted'], state['reported_by']), ('design-drift', False, ['alice']))

    def test_an_untrusted_pass_never_verifies(self):
        self.file(self.report('pass'))
        self.assertEqual(self.state()['state'], 'unverified')
        self.file(self.report('pass'), actor=VERIFIER)          # a listed name, but not the host route
        self.assertEqual(self.state()['state'], 'unverified')

    def test_the_host_route_by_a_listed_actor_verifies_and_a_trusted_fail_is_trusted_drift(self):
        self.file(self.report('pass'), actor=VERIFIER, route='host')
        self.assertEqual((self.state()['state'], self.state()['verified_by']), ('verified', VERIFIER))
        self.file(self.report('fail'), actor=OPERATOR, route='host')
        state = self.state()
        self.assertEqual((state['state'], state['trusted'], state['reported_by']), ('design-drift', True, [OPERATOR]))

    def test_the_host_route_refuses_an_unlisted_actor(self):
        with self.assertRaisesRegex(ValueError, 'operator allowlist or the verifiers list'):
            self.file(self.report('pass'), actor='mallory', route='host')

    def test_a_later_trusted_pass_clears_an_untrusted_drift(self):
        self.file(self.report())
        self.file(self.report('pass'), actor=VERIFIER, route='host')
        self.assertEqual(self.state()['state'], 'verified')

    def test_a_trusted_record_counts_only_while_its_author_is_listed_and_is_its_author(self):
        self.file(self.report('pass'), actor=VERIFIER, route='host')
        self.assertEqual(self.state(verifiers=())['state'], 'unverified')
        row = self.native.row(self.anchor()['id'])
        row['comments'][-1]['author'] = 'someone-else'
        self.assertEqual(self.state()['state'], 'unverified')

    def test_a_report_names_the_accepted_document_or_is_refused(self):
        for bad in ({'doc_sha256': '1' * 64}, {'commit': 'b' * 40}, {'path': 'other.md'}):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, 'not the accepted revision'):
                    self.file(self.report(**bad))
        with self.assertRaisesRegex(ValueError, 'Unknown reference key'):
            self.file(self.report(key='design.nothing'))

    def test_a_draft_only_design_has_nothing_to_verify(self):
        text = document(scope='draft-design')
        self.propose_design(text, key='design.draft-design', operation_id='draft-1')
        auth = authority(text)
        payload = self.report(key='design.draft-design', doc_sha256=auth['sha256'])
        with self.assertRaisesRegex(ValueError, 'no accepted design document'):
            self.file(payload)

    def test_report_rules(self):
        for bad, why in (({'result': 'pass', 'observed_sha256': '0' * 64, 'reason': 'match'}, 'a pass is'),
                         ({'result': 'fail', 'reason': 'match'}, 'result is pass'),
                         ({'observed_sha256': 'x'}, 'observed_sha256'),
                         ({'extra': 1}, 'exactly')):
            with self.subTest(bad=bad):
                payload = self.report()
                payload.update(bad)
                if bad.get('result') == 'pass':
                    payload['observed_sha256'] = '0' * 64
                with self.assertRaisesRegex(ValueError, why):
                    self.file(payload)

    def test_a_back_to_back_repeat_writes_nothing(self):
        self.assertTrue(self.file(self.report())['recorded'])
        writes = self.writes()
        self.assertFalse(self.file(self.report())['recorded'])
        self.assertEqual(self.writes(), writes)

    def test_a_host_pass_after_an_identical_untrusted_pass_is_written_and_verifies(self):
        # Review of part 2, n2: the repeat rule compares submitter and route, not only the result.
        self.file(self.report('pass'))
        self.assertTrue(self.file(self.report('pass'), actor=VERIFIER, route='host')['recorded'])
        self.assertEqual(self.state()['state'], 'verified')

    def test_another_members_identical_fail_is_written_and_named(self):
        # n2: bob's fail identical to alice's is no repeat of alice's.
        self.file(self.report())
        self.assertTrue(self.file(self.report(), actor='bob')['recorded'])
        self.assertEqual(self.state()['reported_by'], ['alice', 'bob'])

    def test_a_verifier_recheck_after_a_newer_fail_is_written_and_clears_it(self):
        # Review of part 2 (B1): pass(V), fail(contributor), pass(V) reads verified.
        self.file(self.report('pass'), actor=VERIFIER, route='host')
        self.file(self.report())
        self.assertEqual(self.state()['state'], 'design-drift')
        self.assertTrue(self.file(self.report('pass'), actor=VERIFIER, route='host')['recorded'])
        self.assertEqual(self.state()['state'], 'verified')

    def test_the_same_fail_after_a_trusted_pass_reopens_drift(self):
        # B1: fail(A), trusted pass, fail(A) reopens drift.
        self.file(self.report())
        self.file(self.report('pass'), actor=VERIFIER, route='host')
        self.assertEqual(self.state()['state'], 'verified')
        self.assertTrue(self.file(self.report())['recorded'])
        self.assertEqual(self.state()['state'], 'design-drift')

    def test_untrusted_reports_are_bounded_per_submitter_passes_and_fails_apart(self):
        # Review of part 2 (N2).
        with patch.object(dd, 'UNTRUSTED_FAILS_PER_SUBMITTER', 2), patch.object(dd, 'UNTRUSTED_PASSES_PER_SUBMITTER', 1):
            self.file(self.report(observed='1' * 64))
            self.file(self.report(observed='2' * 64))
            with self.assertRaisesRegex(ValueError, 'already holds 2 fail reports by alice'):
                self.file(self.report(observed='3' * 64))
            self.file(self.report(observed='3' * 64), actor='bob')        # another member is not blocked
            self.file(self.report('pass'))                                  # passes are counted apart
            self.file(self.report(observed='9' * 64), actor='bob')         # a newer report, so the next is no repeat
            with self.assertRaisesRegex(ValueError, 'already holds 1 pass reports by alice'):
                self.file(self.report('pass'))
            self.file(self.report('pass'), actor=VERIFIER, route='host')   # the trusted route is not bounded

    def test_a_trusted_pass_for_another_commit_never_counts(self):
        # Review of part 2 (G1): the same bytes at another commit or path are another document check.
        self.file(self.report('pass'), actor=VERIFIER, route='host')
        self.assertEqual(self.state()['state'], 'verified')
        moved = dict(self.auth, commit='c' * 40)
        self.propose_design(self.text, operation='revise', operation_id='moved', revision=3,
                            expected_sha256=self.sha(2, key=KEY), authority=moved)
        self.accept(3, self.sha(3, key=KEY), key=KEY, operation_id='accept-moved')
        self.assertEqual(self.state()['state'], 'unverified')

    def test_a_verification_that_is_not_canonical_never_parses(self):
        # Review of part 2 (G2).
        record = dd.verification_record(self.report('pass'), VERIFIER, 'host', 'verified', '2026-01-01T00:00:00Z')
        body = dd.verification_body(record)
        self.assertIsNotNone(dd.parse_verification(body))
        self.assertIsNone(dd.parse_verification(body.replace('"key"', ' "key"', 1)))
        self.assertIsNone(dd.parse_verification(body.replace('\n', '\r\n', 1)))

    def test_the_verification_prefix_is_reserved_and_a_record_comment(self):
        self.file(self.report())
        body = next(c['text'] for c in self.anchor()['comments'] if c['text'].startswith(dd.VERIFICATION_PREFIX))
        self.assertTrue(reserved_comments.is_record_comment(body))
        for text in (body, body.replace('verification-v1', 'verification-v4')):
            with self.assertRaises(ValueError):
                reserved_comments.check_raw_request(['comments', 'add', 'ref-1', text], {}, actor='alice', task='ref-1')

    def test_a_trusted_record_written_by_another_listed_actor_does_not_count(self):
        record = dd.verification_record(self.report('pass'), VERIFIER, 'host', 'verified', '2026-01-01T00:00:00Z')
        self.native.add_comment(self.anchor()['id'], dd.verification_body(record), author=OPERATOR)
        self.assertEqual(self.state()['state'], 'unverified')

    def test_an_untrusted_pass_never_clears_a_drift(self):
        self.file(self.report())
        self.file(self.report('pass'), actor='bob')
        self.assertEqual(self.state()['state'], 'design-drift')

    def test_a_verified_identity_counts_only_from_the_host_route(self):
        record = dd.verification_record(self.report('pass'), VERIFIER, 'endpoint', 'verified', '2026-01-01T00:00:00Z')
        self.native.add_comment(self.anchor()['id'], dd.verification_body(record), author=VERIFIER)
        self.assertEqual(self.state()['state'], 'unverified')

    def test_a_planted_trusted_looking_record_by_another_author_never_verifies(self):
        record = dd.verification_record(self.report('pass'), VERIFIER, 'host', 'verified', '2026-01-01T00:00:00Z')
        self.native.add_comment(self.anchor()['id'], dd.verification_body(record), author='mallory')
        self.assertEqual(self.state()['state'], 'unverified')


class IndexShapeTests(DesignCase):
    """Review of part 1 (part-2 notes): only a fully well-formed stored index is ever used by a reader."""

    def test_every_derived_index_parses_back(self):
        self.propose_design()
        index = self.indexes()[0]
        self.assertEqual(dd.parse_index(dd.index_body(index)), index)

    def test_an_index_with_any_malformed_value_is_ignored(self):
        self.propose_design()
        good = self.indexes()[0]
        changes = {
            'front scope of another key': lambda i: i['front'].update(scope='other'),
            'front status': lambda i: i['front'].update(status='chosen'),
            'front list item': lambda i: i['front'].update(requirements=['x' * 500]),
            'heading level': lambda i: i['headings'][0].update(level=9),
            'heading title': lambda i: i['headings'][0].update(title='t' * 500),
            'heading tokens': lambda i: i['headings'][0].update(tokens=[1]),
            'phase anchor': lambda i: i['plan_phases'][0].update(anchor='not-a-phase'),
            'phase epic': lambda i: i['plan_phases'][0].update(epic='<b>'),
            'phase fields': lambda i: i['plan_phases'][0].update(extra=1),
            'phase line': lambda i: i['plan_phases'][0].update(contract=['c' * 500]),
        }
        for name, change in changes.items():
            with self.subTest(name):
                bad = copy.deepcopy(good)
                change(bad)
                bad.pop('sha256')
                bad['sha256'] = dd.content_hash(bad)
                self.assertIsNone(dd.parse_index(dd.index_body(bad)))


class DesignPageTests(DesignCase):
    def page(self, scope=SCOPE, revision=None, rows=None):
        return rr.design_page(rows if rows is not None else rr.read_key_rows(self.native, 'design.' + scope), scope,
                              self.project, [OPERATOR], [VERIFIER], revision)

    def test_the_page_carries_the_document_its_index_and_its_state(self):
        text = document()
        self.propose_design(text)
        page = self.page()
        self.assertEqual(page['document'], text)
        self.assertEqual(page['index_state'], 'matches')
        self.assertEqual(page['accepted'], False)
        self.assertIsNone(page['verification'])
        self.accept_design(1)
        page = self.page()
        self.assertEqual((page['accepted'], page['verification']['state']), (True, 'unverified'))
        self.assertEqual([h['anchor'] for h in page['index']['headings']], ['server-redesign', 'plan-phase-1'])

    def test_another_revision_is_shown_by_number(self):
        first = document()
        self.propose_design(first)
        self.accept_design(1)
        second = document(body='# Changed\n')
        self.revise_design(second, 3)
        self.assertEqual(self.page()['document'], first)
        self.assertEqual(self.page(revision=3)['document'], second)

    def test_a_differing_index_beside_the_derived_one_is_shown(self):
        self.propose_design()
        index = copy.deepcopy(dd.parse_index(next(c['text'] for c in self.anchor()['comments']
                                                  if c['text'].startswith(dd.INDEX_PREFIX))))
        index['headings'] = []
        index.pop('sha256')
        index['sha256'] = dd.content_hash(index)
        self.native.add_comment(self.anchor()['id'], dd.index_body(index))
        self.assertEqual(self.page()['index_state'], 'differs')

    def test_a_design_entry_written_before_documents_stays_readable_without_a_page(self):
        # Q3: a repo-path design entry from an older kit keeps reading; it has no document, so no page,
        # no verification block, and its sections stay unchecked.
        with patch.object(dd, 'is_design_key', return_value=False):
            self.propose(key='design.legacy', operation_id='legacy')
        self.assertEqual(rr.get(rr.read_key_rows(self.native, 'design.legacy'), 'design.legacy',
                                [OPERATOR])['state'], 'draft-only')
        self.assertNotIn('verification', rr.get(rr.read_key_rows(self.native, 'design.legacy'), 'design.legacy',
                                                [OPERATOR]))
        with self.assertRaisesRegex(ValueError, '^Design not found$'):
            self.page(scope='legacy')
        import briefing
        pointer = briefing.parse_context_pointers('design:legacy#anything')[0]
        answer = briefing.ContextPointers(self.native.rows, 'trial', operators=[OPERATOR]).resolve(pointer)
        self.assertEqual((answer['section']['state'], answer['flags']), ('unchecked', ['not-selected']))

    def test_every_absent_or_hidden_design_answers_the_same_refusal(self):
        self.propose_design()
        self.propose(key='calendar.trading', operation_id='plain')
        cases = {'unknown scope': dict(scope='nothing'), 'not a design key': dict(scope='x', rows=self.native.rows),
                 'no such revision': dict(revision=9), 'hidden (no rows readable)': dict(rows=[]),
                 'another project': dict(rows=[dict(row, project_id='other') for row in self.native.rows
                                               if False])}
        for name, kwargs in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError) as caught:
                    self.page(**kwargs)
                self.assertEqual(str(caught.exception), 'Design not found')
        os.remove(str(task_files.blob_path(self.project, authority(document())['sha256'])))
        with self.assertRaisesRegex(ValueError, '^Design not found$'):
            self.page()


@unittest.skipIf(shutil.which('git') is None, 'git is not installed')
class TraceVerifyClientTests(unittest.TestCase):
    """`trace --verify`: the checkout's file at the pinned commit against the stored document."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.repo), True)
        for args in (('init', '-q'), ('config', 'user.email', 't@example.invalid'), ('config', 'user.name', 'T'),
                     ('config', 'core.autocrlf', 'false')):
            subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)
        (self.repo / 'docs').mkdir()
        self.text = document()
        (self.repo / 'docs' / 'D.md').write_bytes(self.text.encode('utf-8'))
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-q', '-m', 'd'], check=True, capture_output=True)
        self.commit = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], check=True,
                                     capture_output=True, text=True).stdout.strip()

    def run_trace(self, stored_sha, extra=()):
        import client
        calls = []
        entry = {'key': KEY, 'record': {'authority': {'type': 'repo-document', 'path': 'docs/D.md',
                                                       'commit': self.commit, 'sha256': stored_sha}}}

        def fake(config, project, actor, args, action='bd', path=None):
            calls.append(list(args))
            if args[0] == 'list':
                body = {'items': [{'key': KEY}, {'key': 'calendar.trading'}], 'next_offset': None}
            elif args[0] == 'get':
                body = entry
            else:
                body = {'recorded': True}
            return {'returncode': 0, 'stdout': json.dumps(body), 'stderr': ''}
        out = self.repo / 'out.json'
        with patch.object(client, 'request', side_effect=fake):
            code = client._trace_command(['--verify', '--repo', str(self.repo), *extra], {}, 'p', 'alice', str(out))
        return code, json.loads(out.read_text(encoding='utf-8')), calls

    def test_a_match_passes_and_a_different_stored_document_fails(self):
        code, summary, calls = self.run_trace(hashlib.sha256(self.text.encode('utf-8')).hexdigest())
        self.assertEqual((code, summary['passed'], summary['failed']), (0, 1, 0))
        self.assertEqual([c[0] for c in calls], ['list', 'get'])
        code, summary, _ = self.run_trace('1' * 64)
        self.assertEqual((code, summary['failed'], summary['results'][0]['reason']), (3, 1, 'mismatch'))

    def test_record_files_each_report_and_payloads_writes_the_host_file(self):
        digest = hashlib.sha256(self.text.encode('utf-8')).hexdigest()
        _, summary, calls = self.run_trace(digest, ['--record'])
        self.assertEqual(calls[-1][:2], ['verify', '--file'])
        target = self.repo / 'payloads.json'
        self.run_trace(digest, ['--payloads', str(target)])
        items = json.loads(target.read_text(encoding='utf-8'))['items']
        self.assertEqual(items[0], {'schema_version': 1, 'key': KEY, 'doc_sha256': digest, 'path': 'docs/D.md',
                                    'commit': self.commit, 'observed_sha256': digest, 'result': 'pass',
                                    'reason': 'match'})
        dd.check_verification_payload(items[0])

    def test_a_commit_the_checkout_lacks_is_not_checked_never_a_fail(self):
        # Review of part 2 (N3).
        import client
        code, summary, calls = None, None, None
        entry = {'key': KEY, 'record': {'authority': {'type': 'repo-document', 'path': 'docs/D.md',
                                                       'commit': 'f' * 40, 'sha256': '1' * 64}}}

        def fake(config, project, actor, args, action='bd', path=None):
            body = {'items': [{'key': KEY}], 'next_offset': None} if args[0] == 'list' else entry
            return {'returncode': 0, 'stdout': json.dumps(body), 'stderr': ''}
        out = self.repo / 'out2.json'
        with patch.object(client, 'request', side_effect=fake):
            code = client._trace_command(['--verify', '--repo', str(self.repo), '--record'], {}, 'p', 'alice', str(out))
        summary = json.loads(out.read_text(encoding='utf-8'))
        # n5: nothing checked is exit 4, never a silent pass.
        self.assertEqual((code, summary['checked'], summary['failed']), (4, 0, 0))
        self.assertFalse(summary['results'][0]['checked'])

    def test_trace_without_verify_says_where_trace_is(self):
        import client
        with self.assertRaisesRegex(ValueError, 'trace --verify'):
            client._trace_command([], {}, 'p', 'alice', None)


class DesignPageAnchorTests(unittest.TestCase):
    """Review of part 2, n1: every anchor the server derives is rendered on its line, whatever the bytes."""

    def test_the_page_renders_exactly_the_servers_headings_on_their_lines(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        from capabilities import markdown_headings
        text = ('<!-- design-v1\nscope: s\nstatus: candidate\n-->\n# One\r\r\n## Two\u2028still two\n'
                '## Lone\rcarriage\n# ' + '\U0001D400' * 600 + '\n```\n# fenced\n```\n### Three\n#nospace\n')
        found = markdown_headings(text.encode('utf-8'))
        index = dd.derive_index(text, 'design.s', 'a.md', 'a' * 40)
        lines = [item[0] for item in found][:len(index['headings'])]
        code = '''await import(process.argv[1]);
const view=await import(process.argv[2]);
const input=JSON.parse(process.argv[3]);
const el=view.renderDocument(input.text,input.headings,input.lines);
const ids=[];for(const tag of ['h2','h3','h4','h5','h6','h7']){for(const x of el.querySelectorAll(tag))ids.push(x.getAttribute('id'));}
console.log(JSON.stringify({ids}));'''
        payload = json.dumps({'text': text, 'headings': index['headings'], 'lines': lines})
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/designs.js').as_uri(), payload)
        self.assertEqual(done.returncode, 0, done.stderr)
        rendered = json.loads(done.stdout.strip().splitlines()[-1])['ids']
        self.assertEqual(sorted(rendered), sorted('design-' + h['anchor'] for h in index['headings']))
        self.assertEqual(len(index['headings']), 5)


class DesignVerifyCommandTests(DesignCase):
    """`admin.py design-verify` (review of part 2, N5): trusted records, and a non-zero exit when any is refused."""

    def test_the_host_command_records_and_exits_non_zero_on_any_refusal(self):
        import contextlib
        import io
        import types
        from unittest.mock import Mock
        import admin
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root), True)
        project = root / 'projects' / 'trial'
        (project / '.beads').mkdir(parents=True)
        (project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (root / 'deployment.private.json').write_text(json.dumps({'password': 'x', 'unit': 'none',
                                                                  'operators': [OPERATOR], 'verifiers': [VERIFIER]}),
                                                      encoding='utf-8')
        self.project = project
        text = document()
        self.propose_design(text)
        self.accept_design(1)
        auth = authority(text)
        good = {'schema_version': 1, 'key': KEY, 'doc_sha256': auth['sha256'], 'path': auth['path'],
                'commit': auth['commit'], 'observed_sha256': auth['sha256'], 'result': 'pass', 'reason': 'match'}

        def run_native(root_, name, args):
            if args[:1] == ['--actor']:
                self.native.actor = args[1]
                args = args[2:]
            return self.native(list(args))

        def run(items):
            path = root / 'checks.json'
            path.write_text(json.dumps({'items': items}), encoding='utf-8')
            argv = ['admin.py', '--root', str(root), 'design-verify', 'trial', '--actor', VERIFIER, '--file', str(path)]
            with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=root), \
                    patch.object(admin, 'run_bd', side_effect=run_native), \
                    patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=Mock(), LOCK_EX=2)}), \
                    patch.dict(os.environ, {'ORCHESTRA_OPERATORS': '', 'ORCHESTRA_VERIFIERS': ''}), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    admin.main()
                    code = 0
                except SystemExit as stop:
                    code = stop.code
            return code, json.loads(out.getvalue())
        code, result = run([good])
        self.assertEqual((code, result['recorded']), (0, 1))
        state = rr.get(rr.read_key_rows(self.native, KEY), KEY, [OPERATOR], verifiers=[VERIFIER])['verification']
        self.assertEqual(state['state'], 'verified')
        code, result = run([dict(good, key='design.nothing')])
        self.assertEqual(code, 1)
        self.assertIn('refused', result['items'][0])


class DesignPageWebTests(unittest.TestCase):
    def test_the_document_renders_as_text_with_the_servers_anchors(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        code = '''await import(process.argv[1]);
const view=await import(process.argv[2]);
const text='# Title <script>evil()</script>\\n\\n[link](javascript:evil) <img src=x onerror=evil()> \\u202e\\n\\n```\\n# not a heading\\n```\\n## Plan phase 1\\n';
const el=view.renderDocument(text,[{anchor:'title-scriptevilscript',level:1,title:'Title <script>evil()</script>',tokens:['req:k@1']},{anchor:'plan-phase-1',level:2,title:'Plan phase 1',tokens:[]}],[1,8]);
console.log(JSON.stringify({text:el.textContent,scripts:el.querySelectorAll('script').length,
 images:el.querySelectorAll('img').length,links:el.querySelectorAll('a').length,
 ids:[...el.querySelectorAll('h2'),...el.querySelectorAll('h3')].map(x=>x.getAttribute('id')),pre:el.querySelectorAll('pre').length}));'''
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/designs.js').as_uri())
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertIn('<script>evil()</script>', result['text'])
        self.assertIn('<img src=x onerror=evil()>', result['text'])
        self.assertEqual((result['scripts'], result['images'], result['links']), (0, 0, 0))
        self.assertEqual(result['ids'], ['design-title-scriptevilscript', 'design-plan-phase-1'])
        self.assertEqual(result['pre'], 1)
        self.assertIn('# not a heading', result['text'])


if __name__ == '__main__':
    unittest.main()
