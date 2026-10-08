"""Deep-row classification against real pinned bd, in an explicitly disposable runtime.

ORCHESTRA_MEMBERSHIP_TEST_ROOT names a scratch runtime with the marker
.orchestra-test-runtime containing 'synthetic test runtime'. No mocked native rows.
Each test creates its own project. The caller owns server setup and cleanup.
"""
import datetime
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import admin, endpoint
except ImportError:
    admin = endpoint = None
import record_json
from requirements import content_hash

ROOT = os.environ.get('ORCHESTRA_MEMBERSHIP_TEST_ROOT')


@unittest.skipIf(endpoint is None, 'native membership tests need POSIX endpoint imports')
@unittest.skipIf(not ROOT, 'disposable real bd runtime not configured (ORCHESTRA_MEMBERSHIP_TEST_ROOT)')
class NativeMembershipTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(ROOT)
        self.assertEqual((self.root/'.orchestra-test-runtime').read_text().strip(), 'synthetic test runtime')
        self.project = 'nm' + str(time.time_ns())[-16:]
        admin.initialize_project(self.root, self.project)
        self.path = self.root/'projects'/self.project
        self.epic = self.created('Job', ['--type', 'epic'])
        self.good = self.created('Healthy task', ['--assignee', 'alice'])
        self.acceptance = dict(owners=['owner-a'], approvers=['owner-a'], policy='any-owner',
                               decision_id='decision-1', evidence='synthetic review')
        self.seq = 0

    def run_bd(self, args):
        return admin.run_bd(self.root, self.project, ['--actor', 'ops', *args])

    def created(self, title, extra=()):
        return json.loads(self.run_bd(['create', title, *extra, '--json']))['id']

    def ep(self, action, args, payload=None, actor='alice'):
        request = dict(project=self.project, actor=actor, action=action, args=list(args))
        if payload is not None:
            request['args'].append('@attachment:p')
            request['attachments']={'p': dict(flag='--file', text=json.dumps(payload))}
        return endpoint.execute(self.root, request)

    def read(self, action, args, actor='alice'):
        result=self.ep(action, args, actor=actor)
        self.assertEqual(result['returncode'], 0, result)
        return json.loads(result['stdout'])

    def op(self):
        self.seq += 1
        return 'native-membership/%s/%d' % (self.project, self.seq)

    def deep(self, task, depth=751):
        self.run_bd(['update', task, '--metadata', '{"deep":'+'['*depth+'0'+']'*depth+'}', '--json'])
        self.addCleanup(self.heal, task)

    def heal(self, task):
        # bd update merges metadata: an empty object does not remove deep.
        self.run_bd(['update', task, '--metadata', '{"deep":null}', '--json'])

    def requirement(self, key, title):
        import requirement_records as rr
        payload=dict(schema_version=1, operation_id=self.op(), operation='draft', kind='requirement',
                     title=title, key=key, description='Statement.', acceptance_state='accepted',
                     acceptance=self.acceptance, parent=self.epic)
        return rr.apply_native(payload, 'ops', self.run_bd, self.path, operator=True, operators=['ops'])

    def entry(self, kind, key):
        if kind == 'reference':
            import reference_records as module
            payload=dict(title='Readable reference', statement='Statement.',
                         authority=dict(type='repo-path', path='docs/REVIEWS.md', commit='0'*40),
                         review_by=(datetime.date.today()+datetime.timedelta(days=200)).isoformat())
        else:
            import capability_records as module
            payload=dict(name='Readable capability', summary='Summary.')
        payload.update(schema_version=1, operation_id=self.op(), operation='draft', key=key,
                       owner='person:alice', acceptance_state='accepted', acceptance=self.acceptance)
        return module.apply_native(payload, 'ops', self.run_bd, self.path, operator=True, operators=['ops'])

    def rows(self):
        return record_json.loads_rows(self.run_bd(['export', '--all']))

    def fact_payload(self, task, dimension='tested', value='passed'):
        return dict(schema_version=1,operation_id=self.op(),task=task,dimension=dimension,value=value,
            scope=dict(source_commit='1'*40,integration_commit='2'*40,release_id='',environment=''),
            evidence=['synthetic'],provenance='performed',actor='ops')

    def fact(self, payload, run=None):
        import lifecycle
        return lifecycle.apply_native(payload,'ops',run or self.run_bd,operators=['ops'],journal=self.path)

    def test_native_event_membership_at_751_and_3000_and_read_failure(self):
        import lifecycle, capability_verification
        scope=self.fact_payload(self.good,'lifecycle-scope')
        scope['value']=content_hash(scope['scope']);self.fact(scope)
        self.fact(self.fact_payload(self.good))
        self.fact(self.fact_payload(self.good,'integrated'))
        self.assertIn('2'*40,capability_verification.integrated_commits(self.rows(),['ops'],self.path))
        newest=self.fact_payload(self.good);event=self.fact(newest)['event_id']
        for depth in (751,3000):
            with self.subTest(depth=depth):
                self.deep(event,depth)
                selected=record_json.native_ids(self.run_bd,'--type','event')
                self.assertIn(event,selected)
                raw=next(r for r in self.rows() if r['id']==event)
                self.assertTrue(raw['malformed']);self.assertEqual(raw['issue_type'],'unknown')
                rows=lifecycle.read_event_rows(self.run_bd)
                marked=next(r for r in rows if r['id']==event)
                self.assertTrue(record_json.selected(marked,types=['event']))
                state=next(r for r in lifecycle.project_facts(rows) if r['id']==self.good)
                self.assertEqual(state['facts']['tested']['value'],'unknown')
                self.assertEqual(capability_verification.integrated_commits(rows,['ops'],self.path),set())
                integrated=capability_verification.Integrated(self.run_bd,['ops'],self.path)
                self.assertFalse(integrated('2'*40))
                integrated._export();self.assertEqual(integrated.everything,set())
                page=self.read('work',['--mine'])
                self.assertNotIn(event,{r['task'] for r in page['items']})
                self.assertEqual(next(r for r in page['items'] if r['task']==self.good)['lifecycle']['tested'],'unknown')
                brief=self.read('brief',[self.good,'--json'])
                self.assertEqual(brief['lifecycle']['tested']['value'],'unknown')
                before=len(self.rows())
                for payload in (newest,dict(newest,value='failed'),self.fact_payload(self.good)):
                    with self.assertRaisesRegex(ValueError,event+'.*operator must reconcile or repair the event'):
                        self.fact(payload)
                self.assertEqual(len(self.rows()),before)
                for error in (ValueError('selection failed'),subprocess.TimeoutExpired('native event selection',120)):
                    def failed(args):
                        if args[:3]==['list','--type','event']:raise error
                        return self.run_bd(args)
                    with self.assertRaisesRegex(ValueError,'operator must reconcile or repair the event index'):
                        self.fact(self.fact_payload(self.good),failed)
                self.assertEqual(len(self.rows()),before)
                self.heal(event)
        state=next(r for r in lifecycle.project_facts(lifecycle.read_event_rows(self.run_bd)) if r['id']==self.good)
        self.assertEqual(state['facts']['tested']['value'],'passed')

    def test_contributor_event_creation_and_type_transitions_are_native_membership(self):
        import lifecycle
        made=self.read('bd',['create','Contributor event','--type','event','--json'])['id']
        self.assertIn(made,record_json.native_ids(self.run_bd,'--type','event'))
        result=self.ep('bd',['update',self.good,'--type','event','--json'])
        self.assertEqual(result['returncode'],0,result)
        self.deep(self.good)
        scope=self.fact_payload(self.epic,'lifecycle-scope');scope['value']=content_hash(scope['scope'])
        with self.assertRaisesRegex(ValueError,self.good+'.*operator must reconcile or repair the event'):
            self.fact(scope)
        self.heal(self.good)
        self.ep('bd',['update',self.good,'--type','task','--json'])
        self.assertNotIn(self.good,record_json.native_ids(self.run_bd,'--type','event'))
        scope=self.fact_payload(self.good,'lifecycle-scope');scope['value']=content_hash(scope['scope'])
        self.fact(scope);event=self.fact(self.fact_payload(self.good))['event_id']
        self.ep('bd',['update',event,'--type','task','--json'])
        self.assertNotIn(event,record_json.native_ids(self.run_bd,'--type','event'))
        row=next(r for r in self.rows() if r['id']==event)
        self.assertIsNone(lifecycle.native_event(row))
        self.deep(event)
        # A retyped row is an ordinary unreadable task and cannot hold an event operation.
        self.fact(self.fact_payload(self.good))
        with self.assertRaisesRegex(ValueError,'operator-only'):
            self.ep('bd',['update',made,'--metadata','{}','--json'])

    def test_requirement_keys_use_separate_validated_comments_not_titles(self):
        import requirement_records as rr
        for index, title in enumerate(['Backups are restorable', 'ZZ9: Wrong key', 'R2: First']):
            key='R%d' % index
            task=self.requirement(key, title)['id']
            if index == 2:
                self.ep('bd', ['update', task, '--title', 'Tidy the garden shed', '--json'])
            self.deep(task)
            native=record_json.loads_array_rows(self.run_bd(['list', '--label', 'requirement', '--all', '--limit', '0', '--json']))
            self.assertTrue(next(r for r in native if r['id']==task)['malformed'])
            before=len(self.rows())
            with self.assertRaisesRegex(ValueError, 'Cannot verify requirement key uniqueness.*'+task):
                self.requirement(key, 'A second requirement')
            self.assertEqual(len(self.rows()), before)
            self.requirement(key+'x', 'Another key')
            self.heal(task)
            holders=[r['id'] for r in self.rows() if 'requirement' in (r.get('labels') or [])
                     and rr.existing_key(rr.existing_revisions(r))==key]
            self.assertEqual(holders, [task])

    def test_misleading_ordinary_tasks_block_nothing_and_stay_in_work(self):
        ids=[]
        for title in ['R1: my notes', 'Requirement notes', 'Reference manual needs an update',
                      'Capability matrix', 'Settings page', 'anchor: replace mooring']:
            task=self.created(title, ['--assignee', 'alice']);ids.append(task);self.deep(task)
        self.requirement('R1', 'A real requirement')
        self.entry('reference', 'other.ref')
        self.entry('capability', 'other.cap')
        page=self.read('work', ['--mine', '--limit', '100'])
        self.assertTrue(set(ids).issubset({r['task'] for r in page['items']}))
        self.assertTrue(set(ids).isdisjoint(self.read('anchors', ids)['anchors']))
        self.ep('refresh', [])
        current=(self.path/'views/CURRENT.md').read_text()
        self.assertTrue(all(task in current for task in ids))

    def test_retitled_reference_and_capability_have_consistent_read_surfaces(self):
        for kind, action, key in [('reference','ref','deep.ref'), ('capability','capability','deep.cap')]:
            self.entry(kind, key)
            task=self.read(action, ['get', key])['native_id']
            # Host fixture setup: the contributor endpoint correctly protects
            # an existing record anchor's title on current main.
            self.run_bd(['update', task, '--title', 'A host changed this title', '--json'])
            self.deep(task)
            with self.assertRaisesRegex(ValueError, 'exists .* cannot be read'):
                self.read(action, ['get', key])
            listing=self.read(action, ['list', '--state', 'all', '--limit', '100'])
            matching=[r for r in listing['items'] if r['native_id']==task]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]['state'], 'malformed')
            self.assertIn(task, listing['coverage'])
            self.assertNotIn(task, {r['task'] for r in self.read('work', ['--mine'])['items']})
            for args in ([], [task]):
                self.assertIn(task, self.read('anchors', args)['anchors'])
                self.assertIn(task, self.ep('anchors', args)['stderr'])
            self.ep('refresh', [])
            self.assertNotIn(task, (self.path/'views/CURRENT.md').read_text())
            with self.assertRaisesRegex(ValueError, 'Cannot verify .*key uniqueness'):
                self.entry(kind, key)
            self.entry(kind, key+'.other')
            self.heal(task)

    def test_catalog_export_path_and_membership_read_failure(self):
        import capability_verification
        scope=self.fact_payload(self.good,'lifecycle-scope');scope['value']=content_hash(scope['scope'])
        self.fact(scope);self.fact(self.fact_payload(self.good,'integrated'))
        for kind,action,suffix in [('reference','ref','ref'),('capability','capability','cap')]:
            for index in range(21): self.entry(kind, 'scale%d.%s' % (index,suffix))
            task=self.read(action, ['get', 'scale0.'+suffix])['native_id'];self.deep(task)
            listing=self.read(action, ['list', '--state', 'all', '--limit', '100'])
            self.assertIn(task, {r['native_id'] for r in listing['items']})
            self.assertIn(task,listing['coverage'])
            integrated=capability_verification.Integrated(self.run_bd,['ops'],self.path)
            integrated._export();self.assertIn('2'*40,integrated.everything)
            self.heal(task)
        self.deep(self.created('Ordinary task with a verification-looking title'))
        integrated=capability_verification.Integrated(self.run_bd,['ops'],self.path)
        integrated._export();self.assertIn('2'*40,integrated.everything)
        rows=self.rows()
        def broken(args):
            raise ValueError('native classification unavailable')
        with self.assertRaisesRegex(ValueError, 'native classification unavailable'):
            record_json.classify(rows, broken, ['reference'])

    def test_proposal_get_and_submit_retry_refuse_the_unreadable_anchor(self):
        payload=dict(schema_version=1, operation_id=self.op(), operation='submit', submitter='person:alice',
                     target={'kind':'requirement-new'}, text='Charts must be reproducible.', rationale='Why.',
                     evidence=['https://example.invalid/evidence'], attachments=[])
        made=json.loads(self.ep('proposal', ['submit'], payload)['stdout'])
        task=made['native_id'];self.deep(task)
        with self.assertRaisesRegex(ValueError, 'exists .* cannot be read'):
            self.read('proposal', ['get', made['key']])
        with self.assertRaisesRegex(ValueError, 'exists but cannot be read'):
            self.ep('proposal', ['submit'], payload)
        # An unreadable row from the request arm for another key must not make a
        # one-row get claim that the requested key exists.
        import proposal_records as pr
        rows=pr.read_key_rows(self.run_bd, made['key'])
        with self.assertRaisesRegex(ValueError, 'Unknown proposal key'):
            pr.find_entry([r for r in rows if r['id']==task], 'p-000000000000')
        self.heal(task)

    def test_unreadable_rows_of_seven_kinds_isolate_unrelated_operations(self):
        import lifecycle
        self.requirement('M1', 'Requirement')
        req=next(r['id'] for r in self.rows() if 'requirement' in (r.get('labels') or []))
        self.entry('reference', 'matrix.ref');ref=self.read('ref', ['get','matrix.ref'])['native_id']
        self.entry('capability','matrix.cap');cap=self.read('capability',['get','matrix.cap'])['native_id']
        child=self.created('Child', ['--parent', self.epic])
        scope=dict(source_commit='1'*40, integration_commit='2'*40, release_id='', environment='')
        def fact(task, dimension='tested', value='passed'):
            return lifecycle.apply_native(dict(schema_version=1, operation_id=self.op(), task=task,
                dimension=dimension, value=value, scope=scope, evidence=['synthetic'], provenance='performed',
                actor='ops'), 'ops', self.run_bd, operators=['ops'], journal=self.path)
        fact(self.good, 'lifecycle-scope', content_hash(scope))
        event=fact(self.good)['event_id']
        slot=self.project+'-merge-slot'
        for index, bad in enumerate([self.created('Top'), child, req, ref, cap, event, slot]):
            with self.subTest(bad=bad):
                self.deep(bad)
                result=self.ep('bd',['comments','add',self.good,'healthy comment','--json'])
                self.assertEqual(result['returncode'],0,result)
                result=self.ep('bd',['create','Healthy new task','--json'])
                self.assertEqual(result['returncode'],0,result)
                self.requirement('N%d'%index, 'Other key')
                self.entry('reference','n%d.ref'%index)
                self.entry('capability','n%d.cap'%index)
                proposal=dict(schema_version=1,operation_id=self.op(),submitter='person:alice',
                    target={'kind':'requirement-new'},text='Other proposal.',rationale='Why.',
                    evidence=['https://example.invalid/evidence'],attachments=[])
                result=self.ep('proposal',['submit'],proposal)
                self.assertEqual(result['returncode'],0,result)
                result=self.ep('coordinate',[json.dumps(dict(operation='create-child',request_id=self.op(),
                    parent=self.epic,title='Another child',description='Description.',type='task'))])
                self.assertEqual(result['returncode'],0,result)
                result=self.ep('coordinate',[json.dumps(dict(operation='merge-acquire',task=self.good,target='main'))])
                self.assertEqual(result['returncode'],0,result)
                self.assertTrue(json.loads(result['stdout'])['acquired'])
                self.ep('coordinate',[json.dumps(dict(operation='merge-release'))])
                work_ids={r['task'] for r in self.read('work',['--mine'])['items']}
                if bad in (ref,cap,event,slot):
                    self.assertNotIn(bad,work_ids)
                else:
                    self.assertIn(bad,work_ids)
                self.read('anchors',[])
                result=self.ep('refresh',[]);self.assertEqual(result['returncode'],0,result)
                if bad==event:
                    with self.assertRaisesRegex(ValueError,'operator must reconcile or repair the event'):
                        fact(self.good)
                else: fact(self.good)
                self.heal(bad)


if __name__ == '__main__': unittest.main()
