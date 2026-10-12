"""Every catalogued document is served, whole (kittrial-5bb.151).

`docs reviews` and `docs cli-contract` were refused on every installation: the kit's own
documents were measured against a limit meant for text people write, two of them outgrew
it, and no test compared the catalogue with the limit. These tests are that comparison.
They read the files the kit ships, so a document that goes missing, is emptied or
outgrows its bound fails here, before a release.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import onboarding

try:
    import endpoint
except ImportError:  # endpoint needs fcntl (POSIX)
    endpoint = None


class CatalogueTests(unittest.TestCase):
    def test_every_catalogued_document_and_template_is_there_not_empty_and_within_its_bound(self):
        self.assertGreaterEqual(len(onboarding.DOCUMENTS), 16)
        for name, relative in onboarding.DOCUMENTS.items():
            with self.subTest(document=name):
                path = KIT / relative
                self.assertTrue(path.is_file(), '%s is catalogued as %s and is missing' % (name, relative))
                size = path.stat().st_size
                self.assertGreater(len(path.read_text(encoding='utf-8-sig').strip()), 0, '%s is empty' % relative)
                self.assertLessEqual(size, onboarding.KIT_DOCUMENT_LIMIT,
                                     '%s is %d bytes, over the bound for a kit document (%d): `docs %s` would be '
                                     'refused on every installation' % (relative, size, onboarding.KIT_DOCUMENT_LIMIT, name))

    def test_the_start_document_fits_the_bound_onboard_reads_it_with(self):
        size = (KIT / onboarding.DOCUMENTS['start']).stat().st_size
        self.assertLessEqual(size, onboarding.START_LIMIT,
                             'docs/WORKER_START.md is %d bytes, over %d: `onboard` would be refused for every worker'
                             % (size, onboarding.START_LIMIT))

    def test_the_bounds_are_what_the_docs_say(self):
        self.assertEqual((onboarding.PROJECT_LIMIT, onboarding.START_LIMIT, onboarding.KIT_DOCUMENT_LIMIT),
                         (8000, 8000, 1_000_000))

    def test_the_two_documents_that_were_refused_are_over_the_old_limit_and_are_served(self):
        for name in ('reviews', 'cli-contract'):
            with self.subTest(document=name):
                text = onboarding.execute(KIT, KIT, 'example', 'worker', 'docs', [name])
                self.assertGreater(len(text.encode('utf-8')), 64000)
                self.assertEqual(text, (KIT / onboarding.DOCUMENTS[name]).read_text(encoding='utf-8-sig'))

    def test_docs_serves_every_catalogued_name_whole(self):
        for name, relative in onboarding.DOCUMENTS.items():
            with self.subTest(document=name):
                text = onboarding.execute(KIT, KIT, 'example', 'worker', 'docs', [name])
                self.assertEqual(text, (KIT / relative).read_text(encoding='utf-8-sig'))

    def test_the_catalogue_lists_every_name(self):
        listed = onboarding.execute(KIT, KIT, 'example', 'worker', 'docs', [])
        for name in ['project', *onboarding.DOCUMENTS]:
            self.assertIn('  docs %s\n' % name, listed + '\n')


class BoundTests(unittest.TestCase):
    """What each bound refuses, and with which words."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.kit = Path(temp.name) / 'kit'
        self.project = Path(temp.name) / 'project'
        for relative in onboarding.DOCUMENTS.values():
            (self.kit / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.kit / relative).write_text('text\n', encoding='utf-8')
        self.project.mkdir()
        (self.project / 'ONBOARDING.md').write_text('Start with the README.\n', encoding='utf-8')

    def docs(self, name):
        return onboarding.execute(self.kit, self.project, 'example', 'worker', 'docs', [name])

    def test_a_kit_document_up_to_its_bound_is_served_and_one_byte_more_is_refused_whole(self):
        path = self.kit / onboarding.DOCUMENTS['reviews']
        path.write_bytes(b'x' * onboarding.KIT_DOCUMENT_LIMIT)
        self.assertEqual(len(self.docs('reviews')), onboarding.KIT_DOCUMENT_LIMIT)
        path.write_bytes(b'x' * (onboarding.KIT_DOCUMENT_LIMIT + 1))
        with self.assertRaises(ValueError) as refused:
            self.docs('reviews')
        self.assertEqual(str(refused.exception),
                         'The kit document "reviews" is larger than a kit document can be (over 1000000 bytes), so the '
                         'file at that place in this installation is not the one the kit ships. Nothing was returned; '
                         'ask the operator to reinstall the kit. The same text is in the repository (docs/REVIEWS.md)')
        self.assertNotIn('shorten', str(refused.exception))            # nobody at an installation can shorten it

    def test_the_project_text_keeps_its_own_bound_and_its_own_sentence(self):
        (self.project / 'ONBOARDING.md').write_bytes(b'x' * onboarding.PROJECT_LIMIT)
        self.assertEqual(len(self.docs('project')), onboarding.PROJECT_LIMIT)
        (self.project / 'ONBOARDING.md').write_bytes(b'x' * (onboarding.PROJECT_LIMIT + 1))
        for call in (lambda: self.docs('project'),
                     lambda: onboarding.execute(self.kit, self.project, 'example', 'worker', 'onboard', [])):
            with self.assertRaisesRegex(ValueError, '^Onboarding document exceeds size limit; operator must shorten it'):
                call()

    def test_onboard_keeps_the_start_document_short(self):
        start = self.kit / onboarding.DOCUMENTS['start']
        start.write_bytes(b'x' * (onboarding.START_LIMIT + 1))
        with self.assertRaises(ValueError) as refused:
            onboarding.execute(self.kit, self.project, 'example', 'worker', 'onboard', [])
        # The start document is a file the kit ships (kittrial-5bb.153 item 1): the
        # installation sentence, never "operator must shorten it".
        self.assertNotIn('shorten', str(refused.exception))
        self.assertIn('ask the operator to reinstall the kit', str(refused.exception))
        # The same file as a catalogued document is a kit document, and is served.
        self.assertEqual(len(self.docs('start')), onboarding.START_LIMIT + 1)

    def test_a_missing_or_empty_kit_document_is_refused_as_before(self):
        path = self.kit / onboarding.DOCUMENTS['workflow']
        path.write_text('  \n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Onboarding document is empty'):
            self.docs('workflow')
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'Onboarding document missing'):
            self.docs('workflow')

    def test_a_symlink_in_place_of_a_kit_document_is_refused(self):
        # A mutant dropping the symlink refusal survives without this (kittrial-5bb.153 item 3).
        path = self.kit / onboarding.DOCUMENTS['workflow']
        path.unlink()
        path.symlink_to(self.kit / onboarding.DOCUMENTS['reviews'])
        with self.assertRaisesRegex(ValueError, 'must not use symlinks'):
            self.docs('workflow')

    def test_a_directory_in_place_of_a_kit_document_is_refused(self):
        path = self.kit / onboarding.DOCUMENTS['workflow']
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(ValueError, 'Onboarding document missing'):
            self.docs('workflow')

    def test_a_fifo_in_place_of_a_kit_document_is_refused(self):
        # A mutant that drops the regular-file check blocks for ever on a FIFO; the read
        # must finish quickly and leave nothing behind (review 01a12be9).
        import os
        import threading
        if not hasattr(os, 'mkfifo'):
            self.skipTest('this platform has no FIFO')
        path = self.kit / onboarding.DOCUMENTS['workflow']
        path.unlink()
        os.mkfifo(path)
        self.addCleanup(path.unlink, missing_ok=True)
        outcome = {}
        def run():
            try:
                outcome['text'] = self.docs('workflow')
            except Exception as error:
                outcome['error'] = error
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), 'the read blocked on a FIFO instead of refusing')
        self.assertIsInstance(outcome.get('error'), ValueError)
        self.assertRegex(str(outcome['error']), 'Onboarding document missing')

    def test_docs_status_reports_every_catalogued_name(self):
        # One read for release live verification (kittrial-5bb.153 item 4).
        import json
        (self.kit / onboarding.DOCUMENTS['workflow']).write_bytes(b'hello\n')
        shown = json.loads(onboarding.execute(self.kit, self.project, 'example', 'worker', 'docs', ['--status']))
        names = [row['name'] for row in shown['documents']]
        self.assertEqual(names, ['project', *onboarding.DOCUMENTS])
        by_name = {row['name']: row for row in shown['documents']}
        self.assertEqual(by_name['workflow']['bytes'], 6)          # the file as read, not utf-8-sig
        self.assertTrue(by_name['start']['served'])
        # A refusal is a row, not a raise.
        (self.kit / onboarding.DOCUMENTS['workflow']).unlink()
        shown = json.loads(onboarding.execute(self.kit, self.project, 'example', 'worker', 'docs', ['--status']))
        by_name = {row['name']: row for row in shown['documents']}
        self.assertFalse(by_name['workflow']['served'])
        self.assertIn('missing', by_name['workflow']['refused'])

    def test_docs_status_refuses_a_start_document_over_the_onboard_bound(self):
        import json
        (self.kit / onboarding.DOCUMENTS['start']).write_bytes(b'x' * (onboarding.START_LIMIT + 1))
        shown = json.loads(onboarding.execute(self.kit, self.project, 'example', 'worker', 'docs', ['--status']))
        by_name = {row['name']: row for row in shown['documents']}
        self.assertFalse(by_name['start']['served'])
        # The bound is named for what it is (kittrial-5bb.262 C5): this row refused the file for
        # the START bound the status read applies, which is tighter than the kit-document bound.
        self.assertIn('larger than the start bound', by_name['start']['refused'])
        self.assertIn('tighter than the kit-document bound', by_name['start']['refused'])

    def test_docs_start_over_the_kit_bound_says_the_kit_document_bound(self):
        """kittrial-5bb.262 C5: "The start document is larger than the start document may be"
        never said WHICH of the two start bounds refused it; `docs start` applies the kit one."""
        start = self.kit / onboarding.DOCUMENTS['start']
        start.write_bytes(b'x' * (onboarding.KIT_DOCUMENT_LIMIT + 1))
        with self.assertRaises(ValueError) as refused:
            self.docs('start')
        said = str(refused.exception)
        self.assertIn('larger than the kit-document bound (over %d bytes)' % onboarding.KIT_DOCUMENT_LIMIT, said)
        self.assertNotIn('start document may be', said)
        self.assertNotIn('shorten', said)
        self.assertIn('ask the operator to reinstall the kit', said)

    def test_the_listing_says_size_only_for_served_names(self):
        """kittrial-5bb.262 C6: the listing said "with its size" for refused names too."""
        listed = onboarding.execute(self.kit, self.project, 'example', 'worker', 'docs', [])
        self.assertIn('as served, with its size, or as refused', listed)
        self.assertNotIn('every name as served or refused, with its size', listed)

    def test_an_unreadable_document_is_refused_without_a_host_path_for_docs_and_onboard(self):
        """kittrial-5bb.262 C4 (and C5): only the `--status` row was held to the path-free
        sentence, so a mutant that prints the absolute path from `docs NAME` or `onboard`
        survived. The sentence also NAMES the document now."""
        import os
        if os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0):
            self.skipTest('a mode cannot make a file unreadable here')
        path = self.kit / onboarding.DOCUMENTS['workflow']
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        with self.assertRaises(ValueError) as refused:
            self.docs('workflow')
        said = str(refused.exception)
        self.assertNotIn(str(path), said)
        self.assertNotIn(str(self.kit), said)
        self.assertIn('ask the operator', said)
        self.assertIn('The document "%s"' % onboarding.DOCUMENTS['workflow'], said)
        # onboard reads the START document from the same directory: one unreadable document
        # refuses the whole answer, with the same path-free sentence.
        start = self.kit / onboarding.DOCUMENTS['start']
        start.chmod(0)
        self.addCleanup(start.chmod, 0o644)
        with self.assertRaises(ValueError) as refused:
            onboarding.execute(self.kit, self.project, 'example', 'worker', 'onboard', [])
        said = str(refused.exception)
        self.assertNotIn(str(start), said)
        self.assertNotIn(str(self.kit), said)
        self.assertIn('The document "%s"' % onboarding.DOCUMENTS['start'], said)
        self.assertIn('ask the operator', said)

    def test_an_unsearchable_documents_directory_is_refused_without_a_host_path(self):
        """kittrial-5bb.262 C2: with kit/docs mode 000, `docs NAME` and `onboard` answered
        `PermissionError: [Errno 13] Permission denied: /home/.../docs/WORKFLOW.md` - raised by
        the symlink walk BEFORE the read - and `docs --status` named the path too."""
        import json
        import os
        if os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0):
            self.skipTest('a mode cannot make a directory unsearchable here')
        directory = self.kit / 'docs'
        directory.chmod(0)
        self.addCleanup(directory.chmod, 0o755)
        with self.assertRaises(ValueError) as refused:
            self.docs('workflow')
        said = str(refused.exception)
        self.assertNotIn(str(self.kit), said)
        self.assertNotIn('Permission denied: /', said)
        self.assertIn('The document "%s"' % onboarding.DOCUMENTS['workflow'], said)
        self.assertIn('ask the operator', said)
        with self.assertRaises(ValueError) as refused:
            onboarding.execute(self.kit, self.project, 'example', 'worker', 'onboard', [])
        said = str(refused.exception)
        self.assertNotIn(str(self.kit), said)
        self.assertIn('ask the operator', said)
        # A refusal is a row, not a raise, and the row names the document without a host path.
        shown = json.loads(onboarding.execute(self.kit, self.project, 'example', 'worker', 'docs', ['--status']))
        by_name = {row['name']: row for row in shown['documents']}
        self.assertFalse(by_name['workflow']['served'])
        self.assertNotIn(str(self.kit), by_name['workflow']['refused'])
        self.assertIn('The document "%s"' % onboarding.DOCUMENTS['workflow'], by_name['workflow']['refused'])
        self.assertTrue(by_name['project']['served'])

    def test_docs_status_refuses_a_symlink_directory_or_fifo_without_hanging(self):
        import json
        import os
        import threading
        path = self.kit / onboarding.DOCUMENTS['workflow']
        def clear():
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                path.rmdir()
        self.addCleanup(clear)
        for kind in ('symlink', 'directory', 'fifo'):
            with self.subTest(kind=kind):
                clear()
                if kind == 'symlink':
                    path.symlink_to(self.kit / onboarding.DOCUMENTS['reviews'])
                    want = 'must not use symlinks'
                elif kind == 'directory':
                    path.mkdir()
                    want = 'Onboarding document missing'
                else:
                    if not hasattr(os, 'mkfifo'):
                        self.skipTest('this platform has no FIFO')
                    os.mkfifo(path)
                    want = 'Onboarding document missing'
                outcome = {}
                def run():
                    try:
                        outcome['shown'] = json.loads(onboarding.execute(
                            self.kit, self.project, 'example', 'worker', 'docs', ['--status']))
                    except Exception as error:
                        outcome['error'] = error
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                worker.join(5)
                self.assertFalse(worker.is_alive(), '%s blocked instead of refusing' % kind)
                self.assertNotIn('error', outcome, outcome.get('error'))
                by_name = {row['name']: row for row in outcome['shown']['documents']}
                self.assertFalse(by_name['workflow']['served'], kind)
                self.assertIn(want, by_name['workflow']['refused'])

    def test_docs_status_never_reports_a_host_path(self):
        import json
        import os
        if os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0):
            self.skipTest('a mode cannot make a file unreadable here')
        path = self.kit / onboarding.DOCUMENTS['workflow']
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        shown = json.loads(onboarding.execute(self.kit, self.project, 'example', 'worker', 'docs', ['--status']))
        by_name = {row['name']: row for row in shown['documents']}
        self.assertFalse(by_name['workflow']['served'])
        self.assertNotIn(str(path), by_name['workflow']['refused'])
        self.assertNotIn(str(self.kit), by_name['workflow']['refused'])
        self.assertIn('ask the operator', by_name['workflow']['refused'])

    def test_read_document_has_no_default_bound(self):
        with self.assertRaises(TypeError):
            onboarding.read_document(self.kit, onboarding.DOCUMENTS['workflow'])


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointTests(unittest.TestCase):
    """`docs NAME` through the endpoint action, for every catalogued name, from the kit as shipped."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'runtime'
        project = self.root / 'projects' / 'example'
        (project / '.beads').mkdir(parents=True)
        (project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (project / 'ONBOARDING.md').write_text('Start with the README.\n', encoding='utf-8')

    def ask(self, *args):
        return endpoint.execute(self.root, {'project': 'example', 'actor': 'worker', 'action': 'docs', 'args': list(args)})

    def test_every_catalogued_name_is_answered_whole(self):
        for name, relative in onboarding.DOCUMENTS.items():
            with self.subTest(document=name):
                answer = self.ask(name)
                self.assertEqual((answer['returncode'], answer['stderr']), (0, ''), answer['stderr'][-300:])
                self.assertEqual(answer['stdout'], (KIT / relative).read_text(encoding='utf-8-sig'))
                # There is no cap on the size of an answer; the 2 MB limit is on requests only
                # (kittrial-5bb.153 item 2). The value is the request bound, kept as a loose
                # sanity figure for these documents, not an answer cap.
                encoded = json.dumps(answer, ensure_ascii=False).encode('utf-8')
                self.assertLess(len(encoded), 2_000_000)

    def test_the_project_text_and_the_catalogue_are_answered(self):
        self.assertEqual(self.ask('project')['stdout'], 'Start with the README.\n')
        self.assertIn('  docs cli-contract', self.ask()['stdout'])


if __name__ == '__main__':
    unittest.main()
