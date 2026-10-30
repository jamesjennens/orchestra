"""Interface slice3a: actual shared-kind lifecycle and caller-scoped graph checks."""
import datetime
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

KIT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(KIT));sys.path.insert(0,str(KIT/'tests'))
import interface_records as ir
import capability_records as cr
import reference_records as rr
from requirements import content_hash
from test_reference_records import RefNative, acceptance, OPERATOR
from test_capability_records import entry as cap_entry


def payload(**changes):
    value={'schema_version':1,'operation':'propose','operation_id':'interface-1','revision':1,
           'expected_sha256':None,'key':'iface.quotes.v1','name':'Quotes','meaning':'Synthetic quotes',
           'owner_project':'area','owner':'person:example','schema':{'type':'repo-path','path':'schema.json',
           'commit':'1'*40,'anchor':None,'media_type':'application/schema+json','sha256':'2'*64},
           'compatibility':{'rule':'additive-within-major','change':'initial'},'replaces':None,
           'state':'accepted','review_by':(datetime.date.today()+datetime.timedelta(days=90)).isoformat(),'tags':[]}
    value.update(changes);return value


class InterfaceCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'prog';self.path.mkdir()
        self.native=RefNative();self.area=RefNative()
        self.projects={'prog':self.native.rows,'area':self.area.rows}
        for native,path,key,text in [(self.native,self.path,'program.area.area','Area: area\nState: in'),
                                      (self.area,self.path.parent/'area','program.member-of','Program: prog\nState: in')]:
            path.mkdir(exist_ok=True);native.seed('decision-42',issue_type='decision');native.actor=OPERATOR
            rr.apply_native({'schema_version':1,'operation':'draft','operation_id':'link-'+key,
              'key':key,'title':'Link','statement':text,'authority':{'type':'decision','id':'decision-42'},
              'owner':'person:example','review_by':(datetime.date.today()+datetime.timedelta(days=90)).isoformat(),'tags':[],'decisions':[],
              'acceptance_state':'accepted','acceptance':acceptance()},OPERATOR,native,path,
              operator=True,operators=[OPERATOR])
        self.native.calls.clear()

    def context(self,may=None):
        return ir.ProgramContext('prog',lambda name:self.projects.get(name),operators=[OPERATOR],may_propose=may)

    def apply(self,p,actor='author',operator=False,ctx=None):
        self.native.actor=actor
        with ir.context(ctx or self.context()):
            return ir.apply_native(p,actor,self.native,self.path,operator=operator,operators=[OPERATOR])

    def entry(self):return ir.get(self.native.rows,'iface.quotes.v1',[OPERATOR])

    def accept(self,p=None,operation='accept-1',ctx=None):
        draft=self.entry()['proposed'] if p is None else p
        return self.apply({'schema_version':1,'operation':'accept','operation_id':operation,
            'key':draft['key'],'revision':draft['revision'],'record_sha256':draft['sha256'],
            'acceptance_state':'accepted','acceptance':acceptance()},OPERATOR,True,ctx=ctx)

    def test_shared_anchor_draft_accept_exact_replay_and_reserved_visibility(self):
        from reserved_comments import is_record_anchor,is_record_comment,reserved_match
        proposed=self.apply(payload());draft=self.entry()['proposed']
        self.assertEqual('draft',self.entry()['lifecycle'])
        result=self.accept();self.assertEqual(2,result['revision'])
        writes=len(self.native.writes());self.accept(draft)
        self.assertEqual(writes,len(self.native.writes()))
        selected=self.entry();self.assertEqual('accepted',selected['lifecycle'])
        self.assertEqual('compatibility-unverified',selected['compatibility'])
        row=next(r for r in self.native.rows if r['id']==proposed['native_id'])
        self.assertTrue(is_record_anchor(row))
        self.assertTrue(is_record_comment(ir.ENTRY_PREFIX+'anything'))
        self.assertIsNotNone(reserved_match(ir.ENTRY_PREFIX+'{}'))

    def test_closed_schema_hash_and_bad_pins_refused_without_write(self):
        for changes in [{'key':'iface.x.v0'},{'compatibility':{'rule':'additive-within-major','change':'breaking'}},
                        {'schema':dict(payload()['schema'],commit='main')},{'owner':'session-1234'},
                        {'state':'unknown'},{'review_by':'2099-99-99'}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.apply(payload(**changes))
        self.assertEqual([],self.native.writes())
        rec=ir.entry_record(payload(),1,'draft');rec['meaning']='changed'
        with self.assertRaisesRegex(ValueError,'hash'):ir.validate_entry(rec)

    def test_cas_and_first_initial_then_additive_only(self):
        self.apply(payload());self.accept();rec=self.entry()['record']
        changes=payload(operation='revise',operation_id='revise-3',revision=3,expected_sha256=rec['sha256'])
        self.apply(changes)
        writes=len(self.native.writes())
        with self.assertRaisesRegex(ValueError,'additive'):self.accept(operation='bad-initial')
        self.assertEqual(writes,len(self.native.writes()))
        changes.update(operation_id='revise-4',revision=4,expected_sha256=self.entry()['proposed']['sha256'],
                       compatibility={'rule':'additive-within-major','change':'additive'},meaning='additive synthetic change')
        self.apply(changes);self.accept(operation='accept-4')
        self.assertEqual('additive synthetic change',self.entry()['record']['meaning'])
        with self.assertRaises(ValueError):self.apply(dict(changes,operation_id='stale',meaning='different stale content'))

    def test_retire_is_state_only_complete_and_consumer_free(self):
        self.apply(payload());self.accept();record=self.entry()['record']
        retirement=payload(operation='revise',operation_id='retire-draft',revision=3,
                           expected_sha256=record['sha256'],state='retired')
        self.apply(retirement)
        # A current accepted consumer in the linked area blocks retirement, regardless of the pinned minor.
        self.area.actor=OPERATOR
        cap=cap_entry(operation='draft',operation_id='consumer',interfaces=[{'iface':'iface:prog/iface.quotes.v1@2','role':'consumes'}],
                      acceptance_state='accepted',acceptance=acceptance())
        cr.apply_native(cap,OPERATOR,self.area,self.path.parent/'area',operator=True,operators=[OPERATOR])
        writes=len(self.native.writes())
        with self.assertRaisesRegex(ValueError,'current consumers'):self.accept(operation='refused-consumer')
        self.assertEqual(writes,len(self.native.writes()))
        del self.projects['area']
        with self.assertRaises(ValueError):self.accept(operation='refused-hidden')
        self.projects['area']=self.area.rows
        self.area.rows[:]=[r for r in self.area.rows if 'capability' not in r.get('labels',[])]
        self.accept(operation='retire-ok');self.assertEqual('retired',self.entry()['lifecycle'])

    def test_proposer_loses_source_coordination_and_link_refused(self):
        with self.assertRaisesRegex(ValueError,'coordination'):self.apply(payload(),ctx=self.context(lambda p:False))
        self.assertEqual([],self.native.writes())
        self.projects['area']=None
        with self.assertRaisesRegex(ValueError,'linked'):self.apply(payload())
        self.assertEqual([],self.native.writes())

    def test_capability_v1_bytes_preserved_v2_closed_links_and_old_versions(self):
        original=cr.entry_record(cap_entry(),1,'draft')
        body=cr.ENTRY_PREFIX+__import__('requirements').canonical_bytes(original).decode()
        self.assertEqual(original,cr.parse_entry(body))
        links=[{'iface':'iface:prog/iface.quotes.v1@2','role':'provides'}]
        v2=cr.entry_record(cap_entry(interfaces=links),1,'draft')
        self.assertEqual(2,v2['schema_version']);self.assertEqual(links,v2['interfaces'])
        self.assertEqual(v2,cr.parse_entry(cr.ENTRY_V2_PREFIX+__import__('requirements').canonical_bytes(v2).decode()))
        self.assertIsNone(cr.parse_entry(cr.ENTRY_V2_PREFIX+__import__('requirements').canonical_bytes(original).decode()))
        for bad in [[links[0],links[0]],[{'iface':'iface:prog/iface.quotes.v1','role':'consumes'}],
                    [{'iface':'iface:prog/iface.quotes.v1@2','role':'other'}]]:
            with self.assertRaises(ValueError):cr.entry_record(cap_entry(interfaces=bad),1,'draft')

    def test_iface_qualified_resolution_and_no_hidden_read(self):
        from briefing import ContextPointers,parse_context_pointers
        import trace_read
        self.apply(payload());self.accept()
        self.assertEqual([],parse_context_pointers('iface:prog/iface.quotes.v1 iface:prog/iface.quotes.v0@2'))
        p=parse_context_pointers('iface:prog/iface.quotes.v1@2')[0]
        here=ContextPointers(self.native.rows,'prog',[OPERATOR])
        self.assertEqual('live',here.resolve(p)['state'])
        outsider=ContextPointers(self.native.rows,'other',[OPERATOR])
        self.assertEqual('cross-project',outsider.resolve(p)['state'])
        calls=[];load=trace_read.others_loader(set(),lambda p:calls.append(p))
        self.assertIsNone(load('prog'));self.assertEqual([],calls)
        hidden=ContextPointers(self.native.rows,'prog',[OPERATOR],can_read=lambda row:False)
        self.assertNotEqual('live',hidden.resolve(p)['state'])

    def test_provider_minima_flags_and_incomplete_coverage_no_absence_claim(self):
        record=ir.entry_record(payload(),2,'accepted')
        usage=[{'key':record['key'],'role':'consumes','minimum':3}]
        self.assertIn('no-provider',ir.flags(record,usage,True))
        self.assertNotIn('no-provider',ir.flags(record,usage,False))
        usage.append({'key':record['key'],'role':'provides','minimum':4})
        self.assertNotIn('no-provider',ir.flags(record,usage,True))
        self.assertIn('orphan',ir.flags(record,[],True))
        self.assertNotIn('orphan',ir.flags(record,[],False))

    def test_new_major_deprecates_only_predecessor_state_and_exact_retry(self):
        self.apply(payload());self.accept();old=self.entry()['record']
        self.apply(payload(key='iface.quotes.v2',replaces='iface.quotes.v1',operation_id='major2'))
        proposed=ir.get(self.native.rows,'iface.quotes.v2',[OPERATOR])['proposed']
        self.accept(proposed,operation='accept-major2')
        after=self.entry()['record']
        self.assertEqual('deprecated',after['state'])
        self.assertEqual('iface.quotes.v2',self.entry()['replacement'])
        self.assertEqual({k:old[k] for k in ir.CONTENT_FIELDS if k!='state'},
                         {k:after[k] for k in ir.CONTENT_FIELDS if k!='state'})
        count=len(self.native.writes());self.accept(proposed,operation='accept-major2')
        self.assertEqual(count,len(self.native.writes()))

    def test_deprecation_cut_between_evidence_and_revision_retries_without_duplicates(self):
        self.apply(payload());self.accept()
        self.apply(payload(key='iface.quotes.v2',replaces='iface.quotes.v1',operation_id='cut-major2'))
        proposed=ir.get(self.native.rows,'iface.quotes.v2',[OPERATOR])['proposed']
        original=self.native.__class__.__call__;fired=[]
        from unittest.mock import patch
        def cut(native,args):
            if args[:2]==['comments','add'] and len(args)>3 and args[3].startswith(ir.ENTRY_PREFIX):
                record=ir.parse_entry(args[3])
                if record and record['state']=='deprecated' and not fired:
                    fired.append(True);raise RuntimeError('synthetic cut before predecessor revision')
            return original(native,args)
        with patch.object(self.native.__class__,'__call__',cut):
            with self.assertRaisesRegex(RuntimeError,'synthetic cut'):self.accept(proposed,operation='cut-accept2')
        self.accept(proposed,operation='cut-accept2')
        self.assertEqual('deprecated',self.entry()['record']['state'])
        comments=[c['text'] for row in self.native.rows for c in row.get('comments',[]) if c.get('text','').startswith(ir.ACCEPTANCE_PREFIX)]
        self.assertEqual(len(comments),len(set(comments)))

    def test_unknown_versions_and_corrupt_graph_make_retirement_incomplete(self):
        self.apply(payload());self.accept();record=self.entry()['record']
        self.apply(payload(operation='revise',operation_id='retirement-incomplete',revision=3,
                           expected_sha256=record['sha256'],state='retired'))
        self.area.seed('malformed-capability',labels=['capability','capability-key:future'],status='closed',
                       comments=[{'id':'future','author':OPERATOR,'text':'Kind: capability-entry-v99\n{}'}])
        self.assertFalse(self.context().usage()[1]['complete'])
        writes=len(self.native.writes())
        with self.assertRaisesRegex(ValueError,'incomplete coverage'):self.accept(operation='unknown-capability-refusal')
        self.assertEqual(writes,len(self.native.writes()))

    def test_complete_transition_receipt_is_backed_up_validated_and_not_releasable(self):
        import admin
        self.apply(payload());self.accept()
        self.apply(payload(key='iface.quotes.v2',replaces='iface.quotes.v1',operation_id='backup-major2'))
        proposed=ir.get(self.native.rows,'iface.quotes.v2',[OPERATOR])['proposed']
        self.accept(proposed,operation='backup-accept2')
        path=next((self.path/'.interface-transitions').glob('*.json'))
        receipt=json.loads(path.read_text());self.assertEqual('complete',receipt['status'])
        admin.validate_coordination_files({'.interface-transitions/'+path.name:receipt})
        broken=copy.deepcopy(receipt);broken['record']['meaning']='tampered'
        with self.assertRaises(ValueError):admin.validate_coordination_files({'.interface-transitions/'+path.name:broken})
        with self.assertRaises(ValueError):ir.reconcile(self.path,'backup-accept2',OPERATOR,'do not discard','released',self.native,operators=[OPERATOR])

    def test_initial_new_major_requires_replaces_when_a_predecessor_exists(self):
        self.apply(payload());self.accept()
        self.apply(payload(key='iface.quotes.v2',operation_id='unbound-major2'))
        proposed=ir.get(self.native.rows,'iface.quotes.v2',[OPERATOR])['proposed']
        writes=len(self.native.writes())
        with self.assertRaisesRegex(ValueError,'preceding major'):self.accept(proposed,operation='refused-major2')
        self.assertEqual(writes,len(self.native.writes()))

    def test_provider_pin_requires_live_readable_accepted_revision(self):
        self.apply(payload())
        self.assertFalse(self.context().accepted_pin('iface.quotes.v1',1))
        self.accept()
        self.assertTrue(self.context().accepted_pin('iface.quotes.v1',2))
        self.assertFalse(self.context().accepted_pin('iface.quotes.v1',99))
        row=next(row for row in self.native.rows if 'interface' in row.get('labels',[]))
        row['comments'].append({'id':'corrupt','text':'Kind: interface-entry-v99\n{}','author':OPERATOR})
        self.assertFalse(self.context().accepted_pin('iface.quotes.v1',2))

    def test_area_limit_fails_before_any_area_read(self):
        import program_links
        from unittest.mock import patch
        calls=[]
        context=ir.ProgramContext('prog',lambda name:calls.append(name) or [],operators=[OPERATOR])
        entries=[{'key':'program.area.area'+str(n),'state':'accepted'} for n in range(ir.AREA_MAX+1)]
        with patch.object(rr.KIND,'catalog',return_value=(entries,False)),patch.object(program_links,'side_status',return_value=('in',{})):
            self.assertEqual(([],False,ir.AREA_MAX+1),context.areas())
        self.assertEqual(['prog'],calls)

    def test_media_type_is_bounded_and_version_change_is_explicit(self):
        bad=payload();bad['schema']['media_type']='x'*101+'/json'
        with self.assertRaises(ValueError):ir.entry_record(bad,1,'draft')
        self.apply(payload());self.accept();record=self.entry()['record']
        self.apply(payload(operation='revise',operation_id='bad-change',revision=3,
                           expected_sha256=record['sha256'],meaning='A changed contract',state='accepted'))
        writes=len(self.native.writes())
        with self.assertRaisesRegex(ValueError,'additive'):self.accept(operation='initial-again')
        self.assertEqual(writes,len(self.native.writes()))

    def test_capability_local_and_qualified_alias_cannot_conflict(self):
        links=[{'iface':'iface:iface.quotes.v1@2','role':'provides'},
               {'iface':'iface:prog/iface.quotes.v1@3','role':'consumes'}]
        writes=len(self.native.writes())
        with self.assertRaisesRegex(ValueError,'Duplicate or conflicting'):
            cr.apply_native(cap_entry(interfaces=links), 'author', self.native, self.path, operators=[OPERATOR])
        self.assertEqual(writes,len(self.native.writes()))
        # A foreign/raw historical record with the same semantic conflict is
        # unknown coverage, not a missing consumer or a trusted provider.
        from unittest.mock import patch
        bad=[{'iface':'iface:iface.quotes.v1@2','role':'provides'},
             {'iface':'iface:area/iface.quotes.v1@3','role':'consumes'}]
        self.area.actor=OPERATOR
        with patch.object(cr,'_pre_write',return_value=None):
            cr.apply_native(cap_entry(operation='draft',operation_id='foreign-conflict',interfaces=bad,
                acceptance_state='accepted',acceptance=acceptance()),OPERATOR,self.area,self.path.parent/'area',
                operator=True,operators=[OPERATOR])
        self.assertFalse(self.context().usage()[1]['complete'])

    def test_missing_owner_authority_cannot_prove_consumer_free_coverage(self):
        # A valid owner-backed admission cannot be classified from a host view
        # that loaded no current memberships/accounts. Keep the operator-backed
        # defining area visible so retirement must reach the completeness check.
        from unittest.mock import patch
        self.apply(payload());self.accept();record=self.entry()['record']
        self.native.actor=OPERATOR
        rr.apply_native({'schema_version':1,'operation':'draft','operation_id':'other-owner-link',
            'key':'program.area.ownerarea','title':'Owner-backed area','statement':'Area: ownerarea\nState: in',
            'authority':{'type':'decision','id':'decision-42'},'owner':'person:example',
            'review_by':payload()['review_by'],'tags':[],'decisions':[],
            'acceptance_state':'accepted','acceptance':acceptance()},OPERATOR,self.native,self.path,
            operator=True,operators=[OPERATOR])
        original=rr.KIND.catalog
        def owner_projection(rows,operators):
            entries,incomplete=original(rows,operators)
            for entry in entries:
                if entry.get('key')=='program.area.ownerarea':entry['accepted_by']='usr_'+'b'*16
            return entries,incomplete
        with patch.object(rr.KIND,'catalog',side_effect=owner_projection):
            self.apply(payload(operation='revise',operation_id='retire-owner-unknown',revision=3,
                               expected_sha256=record['sha256'],state='retired'))
            writes=len(self.native.writes())
            with self.assertRaisesRegex(ValueError,'incomplete coverage'):
                self.accept(operation='owner-authority-unknown')
            self.assertEqual(writes,len(self.native.writes()))
            uid='usr_'+'b'*16
            visible_owner=ir.ProgramContext('prog',lambda name:self.projects.get(name),operators=[OPERATOR],
                memberships={'prog':{uid:'owner'}},users={uid:{'disabled':False}})
            with self.assertRaisesRegex(ValueError,'incomplete coverage'):
                self.accept(operation='owner-area-unavailable',ctx=visible_owner)
            # An authoritative current view proving that the old acceptor is no
            # longer an owner makes the old admission inactive, not unknown.
            inactive=ir.ProgramContext('prog',lambda name:self.projects.get(name),operators=[OPERATOR],
                memberships={'prog':{uid:'viewer'}},users={uid:{'disabled':False}})
            self.accept(operation='former-owner-inactive',ctx=inactive)
            self.assertEqual('retired',self.entry()['lifecycle'])

    def test_missing_area_owner_view_is_incomplete_until_current_roles_loaded(self):
        from unittest.mock import patch
        uid='usr_'+'c'*16;original=rr.KIND.catalog
        def owner_projection(rows,operators):
            entries,incomplete=original(rows,operators)
            for entry in entries:
                if entry.get('key')=='program.member-of':entry['accepted_by']=uid
            return entries,incomplete
        with patch.object(rr.KIND,'catalog',side_effect=owner_projection):
            self.assertFalse(self.context().usage()[1]['complete'])
            known=ir.ProgramContext('prog',lambda name:self.projects.get(name),operators=[OPERATOR],
                memberships={'area':{uid:'owner'}},users={uid:{'disabled':False}})
            self.assertTrue(known.usage()[1]['complete'])
            self.assertEqual(['area'],known.areas()[0])
