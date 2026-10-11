"""Pinned bd/Dolt endpoint proof, using only disposable synthetic tracker data."""
import json
import errno
import itertools
import subprocess
import unittest
import uuid
try:
    import fcntl
except ImportError:  # Native endpoint tests are skipped on Windows.
    fcntl = None
from unittest import mock

import test_bd_label_aliases as fixture
endpoint = fixture.endpoint


class NativeLifecycleTests(fixture.RealBdLabelAliasTests):
    def target(self):
        result = self.bd('create', 'synthetic lifecycle target', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['id']

    def no_write(self, args, actor='mallory', regex='lifecycle action.*no native write', **request_fields):
        before = self.export_rows()
        native = subprocess.run
        attempted = []
        def traced(argv, **kw):
            if str(argv[0]) == str(self.bd_path) and len(argv) > 6:
                attempted.append(argv[6])
            return native(argv, **kw)
        with mock.patch.object(endpoint.subprocess, 'run', traced):
            with self.assertRaisesRegex(ValueError, regex):
                endpoint.execute(self.root, dict(project='pp', actor=actor, action='bd', args=args, **request_fields))
        self.assertFalse(set(attempted) & {'create', 'update', 'dep', 'set-state', 'close', 'reopen', 'comments'}, attempted)
        self.assertEqual(self.export_rows(), before)

    def record_facts(self, target, actor='worker-one'):
        from requirements import content_hash
        scope = dict(source_commit='b'*40, integration_commit='', release_id='', environment='synthetic')
        for dimension, value, writer in [('lifecycle-scope', content_hash(scope), actor),
                                          ('implemented', 'passed', actor), ('tested', 'passed', actor),
                                          ('reviewed', 'failed', 'op')]:
            payload = dict(schema_version=1, operation_id='native-revision-'+uuid.uuid4().hex,
                           task=target, actor=writer, dimension=dimension, value=value,
                           scope=scope, evidence=['synthetic revision evidence'], provenance='performed')
            answer = endpoint.execute(self.root, dict(project='pp', actor=writer, action='lifecycle',
                                                       args=[json.dumps(payload)]))
            self.assertEqual(answer['returncode'], 0, answer)

    def test_facts_cannot_be_hidden_by_retyping_or_ephemeral_but_close_is_allowed(self):
        import lifecycle
        target = self.target()
        self.assertEqual(self.raw(['update', target, '--claim', '--json'], actor='worker-one')['returncode'], 0)
        self.record_facts(target)
        for actor in ('worker-one', 'worker-two'):
            for flags in (['--type=event'], ['--ephemeral'], ['--ephemeral=true']):
                self.no_write(['update', target, *flags, '--json'], actor=actor)
        self.assertEqual(self.raw(['close', target, '--json'], actor='worker-one')['returncode'], 0)
        facts = {x['id']: x for x in lifecycle.project_facts(self.export_rows())}
        self.assertEqual(facts[target]['facts']['reviewed']['value'], 'failed')
        self.assertIn(target, lifecycle.trusted_payloads(self.export_rows()))
        brief = endpoint.execute(self.root, dict(project='pp', actor='worker-one', action='brief',
                                                 args=[target, '--json']))
        self.assertEqual(brief['returncode'], 0, brief)

    def test_every_staged_conversion_order_without_scoped_parent(self):
        parent = self.target()
        self.assertFalse(next(r for r in self.export_rows() if r['id']==parent).get('labels'))
        fields = [('title', ['--title=State change: reviewed → passed']),
                  ('description', ['--description=Set reviewed to passed']),
                  ('type', ['--type=event']), ('parent', ['--parent', parent]),
                  ('labels', ['--add-label=reviewed:passed'])]
        # Every permutation is attempted one scalar write at a time. Each refusal
        # gets its own export equality and native-writer trace assertion.
        for initial_type in ('event', 'task'):
            for order in itertools.permutations(fields):
                event = json.loads(self.bd('create', 'ordinary staged row', '-t'+initial_type, '--json').stdout)['id']
                for name, flags in order:
                    with self.subTest(initial_type=initial_type, order=[x[0] for x in order], step=name):
                        forbidden = name=='labels' or (initial_type=='event' and name in ('title', 'description')) or (initial_type=='task' and name=='type')
                        if forbidden:
                            self.no_write(['update', event, *flags, '--json'])
                        else:
                            answer = self.raw(['update', event, *flags, '--json'])
                            self.assertEqual(answer['returncode'], 0, answer)
        ordinary = self.target()
        for flag in ('--title=State change: reviewed → passed', '--description=Set reviewed to passed'):
            self.assertEqual(self.raw(['update', ordinary, flag, '--json'])['returncode'], 0)
        self.no_write(['update', ordinary, '--type=event', '--json'])
        event = json.loads(self.bd('create', 'ordinary duplicate description event', '-tevent', '--json').stdout)['id']
        answer = self.raw(['update', event, '--description=Set reviewed to passed', '--description=ordinary', '--json'])
        self.assertEqual(answer['returncode'], 0, answer)
        self.assertEqual(json.loads(answer['stdout'])[0]['description'], 'ordinary')
        self.no_write(['update', event, '--description=ordinary', '--description=Set reviewed to passed', '--json'])

    def test_native_target_resolution_multi_id_parent_substring_absence_and_ambiguity(self):
        # Isolate this resolver proof from random IDs in other tests, so `10`
        # really resolves uniquely rather than merely hitting an ambiguity.
        class ResolverFixture(NativeLifecycleTests):
            pass
        ResolverFixture.setUpClass()
        try:
            ResolverFixture()._check_native_target_resolution()
        finally:
            ResolverFixture.tearDownClass()

    def _check_native_target_resolution(self):
        target, other = 'pp-plain2', 'pp-plain3'
        for named in (target, other):
            self.assertEqual(self.bd('create', 'ordinary', '--id='+named, '--json').returncode, 0)
        created = self.bd('create', 'State change: reviewed → failed', '--id=pp-fact2',
                          '-tevent', '-dSet reviewed to failed', '--json')
        self.assertEqual(created.returncode, 0, created.stderr)
        fact = json.loads(created.stdout)
        self.assertEqual(self.bd('dep', 'add', fact['id'], target,
                                 '--type=parent-child', '--json').returncode, 0)
        for args in (['update', other, fact['id'], '--status=open'], ['close', other, fact['id']],
                     ['update', fact['id'], '--parent', other],
                     ['update', other, '--parent', fact['id']]):
            self.no_write(args)
        # Explicit native IDs make the unique substring spellings reproducible.
        for suffix in ('7vv.2', '7vv.10'):
            result = self.bd('create', 'State change: tested → failed', '--id=pp-'+suffix,
                             '-tevent', '-dSet tested to failed', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
        for token, named in (('vv.2', 'pp-7vv.2'), ('10', 'pp-7vv.10')):
            resolved = self.bd('show', token, '--json')
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertEqual([r['id'] for r in json.loads(resolved.stdout)], [named])
        self.no_write(['update', 'vv.2', '--assignee=worker-two'])
        self.no_write(['close', '10'])
        self.no_write(['update', 'vv.', '--title=ordinary'])
        self.no_write(['update', 'pp-no-such-row', '--title=ordinary'])
        self.assertEqual(self.raw(['update', target, other, '--priority=2', '--json'])['returncode'], 0)
        self.assertEqual(self.raw(['update', other, '--parent', target, '--json'])['returncode'], 0)

    def test_dependency_spellings_and_event_scalar_flags(self):
        target = self.target()
        read = self.raw(['dep', 'list', target, '--json'])
        self.assertEqual(read['returncode'], 0, read)
        result = self.bd('set-state', target, 'deployed=failed', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        fact = next(r for r in self.export_rows() if r.get('issue_type')=='event' and
                    r.get('title')=='State change: deployed → failed' and
                    any(d.get('depends_on_id')==target for d in r.get('dependencies', [])))
        for flags in (['--deps', 'parent-child:'+fact['id']], ['--waits-for', fact['id']]):
            self.no_write(['create', 'ordinary new row', *flags, '--json'])
        for args in (['dep', target, '-b', fact['id']], ['dep', target, '--blocks='+fact['id']],
                     ['dep', 'relate', target, fact['id']], ['dep', 'unrelate', target, fact['id']],
                     ['dep', 'remove', fact['id'], target]):
            self.no_write(args)
        body = json.dumps(dict(issue_id=fact['id'], depends_on_id=target, type='blocks'))
        self.no_write(['dep', 'add', '@attachment:edges', '--json'],
                      attachments={'edges': {'flag': '--file', 'text': body}})
        for flags in (['--status=open'], ['--assignee=worker-two'], ['--labels=a,b'],
                      ['-la', '-lb']):
            self.no_write(['update', fact['id'], *flags],
                          regex='lifecycle action.*no native write|unrecognized flag.*no native write|configuration flags are operator-only')

    def test_status_defer_persistent_and_creation_references_preserve_fact_visibility(self):
        import lifecycle
        import raw_lifecycle
        target = self.target()
        self.record_facts(target)
        for flags in (['--status=open'], ['--status=in_progress'], ['--status=blocked'],
                      ['--status=deferred'], ['--status=closed'], ['--defer=+1d'], ['--persistent']):
            with self.subTest(flags=flags):
                answer = self.raw(['update', target, *flags, '--json'])
                self.assertEqual(answer['returncode'], 0, answer)
                facts = {x['id']: x for x in lifecycle.project_facts(self.export_rows())}
                self.assertEqual(facts[target]['facts']['reviewed']['value'], 'failed')
        for value in ('tombstone', 'deleted'):
            before = self.export_rows()
            answer = self.raw(['update', target, '--status='+value, '--json'])
            self.assertNotEqual(answer['returncode'], 0, answer)
            self.assertEqual(self.export_rows(), before)
        for verb in ('delete', 'remove', 'tombstone'):
            self.no_write([verb, target], regex='outside the contributor interface')
        self.no_write(['update', target, '--delete'], regex='unrecognized flag.*no native write')
        for flags in (['--deps', 'parent-child:'+target], ['--waits-for', target]):
            answer = self.raw(['create', 'ordinary dependency child', *flags, '--json'])
            self.assertEqual(answer['returncode'], 0, answer)
            child = json.loads(answer['stdout'])
            self.assertFalse(any(raw_lifecycle.reserved_label(v) for v in child.get('labels', [])))
        for flags in ([], ['--no-inherit-labels=false']):
            self.no_write(['create', 'scoped parent child', '--parent', target, *flags])
        answer = self.raw(['create', 'scoped parent child', '--parent', target,
                           '--no-inherit-labels', '-lordinary', '--json'])
        self.assertEqual(answer['returncode'], 0, answer)
        self.assertEqual(json.loads(answer['stdout'])['labels'], ['ordinary'])

    def test_case_space_newline_and_repeated_label_storage(self):
        answer = self.raw(['create', 'ordinary case label', '-lReviewed:passed', '--json'])
        self.assertEqual(answer['returncode'], 0, answer)
        self.assertIn('Reviewed:passed', json.loads(answer['stdout'])['labels'])
        for flags in (['--labels=a,reviewed:passed'], ['-la', '-lreviewed:passed'],
                      ['-l', ' reviewed:passed '], ['-l', '\nreviewed:passed']):
            self.no_write(['create', 'reserved label attempt', *flags])

    def test_incomplete_ambiguous_reads_refuse_and_guard_holds_project_lock(self):
        import raw_lifecycle
        target = self.target()
        for value in ((None, 'incomplete read'), ([], ''),
                      ([{'id': target, 'title': None, 'issue_type': 'task'}], '')):
            with mock.patch.object(endpoint, '_bd_read', return_value=value):
                self.no_write(['update', target, '--title=ordinary'],
                              regex='lifecycle action.*no native write|incomplete read|does not name exactly one task')
        original = raw_lifecycle.guard
        entered = []
        def inspect_lock(*args):
            with (self.project/'.coordination.lock').open('a') as competing:
                with self.assertRaises(OSError) as held:
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIn(held.exception.errno, (errno.EAGAIN, errno.EACCES))
            entered.append(True)
            return original(*args)
        with mock.patch.object(raw_lifecycle, 'guard', inspect_lock):
            answer = self.raw(['update', target, '--priority=2', '--json'])
        self.assertEqual(answer['returncode'], 0, answer)
        self.assertEqual(entered, [True])

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
            ordinary = self.target()
            for flags in (['--type=event'], ['--type=task'], ['--ephemeral'], ['--persistent']):
                answer = self.raw(['update', ordinary, *flags, '--json'], actor='op')
                self.assertEqual(answer['returncode'], 0, answer)
        self.assertIn('reviewed:passed', next(r for r in self.export_rows() if r['id']==target)['labels'])

    def test_ordinary_worker_flow_with_all_rules_off(self):
        import admin
        import lifecycle
        from requirements import content_hash
        self.assertTrue(all(v is False for v in admin.coordinator_rules(self.root).values()))
        target = self.target()
        for args in (['update', target, '--claim', '--json'],
                     ['comments', 'add', target, 'ordinary progress', '--json'],
                     ['update', target, '--status=blocked', '--json'],
                     ['update', target, '--add-label=review-ready', '--json']):
            answer = self.raw(args, actor='worker')
            self.assertEqual(answer['returncode'], 0, answer)
        brief = endpoint.execute(self.root, dict(project='pp', actor='worker', action='brief',
                                                 args=[target, '--json']))
        cp = dict(schema_version=1, task=target, previous=None,
                  activity_cursor=json.loads(brief['stdout'])['activity_cursor'],
                  source_commit='a'*40, branch='synthetic', intent='ordinary worker flow',
                  acceptance='claim to delivery', summary='synthetic progress',
                  next_action='record facts', open_items=[], resolved=[])
        contribution = dict(schema_version=1, operation='contribute', operation_id='flow-'+target,
                            task=target, previous=None, supersedes=None,
                            repository='https://example.invalid/synthetic', commit='a'*40,
                            base_commit='b'*40, delivery=dict(kind='bundle', path='synthetic:d.bundle',
                                                            sha256='c'*64), summary='synthetic delivery')
        for action, payload in (('checkpoint', cp), ('review', contribution)):
            answer = endpoint.execute(self.root, dict(project='pp', actor='worker', action=action,
                args=[target, '@attachment:body'], attachments={'body': dict(flag='--file', text=json.dumps(payload))}))
            self.assertEqual(answer['returncode'], 0, answer)
        scope = dict(source_commit='a'*40, integration_commit='', release_id='', environment='synthetic')
        for dimension, value in (('lifecycle-scope', content_hash(scope)), ('implemented', 'passed'), ('tested', 'passed')):
            payload = dict(schema_version=1, operation_id='flow-'+target+'-'+dimension, task=target,
                           actor='worker', dimension=dimension, value=value, scope=scope,
                           evidence=['synthetic worker evidence'], provenance='performed')
            answer = endpoint.execute(self.root, dict(project='pp', actor='worker', action='lifecycle', args=[json.dumps(payload)]))
            self.assertEqual(answer['returncode'], 0, answer)
        facts = next(x for x in lifecycle.project_facts(self.export_rows()) if x['id']==target)
        for dimension in ('implemented', 'tested'):
            self.assertEqual(facts['facts'][dimension]['value'], 'passed')

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
