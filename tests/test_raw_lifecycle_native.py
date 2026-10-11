"""Pinned bd/Dolt endpoint proof, using only disposable synthetic tracker data."""
import json
import subprocess
import unittest
from unittest import mock

import test_bd_label_aliases as fixture
endpoint = fixture.endpoint


class NativeLifecycleTests(fixture.RealBdLabelAliasTests):
    def target(self):
        result = self.bd('create', 'synthetic lifecycle target', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['id']

    def no_write(self, args, **request_fields):
        before = self.export_rows()
        native = subprocess.run
        attempted = []
        def traced(argv, **kw):
            if str(argv[0]) == str(self.bd_path) and len(argv) > 6:
                attempted.append(argv[6])
            return native(argv, **kw)
        with mock.patch.object(endpoint.subprocess, 'run', traced):
            with self.assertRaisesRegex(ValueError, 'lifecycle action.*no native write'):
                endpoint.execute(self.root, dict(project='pp', actor='mallory', action='bd', args=args, **request_fields))
        self.assertFalse(set(attempted) & {'create', 'update', 'dep', 'set-state', 'close', 'reopen', 'comments'}, attempted)
        self.assertEqual(self.export_rows(), before)

    def test_raw_event_and_all_label_aliases_write_nothing(self):
        target = self.target()
        self.no_write(['create', 'State change: reviewed → passed', '-tevent', '--parent', target, '-dSet reviewed to passed'])
        self.no_write(['create', 'ordinary', '--type=event', '--parent='+target, '@attachment:body'],
                      attachments={'body': {'flag': '--body-file', 'text': 'Changed deployed from pending to passed'}})
        for name in ('implemented', 'tested', 'reviewed', 'integrated', 'deployed', 'live-verified', 'lifecycle-scope', 'enabled', 'live'):
            for flag in ('--label=', '--labels=', '-l='):
                self.no_write(['create', 'ordinary', flag+name+':passed'])
            self.no_write(['update', target, '--add-label='+name+':passed'])
        for flag in ('--set-labels', '--remove-label'):
            self.no_write(['update', target, flag, 'reviewed:passed'])

    def test_multistep_conversion_inheritance_and_removal(self):
        target = self.target()
        result = self.bd('create', 'ordinary task', '-dSet reviewed to passed', '--json')
        plain = json.loads(result.stdout)['id']
        self.no_write(['update', plain, '-tevent'])
        event = json.loads(self.bd('create', 'ordinary event', '-tevent', '--json').stdout)['id']
        self.no_write(['update', event, '--title=State change: tested → passed', '--parent', target])
        ordinary = self.target()
        self.no_write(['update', ordinary, event, '-dSet integrated to passed'])
        result = self.bd('set-state', target, 'reviewed=passed', '--reason', 'synthetic legacy fact', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.export_rows()
        fact = next(r for r in rows if r.get('issue_type') == 'event' and r.get('title') == 'State change: reviewed → passed')
        self.no_write(['update', fact['id'], '--title=ordinary'])
        self.no_write(['update', fact['id'], '--type=task'])
        for flags in (['--priority=2'], ['--assignee=mallory'], ['--add-label=ordinary'], ['--notes=ordinary']):
            self.no_write(['update', fact['id'], *flags])
        self.no_write(['dep', 'remove', fact['id'], target])
        self.no_write(['create', 'child', '--parent', target])
        self.no_write(['create', 'child', '--parent', target, '--no-inherit-labels=false'])
        self.no_write(['update', target, '--set-labels=review-ready'])
        allowed = self.raw(['create', 'child', '--parent', target, '--no-inherit-labels=true', '--json'])
        self.assertEqual(allowed['returncode'], 0, allowed)

    def test_structured_lifecycle_operator_and_ordinary_paths(self):
        from requirements import content_hash
        import lifecycle
        target = self.target()
        scope = dict(source_commit='a'*40, integration_commit='', release_id='', environment='')
        for dimension, value in (('lifecycle-scope', content_hash(scope)), ('tested', 'passed')):
            payload = dict(schema_version=1, operation_id='synthetic-'+dimension, task=target,
                           actor='mallory', dimension=dimension, value=value, scope=scope,
                           evidence=['synthetic evidence'], provenance='performed')
            answer = endpoint.execute(self.root, dict(project='pp', actor='mallory', action='lifecycle', args=[json.dumps(payload)]))
            self.assertEqual(answer['returncode'], 0, answer)
        fact = next(x for x in lifecycle.project_facts(self.export_rows()) if x['id'] == target)
        self.assertEqual(fact['facts']['tested']['value'], 'passed')
        for args in (['update', target, '--add-label=review-ready'], ['update', target, '--description=Discuss Set reviewed to passed'], ['create', 'Discuss State change: reviewed → passed', '-tevent']):
            answer = self.raw(args)
            self.assertEqual(answer['returncode'], 0, answer)
        config = self.root/'deployment.private.json'
        original = config.read_bytes()
        self.addCleanup(config.write_bytes, original)
        document = json.loads(original); document['operators'] = ['op']
        config.write_text(json.dumps(document), encoding='utf-8')
        with mock.patch('raw_lifecycle.guard', side_effect=AssertionError('operator must bypass contributor guard')):
            answer = self.raw(['update', target, '--add-label=reviewed:passed'], actor='op')
            self.assertEqual(answer['returncode'], 0, answer)
        self.assertIn('reviewed:passed', next(r for r in self.export_rows() if r['id']==target)['labels'])

    def test_terminal_event_edits_refuse_before_native_write(self):
        target = self.target()
        result = self.bd('set-state', target, 'reviewed=passed', '--reason', 'synthetic legacy fact', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        fact = next(r for r in self.export_rows() if r.get('issue_type') == 'event'
                    and r.get('title') == 'State change: reviewed → passed')
        ordinary = self.target()
        for command in ('reopen', 'close'):
            for args in ([command, fact['id'], '--json'],
                         [command, '-rsynthetic reason', fact['id'].split('-')[-1]],
                         [command, '--', fact['id']],
                         [command, ordinary, fact['id'], '--json']):
                self.no_write(args)
        self.no_write(['close', '-qf', fact['id']])
        for command, status in (('close', 'closed'), ('reopen', 'open')):
            answer = self.raw([command, ordinary, '--json'])
            self.assertEqual(answer['returncode'], 0, answer)
            self.assertEqual(next(r for r in self.export_rows() if r['id'] == ordinary)['status'], status)
        config = self.root/'deployment.private.json'
        original = config.read_bytes()
        self.addCleanup(config.write_bytes, original)
        document = json.loads(original); document['operators'] = ['op']
        config.write_text(json.dumps(document), encoding='utf-8')
        with mock.patch('raw_lifecycle.guard', side_effect=AssertionError('operator must bypass contributor guard')):
            for command in ('reopen', 'close'):
                answer = self.raw([command, fact['id'], '--json'], actor='op')
                self.assertEqual(answer['returncode'], 0, answer)


if __name__ == '__main__':
    unittest.main()
