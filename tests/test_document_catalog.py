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
        # A kit document always fits one answer on the wire, with room for its envelope.
        self.assertLess(onboarding.KIT_DOCUMENT_LIMIT * 2, 2_000_001)

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
        with self.assertRaisesRegex(ValueError, 'exceeds size limit'):
            onboarding.execute(self.kit, self.project, 'example', 'worker', 'onboard', [])
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
                # The answer, as the endpoint prints it, fits what a client reads.
                self.assertLess(len(json.dumps(answer, ensure_ascii=False).encode('utf-8')), 2_000_000)

    def test_the_project_text_and_the_catalogue_are_answered(self):
        self.assertEqual(self.ask('project')['stdout'], 'Start with the README.\n')
        self.assertIn('  docs cli-contract', self.ask()['stdout'])


if __name__ == '__main__':
    unittest.main()
