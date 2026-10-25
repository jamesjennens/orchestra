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

    def test_the_bounds_themselves_are_allowed(self):
        exact = document(body='')
        exact = exact + 'x' * (dd.DOCUMENT_MAX_BYTES - len(exact.encode('utf-8')))
        self.assertEqual(len(dd.document_bytes(exact)), dd.DOCUMENT_MAX_BYTES)
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
        for bad in ({'commit': 'abc'}, {'sha256': 'A' * 64}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
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


if __name__ == '__main__':
    unittest.main()
