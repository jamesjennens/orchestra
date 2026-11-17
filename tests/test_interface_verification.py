"""Real source proof with emulated native anchors and live trust/replay boundaries."""
import copy
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
KIT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(KIT),str(KIT/'tests')]
import test_interfaces as fixture
import interface_records as interfaces
import interface_verification as checks
from requirements import content_hash


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.flow=fixture.InterfaceCase('test_shared_anchor_draft_accept_exact_replay_and_reserved_visibility')
        self.flow.setUp();self.addCleanup(self.flow.doCleanups)
        self.repo=Path(self.flow.tmp.name)/'source';self.repo.mkdir()
        self.git('init','-q')
        old=self.schema({'$schema':'https://json-schema.org/draft/2020-12/schema','type':'string','maxLength':4},'Old')
        self.flow.apply(fixture.payload(schema=old));self.flow.accept()
        self.old=copy.deepcopy(self.flow.entry()['record'])
        new=self.schema({'$schema':'https://json-schema.org/draft/2020-12/schema','type':'string','maxLength':4,'description':'New annotation'},'New')
        self.flow.apply(fixture.payload(operation='revise',operation_id='new-schema',revision=3,
            expected_sha256=self.old['sha256'],schema=new,compatibility={'rule':'additive-within-major','change':'additive'}))
        self.flow.accept(operation='accept-new')
        self.new=copy.deepcopy(self.flow.entry()['record'])
        self.payload={'operation_id':'check-1','key':self.new['key'],'old_revision':self.old['revision'],
            'old_record_sha256':self.old['sha256'],'new_revision':self.new['revision'],
            'new_record_sha256':self.new['sha256'],'direction':'request','decision_id':None}
        self.verifier='schema-verifier'

    def git(self,*args):
        return subprocess.check_output(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',*args],cwd=self.repo,stderr=subprocess.DEVNULL)

    def schema(self,value,message):
        body=json.dumps(value,separators=(',',':')).encode();(self.repo/'schema.json').write_bytes(body)
        self.git('add','.');self.git('commit','-qm',message)
        return {'type':'repo-path','path':'schema.json','commit':self.git('rev-parse','HEAD').strip().decode(),
                'anchor':None,'media_type':'application/schema+json','sha256':hashlib.sha256(body).hexdigest()}

    def write(self,payload=None,actor=None,**changes):
        actor=actor or self.verifier;self.flow.native.actor=actor
        options=dict(operators=[fixture.OPERATOR],verifiers=[self.verifier],repository=self.repo,trusted=True)
        options.update(changes)
        return checks.record(payload or self.payload,actor,self.flow.native,self.flow.path,**options)

    def block(self,**changes):
        entry=self.flow.entry();row=self.flow.native.row(entry['native_id'])
        options=dict(operators=[fixture.OPERATOR],verifiers=[self.verifier],repository=self.repo)
        options.update(changes)
        return checks.derive(entry,row,'prog',**options)

    def test_exact_source_and_both_directions_are_needed_for_unqualified_additive(self):
        first=self.write();self.assertEqual('additive',first['record']['result']['classification'])
        self.assertEqual('compatibility-unverified',self.block()['state'])
        second=dict(self.payload,operation_id='check-response',direction='response');self.write(second)
        self.assertEqual('additive',self.block()['state'])
        self.assertEqual(['request','response'],self.block()['checked_directions'])

    def test_removed_openapi_success_response_leaves_register_breaking(self):
        media = 'application/vnd.oai.openapi+json'
        previous = self.new
        accepted = []
        for revision, statuses in ((5, ('200', '404')), (7, ('404',))):
            responses = {status: {'description': status,
                                  'content': {'application/json': {'schema': {'type': 'string'}}}}
                         for status in statuses}
            document = {'openapi': '3.1.0', 'info': {'title': 'Items', 'version': '1'},
                        'paths': {'/items': {'get': {'responses': responses}}}}
            pointer = dict(self.schema(document, f'OpenAPI revision {revision}'), media_type=media)
            self.flow.apply(fixture.payload(operation='revise', operation_id=f'api-{revision}',
                revision=revision, expected_sha256=previous['sha256'], schema=pointer,
                compatibility={'rule': 'additive-within-major', 'change': 'additive'}))
            self.flow.accept(operation=f'accept-api-{revision}')
            previous = self.flow.entry()['record']
            accepted.append(previous)
        old, new = accepted
        payload = dict(self.payload, operation_id='check-removed-response',
                       old_revision=old['revision'], old_record_sha256=old['sha256'],
                       new_revision=new['revision'], new_record_sha256=new['sha256'])
        with patch.object(checks.classifier, 'VERSION', 'interface-subset-1'), \
                patch.object(checks.classifier, 'compare_openapi', return_value='additive'):
            self.assertEqual('additive', self.write(payload)['record']['result']['classification'])
            self.assertEqual('additive', self.block()['state'])
        self.assertEqual('compatibility-unverified', self.block()['state'])
        result = self.write(dict(payload, operation_id='check-removed-response-r3'))['record']
        self.assertEqual('breaking', result['result']['classification'])
        self.assertEqual('breaking-without-version', self.block()['state'])
        self.assertEqual(1, self.block()['trusted_results'])

    def test_unauthorized_or_demoted_verifier_report_untrusted(self):
        before=copy.deepcopy(self.flow.native.rows)
        with self.assertRaisesRegex(ValueError,'configured interface verifier'):self.write(actor='outsider')
        self.assertEqual(before,self.flow.native.rows)
        self.write();self.write(dict(self.payload,operation_id='response',direction='response'))
        self.assertEqual('additive',self.block()['state'])
        self.assertEqual('compatibility-unverified',self.block(verifiers=[])['state'])

    def test_stale_interface_revision_evidence_does_not_clear_drift(self):
        self.write();self.write(dict(self.payload,operation_id='response',direction='response'))
        self.assertEqual('additive',self.block()['state'])
        self.flow.apply(fixture.payload(operation='revise',operation_id='newer-schema',revision=5,
            expected_sha256=self.new['sha256'],schema=self.new['schema'],
            compatibility={'rule':'additive-within-major','change':'additive'}))
        self.flow.accept(operation='accept-newer')
        self.assertEqual('compatibility-unverified',self.block()['state'])
        before=copy.deepcopy(self.flow.native.rows)
        with self.assertRaisesRegex(ValueError,'current revision'):self.write(dict(self.payload,operation_id='stale'))
        self.assertEqual(before,self.flow.native.rows)

    def test_replay_exact_evidence_and_conflict(self):
        first=self.write();before=copy.deepcopy(self.flow.native.rows);again=self.write()
        self.assertTrue(again['reconciled']);self.assertEqual(first['record'],again['record']);self.assertEqual(before,self.flow.native.rows)
        with self.assertRaisesRegex(ValueError,'conflicts'):self.write(dict(self.payload,direction='response'))
        self.assertEqual(before,self.flow.native.rows)

    def test_missing_source_proof_and_relocated_repository_stay_unverified(self):
        outcome=self.write(repository=None);self.assertEqual('unknown',outcome['record']['result']['classification'])
        self.assertEqual('compatibility-unverified',self.block()['state'])
        with self.assertRaisesRegex(ValueError,'conflicts'):self.write()
        self.write(dict(self.payload,operation_id='retry-new-source'))
        self.write(dict(self.payload,operation_id='response',direction='response'))
        self.assertEqual('additive',self.block()['state'])
        self.assertEqual('compatibility-unverified',self.block(repository=self.repo/'moved')['state'])

    def test_contributor_report_never_clears_drift(self):
        # A negative trusted result at exactly these pins wins over any reported pass.
        # Inject at the classifier boundary, retaining real source and production write.
        original=checks.classifier.classify
        def breaking(*args,**kwargs):
            value=original(*args,**kwargs);value.update(classification='breaking',compatibility='breaking');return value
        with patch.object(checks.classifier,'classify',side_effect=breaking):self.write()
        self.assertEqual('breaking-without-version',self.block()['state'])
        self.write(dict(self.payload,operation_id='reported-pass'),actor='contributor',trusted=False,claimed='additive')
        self.assertEqual('breaking-without-version',self.block()['state'])

    def test_native_author_binding_and_algorithm_are_checked_at_read_time(self):
        self.write();self.write(dict(self.payload,operation_id='response',direction='response'))
        original=copy.deepcopy(self.flow.native.rows)
        for change in ('author','algorithm'):
            with self.subTest(change=change):
                self.flow.native.rows[:]=copy.deepcopy(original)
                for row in self.flow.native.rows:
                    for comment in row.get('comments',[]):
                        value=checks.parse(comment['text'])
                        if value is None:continue
                        if change=='author':comment['author']='someone-else'
                        else:
                            value['result']['algorithm']='old-algorithm';value['sha256']=content_hash(value)
                            from requirements import canonical_bytes
                            comment['text']=checks.PREFIX+canonical_bytes(value).decode()
                self.assertEqual('compatibility-unverified',self.block()['state'])

    def test_delegated_report_requires_live_binding_grant_and_interface_scope(self):
        import datetime
        import delegations
        owner='usr_'+'1'*16;person='usr_'+'2'*16;agent='agent_'+'a'*16
        at=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        state={'users':{owner:{'id':owner},person:{'id':person}},
            'memberships':{'prog':{owner:'owner',person:'coordinator'}},
            'agents':{agent:{'id':agent,'owner':person,'enabled':True,'projects':['prog']}},
            'coordinator_grants':{'prog':{agent:{'granted_by':owner,'granted_at':at}}}}
        self.flow.native.seed('verify-decision',issue_type='decision');self.flow.native.actor=fixture.OPERATOR
        payload={'schema_version':1,'operation_id':'verify-delegation','decision':'verify-decision','title':None,
            'action':'grant','right':'record-acceptance','grantee':agent,'scope':['interfaces'],
            'escalate':'Changes outside the interface check.','reason':'Synthetic verifier delegation.'}
        delegation=delegations.apply(payload,project='prog',journal=self.flow.path,run=self.flow.native,
            recorded_by=fixture.OPERATOR,via='operator',account=None,at=at)
        p=dict(self.payload,decision_id='verify-decision')
        for direction in ('request','response'):
            self.write(dict(p,operation_id='agent-'+direction,direction=direction),actor=agent,verifiers=[self.verifier],bindings={agent:self.verifier},state=state)
        options=dict(verifiers=[self.verifier],bindings={agent:self.verifier},state=state,rows=self.flow.native.rows,journal=self.flow.path)
        self.assertEqual('additive',self.block(**options)['state'])
        self.assertEqual('compatibility-unverified',self.block(**dict(options,verifiers=[]))['state'])
        self.assertEqual('compatibility-unverified',self.block(**dict(options,bindings={}))['state'])
        for change in ('grant','project','enabled','owner-role'):
            with self.subTest(change=change):
                changed=copy.deepcopy(state)
                if change=='grant':changed['coordinator_grants']['prog'].clear()
                elif change=='enabled':changed['agents'][agent]['enabled']=False
                elif change=='owner-role':changed['memberships']['prog'][owner]='viewer'
                else:changed['agents'][agent]['projects']=[]
                self.assertEqual('compatibility-unverified',self.block(**dict(options,state=changed))['state'])
        self.flow.native.seed('wrong-scope',issue_type='decision');self.flow.native.actor=fixture.OPERATOR
        wrong=dict(payload,operation_id='wrong-scope-grant',decision='wrong-scope',scope=['references'])
        delegations.apply(wrong,project='prog',journal=self.flow.path,run=self.flow.native,
            recorded_by=fixture.OPERATOR,via='operator',account=None,at=at)
        before=copy.deepcopy(self.flow.native.rows)
        with self.assertRaisesRegex(ValueError,'configured interface verifier'):
            self.write(dict(p,operation_id='wrong-scope-check',decision_id='wrong-scope'),actor=agent,verifiers=[self.verifier],bindings={agent:self.verifier},state=state)
        self.assertEqual(before,self.flow.native.rows)
        # A lost grant receipt never converts the native comment into authority.
        receipt=self.flow.path/delegations.JOURNAL_DIR/(delegation['record']['sha256']+'.json')
        saved=receipt.read_bytes();receipt.unlink()
        try:
            self.assertEqual('compatibility-unverified',self.block(**options)['state'])
            before=copy.deepcopy(self.flow.native.rows)
            with self.assertRaisesRegex(ValueError,'configured interface verifier'):
                self.write(dict(p,operation_id='after-lost-grant'),actor=agent,verifiers=[self.verifier],bindings={agent:self.verifier},state=state)
            self.assertEqual(before,self.flow.native.rows)
        finally:receipt.write_bytes(saved)
        # Record a real restriction, then lose ITS receipt: the original broad
        # grant must not return (the approved .462 reader is the prerequisite).
        self.flow.native.actor=fixture.OPERATOR
        revoke=dict(payload,operation_id='revoke-verifier',action='revoke',scope=[],escalate='')
        revoked=delegations.apply(revoke,project='prog',journal=self.flow.path,run=self.flow.native,
            recorded_by=fixture.OPERATOR,via='operator',account=None,at=at)
        self.assertEqual('compatibility-unverified',self.block(**options)['state'])
        lost=self.flow.path/delegations.JOURNAL_DIR/(revoked['record']['sha256']+'.json')
        lost.unlink()
        self.assertEqual('compatibility-unverified',self.block(**options)['state'])
        before=copy.deepcopy(self.flow.native.rows)
        with self.assertRaisesRegex(ValueError,'configured interface verifier'):
            self.write(dict(p,operation_id='after-lost-revoke'),actor=agent,verifiers=[self.verifier],bindings={agent:self.verifier},state=state)
        self.assertEqual(before,self.flow.native.rows)

    def test_reserved_interface_verification_comments_cannot_use_raw_routes(self):
        from reserved_comments import check_comment_body,reserved_match,is_record_comment
        record=self.write()['record']
        from requirements import canonical_bytes
        body=checks.PREFIX+canonical_bytes(record).decode()
        for value in (body,'\ufeff'+body,body.replace('\n','\r\n'),'Kind: interface-verification-v1 '+body.split('\n',1)[1]):
            with self.subTest(value=value[:60]):
                self.assertTrue(is_record_comment(value));self.assertIsNotNone(reserved_match(value))
                with self.assertRaises(ValueError):check_comment_body(value,'attachment',actor='someone',task='x')

    def test_copied_check_cannot_replay_on_a_different_anchor_or_project(self):
        first=self.write();entry=self.flow.entry();row=self.flow.native.row(entry['native_id'])
        comment=next(c for c in row['comments'] if checks.parse(c['text'])==first['record'])
        original=comment['text']
        for field,value in (('native_id','foreign-anchor'),('project','foreign')):
            altered=copy.deepcopy(first['record']);altered[field]=value;altered['sha256']=content_hash(altered)
            comment['text']=checks.PREFIX+checks.canonical_bytes(altered).decode()
            before=copy.deepcopy(self.flow.native.rows)
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'conflicts'):
                self.write(check_only=True)
            self.assertEqual(before,self.flow.native.rows)
            self.assertEqual('compatibility-unverified',self.block()['state'])
            comment['text']=original

    def test_readonly_replay_refuses_missing_or_stale_evidence_without_repair(self):
        first=self.write();entry=self.flow.entry();row=self.flow.native.row(entry['native_id'])
        comment=next(c for c in row['comments'] if checks.parse(c['text'])==first['record'])
        original=comment['text']
        for field in ('algorithm','source'):
            changed=copy.deepcopy(first['record'])
            if field=='algorithm':changed['result']['algorithm']='obsolete'
            else:changed['old_source']['path']='a-different-schema.json'
            changed['sha256']=content_hash(changed);comment['text']=checks.PREFIX+checks.canonical_bytes(changed).decode()
            before=copy.deepcopy(self.flow.native.rows)
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'stale'):
                self.write(check_only=True)
            self.assertEqual(before,self.flow.native.rows);comment['text']=original
        row['comments'].remove(comment);before=copy.deepcopy(self.flow.native.rows)
        with self.assertRaisesRegex(ValueError,'missing or voided'):self.write(check_only=True)
        self.assertEqual(before,self.flow.native.rows)

    def test_report_on_new_draft_does_not_promote_the_accepted_record(self):
        self.write();self.write(dict(self.payload,operation_id='response',direction='response'))
        before=self.block();self.assertEqual('additive',before['state'])
        self.flow.apply(fixture.payload(operation='revise',operation_id='later-draft',revision=5,
            expected_sha256=self.new['sha256'],schema=self.new['schema']))
        proposed=self.flow.entry()['proposed']
        report=dict(self.payload,operation_id='new-draft-report',old_revision=self.new['revision'],
            old_record_sha256=self.new['sha256'],new_revision=proposed['revision'],new_record_sha256=proposed['sha256'])
        self.write(report,actor='contributor',trusted=False,claimed='breaking')
        current=self.block();self.assertEqual(before,current)
        self.assertEqual(self.new['sha256'],current['record_sha256'])


    def test_party_checks_refuse_self_same_person_and_unknown_independence(self):
        person='usr_'+'1'*16;other='usr_'+'2'*16;agent='agent_'+'a'*16
        state={'users':{person:{'id':person},other:{'id':other}},
            'agents':{agent:{'id':agent,'owner':person,'enabled':True,'projects':['prog']}},
            'credentials':{}}
        self.assertFalse(checks.party_allows(agent,[agent],state,'prog'))
        self.assertFalse(checks.party_allows(agent,[person],state,'prog',True))
        self.assertFalse(checks.party_allows(person,[person+'/other-lane'],state,'prog',True))
        self.assertFalse(checks.party_allows(person,['unknown-native'],state,'prog',True))
        self.assertFalse(checks.party_allows(self.verifier,['unknown-native'],None,'prog',True))
        self.assertTrue(checks.party_allows(person,[other],state,'prog',True))
        before=copy.deepcopy(self.flow.native.rows)
        with self.assertRaisesRegex(ValueError,'party rule'):self.write(independent=True)
        self.assertEqual(before,self.flow.native.rows)



    def test_each_revision_hash_pin_is_checked_before_any_native_write(self):
        for field in ('old_record_sha256','new_record_sha256'):
            before=copy.deepcopy(self.flow.native.rows)
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'revision/hash pins'):
                self.write(dict(self.payload,**{field:'0'*64}))
            self.assertEqual(before,self.flow.native.rows)

    def test_untrusted_reports_never_load_source_or_gain_trusted_status(self):
        with patch.object(checks,'prove',side_effect=AssertionError('untrusted source execution')):
            try:result=self.write(actor='contributor',trusted=False,claimed='additive')['record']
            except ValueError as error:self.fail('An ordinary untrusted report must remain recordable: %s'%error)
        self.assertFalse(result['trusted']);self.assertEqual('additive',result['claimed'])
        self.assertEqual('unknown',result['result']['classification'])
        self.assertIsNone(result['old_source']);self.assertIsNone(result['new_source'])
        self.assertEqual('compatibility-unverified',self.block()['state'])
        for field,value in (('trusted',True),('actor',self.verifier),('repository',str(self.repo)),('result',{'classification':'additive'}),('http_only',False)):
            before=copy.deepcopy(self.flow.native.rows)
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'Wrong interface check fields'):
                self.write(dict(self.payload,**{field:value}))
            self.assertEqual(before,self.flow.native.rows)



    def test_binding_issuer_is_native_even_when_legacy_allowlists_contain_http_ids(self):
        person='usr_'+'1'*16;agent='agent_'+'a'*16
        for issuer in (person,'usr_short','agent_short','USR_0123456789ABCDEF'):
            with self.subTest(issuer=issuer),self.assertRaisesRegex(ValueError,'native issuers'):
                checks.configured_bindings({'interface_verifier_bindings':{agent:issuer}})
        state={'users':{person:{'id':person}},'memberships':{'prog':{person:'coordinator'}}}
        self.assertIsNone(checks.authority(person,'prog',[person],[],state,[],self.flow.path,None,bindings={person:person}))
        original=self.write()['record']
        for issuer in (person,'usr_short','agent_short','USR_0123456789ABCDEF'):
            value=copy.deepcopy(original);value['verifier_binding']=issuer;value['sha256']=content_hash(value)
            with self.subTest(issuer=issuer):self.assertIsNone(checks.parse(checks.PREFIX+checks.canonical_bytes(value).decode()))



    def test_old_algorithm_unknown_results_also_require_a_new_operation(self):
        first=self.write(repository=None)['record'];self.assertEqual('unknown',first['result']['classification'])
        row=self.flow.native.row(first['native_id'])
        comment=next(c for c in row['comments'] if checks.parse(c['text'])==first)
        first['result']['algorithm']='obsolete';first['sha256']=content_hash(first)
        comment['text']=checks.PREFIX+checks.canonical_bytes(first).decode()
        before=copy.deepcopy(self.flow.native.rows)
        with self.assertRaisesRegex(ValueError,'stale'):self.write(repository=None,check_only=True)
        self.assertEqual(before,self.flow.native.rows)



    def test_http_producer_needs_a_binding_even_for_a_configured_native_name(self):
        before=copy.deepcopy(self.flow.native.rows)
        with self.assertRaisesRegex(ValueError,'configured interface verifier'):self.write(http_only=True)
        self.assertEqual(before,self.flow.native.rows)
        # The same configured native issuer remains a valid host producer.
        self.assertEqual('additive',self.write()['record']['result']['classification'])
        self.assertIsNone(checks.authority(self.verifier,'prog',[fixture.OPERATOR],[self.verifier],{},[],
            self.flow.path,None,http_only=True))



    def test_unknown_media_at_the_interface_contract_limit_is_recordable(self):
        pointer=dict(self.new['schema'],media_type='a'*100+'/'+'b'*100)
        self.flow.apply(fixture.payload(operation='revise',operation_id='unsupported-media',revision=5,
            expected_sha256=self.new['sha256'],schema=pointer,compatibility={'rule':'additive-within-major','change':'additive'}))
        self.flow.accept(operation='accept-unsupported-media')
        target=self.flow.entry()['record']
        payload=dict(self.payload,old_revision=self.new['revision'],old_record_sha256=self.new['sha256'],
            new_revision=target['revision'],new_record_sha256=target['sha256'])
        with patch.object(checks,'prove',side_effect=AssertionError('unsupported media must not load source')):
            try:result=self.write(payload)['record']
            except ValueError as error:self.fail('Valid unsupported media must remain recordable as unknown: %s'%error)
        self.assertEqual(201,len(result['result']['media_type']))
        self.assertEqual('unknown',result['result']['classification'])
        self.assertEqual('compatibility-unverified',self.block()['state'])



class BackendReplayTests(unittest.TestCase):
    def test_both_cached_backend_routes_check_native_current_authority_before_returning(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import Mock
        from http_service import EndpointBackend
        for route,trusted in (('interfaces.verify',True),('interfaces.report',False)):
            backend=object.__new__(EndpointBackend)
            cached={'record':{'trusted':trusted}}
            backend.service=SimpleNamespace(store=SimpleNamespace(lock=threading.RLock()),
                has_result=lambda key:True,result_get=lambda key:cached,result_written_at=lambda key:None)
            backend._result_key=lambda *args:'canonical-result-identity'
            backend.interface_verification_replay_guard=Mock(side_effect=ValueError('revoked or missing native proof'))
            principal=object();payload={'body':{'key':'iface.sample.v1','operation_id':None}}
            with self.subTest(route=route),self.assertRaisesRegex(ValueError,'revoked'):
                backend.invoke(route,principal,'prog',payload,'same-idempotency-key')
            backend.interface_verification_replay_guard.assert_called_once_with(principal,'prog',
                {'key':'iface.sample.v1','operation_id':'canonical-result-identity'},trusted=trusted)
            backend.interface_verification_replay_guard.side_effect=None
            self.assertEqual(cached,backend.invoke(route,principal,'prog',payload,'same-idempotency-key'))
