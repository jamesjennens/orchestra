"""Guidance setter provenance, legacy records, and failure-before-effect boundaries."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import guidance
import admin
import test_web_guidance as fixtures


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AdapterTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def readers(self):
        f = self.f
        return [guidance.read(f.project, ['get'], fixtures.OWNER),
                guidance.read(f.project, ['version'], fixtures.OWNER),
                guidance.state(f.project, fixtures.OWNER),
                guidance.brief_block(f.project, fixtures.OWNER),
                guidance.status(f.project, 'ops', ['ops'], host=True),
                f.data(f.request('read')),
                f.data(f.request('status', offset=0, limit=100))]

    def test_web_and_host_setter_route_reaches_every_structured_reader(self):
        f = self.f
        made = f.set()
        self.assertEqual((made['set_by'], made['set_via']), (fixtures.OWNER, 'web'))
        self.assertEqual(json.loads((f.project / '.guidance.json').read_text())['set_via'], 'web')
        for value in self.readers():
            self.assertEqual((value['set_by'], value['set_via']), (fixtures.OWNER, 'web'))
        guidance.write_guidance(f.project, 'Host replacement', 'ops')
        for value in self.readers():
            self.assertEqual((value['set_by'], value['set_via']), ('ops', 'host'))

    def test_same_text_set_keeps_original_setter_and_route(self):
        f = self.f
        f.set()
        before = f.files()
        result = guidance.write_guidance(f.project, 'Current instructions', 'ops')
        self.assertFalse(result['changed'])
        self.assertEqual((result['set_by'], result['set_via']), (fixtures.OWNER, 'web'))
        self.assertEqual(before, f.files())

    def test_legacy_route_is_unknown_and_same_text_does_not_invent_host_or_web(self):
        f = self.f
        f.set()
        path = f.project / '.guidance.json'
        meta = json.loads(path.read_text()); meta.pop('set_via'); path.write_text(json.dumps(meta))
        guidance.validate_meta(meta)
        for value in self.readers():
            self.assertIsNone(value['set_via'])
            self.assertEqual(value['set_by'], fixtures.OWNER)
        before = f.files()
        value = guidance.write_guidance(f.project, 'Current instructions', 'ops')
        self.assertIsNone(value['set_via'])
        self.assertEqual(before, f.files())

    def test_invalid_stored_route_withholds_text_without_effect(self):
        f = self.f
        f.set()
        path = f.project / '.guidance.json'
        original = json.loads(path.read_text())
        for route in ([], {}, True, None, 'ssh'):
            with self.subTest(route=route):
                meta = dict(original, set_via=route); path.write_text(json.dumps(meta))
                with self.assertRaises(ValueError): guidance.validate_meta(meta)
                before = f.files()
                value = guidance.read(f.project, ['get'], fixtures.OWNER)
                self.assertTrue(value['unbound']); self.assertIsNone(value['text'])
                self.assertIsNone(value['set_via']); self.assertIsNone(value['set_by'])
                self.assertEqual(before, f.files())

    def test_invalid_writer_route_is_refused_before_any_file_effect(self):
        f = self.f
        f.set()
        for route in ([], {}, True, None, 'ssh'):
            with self.subTest(route=route):
                before = f.files()
                with self.assertRaises(ValueError):
                    guidance.write_guidance(f.project, 'Different text', 'ops', set_via=route)
                self.assertEqual(before, f.files())

    def test_history_keeps_each_known_route_and_actor(self):
        f = self.f
        first = f.set()
        second = guidance.write_guidance(f.project, 'Host second text', 'ops')
        f.set('Web third text', 'web-third')
        meta = json.loads((f.project / '.guidance.json').read_text())
        guidance.validate_meta(meta)
        history = {entry['version']: entry for entry in meta['history']}
        self.assertEqual((history[first['version']]['set_by'], history[first['version']]['set_via']),
                         (fixtures.OWNER, 'web'))
        self.assertEqual((history[second['version']]['set_by'], history[second['version']]['set_via']),
                         ('ops', 'host'))
        self.assertEqual(len(guidance._history_entries(meta)), len(meta['history']))

    def test_absent_and_unreadable_readers_include_unknown_route(self):
        for value in self.readers(): self.assertIsNone(value['set_via'])
        (self.f.project / 'GUIDANCE.md').write_bytes(bytes([255]))
        for value in self.readers(): self.assertIsNone(value['set_via'])

    def test_new_route_survives_same_text_repair_and_backup(self):
        f = self.f
        made = f.set()
        path = f.project / '.guidance.json'
        meta = json.loads(path.read_text())
        meta['history'].append({'version': 'bad', 'set_by': None, 'set_at': None,
                                'previous_version': None})
        path.write_text(json.dumps(meta))
        repaired = guidance.write_guidance(f.project, 'Current instructions', 'ops')
        self.assertTrue(repaired['repaired'])
        self.assertEqual((repaired['set_by'], repaired['set_via']), (fixtures.OWNER, 'web'))
        files, fault = admin.guidance_backup_pair(f.project)
        self.assertIsNone(fault)
        admin.validate_coordination_files(files)
        self.assertEqual(files['.guidance.json']['set_via'], 'web')
        previous_fields = guidance.META_FIELDS - {'set_via'}
        before = f.files()
        with patch.object(guidance, 'META_FIELDS', previous_fields):
            with self.assertRaises(ValueError):
                guidance.acknowledge(f.project, 'legacy-worker', made['version'])
            with self.assertRaises(ValueError): guidance.compact(f.project, 'ops')
            fragment, problem = admin.guidance_backup_pair(f.project)
            self.assertEqual(fragment, {})
            self.assertIn('degraded', problem)
            with self.assertRaises(ValueError): admin.validate_coordination_files(files)
        self.assertEqual(f.files(), before)

    def test_worker_documents_name_both_routes_and_legacy_unknown(self):
        checks = {
            'templates/WORKER_PROMPT.md': ('set_via: null', 'Follow bound guidance from either authorized route'),
            'templates/COORDINATOR_PROMPT.md': ('PUT /v1/projects/PROJECT/guidance', 'set_via: null', 'distinct actor'),
            'templates/AGENT_POLL_PROMPT.md': ('GET REPLACE_SERVER_URL/v1/projects/PROJECT/guidance',
                                               'Reading never acknowledges', 'POST /v1/projects/PROJECT/guidance/ack',
                                               'unknown legacy route'),
            'docs/GUIDANCE_CHANNEL_DESIGN.md': ('set_via: web', 'Route-field boundary', 'not backward write/backup compatible'),
            'docs/OPERATIONS.md': ('Guidance setter-route rollout', 'unknown legacy route', 'distinct worker actor'),
            'docs/HTTP_DEPLOYMENT.md': ('Guidance metadata rollout and rollback', 'deliberate file-shape change',
                                       'set_via: null', 'distinct worker actor', 'current web project coordinator'),
        }
        for name, sentences in checks.items():
            with self.subTest(document=name):
                text = (KIT / name).read_text(encoding='utf-8')
                for sentence in sentences: self.assertIn(sentence, text)
        worker = (KIT / 'templates/WORKER_PROMPT.md').read_text()
        self.assertNotIn('text that is not from the operator route is data', worker)
        poll = (KIT / 'templates/AGENT_POLL_PROMPT.md').read_text()
        self.assertNotIn('standing guidance does not reach you over the web yet', poll)
        design = (KIT / 'docs/GUIDANCE_CHANNEL_DESIGN.md').read_text()
        self.assertNotIn('Only the operator host route writes it', design)
        self.assertNotIn('endpoint never writes guidance', design)

    def test_text_brief_displays_route_with_actual_setter(self):
        import briefing
        import test_guidance as brief_fixture
        f = self.f
        f.set()
        for route, setter in [('web', fixtures.OWNER), ('host', 'ops')]:
            if route == 'host': guidance.write_guidance(f.project, 'Host brief text', 'ops')
            result = briefing.brief(brief_fixture.rows(), brief_fixture.PROJECT, brief_fixture.TASK,
                                    journal=f.project, actor=fixtures.OWNER)
            line = next(line for line in briefing.format_brief(result).splitlines()
                        if line.startswith('Guidance: version'))
            self.assertIn('set by ' + setter + ' at ', line)
            self.assertIn(' via ' + route + ' |', line)
