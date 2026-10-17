"""Host correction boundary, durable reading and native-first cut recovery."""
import atexit
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

import lifecycle
import lifecycle_voids as voids
from requirements import canonical_bytes, content_hash


#: The released F0 kit this project's rollback target is measured against
#: (kittrial-5bb.271 item 1): main at the time of the revision.
RELEASED_F0_COMMIT = '04ed25d1453dc0112a740aacae7ac495e6fd1d95'


def rows():
    scope = dict(source_commit='a' * 40, integration_commit='b' * 40,
                 release_id='', environment='synthetic')
    task = dict(id='p-task', issue_type='task', title='synthetic', status='closed',
                assignee='worker', comments=[], labels=['lifecycle-scope:' + content_hash(scope),
                                                       'integrated:passed'])
    events = []
    for number, dimension, value in [(1, 'lifecycle-scope', content_hash(scope)), (2, 'integrated', 'passed')]:
        payload = dict(schema_version=1, operation_id='fact-' + str(number), task=task['id'],
                       dimension=dimension, value=value, scope=scope, actor='op',
                       evidence=['synthetic source'], provenance='performed')
        lifecycle.validate_payload(payload)
        events.append(dict(id=task['id'] + '.' + str(number), issue_type='event',
                           title='State change: ' + dimension + ' → ' + value,
                           description='Set ' + dimension + ' to ' + value + '\n\nReason: '
                           + lifecycle.PREFIX + canonical_bytes(payload).decode(), created_by='op',
                           created_at='2026-10-01T00:00:0' + str(number) + 'Z',
                           dependencies=[dict(type='parent-child', depends_on_id=task['id'])]))
    return [task] + events


def _extract_kit(root, commit, destination):
    """Export the tracked tree at ``commit`` into ``destination`` (no working copy)."""
    archive = subprocess.run(['git', '-C', str(root), 'archive', '--format=tar', commit],
                             capture_output=True, timeout=180)
    if archive.returncode:
        raise OSError(archive.stderr.decode('utf-8', 'replace')[:300])
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        bundle.extractall(destination)


_RELEASED_KIT = []
_RELEASED_RESOLVED = []
_KIT_DIRECTORIES = {}


def resolve_kit(commit):
    """A directory holding the tracked tree at ``commit``, or None.

    ``ORCHESTRA_KIT_<7-char commit>`` names one explicitly (for a CI checkout
    without full history). Otherwise the commit is exported from this checkout's
    own history, so an ordinary CI checkout with full history runs the older-kit
    test instead of skipping. None means neither source is available and the
    caller decides (skip, or fail on CI, exactly as the older-kit tests do).
    """
    commit = str(commit)
    if commit not in _KIT_DIRECTORIES:
        _KIT_DIRECTORIES[commit] = _resolve_kit(commit)
    return _KIT_DIRECTORIES[commit]


def _resolve_kit(commit):
    configured = os.environ.get('ORCHESTRA_KIT_' + str(commit)[:7].upper())
    if not configured and str(commit) == RELEASED_F0_COMMIT:
        configured = os.environ.get('ORCHESTRA_F0_RELEASED_KIT')
    if configured:
        return Path(configured)
    root = Path(lifecycle.__file__).resolve().parent
    if not (root / '.git').exists():
        return None
    try:
        found = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', '--quiet',
                                str(commit) + '^{commit}'],
                               capture_output=True, text=True, timeout=60)
        if found.returncode or found.stdout.strip() != str(commit):
            return None
        destination = Path(tempfile.mkdtemp(prefix='orchestra-older-kit-'))
        atexit.register(shutil.rmtree, destination, ignore_errors=True)
        _extract_kit(root, str(commit), destination)
        return destination
    except (OSError, subprocess.SubprocessError, tarfile.TarError):
        return None


def resolve_released_f0_kit():
    """A directory holding the released F0 kit (04ed25d), or None."""
    if not _RELEASED_RESOLVED:
        _RELEASED_KIT.append(resolve_kit(RELEASED_F0_COMMIT))
        _RELEASED_RESOLVED.append(True)
    return _RELEASED_KIT[0]


class ReleasedKitTests(unittest.TestCase):
    """kittrial-5bb.271 item 1: what the RELEASED F0 kit reads once an abandonment exists."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.project = Path(self.scratch.name)
        self.rows = rows()
        self.native_writes = 0

    def payload(self, operation='void', key='void-one', target=None, actor='op'):
        return dict(schema_version=1, operation=operation, operation_id=key, actor=actor,
                    reason='Operator reviewed the source evidence.', evidence=['synthetic review'],
                    binding=voids.event_binding(self.rows[2]), target_void=target)

    def native_run(self, argv):
        if argv == ['comments', 'p-task', '--json']:
            return json.dumps(self.rows[0]['comments'])
        self.native_writes += 1
        comment = dict(id=str(self.native_writes), author=json.loads(argv[3][len(voids.PREFIX):])['actor'],
                       text=argv[3], created_at='2026-10-02T00:00:0' + str(self.native_writes) + 'Z')
        self.rows[0]['comments'].append(comment)
        return json.dumps(comment)

    def abandoned_project(self):
        """A project with an issued void, a cut retraction and the abandonment of it.

        kittrial-5bb.274 item 1: the abandonment writes its OWN native comment, so
        two native correction comments exist (the cut and the abandonment) beside
        the void. Returns ``(cut_comment, abandon_answer)``.
        """
        voids.apply(self.rows, 'op', self.payload(), self.native_run, self.project,
                    operators=['op'], enabled=True)
        cut = self.payload('retract', 'retract-cut', 'void-one')
        with mock.patch.object(voids, 'publish', side_effect=OSError('synthetic cut')):
            with self.assertRaisesRegex(ValueError, 'uncertain'):
                voids.apply(self.rows, 'op', cut, self.native_run, self.project,
                            operators=['op'], enabled=True)
        comment = self.rows[0]['comments'][-1]
        answer = voids.abandon(self.rows, 'op', dict(schema_version=1, operation='abandon',
                                                     operation_id='abandon-released', actor='op',
                                                     reason='Operator closed the cut.',
                                                     evidence=['synthetic audit'], binding=cut['binding'],
                                                     target_void=cut['operation_id']),
                               self.native_run, self.project, operators=['op'], enabled=True,
                               comment_id=comment['id'])
        return comment, answer

    def released(self):
        base = resolve_released_f0_kit()
        if base is None:
            message = ('no released F0 kit: set ORCHESTRA_F0_RELEASED_KIT or run from a git checkout '
                       'with history holding ' + RELEASED_F0_COMMIT)
            if os.environ.get('GITHUB_ACTIONS'):
                self.fail('CI must not skip this test: ' + message)
            self.skipTest(message)
        return base

    def test_the_released_reader_reads_a_project_that_holds_an_abandonment(self):
        base = self.released()
        comment, answer = self.abandoned_project()
        # The family itself is schema-1 void/retract only: no abandon member exists.
        journal = voids.read_journal(self.project)
        self.assertEqual({e['payload']['operation'] for e in journal['entries'].values()}, {'void'})
        self.assertEqual(set(journal['abandons']), {'abandon-released'})
        # The released reader reads the family, so no lifecycle fact is UNKNOWN for
        # a journal reason and the issued void is still honoured.
        spec = importlib.util.spec_from_file_location('released_f0_lifecycle_voids',
                                                      base / 'lifecycle_voids.py')
        released = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(released)
        released_journal = released.read_journal(self.project)
        self.assertEqual(released_journal['invalid'], [])
        released_state = released.context(self.rows, released_journal)
        self.assertEqual(released_state['global_reason'], None)
        self.assertEqual(released_state['reasons'], {'p-task.2': 'voided'})
        # The released kit cannot see the abandonment store, so the abandoned cut
        # comment AND the abandonment's own native comment are honestly UNPAIRED
        # there (that is the documented rollback limit).
        self.assertEqual({c['id'] for c in released_state['incomplete']},
                         {str(comment['id']), str(answer['comment_id'])})
        with self.assertRaisesRegex(ValueError, 'native correction comments without their host entries'):
            released.backup_files(self.project, self.rows)
        # And running the released kit's own CLI on the export agrees: it never
        # falls to lifecycle-void-journal-unreadable, it names the abandoned
        # comment's event as not-consulted, and our kit reads the void.
        exported = self.project / 'released-export.jsonl'
        exported.write_text(''.join(json.dumps(row) + '\n' for row in self.rows))
        result = subprocess.run([sys.executable, str(base / 'lifecycle.py'), 'list',
                                 '--export', str(exported), '--journal', str(self.project)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('lifecycle-void-journal-unreadable', result.stdout)
        row = next(r for r in json.loads(result.stdout) if r['id'] == 'p-task')
        self.assertEqual(row['facts']['integrated'].get('reason'), 'lifecycle-corrections-not-consulted')
        self.assertEqual(voids.context(self.rows, voids.read_journal(self.project))['reasons'],
                         {'p-task.2': 'voided'})
        self.assertIn(voids.ABANDON_FILE, voids.backup_files(self.project, self.rows))

    def test_the_released_kit_reopens_an_abandoned_correction_after_a_rollback(self):
        """kittrial-5bb.274 item 4: run rollback step 3 and the released retry.

        Step 3 of the documented rollback: the released kit restores an older,
        pre-abandonment generation into an unused destination, and that
        destination is rollback-clean. Then the released kit ACCEPTS the exact
        retry of the correction this kit abandoned, so the "abandoned is final"
        rule does not hold while the host runs 04ed25d.
        """
        base = self.released()
        spec = importlib.util.spec_from_file_location('released_f0_lifecycle_voids_rollback',
                                                      base / 'lifecycle_voids.py')
        released = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(released)
        # A complete generation that predates the cut comment.
        voids.apply(self.rows, 'op', self.payload(), self.native_run, self.project,
                    operators=['op'], enabled=True)
        older_rows = copy.deepcopy(self.rows)
        older = voids.backup_files(self.project, self.rows)
        cut = self.payload('retract', 'rollback-cut', 'void-one')
        with mock.patch.object(voids, 'publish', side_effect=OSError('synthetic cut')):
            with self.assertRaisesRegex(ValueError, 'uncertain'):
                voids.apply(self.rows, 'op', cut, self.native_run, self.project,
                            operators=['op'], enabled=True)
        comment = self.rows[0]['comments'][-1]
        voids.abandon(self.rows, 'op', dict(schema_version=1, operation='abandon',
                                            operation_id='rollback-abandon', actor='op',
                                            reason='Operator closed the cut.',
                                            evidence=['synthetic audit'], binding=cut['binding'],
                                            target_void=cut['operation_id']),
                      self.native_run, self.project, operators=['op'], enabled=True,
                      comment_id=comment['id'])
        destination = self.project / 'unused'
        destination.mkdir()
        self.assertEqual(released.restore_files(destination, older, older_rows), True)
        self.assertEqual(released.read_journal(destination)['invalid'], [])
        self.assertFalse((destination / voids.ABANDON_FILE).exists())
        self.assertEqual(released.context(older_rows, released.read_journal(destination))['incomplete'], [])
        # Our kit refuses the exact retry; the released kit writes it.
        with self.assertRaisesRegex(ValueError, r'abandoned .*final'):
            voids.apply(self.rows, 'op', cut, self.native_run, self.project,
                        operators=['op'], enabled=True)
        answer = released.apply(self.rows, 'op', cut, self.native_run, self.project,
                                operators=['op'], enabled=True)
        self.assertEqual((answer['operation'], answer['reconciled']), ('retract', True))
        self.assertEqual(released.context(self.rows, released.read_journal(self.project))['reasons'], {})
        # kittrial-5bb.274 item 2: COME BACK to this kit. The void in effect wins,
        # the abandonment reads superseded by that later host entry, the audit is
        # complete and the backup runs again - a defined, non-damaged reading.
        state = voids.context(self.rows, voids.read_journal(self.project))
        self.assertEqual(state['reasons'], {})
        self.assertEqual(state['incomplete'], [])
        self.assertEqual(state['abandon_problems'], [])
        self.assertEqual(state['superseded_abandons'], {'rollback-abandon': 'rollback-cut'})
        page = voids.audit(voids.snapshot(self.rows, self.project), operators=['op'])
        self.assertEqual((page['correction_status'], page['damaged_correction_count'],
                          page['superseded_abandonment_count'], page['incomplete_correction_count']),
                         ('complete', 0, 1, 0))
        by_key = {c['operation_id']: c for c in page['corrections']}
        self.assertEqual(by_key['rollback-abandon']['status'], 'superseded')
        self.assertEqual(by_key['rollback-abandon']['superseded_by'], 'rollback-cut')
        self.assertEqual(by_key['rollback-abandon']['target_void'], 'rollback-cut')
        # The re-opened correction reads in effect as its operation says (here a
        # retraction); a re-opened void would read 'in-effect'.
        self.assertEqual(by_key['rollback-cut']['status'], 'retraction')
        files = voids.backup_files(self.project, self.rows)
        self.assertIn(voids.ABANDON_FILE, files)
        # The exact retry now reconciles with the entry already in effect instead
        # of being refused as abandoned (the void in effect wins).
        again = voids.apply(self.rows, 'op', cut, self.native_run, self.project,
                            operators=['op'], enabled=True)
        self.assertTrue(again['reconciled'])
        self.assertEqual(self.native_writes, 3)
        # A HAND-ADDED duplicate of the abandonment has no native abandonment
        # comment of its own, so it stays damaged: that is the tell-apart.
        hand = dict(schema_version=1, operation='abandon', operation_id='hand-added',
                    actor='op', reason='typed into the file', evidence=['none'],
                    binding=cut['binding'], target_void=cut['operation_id'])
        entry = dict(schema_version=1, payload=hand, sha256=content_hash(hand),
                     comment_id=comment['id'], created_at='2026-10-05T00:00:00Z')
        store = voids.read_journal(self.project)['abandons']
        hand_doc = voids.abandon_document(dict(store, **{hand['operation_id']: entry}))
        (self.project / voids.ABANDON_FILE).write_text(
            json.dumps(hand_doc, ensure_ascii=False, sort_keys=True))
        if os.name == 'posix':
            os.chmod(self.project / voids.ABANDON_FILE, 0o600)
        damaged = voids.audit(voids.snapshot(self.rows, self.project), operators=['op'])
        self.assertEqual(damaged['correction_status'], 'damaged')
        self.assertEqual(damaged['superseded_abandonment_count'], 1)
        handrow = next(c for c in damaged['corrections'] if c['operation_id'] == 'hand-added')
        self.assertEqual((handrow['status'], handrow['native_operator_proof']),
                         ('damaged', False))
        self.assertEqual(handrow['classification_reason'], 'abandonment-without-native-operator-proof')


#: The release current at the time of the third review (kittrial-5bb.274 round 3):
#: it KNOWS the abandonment store but not the one-native-comment-per-abandonment
#: proof, so a project holding this kit's abandonment comments is not rollback-clean.
REVIEWED_RELEASE_COMMIT = 'beca5d69bcc03965f9896412ec93aca0e2d664b5'
#: The release at this revision's base (main when the revision was written). It is
#: the reviewed release plus the web lifecycle work, so its lifecycle correction
#: reader is the same one and it behaves the same way here.
CURRENT_RELEASE_COMMIT = '987cd5026e3f3def2ed3dba577ee4a8a383e0214'


class OlderReleaseRollbackTests(unittest.TestCase):
    """Round 3 item 2(2): rollback to the release current at this revision's base.

    beca5d6 (current at the review) and 987cd50 (current at this revision's base)
    both read the abandonment store, but with the PRE-own-comment proof, so on a
    project holding this kit's native abandonment comments they cannot pair the new
    store records with their comments: the audit reads damaged and the backup is
    refused. 04ed25d cannot see the store at all and reads both comments
    incomplete (the separate ReleasedKitTests case). OPERATIONS.md documents every
    case; these tests load each release from this checkout's history and RUN it.
    """

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.project = Path(self.scratch.name)
        self.rows = rows()
        self.native_writes = 0

    def payload(self, operation='void', key='void-one', target=None, actor='op'):
        return dict(schema_version=1, operation=operation, operation_id=key, actor=actor,
                    reason='Operator reviewed the source evidence.', evidence=['synthetic review'],
                    binding=voids.event_binding(self.rows[2]), target_void=target)

    def native_run(self, argv):
        if argv == ['comments', 'p-task', '--json']:
            return json.dumps(self.rows[0]['comments'])
        self.native_writes += 1
        comment = dict(id=str(self.native_writes),
                       author=json.loads(argv[3][len(voids.PREFIX):])['actor'],
                       text=argv[3], created_at='2026-10-02T00:00:0' + str(self.native_writes) + 'Z')
        self.rows[0]['comments'].append(comment)
        return json.dumps(comment)

    def abandoned_project(self):
        """An issued void, a cut retraction and the NEW abandonment of that cut."""
        voids.apply(self.rows, 'op', self.payload(), self.native_run, self.project,
                    operators=['op'], enabled=True)
        cut = self.payload('retract', 'retract-cut', 'void-one')
        with mock.patch.object(voids, 'publish', side_effect=OSError('synthetic cut')):
            with self.assertRaisesRegex(ValueError, 'uncertain'):
                voids.apply(self.rows, 'op', cut, self.native_run, self.project,
                            operators=['op'], enabled=True)
        comment = self.rows[0]['comments'][-1]
        voids.abandon(self.rows, 'op', dict(schema_version=1, operation='abandon',
                                            operation_id='abandon-current', actor='op',
                                            reason='Operator closed the cut.',
                                            evidence=['synthetic audit'], binding=cut['binding'],
                                            target_void=cut['operation_id']),
                      self.native_run, self.project, operators=['op'], enabled=True,
                      comment_id=comment['id'])
        return comment

    def older_kit(self, commit, name):
        base = resolve_kit(commit)
        if base is None:
            message = ('no kit at ' + commit + ': run from a git checkout with history holding it, '
                       'or set ORCHESTRA_KIT_' + commit[:7].upper())
            if os.environ.get('GITHUB_ACTIONS'):
                self.fail('CI must not skip this test: ' + message)
            self.skipTest(message)
        spec = importlib.util.spec_from_file_location(name, base / 'lifecycle_voids.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return base, module

    def assert_older_release_not_rollback_clean(self, commit, name):
        comment = self.abandoned_project()
        base, module = self.older_kit(commit, name)
        journal = module.read_journal(self.project)
        self.assertEqual(journal['invalid'], [])
        self.assertEqual(set(journal['abandons']), {'abandon-current'})
        state = module.context(self.rows, journal)
        self.assertEqual(state['global_reason'], None)
        # OBSERVED: the pre-own-comment reader cannot pair the new store record with
        # its comment, so the abandonment reads damaged with no lifecycle fact
        # silently restored (the void it re-opened is not honoured).
        self.assertEqual(state['entry_reasons'], {'abandon-current': 'lifecycle-void-journal-mismatch'})
        self.assertEqual(state['reasons'], {'p-task.2': 'lifecycle-void-journal-mismatch'})
        self.assertEqual([str(c['id']) for c in state['incomplete']], [str(comment['id'])])
        with self.assertRaisesRegex(ValueError, 'does not bind the native target/comment generation'):
            module.backup_files(self.project, self.rows)
        # The older kit's own CLI still READS the project: the family stays readable
        # (no lifecycle-void-journal-unreadable) and the command exits 0.
        exported = self.project / 'older-export.jsonl'
        exported.write_text(''.join(json.dumps(row) + '\n' for row in self.rows))
        result = subprocess.run([sys.executable, str(base / 'lifecycle.py'), 'list',
                                 '--export', str(exported), '--journal', str(self.project)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('lifecycle-void-journal-unreadable', result.stdout)
        # A void of ANOTHER event is still accepted: the family itself stays a
        # schema-version-1 void/retract journal; only the new abandonment is
        # unreadable to the older proof rule.
        fresh = dict(self.payload('void', 'void-other'), binding=module.event_binding(self.rows[1]))
        answer = module.apply(self.rows, 'op', fresh, self.native_run, self.project,
                              operators=['op'], enabled=True)
        self.assertEqual((answer['operation'], answer['reconciled']), ('void', False))

    def test_the_reviewed_release_reads_the_audit_damaged_and_refuses_the_backup(self):
        self.assert_older_release_not_rollback_clean(REVIEWED_RELEASE_COMMIT, 'reviewed_release_voids')

    def test_the_current_release_reads_the_audit_damaged_and_refuses_the_backup(self):
        self.assert_older_release_not_rollback_clean(CURRENT_RELEASE_COMMIT, 'current_release_voids')


class HostCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.project = Path(self.scratch.name)
        self.rows = rows()
        self.native_writes = 0

    def payload(self, operation='void', key='void-one', target=None, actor='op'):
        return dict(schema_version=1, operation=operation, operation_id=key, actor=actor,
                    reason='Operator reviewed the source evidence.', evidence=['synthetic review'],
                    binding=voids.event_binding(self.rows[2]), target_void=target)

    def native_run(self, argv):
        if argv == ['comments', 'p-task', '--json']:
            return json.dumps(self.rows[0]['comments'])
        self.assertEqual(argv[:3], ['comments', 'add', 'p-task'])
        self.native_writes += 1
        comment = dict(id=str(self.native_writes), author=json.loads(argv[3][len(voids.PREFIX):])['actor'],
                       text=argv[3], created_at='2026-10-02T00:00:0' + str(self.native_writes) + 'Z')
        self.rows[0]['comments'].append(comment)
        return json.dumps(comment)

    def apply(self, payload, **options):
        return voids.apply(self.rows, payload['actor'], payload, self.native_run, self.project,
                           operators=options.pop('operators', ['op']), enabled=options.pop('enabled', True),
                           **options)

    def reading(self):
        return voids.context(self.rows, voids.read_journal(self.project))

    def family(self):
        """The on-disk ``.lifecycle-voids`` family only (never the abandonment store)."""
        return voids.read_journal(self.project)['entries']

    def cut(self, operation='void', key='void-one', target=None):
        """A native correction comment whose host record was cut before publication."""
        payload = self.payload(operation, key, target)
        with mock.patch.object(voids, 'publish', side_effect=OSError('synthetic cut')):
            with self.assertRaisesRegex(ValueError, 'uncertain'):
                self.apply(payload)
        return payload

    def abandon_payload(self, cut, key='abandon-one', actor='op'):
        return dict(schema_version=1, operation='abandon', operation_id=key, actor=actor,
                    reason='Operator closed the cut without the original words.',
                    evidence=['synthetic audit pointer'], binding=cut['binding'],
                    target_void=cut['operation_id'])

    def abandon(self, payload, comment_id, **options):
        """Abandon ``comment_id`` through the real guarded native write."""
        return voids.abandon(self.rows, payload['actor'], payload, self.native_run, self.project,
                             operators=options.pop('operators', ['op']),
                             enabled=options.pop('enabled', True),
                             comment_id=comment_id, **options)

    def cut_abandonment(self, key='abandon-cut', actor='op', **options):
        """A cut ABANDONMENT: its OWN native comment is written, its store record is not.

        kittrial-5bb.274 round 3. Returns ``(cut, cut_comment, payload, own)`` where
        ``own`` is the abandonment's own native comment that the audit must parse.
        """
        cut = self.cut()
        comment = self.rows[0]['comments'][0]
        payload = self.abandon_payload(cut, key=key, actor=actor)
        with mock.patch.object(voids, 'publish_abandon', side_effect=OSError('synthetic cut')):
            with self.assertRaisesRegex(ValueError, 'host record is uncertain'):
                self.abandon(payload, comment['id'], **options)
        own = self.rows[0]['comments'][-1]
        self.assertEqual((own['author'], json.loads(own['text'][len(voids.PREFIX):])),
                         (actor, payload))
        return cut, comment, payload, own

    def store(self, records):
        """Write the abandonment store directly (the cut/typed/hand-added cases)."""
        (self.project / voids.ABANDON_FILE).write_text(json.dumps(
            voids.abandon_document(records), ensure_ascii=False, sort_keys=True))
        if os.name == 'posix':
            os.chmod(self.project / voids.ABANDON_FILE, 0o600)

    def store_record(self, payload, comment_id, created_at, **overrides):
        entry = dict(schema_version=1, payload=payload, sha256=content_hash(payload),
                     comment_id=str(comment_id), created_at=created_at)
        entry.update(overrides)
        return entry

    def audit_row(self, key):
        page = voids.audit(voids.snapshot(self.rows, self.project), operators=['op'])
        return page, next(c for c in page['corrections'] if c['operation_id'] == key)

    def test_a_cut_abandonment_parses_and_names_its_exact_retry(self):
        """Round 3 item 1: the orphan abandonment comment is named, not unclassifiable."""
        cut, comment, payload, own = self.cut_abandonment()
        self.assertEqual(self.native_writes, 2)
        self.assertFalse((self.project / voids.ABANDON_FILE).exists())
        state = self.reading()
        self.assertEqual(state['reasons'], {})
        self.assertEqual(state['abandon_problems'], [])
        self.assertEqual({str(c['id']) for c in state['incomplete']},
                         {str(comment['id']), str(own['id'])})
        self.assertIn(str(own['id']), {str(k) for k in state['orphan_abandons']})
        page, item = self.audit_row('abandon-cut')
        self.assertEqual((page['correction_status'], page['incomplete_correction_count']),
                         ('incomplete', 2))
        self.assertEqual((item['status'], item['operation'], item['operator'],
                          item['retry_operation_id'], item['retry_operator'],
                          item['target_void'], item['target_comment_id'],
                          item['classification_reason']),
                         ('incomplete', 'abandon', 'op', 'abandon-cut', 'op',
                          cut['operation_id'], comment['id'],
                          'native-correction-without-host-entry'))
        with self.assertRaisesRegex(ValueError, 'without their host entries'):
            voids.backup_files(self.project, self.rows)

    def test_the_exact_retry_of_a_cut_abandonment_completes_while_the_cut_is_open(self):
        """Round 3 item 1: the exact retry writes the missing store record, reusing the comment."""
        cut, comment, payload, own = self.cut_abandonment()
        answer = self.abandon(payload, comment['id'])
        self.assertTrue(answer['reconciled'])
        self.assertEqual(answer['comment_id'], own['id'])
        self.assertEqual(answer['target_comment_id'], comment['id'])
        self.assertEqual(self.native_writes, 2)
        state = self.reading()
        self.assertEqual(state['incomplete'], [])
        self.assertEqual(state['reasons'], {})
        self.assertEqual(state['orphan_abandons'], {})
        page, item = self.audit_row('abandon-cut')
        self.assertEqual((page['correction_status'], item['status'], item['target_comment_id'],
                          item['native_operator_proof']),
                         ('complete', 'abandoned', comment['id'], True))
        self.assertIn(voids.ABANDON_FILE, voids.backup_files(self.project, self.rows))

    def test_a_cut_abandonment_whose_operator_left_reads_superseded(self):
        """Round 3 item 1: a listed operator closes the same cut; the orphan reads superseded."""
        cut, comment, payload, own = self.cut_abandonment(key='abandon-cut', actor='op2',
                                                          operators=['op', 'op2'])
        fresh = self.abandon_payload(cut, key='abandon-other', actor='op')
        answer = self.abandon(fresh, comment['id'], operators=['op'])
        self.assertEqual(answer['target_comment_id'], comment['id'])
        self.assertEqual(self.rows[0]['comments'][-1]['author'], 'op')
        state = self.reading()
        self.assertEqual((state['reasons'], state['incomplete'], state['abandon_problems']),
                         ({}, [], []))
        page, orphan = self.audit_row('abandon-cut')
        self.assertEqual((orphan['status'], orphan['superseded_by'], orphan['duplicate_host_entry'],
                          orphan['target_comment_id'], orphan['retry_operation_id'],
                          orphan['retry_operator'], orphan['operator_listed']),
                         ('superseded', 'abandon-other', True, comment['id'],
                          'abandon-cut', 'op2', False))
        self.assertEqual((page['correction_status'], page['superseded_abandonment_count']),
                         ('complete', 1))
        self.assertIn(voids.ABANDON_FILE, voids.backup_files(self.project, self.rows))
        # op2 re-listed: the exact retry reconciles with that later record instead of
        # dead-ending in "already has a host entry".
        retry = self.abandon(payload, comment['id'], operators=['op', 'op2'])
        self.assertTrue(retry['reconciled'])
        self.assertEqual((retry['comment_id'], retry['superseded_by']),
                         (own['id'], 'abandon-other'))
        self.assertEqual(set(voids.read_journal(self.project)['abandons']), {'abandon-other'})

    def test_the_audit_says_whether_a_rows_operator_is_a_listed_operator(self):
        """kittrial-5bb.282: a native abandonment written straight to bd by someone who is not a
        listed operator closes nothing, and once a proved record closes its cut it reads
        ``superseded`` - the row now also SAYS that its operator is not one this installation
        lists (True, False, or null when the caller passed no list)."""
        cut, comment, payload, own = self.cut_abandonment(key='abandon-cut', actor='op2',
                                                          operators=['op', 'op2'])
        fresh = self.abandon_payload(cut, key='abandon-other', actor='op')
        self.abandon(fresh, comment['id'], operators=['op'])
        page, orphan = self.audit_row('abandon-cut')
        self.assertEqual((orphan['status'], orphan['operator'], orphan['operator_listed']),
                         ('superseded', 'op2', False))
        # The proved record that closed the cut is by a listed operator: True.
        other = next(c for c in page['corrections'] if c['operation_id'] == 'abandon-other')
        self.assertEqual((other['operator'], other['operator_listed']), ('op', True))
        # No list given: unknown, never a claim about the person.
        unknown = voids.audit(voids.snapshot(self.rows, self.project))
        row = next(c for c in unknown['corrections'] if c['operation_id'] == 'abandon-cut')
        self.assertIsNone(row['operator_listed'])

    def assert_the_proof_refuses(self, key='abandon-cut'):
        state = self.reading()
        self.assertEqual(state['reasons'], {})
        self.assertEqual(state['unproved_abandons'], [key])
        self.assertIn('no native comment by its operator', state['abandon_problems'][0])
        page, item = self.audit_row(key)
        self.assertEqual((page['correction_status'], item['status'],
                          item['classification_reason']),
                         ('damaged', 'damaged', 'abandonment-without-native-operator-proof'))
        with self.assertRaisesRegex(ValueError, 'unproved or duplicate abandonment'):
            voids.backup_files(self.project, self.rows)

    def test_the_proof_compares_the_own_comments_author(self):
        """Round 3 item 2(1): removing the author comparison must break this test."""
        cut, comment, payload, own = self.cut_abandonment()
        own['author'] = 'mallory'
        self.store({payload['operation_id']: self.store_record(payload, own['id'], own['created_at'])})
        self.assert_the_proof_refuses()

    def test_the_proof_compares_the_own_comments_text(self):
        """Round 3 item 2(1): removing the text comparison must break this test."""
        cut, comment, payload, own = self.cut_abandonment()
        own['text'] = own['text'].replace(payload['reason'], 'rewritten after the native write')
        self.store({payload['operation_id']: self.store_record(payload, own['id'], own['created_at'])})
        self.assert_the_proof_refuses()

    def test_the_proof_compares_the_own_comments_native_time(self):
        """Round 3 item 2(1): removing the native-time comparison must break this test."""
        cut, comment, payload, own = self.cut_abandonment()
        self.store({payload['operation_id']: self.store_record(payload, own['id'],
                                                               '2020-01-01T00:00:00Z')})
        self.assert_the_proof_refuses()

    def test_abandon_closes_an_incomplete_correction_without_touching_the_family(self):
        """Item 1: its OWN native comment proves it; the family stays schema-1."""
        cut = self.cut()
        comment = self.rows[0]['comments'][0]
        self.assertEqual([c['id'] for c in self.reading()['incomplete']], [comment['id']])
        payload = self.abandon_payload(cut)
        answer = self.abandon(payload, comment['id'])
        self.assertFalse(answer['reconciled'])
        # kittrial-5bb.274 item 1: the abandonment writes its OWN native comment
        # through the guarded write; the cut comment is neither re-written nor deleted.
        self.assertEqual(self.native_writes, 2)
        self.assertEqual(self.rows[0]['comments'][0], comment)
        own = self.rows[0]['comments'][1]
        self.assertEqual(answer['comment_id'], own['id'])
        self.assertEqual(answer['target_comment_id'], comment['id'])
        self.assertEqual(json.loads(own['text'][len(voids.PREFIX):]), payload)
        # The family itself is untouched and holds no abandon operation; the record
        # is beside it in ABANDON_FILE.
        self.assertNotIn('abandon', {e['payload']['operation'] for e in self.family().values()})
        self.assertTrue((self.project / voids.ABANDON_FILE).is_file())
        journal = voids.read_journal(self.project)
        self.assertEqual(set(journal['abandons']), {'abandon-one'})
        self.assertEqual(journal['abandons']['abandon-one']['comment_id'], own['id'])
        state = self.reading()
        self.assertEqual(state['incomplete'], [])
        self.assertEqual(state['reasons'], {})
        self.assertEqual(state['abandon_targets'], {'abandon-one': comment['id']})
        page = voids.audit(voids.snapshot(self.rows, self.project), operators=['op'])
        self.assertEqual((page['abandoned_correction_count'], page['incomplete_correction_count'],
                          page['correction_status']), (1, 0, 'complete'))
        item = next(c for c in page['corrections'] if c['operation'] == 'abandon')
        self.assertEqual((item['status'], item['operator'], item['reason'], item['comment_id'],
                          item['target_comment_id'], item['native_operator_proof']),
                         ('abandoned', 'op', 'Operator closed the cut without the original words.',
                          own['id'], comment['id'], True))
        files = voids.backup_files(self.project, self.rows)
        self.assertIn(voids.MANIFEST, files)
        self.assertIn(voids.ABANDON_FILE, files)
        self.assertEqual([name for name in files if name.startswith(voids.DIRECTORY + '/')], [])
        retry = self.abandon(payload, comment['id'])
        self.assertTrue(retry['reconciled'])
        self.assertEqual(self.native_writes, 2)
        self.assertEqual(set(voids.read_journal(self.project)['abandons']), {'abandon-one'})
        self.assertEqual(self.family(), {})

    def test_abandon_records_beside_an_issued_family_and_survives_backup_restore(self):
        answer = self.apply(self.payload())
        cut = self.cut('retract', 'retract-two', 'void-one')
        comment = self.rows[0]['comments'][-1]
        self.abandon(self.abandon_payload(cut, key='abandon-two'), comment['id'])
        files = voids.backup_files(self.project, self.rows)
        self.assertIn(voids.DIRECTORY + '/' + answer['journal_sha256'] + '.json', files)
        self.assertIn(voids.ABANDON_FILE, files)
        destination = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(destination, ignore_errors=True))
        self.assertTrue(voids.restore_files(destination, files, self.rows))
        restored = voids.read_journal(destination)
        self.assertEqual(set(restored['entries']), {'void-one'})
        self.assertEqual(set(restored['abandons']), {'abandon-two'})

    def test_backup_manifest_binds_the_abandonment_store(self):
        """kittrial-5bb.274 item 1: the store is hashed in the manifest."""
        cut = self.cut()
        comment = self.rows[0]['comments'][0]
        self.abandon(self.abandon_payload(cut), comment['id'])
        files = voids.backup_files(self.project, self.rows)
        manifest = files[voids.MANIFEST]
        self.assertEqual(manifest['files'][voids.ABANDON_FILE],
                         hashlib.sha256(voids.entry_bytes(files[voids.ABANDON_FILE])).hexdigest())
        # The review rewrote one record's operator and reason, recomputed its own
        # hash and restore-new accepted it (rev/in/s13.py); the manifest now binds it.
        tampered = copy.deepcopy(files)
        key = sorted(tampered[voids.ABANDON_FILE]['records'])[0]
        record = tampered[voids.ABANDON_FILE]['records'][key]
        record['payload']['actor'] = 'someone-else'
        record['payload']['reason'] = 'rewritten in the backup'
        record['sha256'] = content_hash(record['payload'])
        destination = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(destination, ignore_errors=True))
        with self.assertRaisesRegex(ValueError, 'manifest member/hash/byte mismatch'):
            voids.restore_files(destination, tampered, self.rows)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertTrue(voids.restore_files(destination, files, self.rows))
        self.assertEqual(set(voids.read_journal(destination)['abandons']), {'abandon-one'})

    def test_store_record_without_a_native_operator_comment_is_flagged(self):
        """kittrial-5bb.274 item 1(b): a hand-added record naming the TRUE author is damaged.

        A store record is proved ONLY by an abandonment comment of its own by its
        operator, so a record typed into the file - even one naming the true author
        of the cut - has no proof.
        """
        cut = self.cut()
        comment = self.rows[0]['comments'][0]
        payload = self.abandon_payload(cut, actor='op')     # the TRUE author of the cut
        entry = dict(schema_version=1, payload=payload, sha256=content_hash(payload),
                     comment_id=comment['id'], created_at='2026-10-05T00:00:00Z')
        voids.validate_entry(entry, allow_abandon=True)
        (self.project / voids.ABANDON_FILE).write_text(json.dumps(
            voids.abandon_document({payload['operation_id']: entry}), ensure_ascii=False, sort_keys=True))
        os.chmod(self.project / voids.ABANDON_FILE, 0o600)   # item 6: isolate this flag
        state = self.reading()
        self.assertEqual(state['reasons'], {})
        self.assertIn('no native comment by its operator', state['abandon_problems'][0])
        self.assertEqual(state['unproved_abandons'], ['abandon-one'])
        page = voids.audit(voids.snapshot(self.rows, self.project), operators=['op'])
        self.assertEqual(page['correction_status'], 'damaged')
        self.assertEqual(page['unproved_abandonment_count'], 1)
        item = next(c for c in page['corrections'] if c['operation'] == 'abandon')
        self.assertEqual((item['status'], item['native_operator_proof'],
                          item['classification_reason']),
                         ('damaged', False, 'abandonment-without-native-operator-proof'))
        with self.assertRaisesRegex(ValueError, 'unproved or duplicate abandonment'):
            voids.backup_files(self.project, self.rows)

    def test_a_proved_abandonment_superseded_by_a_later_host_entry_reads_clean(self):
        """kittrial-5bb.274 item 2: the rollback-reopened correction is NOT damaged."""
        cut = self.cut('void', 'void-two')              # a cut void on p-task.2
        cut_comment = self.rows[0]['comments'][0]
        self.abandon(self.abandon_payload(cut, key='abandon-two'), cut_comment['id'])
        # The released kit re-opens the correction by its exact retry: a later host
        # entry for the very operation the abandonment names, so the void in effect wins.
        later = dict(schema_version=1, payload=cut, sha256=content_hash(cut),
                     comment_id=cut_comment['id'], created_at=cut_comment['created_at'])
        voids.publish(self.project, later)
        state = self.reading()
        self.assertEqual(state['reasons'], {'p-task.2': 'voided'})
        self.assertEqual(state['abandon_problems'], [])
        self.assertEqual(state['incomplete'], [])
        self.assertEqual(state['superseded_abandons'], {'abandon-two': 'void-two'})
        page = voids.audit(voids.snapshot(self.rows, self.project), operators=['op'])
        self.assertEqual((page['correction_status'], page['damaged_correction_count'],
                          page['superseded_abandonment_count'], page['in_effect_void_count']),
                         ('complete', 0, 1, 1))
        row = next(c for c in page['corrections'] if c['operation_id'] == 'abandon-two')
        self.assertEqual((row['status'], row['superseded_by'], row['duplicate_host_entry']),
                         ('superseded', 'void-two', True))
        self.assertIn(voids.ABANDON_FILE, voids.backup_files(self.project, self.rows))

    def test_abandon_by_another_listed_operator_closes_a_cut_whose_author_left(self):
        """Item 1(a): the cut's author has left the list; another operator closes it."""
        cut = self.cut()
        cut_comment = self.rows[0]['comments'][0]
        other = self.abandon_payload(cut, key='abandon-other', actor='op2')
        answer = self.abandon(other, cut_comment['id'], operators=['op2'])
        self.assertEqual(answer['target_comment_id'], cut_comment['id'])
        self.assertEqual(self.rows[0]['comments'][-1]['author'], 'op2')
        state = self.reading()
        self.assertEqual(state['incomplete'], [])
        self.assertEqual(state['abandon_problems'], [])
        page = voids.audit(voids.snapshot(self.rows, self.project), operators=['op2'])
        self.assertEqual(page['correction_status'], 'complete')
        self.assertIn(voids.ABANDON_FILE, voids.backup_files(self.project, self.rows))

    def test_abandon_by_a_bound_actor_of_the_listed_operator(self):
        """Item 1: a listed operator's bound actor may abandon, and proves it itself."""
        cut = self.cut()
        cut_comment = self.rows[0]['comments'][0]
        bound = self.abandon_payload(cut, key='abandon-bound', actor='session-one')
        owners = {'op': 'person:owner', 'session-one': 'person:owner'}
        answer = self.abandon(bound, cut_comment['id'], operators=['op'], owners=owners)
        self.assertEqual(answer['comment_id'], self.rows[0]['comments'][-1]['id'])
        self.assertEqual(self.rows[0]['comments'][-1]['author'], 'session-one')
        state = self.reading()
        self.assertEqual(state['incomplete'], [])
        self.assertEqual(state['abandon_problems'], [])
        page = voids.audit(voids.snapshot(self.rows, self.project), operators=['op'], owners=owners)
        self.assertEqual(page['correction_status'], 'complete')

    @unittest.skipUnless(os.name == 'posix', 'POSIX mode bits on the host store')
    def test_abandonment_store_is_created_private_and_world_writable_is_flagged(self):
        """kittrial-5bb.274 item 6: create 600; complain about group/world write."""
        cut = self.cut()
        comment = self.rows[0]['comments'][0]
        self.abandon(self.abandon_payload(cut), comment['id'])
        store = self.project / voids.ABANDON_FILE
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o600)
        os.chmod(store, 0o666)
        state = self.reading()
        self.assertEqual(state['reasons'], {})
        self.assertTrue(any('group- or world-writable' in problem
                            for problem in state['abandon_problems']), state['abandon_problems'])
        page = voids.audit(voids.snapshot(self.rows, self.project), operators=['op'])
        self.assertEqual(page['correction_status'], 'damaged')
        with self.assertRaisesRegex(ValueError, 'unreadable lifecycle abandonment store'):
            voids.backup_files(self.project, self.rows)

    def test_lifecycle_void_generation_command_prints_the_live_value_and_manifest_location(self):
        """kittrial-5bb.274 item 5: the operator's comparison has a read."""
        import admin
        self.apply(self.payload())
        exported = '\n'.join(json.dumps(row) for row in self.rows)
        before = sorted(path.name for path in self.project.iterdir())
        with mock.patch.object(admin, 'project_dir', return_value=self.project), \
                mock.patch.object(admin, 'run_bd', return_value=exported) as native:
            report = admin.lifecycle_void_generation_command(self.project, 'pp')
        native.assert_called_once_with(self.project, 'pp', ['export', '--all'])
        self.assertEqual(report['native_corrections_sha256'],
                         voids.native_generation(self.rows))
        self.assertEqual(report['manifest'], voids.MANIFEST)
        self.assertEqual(report['backup_sidecar'], 'backups/pp.coordination.json')
        self.assertIn('backups/pp.coordination.json', report['note'])
        backup = voids.backup_files(self.project, self.rows)
        self.assertEqual(report['native_corrections_sha256'],
                         backup[voids.MANIFEST]['native_corrections_sha256'])
        self.assertEqual(sorted(path.name for path in self.project.iterdir()), before)

    def test_abandonment_store_damage_reads_closed_never_changes_a_fact(self):
        cut = self.cut()
        comment = self.rows[0]['comments'][0]
        answer = self.abandon(self.abandon_payload(cut), comment['id'])
        (self.project / voids.ABANDON_FILE).write_text('{"schema_version": 2, "records": {}}')
        os.chmod(self.project / voids.ABANDON_FILE, 0o600)   # item 6: isolate the read problem
        state = self.reading()
        self.assertEqual(state['abandon_problems'], ['Invalid lifecycle abandonment store'])
        # With the store unreadable, BOTH the cut and the abandonment's own comment
        # read unpaired again, so backups stay stopped.
        self.assertEqual({c['id'] for c in state['incomplete']},
                         {str(comment['id']), str(answer['comment_id'])})
        self.assertEqual(state['reasons'], {})
        page = voids.audit(voids.snapshot(self.rows, self.project), operators=['op'])
        self.assertEqual((page['correction_status'], page['abandon_problems']),
                         ('damaged', ['Invalid lifecycle abandonment store']))
        with self.assertRaisesRegex(ValueError, 'unreadable lifecycle abandonment store'):
            voids.backup_files(self.project, self.rows)
        self.assertEqual(voids.read_journal(self.project)['entries'], {})

    def test_abandon_refuses_an_issued_correction_off_switch_and_unlisted_actor(self):
        self.apply(self.payload())                      # an issued void, with a host entry
        comment = self.rows[0]['comments'][0]
        issued = self.abandon_payload(self.payload())
        with self.assertRaisesRegex(ValueError, 'already has a host entry'):
            self.abandon(issued, comment['id'])
        cut = self.cut('retract', 'retract-one', 'void-one')
        comment = self.rows[0]['comments'][-1]
        cut_payload = self.abandon_payload(cut, key='abandon-two')
        with self.assertRaisesRegex(ValueError, 'lifecycle_void_writes is off'):
            self.abandon(cut_payload, comment['id'], enabled=False)
        with self.assertRaisesRegex(ValueError, 'currently listed operator'):
            self.abandon(dict(cut_payload, actor='mallory'), comment['id'])
        self.assertEqual(set(voids.read_journal(self.project)['entries']), {'void-one'})
        self.assertEqual(voids.read_journal(self.project)['abandons'], {})
        self.assertEqual(self.native_writes, 2)
        # A second abandonment of the same comment under a new key is refused too.
        self.abandon(cut_payload, comment['id'])
        with self.assertRaisesRegex(ValueError, 'already has a host entry'):
            self.abandon(dict(cut_payload, operation_id='abandon-three'), comment['id'])
        self.assertEqual(self.native_writes, 3)

    def test_abandon_binds_the_named_comment_even_after_its_target_changes(self):
        cut = self.cut()
        comment = self.rows[0]['comments'][0]
        altered = copy.deepcopy(self.rows)
        altered[2]['title'] = 'changed after the cut'
        writes = []

        def run(argv):
            if argv == ['comments', 'p-task', '--json']:
                return json.dumps(altered[0]['comments'])
            writes.append(argv)
            written = dict(id=str(len(altered[0]['comments']) + 1),
                           author=json.loads(argv[3][len(voids.PREFIX):])['actor'],
                           text=argv[3], created_at='2026-10-06T00:00:0' + str(len(writes)) + 'Z')
            altered[0]['comments'].append(written)
            return json.dumps(written)

        voids.abandon(altered, 'op', self.abandon_payload(cut), run, self.project,
                      operators=['op'], enabled=True, comment_id=comment['id'])
        state = voids.context(altered, voids.read_journal(self.project))
        self.assertEqual(state['incomplete'], [])
        self.assertEqual(state['reasons'], {})
        self.assertIn(voids.MANIFEST, voids.backup_files(self.project, altered))

    def test_abandon_closes_an_incomplete_retraction_without_clearing_the_void(self):
        self.apply(self.payload())                      # issued void stays in effect
        retract = self.cut('retract', 'retract-one', 'void-one')
        comment = self.rows[0]['comments'][-1]
        self.assertEqual([c['id'] for c in self.reading()['incomplete']], [comment['id']])
        self.abandon(self.abandon_payload(retract), comment['id'])
        state = self.reading()
        self.assertEqual(state['incomplete'], [])
        self.assertEqual(state['reasons'], {'p-task.2': 'voided'})
        self.assertIn(voids.MANIFEST, voids.backup_files(self.project, self.rows))

    def test_an_abandoned_correction_is_final_so_its_exact_retry_is_refused(self):
        """Item 2, the stated rule: abandonment is final; a fresh void under a new key is the way."""
        cut = self.cut()
        comment = self.rows[0]['comments'][0]
        self.abandon(self.abandon_payload(cut), comment['id'])
        with self.assertRaisesRegex(ValueError, r'abandoned .*final'):
            self.apply(cut)
        self.assertEqual(self.reading()['reasons'], {})
        self.assertEqual([c['id'] for c in self.reading()['incomplete']], [])
        self.assertEqual(self.native_writes, 2)
        # The way to void the event afterwards is a fresh operation identity.
        fresh = self.payload('void', 'void-after-abandon')
        answer = self.apply(fresh)
        self.assertFalse(answer['reconciled'])
        self.assertEqual(self.reading()['reasons'], {'p-task.2': 'voided'})
        page = voids.audit(voids.snapshot(self.rows, self.project), operators=['op'])
        self.assertEqual((page['abandoned_correction_count'], page['in_effect_void_count']), (1, 1))
        abandon_row = next(c for c in page['corrections'] if c['operation'] == 'abandon')
        self.assertEqual((abandon_row['status'], abandon_row['target_comment_id']),
                         ('abandoned', comment['id']))

    def test_abandon_and_the_exact_retry_race_leaves_exactly_one_winner(self):
        """Item 2 (ii): the two racing writers serialize; never both in effect."""
        # Order one: the abandonment is written first, so the exact retry is refused.
        cut = self.cut('void', 'race-void-a')
        comment = self.rows[0]['comments'][-1]
        self.abandon(self.abandon_payload(cut, key='race-abandon-a'), comment['id'])
        with self.assertRaisesRegex(ValueError, 'abandoned'):
            self.apply(cut)
        self.assertEqual(self.family(), {})
        self.assertEqual(set(voids.read_journal(self.project)['abandons']), {'race-abandon-a'})
        self.assertEqual(self.reading()['reasons'], {})
        # Order two, a second project with its own rows: the retry publishes its
        # entry first, so the abandonment of that same comment is refused.
        rows_two = rows()
        writes = []

        def run(argv):
            if argv == ['comments', 'p-task', '--json']:
                return json.dumps(rows_two[0]['comments'])
            writes.append(argv)
            comment = dict(id=str(len(writes)), author=json.loads(argv[3][len(voids.PREFIX):])['actor'],
                           text=argv[3], created_at='2026-10-02T00:00:0' + str(len(writes)) + 'Z')
            rows_two[0]['comments'].append(comment)
            return json.dumps(comment)

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        project = Path(scratch.name)
        second = dict(schema_version=1, operation='void', operation_id='race-void-b', actor='op',
                      reason='Operator reviewed the source evidence.', evidence=['synthetic review'],
                      binding=voids.event_binding(rows_two[2]), target_void=None)
        voids.apply(rows_two, 'op', second, run, project, operators=['op'], enabled=True)
        with self.assertRaisesRegex(ValueError, 'already has a host entry'):
            voids.abandon(rows_two, 'op', self.abandon_payload(second, key='race-abandon-b'),
                          run, project, operators=['op'], enabled=True,
                          comment_id=rows_two[0]['comments'][0]['id'])
        self.assertEqual(set(voids.read_journal(project)['entries']), {'race-void-b'})
        self.assertEqual(voids.read_journal(project)['abandons'], {})
        self.assertEqual(voids.context(rows_two, voids.read_journal(project))['reasons'],
                         {'p-task.2': 'voided'})

    @unittest.skipUnless(os.name == 'posix', 'the host abandon command takes the POSIX project lock')
    def test_admin_abandon_command_prints_the_lost_entry_rule_before_the_write(self):
        import admin
        cut = self.cut()
        comment = self.rows[0]['comments'][0]
        (self.project / '.beads').mkdir(exist_ok=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}')
        exported = '\n'.join(json.dumps(row) for row in self.rows)
        captured_out = io.StringIO()
        captured_err = io.StringIO()
        printed_at_write = []

        def check(*args, **options):
            printed_at_write.append(voids.ABANDON_LOST_ENTRY_RULE in captured_err.getvalue())
            return dict(operation='abandon', comment_id=comment['id'])

        with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err), \
                mock.patch.object(admin, 'project_dir', return_value=self.project), \
                mock.patch.object(admin, 'operators', return_value=['op']), \
                mock.patch.object(admin, 'run_bd', return_value=exported), \
                mock.patch.object(admin, 'lifecycle_void_writes', return_value=True), \
                mock.patch.object(admin, 'deployment_config_lock',
                                  return_value=contextlib.nullcontext()), \
                mock.patch('review_workflow.operator_owners', return_value={}), \
                mock.patch('review_workflow.listed_operator_actor', return_value=True), \
                mock.patch.object(voids, 'abandon', side_effect=check) as writer:
            admin.lifecycle_abandon_command(self.project, 'pp', comment['id'], 'op',
                                            'Operator closed the cut.', ['synthetic audit'],
                                            'admin-abandon-one')
        self.assertTrue(writer.called)
        self.assertEqual(printed_at_write, [True])
        self.assertIn(voids.ABANDON_LOST_ENTRY_RULE, captured_err.getvalue())
        # kittrial-5bb.274 item 3: stdout is exactly one JSON document (empty here,
        # because the rule is on stderr before the write).
        self.assertEqual(captured_out.getvalue(), '')
        self.assertIn('LOST', voids.ABANDON_LOST_ENTRY_RULE)
        operations = (Path(lifecycle.__file__).resolve().parent / 'docs' / 'OPERATIONS.md').read_text(
            encoding='utf-8')
        self.assertIn('restore that family from the backup', operations)
        self.assertIn('exact retry of an abandoned', operations)
        self.assertIn('does NOT hold across this rollback', operations)
        self.assertIn('lifecycle-void-generation', operations)

    @unittest.skipUnless(os.name == 'posix', 'the host abandon command takes the POSIX project lock')
    def test_admin_abandon_command_writes_its_own_comment_and_host_evidence(self):
        import admin
        cut = self.cut()
        comment = self.rows[0]['comments'][0]
        (self.project / '.beads').mkdir(exist_ok=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}')
        exported = '\n'.join(json.dumps(row) for row in self.rows)
        written = []

        def run_bd(root, project, argv):
            rest = argv[2:]
            if rest == ['export', '--all']:
                return exported
            if rest[:2] == ['comments', 'add']:
                written.append(rest)
                record = dict(id=str(len(self.rows[0]['comments']) + 1),
                              author=json.loads(rest[3][len(voids.PREFIX):])['actor'],
                              text=rest[3],
                              created_at='2026-10-07T00:00:0' + str(len(self.rows[0]['comments']) + 1) + 'Z')
                self.rows[0]['comments'].append(record)
                return json.dumps(record)
            if rest == ['comments', 'p-task', '--json']:
                return json.dumps(self.rows[0]['comments'])
            raise AssertionError(rest)

        with mock.patch.object(admin, 'project_dir', return_value=self.project), \
                mock.patch.object(admin, 'operators', return_value=['op']), \
                mock.patch.object(admin, 'run_bd', side_effect=run_bd), \
                mock.patch.object(admin, 'lifecycle_void_writes', return_value=True), \
                mock.patch.object(admin, 'deployment_config_lock',
                                  return_value=contextlib.nullcontext()), \
                mock.patch('review_workflow.operator_owners', return_value={}), \
                mock.patch('review_workflow.listed_operator_actor', return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            answer = admin.lifecycle_abandon_command(self.project, 'pp', comment['id'], 'op',
                                                     'Operator closed the cut.', ['synthetic audit'],
                                                     'admin-abandon-one')
        self.assertEqual(answer['operation'], 'abandon')
        self.assertEqual(answer['target_comment_id'], comment['id'])
        own = self.rows[0]['comments'][-1]
        self.assertEqual(answer['comment_id'], own['id'])
        self.assertNotEqual(answer['comment_id'], comment['id'])
        self.assertEqual(self.native_writes, 1)
        record = voids.read_journal(self.project)['abandons']['admin-abandon-one']
        self.assertEqual(record['comment_id'], own['id'])
        self.assertEqual((record['payload']['actor'], record['payload']['reason']),
                         ('op', 'Operator closed the cut.'))
        self.assertEqual(self.family(), {})

    def test_read_and_off_refusal_create_no_family_and_raw_comment_is_not_authority(self):
        before = copy.deepcopy(self.rows)
        self.assertEqual(voids.read_journal(self.project)['entries'], {})
        self.assertIs(voids.snapshot(self.rows, self.project), self.rows)
        with self.assertRaisesRegex(ValueError, 'lifecycle_void_writes is off'):
            self.apply(self.payload(), enabled=False)
        self.assertEqual(self.rows, before)
        self.assertEqual(list(self.project.iterdir()), [])
        self.native_run(['comments', 'add', 'p-task', voids.PREFIX + canonical_bytes(self.payload()).decode(), '--json'])
        self.assertEqual(self.reading()['reasons'], {})
        self.assertEqual(list(self.project.iterdir()), [])

    def test_authority_before_effect_including_bound_principal_and_unreadable_registry(self):
        payload = self.payload(actor='session-one')
        with self.assertRaisesRegex(ValueError, 'currently listed operator'):
            self.apply(payload, owners={'op': 'person:owner', 'session-one': 'person:other'})
        def unreadable():
            raise ValueError('session registry cannot be read')
        with self.assertRaisesRegex(ValueError, 'registry cannot be read'):
            self.apply(payload, owners=unreadable)
        self.assertEqual(self.native_writes, 0)
        self.assertEqual(list(self.project.iterdir()), [])
        self.apply(payload, owners={'op': 'person:owner', 'session-one': 'person:owner'})
        self.assertEqual(self.reading()['reasons'], {'p-task.2': 'voided'})

    def test_void_survives_operator_replacement_and_current_operator_can_retract(self):
        self.apply(self.payload())
        # No operator/owner argument exists on the reader: authority was checked at write.
        self.assertEqual(self.reading()['reasons'], {'p-task.2': 'voided'})
        retraction = self.payload('retract', 'retract-one', 'void-one', 'replacement')
        with self.assertRaisesRegex(ValueError, 'currently listed operator'):
            self.apply(retraction, operators=['another'])
        self.apply(retraction, operators=['replacement'])
        self.assertEqual(self.reading()['reasons'], {})
        self.assertEqual(len(list((self.project / voids.DIRECTORY).glob('*.json'))), 2)
        self.assertEqual(self.native_writes, 2)

    def test_native_cut_exact_retry_publishes_missing_record_without_duplicate_native_write(self):
        payload = self.payload()
        with mock.patch.object(voids, 'publish', side_effect=OSError('synthetic publication cut')):
            with self.assertRaisesRegex(ValueError, 'uncertain for operation void-one'):
                self.apply(payload)
        self.assertEqual(self.native_writes, 1)
        self.assertEqual(self.reading()['reasons'], {})
        answer = self.apply(payload)
        self.assertTrue(answer['reconciled'])
        self.assertEqual(self.native_writes, 1)
        self.assertEqual(self.reading()['reasons'], {'p-task.2': 'voided'})

    def test_conflicting_native_identity_refuses_before_creating_family(self):
        payload = self.payload()
        self.native_run(['comments', 'add', 'p-task', voids.PREFIX + canonical_bytes(payload).decode(), '--json'])
        conflicting = dict(payload, reason='Changed content for the same identity.')
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.apply(conflicting)
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(self.native_writes, 1)

    def test_target_or_native_comment_change_stays_unknown_and_cannot_be_retracted(self):
        self.apply(self.payload())
        original = copy.deepcopy(self.rows)
        self.rows[2]['description'] += 'changed'
        self.assertEqual(self.reading()['reasons'], {'p-task.2': 'lifecycle-void-target-changed'})
        with self.assertRaisesRegex(ValueError, 'exact host-issued void'):
            self.apply(self.payload('retract', 'retract-one', 'void-one'))
        self.rows = original
        self.rows[0]['comments'][0]['text'] += 'changed'
        self.assertEqual(self.reading()['reasons'], {'p-task.2': 'lifecycle-void-journal-mismatch'})
        with self.assertRaisesRegex(ValueError, 'binding is damaged'):
            self.apply(self.payload('retract', 'retract-two', 'void-one'))
        self.assertEqual(self.native_writes, 1)

    def test_damaged_or_renamed_journal_never_silently_restores_fact(self):
        self.apply(self.payload())
        file = next((self.project / voids.DIRECTORY).glob('*.json'))
        raw = file.read_bytes()
        file.write_text('{')
        self.assertEqual(self.reading()['global_reason'], voids.UNKNOWN_JOURNAL)
        file.write_bytes(raw)
        file.rename(file.with_name('0' * 64 + '.json'))
        self.assertEqual(self.reading()['global_reason'], voids.UNKNOWN_JOURNAL)
        with self.assertRaisesRegex(ValueError, 'journal cannot be read'):
            self.apply(self.payload('retract', 'retract-one', 'void-one'))
        self.assertEqual(self.native_writes, 1)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO boundary')
    def test_fifo_journal_read_is_bounded_and_does_not_block(self):
        directory = self.project / voids.DIRECTORY
        directory.mkdir()
        os.mkfifo(directory / ('0' * 64 + '.json'))
        self.assertTrue(voids.read_journal(self.project)['invalid'])

    def test_closed_payload_and_journal_shapes_refuse_extra_authority_fields(self):
        payload = self.payload()
        for key in ('operators', 'store', 'principal', 'authority', 'attestation'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'fields'):
                self.apply(dict(payload, **{key: 'untrusted'}))
        self.assertEqual(self.native_writes, 0)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_current_historical_and_capability_readers_agree_without_mutating_native_facts(self):
        original_event = copy.deepcopy(self.rows[2])
        self.apply(self.payload())
        captured = voids.snapshot(self.rows, self.project)
        current = lifecycle.project_facts(captured)[0]['facts']['integrated']
        self.assertEqual((current['value'], current['reason']), ('unknown', 'voided'))
        historical = lifecycle.integration_evidence(captured)[0]['scopes'][0]['integrated']
        self.assertEqual((historical['value'], historical['reason']), ('unknown', 'voided'))
        from capability_verification import integrated_commits
        self.assertEqual(integrated_commits(captured), set())
        from review_state import integration
        review = integration({'commit': 'a' * 40}, lifecycle.integration_evidence(captured)[0]['scopes'])
        self.assertEqual((review['fact'], review['reason']), ('unknown', 'voided'))
        self.assertEqual(self.rows[2], original_event)
        self.assertEqual(self.rows[0]['labels'], rows()[0]['labels'])

    def test_deleted_target_does_not_revive_an_older_pass_or_retry_identity(self):
        older = copy.deepcopy(self.rows[2])
        older['id'] = 'p-task.0'
        older['description'] = older['description'].replace('fact-2', 'fact-old')
        self.rows.append(older)
        self.apply(self.payload())
        target = self.rows.pop(2)
        captured = voids.snapshot(self.rows, self.project)
        current = lifecycle.project_facts(captured)[0]['facts']['integrated']
        self.assertEqual((current['value'], current['reason']), ('unknown', 'lifecycle-void-target-changed'))
        historical = lifecycle.integration_evidence(captured)[0]['scopes'][0]['integrated']
        self.assertEqual(historical['value'], 'unknown')
        index = lifecycle._operation_index(captured)
        self.assertNotIn('fact-2', index)
        self.assertEqual(index.blocked['fact-2'], 'lifecycle-void-target-changed')
        self.assertEqual(lifecycle.native_event(target)['value'], 'passed')

    def test_voided_historical_scope_fact_does_not_change_newer_scope_fact(self):
        original_scope = lifecycle.native_event(self.rows[1])['payload']['scope']
        new_scope = dict(original_scope, source_commit='c' * 40, integration_commit='d' * 40)
        for number, dimension, value in [(3, 'lifecycle-scope', content_hash(new_scope)), (4, 'integrated', 'passed')]:
            template = copy.deepcopy(self.rows[1 if dimension == 'lifecycle-scope' else 2])
            payload = dict(lifecycle.native_event(template)['payload'], operation_id='fact-' + str(number),
                           scope=new_scope, value=value)
            template.update(id='p-task.' + str(number), title='State change: ' + dimension + ' → ' + value,
                            description='Set ' + dimension + ' to ' + value + '\n\nReason: '
                            + lifecycle.PREFIX + canonical_bytes(payload).decode())
            self.rows.append(template)
        self.rows[0]['labels'] = ['lifecycle-scope:' + content_hash(new_scope), 'integrated:passed']
        self.apply(self.payload())
        captured = voids.snapshot(self.rows, self.project)
        self.assertEqual(lifecycle.project_facts(captured)[0]['facts']['integrated']['value'], 'passed')
        history = lifecycle.integration_evidence(captured)[0]['scopes']
        self.assertEqual([(s['scope']['source_commit'], s['integrated']['value']) for s in history],
                         [('c' * 40, 'passed'), ('a' * 40, 'unknown')])
        self.assertEqual(history[1]['integrated']['reason'], 'voided')

    def test_native_json_cannot_supply_host_correction_context(self):
        self.rows[0]['lifecycle_corrections'] = dict(reasons={'p-task.2': 'voided'})
        self.assertEqual(lifecycle.project_facts(self.rows)[0]['facts']['integrated']['value'], 'passed')
        self.assertNotIn('reason', lifecycle.project_facts(self.rows)[0]['facts']['integrated'])

    def test_request_reads_family_once_and_voided_fact_retry_is_pre_effect_refused(self):
        self.apply(self.payload())
        from http_authority import NativeRunner
        def native(argv):
            self.assertEqual(argv, ['export', '--all'], 'a voided operation must not attempt a native write')
            return '\n'.join(json.dumps(row) for row in self.rows)
        runner = NativeRunner(native, project=self.project)
        with mock.patch.object(voids, 'read_journal', wraps=voids.read_journal) as read:
            first = lifecycle.read_event_rows(runner)
            lifecycle.project_facts(first)
            lifecycle.integration_evidence(first)
            lifecycle.read_event_rows(runner)
            self.assertEqual(read.call_count, 1)
        payload = lifecycle.native_event(self.rows[2])['payload']
        with self.assertRaisesRegex(ValueError, 'event that reads unknown.*voided'):
            lifecycle.apply_native(payload, 'op', runner)
        self.assertFalse(runner.attempted_write)

    def test_audit_is_read_only_paged_and_names_unclassifiable_native_rows(self):
        self.rows.append(dict(id='p-unreadable', issue_type='event', malformed=True))
        before = copy.deepcopy(self.rows)
        page = voids.audit(self.rows, operators=['op'], limit=1)
        self.assertEqual((page['total'], len(page['items']), page['omitted_after']), (2, 1, 1))
        self.assertEqual(page['unclassifiable_native_event_count'], 1)
        self.assertEqual(page['next_offset'], 1)
        self.assertEqual(voids.audit(self.rows, operators=['op'], limit=1, offset=1)['next_offset'], None)
        self.assertEqual(self.rows, before)
        self.assertEqual(list(self.project.iterdir()), [])
        with self.assertRaisesRegex(ValueError, 'limit'):
            voids.audit(self.rows, limit=101)

    def test_audit_approval_uses_recording_time_and_stored_assignee_snapshot(self):
        import review_workflow as review
        contribution = dict(schema_version=1, operation='contribute', operation_id='contribute-one',
                            task='p-task', previous=None, supersedes=None, follows=None,
                            repository='https://example.test/repository.git', commit='a' * 40,
                            base_commit='f' * 40, delivery=dict(kind='remote', remote='git@example.test:repository.git',
                            branch='worker/change'), summary='Synthetic contribution.')
        approval = dict(schema_version=1, operation='approve', operation_id='approve-one',
                        task='p-task', previous='contribution', contribution='contribution',
                        summary='Synthetic independent approval.', assignee_at_approval='worker')
        review.validate(contribution, 'p-task'); review.validate(approval, 'p-task')
        self.rows[0]['comments'] = [dict(id='contribution', author='worker', created_at='2026-09-30T23:00:00Z',
            text=review.PREFIX + canonical_bytes(contribution).decode()),
            dict(id='approval', author='reviewer', created_at='2026-09-30T23:01:00Z',
            text=review.PREFIX + canonical_bytes(approval).decode())]
        page = voids.audit(self.rows, operators=['op'])
        integrated = next(x for x in page['items'] if x['dimension'] == 'integrated')
        self.assertTrue(integrated['independent_approval_at_recording'])
        self.assertFalse(integrated['historical_principal_ownership_verified'])
        self.rows[0]['comments'][1]['created_at'] = self.rows[2]['created_at']
        integrated = next(x for x in voids.audit(self.rows)['items'] if x['dimension'] == 'integrated')
        self.assertIsNone(integrated['independent_approval_at_recording'])
        self.assertEqual(integrated['classification_reason'], 'review-record-time-unclassifiable')

    def test_admin_audit_reads_one_export_and_creates_no_files(self):
        import admin
        exported = '\n'.join(json.dumps(row) for row in self.rows)
        with mock.patch.object(admin, 'project_dir', return_value=self.project), \
                mock.patch.object(admin, 'operators', return_value=['op']), \
                mock.patch.object(admin, 'run_bd', return_value=exported) as native:
            report = admin.lifecycle_audit_command(self.project, 'pp', limit=1)
        native.assert_called_once_with(self.project, 'pp', ['export', '--all'])
        self.assertEqual(report['project'], 'pp')
        self.assertEqual(report['omitted_after'], 1)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_capacity_refusal_keeps_room_for_retracting_existing_void(self):
        with mock.patch.object(voids, 'MAX_RECORDS', 2):
            self.apply(self.payload())
            new_void = dict(self.payload(key='void-scope'), binding=voids.event_binding(self.rows[1]))
            before = copy.deepcopy(self.rows)
            with self.assertRaisesRegex(ValueError, 'keeps space for outstanding retractions'):
                self.apply(new_void)
            self.assertEqual(self.rows, before)
            self.assertEqual(self.native_writes, 1)
            self.apply(self.payload('retract', 'retract-one', 'void-one'))
            self.assertEqual(self.reading()['reasons'], {})

    def test_raw_correction_prefix_and_future_versions_are_reserved(self):
        from reserved_comments import check_raw_request
        for prefix in (voids.PREFIX, 'Kind: lifecycle-event-correction-v99\n'):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, 'lifecycle correction'):
                check_raw_request(['comments', 'add', 'p-task', prefix + '{}'], {})

    def test_release_dryrun_reports_void_reason_without_mutating_release_or_native_records(self):
        # A recorded deployment/rollback/capability verification is independent
        # native evidence; correction only changes the derived lifecycle view.
        immutable = dict(id='p-release-record', issue_type='task', comments=[dict(
            id='release-comment', author='op', text='Synthetic immutable release and rollback evidence',
            created_at='2026-10-01T01:00:00Z')])
        self.rows.append(immutable)
        self.apply(self.payload())
        before = copy.deepcopy(self.rows)
        release = dict(source_commit='a' * 40, integration_commit='b' * 40,
                       release_id='synthetic-release', environment='synthetic')
        selection = lifecycle.release_selection(voids.snapshot(self.rows, self.project), release,
                                                lambda a,b: a == b)
        self.assertEqual(selection['targets'], [])
        self.assertEqual(selection['skipped'], [{'task':'p-task','reason':'integrated evidence is unknown: voided'}])
        self.assertEqual(self.rows, before)

    def test_completed_release_batch_void_changes_derived_deployment_and_blocks_batch_retry(self):
        import test_lifecycle_release as release
        store = release.NativeStore(tasks=('p-task',))
        scope = release.scope()
        store.record_honest(release.payload('p-task', 'lifecycle-scope', operation='release-test-scope', scope_value=scope))
        store.record_honest(release.payload('p-task', 'integrated', operation='release-test-integration', scope_value=scope))
        batch = release.release_payload([release.target('p-task')], live_verified=True)
        store.record_honest(batch)
        event = next(row for row in store.rows if (lifecycle.native_event(row) or {}).get('dimension') == 'deployed')
        release_events = copy.deepcopy([row for row in store.rows if row['issue_type'] == 'event'])
        def native(argv):
            task = store.rows[0]
            if argv == ['comments', 'p-task', '--json']:
                return json.dumps(task.get('comments', []))
            if argv[:3] == ['comments', 'add', 'p-task']:
                comment = dict(id='release-void-comment', author='op', text=argv[3], created_at='2026-10-02T00:00:00Z')
                task.setdefault('comments', []).append(comment)
                return json.dumps(comment)
            return store.run(argv)
        correction = dict(self.payload(key='void-release-deployment'), binding=voids.event_binding(event))
        voids.apply(store.rows, 'op', correction, native, self.project, operators=['op'], enabled=True)
        snapshot = voids.snapshot(store.rows, self.project)
        facts = next(item for item in lifecycle.project_facts(snapshot) if item['id'] == 'p-task')
        self.assertEqual(facts['facts']['deployed']['value'], 'unknown')
        self.assertEqual(facts['facts']['deployed']['reason'], 'voided')
        before = copy.deepcopy(store.rows)
        native.lifecycle_project = self.project
        with self.assertRaisesRegex(ValueError, 'voided or mismatched lifecycle event'):
            lifecycle.apply_native(batch, release.ACTOR, native)
        self.assertEqual(store.rows, before)
        self.assertEqual([row for row in store.rows if row['issue_type'] == 'event'], release_events)

    def test_raw_fact_void_keeps_an_explicit_unknown_reason(self):
        self.rows[2]['description'] = 'Set integrated to passed\n\nReason: raw assertion'
        self.apply(self.payload())
        facts = next(row for row in lifecycle.project_facts(voids.snapshot(self.rows, self.project))
                     if row['id'] == 'p-task')['facts']['integrated']
        self.assertEqual((facts['value'], facts['reason']), ('unknown', 'voided'))

    def test_restore_uncertainty_keeps_reason_even_when_native_events_are_absent(self):
        (self.project / voids.RESTORE_MARKER).write_text('{}')
        facts = lifecycle.project_facts(voids.snapshot(self.rows[:1], self.project))[0]['facts']
        self.assertEqual(facts['integrated']['value'], 'unknown')
        self.assertEqual(facts['integrated']['reason'], voids.UNKNOWN_JOURNAL)


if __name__ == '__main__':
    unittest.main()


class UnreadableHoldChainAuditTests(unittest.TestCase):
    """kittrial-5bb.297 review item 'audit-reports-unreadable-chains': the audit's own
    counted category for tasks whose hold chain cannot be read, with the reason,
    whether or not the task has lifecycle events."""

    def test_a_task_with_an_unreadable_chain_and_no_events_is_its_own_category(self):
        import review_workflow as reviews
        broken = dict(id='p-quiet', issue_type='task', title='quiet task', status='open',
                      assignee='worker', comments=[], labels=[])
        broken['comments'] = [dict(id='1', text=reviews.PREFIX + '{"broken": not json',
                                   author='worker', created_at='2026-09-16T00:00:00Z')]
        page = voids.audit(rows() + [broken], operators=['op'])
        self.assertEqual(page['unreadable_hold_chain_count'], 1)
        entry = page['unreadable_hold_chains'][0]
        self.assertEqual(entry['task'], 'p-quiet')
        self.assertIn('cannot be read', entry['reason'])
        self.assertIn('p-quiet', entry['reason'])

    def test_an_ordinary_lookalike_comment_is_not_a_chain_record(self):
        # kittrial-5bb.297 review item 'family-prefix-dos': an ordinary comment that
        # merely looks like the reserved prefix is NOT a chain record, so a
        # contributor cannot stop another task's release by writing one - only the
        # reserved any-version pattern with its newline counts.
        import lifecycle
        lookalikes = ['Kind: contribution-reviewer note about the review\nnothing reserved',
                      'Kind: contribution-review-v1 (quoting the docs) is not a record',
                      'Kind: contribution-review is a family of records']
        for text in lookalikes:
            with self.subTest(text=text[:40]):
                row = dict(id='p-look', issue_type='task', title='lookalike', status='open',
                           assignee='worker', comments=[dict(id='1', text=text, author='worker',
                                                             created_at='2026-09-16T00:00:00Z')],
                           labels=[])
                self.assertIsNone(lifecycle.unreadable_hold_chain(row, ['op']))
        page = voids.audit(rows() + [dict(id='p-look', issue_type='task', title='lookalike',
                                          status='open', assignee='worker', comments=[
                                              dict(id='1', text=lookalikes[0], author='worker',
                                                   created_at='2026-09-16T00:00:00Z')],
                                          labels=[])], operators=['op'])
        self.assertEqual(page['unreadable_hold_chain_count'], 0)

    def test_a_voided_bad_record_no_longer_flags(self):
        # kittrial-5bb.297 review item 'family-prefix-dos': the operator voids are
        # applied BEFORE deciding, so the repair the refusal names works - a voided
        # bad record (unparseable bytes, or a version this kit does not know) no
        # longer flags the task.
        import lifecycle
        import recovery
        from requirements import canonical_bytes
        bad = dict(id='1', text='Kind: contribution-review-v1\n{"broken": not json',
                   author='worker', created_at='2026-09-16T00:00:00Z')
        future = dict(id='1', text='Kind: contribution-review-v9\n{"anything"',
                      author='worker', created_at='2026-09-16T00:00:00Z')
        for comment, voidable in ((bad, True), (future, False)):
            with self.subTest(record=comment['text'][:36]):
                row = dict(id='p-voided', issue_type='task', title='voided', status='open',
                           assignee='worker', comments=[comment], labels=[])
                self.assertIsNotNone(lifecycle.unreadable_hold_chain(row, ['op']))
                void = dict(schema_version=1, operation='void-record', operation_id='void-1',
                            task='p-voided', target='1', target_kind='contribution-review',
                            target_sha256=recovery.digest(comment['text']),
                            original=comment['text'], reason='Repairing',
                            disposition='void', operator='op')
                row['comments'].append(dict(id='2',
                                            text=recovery.PREFIX + canonical_bytes(void).decode(),
                                            author='op', created_at='2026-09-16T00:01:00Z'))
                if voidable:
                    self.assertIsNone(lifecycle.unreadable_hold_chain(row, ['op']),
                                      'a voided bad record must not flag the task')
                else:
                    # A record of an unknown VERSION cannot be voided today: recovery's
                    # check_void only accepts targets with the exact v1 prefix, so the
                    # void is refused and the task stays flagged - fail closed is
                    # right; letting recovery void the any-version family is proposed
                    # as a follow-up, not built here.
                    self.assertIsNotNone(lifecycle.unreadable_hold_chain(row, ['op']))

    def test_a_readable_chain_and_a_chain_future_version_and_no_chain(self):
        # A task with no chain comments at all is not flagged, and neither is a
        # readable one. A comment of a FUTURE chain version this kit cannot parse is
        # flagged (review item 4b): it reads as an unreadable chain, never as 'no
        # chain'.
        import review_workflow as reviews
        none_at_all = dict(id='p-none', issue_type='task', title='none', status='open',
                           assignee='worker', comments=[], labels=[])
        future = dict(id='p-future', issue_type='task', title='future', status='open',
                      assignee='worker', comments=[], labels=[])
        family = reviews.PREFIX[:reviews.PREFIX.index('-v')]
        future['comments'] = [dict(id='1', text=family + '-v9\n{"anything',
                                   author='worker', created_at='2026-09-16T00:00:00Z')]
        page = voids.audit(rows() + [none_at_all, future], operators=['op'])
        self.assertEqual(page['unreadable_hold_chain_count'], 1)
        self.assertEqual(page['unreadable_hold_chains'][0]['task'], 'p-future')
