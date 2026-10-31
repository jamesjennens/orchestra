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

    def apply(self,p,actor=OPERATOR,operator=False,ctx=None):
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

    def test_interface_free_capability_staff_turnover_and_retirement_never_block_interface_retirement(self):
        self.apply(payload());self.accept();record=self.entry()['record']
        uid='usr_'+'d'*16
        state={'users':{uid:{'id':uid,'disabled':False}},'memberships':{'area':{uid:'coordinator'}}}
        ctx=self.context();ctx.set_authority(state);self.area.actor=uid
        plain=cap_entry(operation='draft',operation_id='plain-coordinator',acceptance_state='accepted',acceptance=acceptance())
        cr.apply_native(plain,uid,self.area,self.path.parent/'area',operator=True,web_coordinator=True,operators=[OPERATOR])
        self.assertTrue(ctx.usage()[1]['complete'])
        # A second plain capability was retired while this person still coordinated.
        # Both its old acceptance and retirement become role-inert on departure.
        retired_plain=cap_entry(operation='draft',operation_id='plain-old',key='plain.old',
            acceptance_state='accepted',acceptance=acceptance())
        cr.apply_native(retired_plain,uid,self.area,self.path.parent/'area',operator=True,web_coordinator=True,operators=[OPERATOR])
        old_plain=cr.KIND.find_entry(self.area.rows,'plain.old',[OPERATOR])['record']
        cr.apply_native({'schema_version':1,'operation':'retire','operation_id':'plain-old-retire','key':'plain.old',
            'revision':old_plain['revision'],'record_sha256':old_plain['sha256'],'successor':plain['key'],
            'acceptance_state':'superseded','acceptance':acceptance()},uid,self.area,self.path.parent/'area',
            operator=True,web_coordinator=True,operators=[OPERATOR])
        state['memberships']['area'][uid]='contributor'
        self.assertTrue(ctx.usage()[1]['complete'])
        self.assertEqual([],ctx.usage()[0])
        self.assertIn('orphan',ir.flags(record,[],ctx.usage()[1]['complete']))
        self.area.actor=OPERATOR
        cr.apply_native(cap_entry(operation_id='plain-successor',key='plain.successor'),OPERATOR,self.area,self.path.parent/'area',operators=[OPERATOR])
        old=cr.KIND.find_entry(self.area.rows,plain['key'],[OPERATOR])['record']
        cr.apply_native({'schema_version':1,'operation':'retire','operation_id':'plain-retire','key':plain['key'],
            'revision':old['revision'],'record_sha256':old['sha256'],'successor':'plain.successor',
            'acceptance_state':'superseded','acceptance':acceptance()},OPERATOR,self.area,self.path.parent/'area',operator=True,operators=[OPERATOR])
        retired=cr.KIND.find_entry(self.area.rows,plain['key'],[OPERATOR])['record']
        with self.assertRaisesRegex(ValueError,'retired'):
            cr.apply_native(cap_entry(operation='revise',operation_id='plain-cannot-revise',revision=retired['revision']+1,
                expected_sha256=retired['sha256']),OPERATOR,self.area,self.path.parent/'area',operators=[OPERATOR])
        self.assertTrue(ctx.usage()[1]['complete'])
        self.apply(payload(operation='revise',operation_id='retire-beside-plain',revision=3,
            expected_sha256=record['sha256'],state='retired'),ctx=ctx)
        self.accept(operation='retire-beside-plain-ok',ctx=ctx)
        self.assertEqual('retired',self.entry()['lifecycle'])

    def test_consumer_capability_acceptor_role_loss_is_incomplete_and_refuses_retirement(self):
        self.apply(payload());self.accept();record=self.entry()['record']
        uid='usr_'+'d'*16
        state={'users':{uid:{'id':uid,'disabled':False}},'memberships':{'area':{uid:'coordinator'}}}
        ctx=self.context();ctx.set_authority(state);self.area.actor=uid
        cap=cap_entry(operation='draft',operation_id='person-consumer',interfaces=[{'iface':'iface:prog/iface.quotes.v1@2','role':'consumes'}],
            acceptance_state='accepted',acceptance=acceptance())
        cr.apply_native(cap,uid,self.area,self.path.parent/'area',operator=True,web_coordinator=True,operators=[OPERATOR])
        self.assertTrue(ctx.usage()[1]['complete']);self.assertEqual(1,len(ctx.usage()[0]))
        state['memberships']['area'][uid]='contributor'
        self.assertFalse(ctx.usage()[1]['complete'])
        self.apply(payload(operation='revise',operation_id='retire-role-lost',revision=3,
            expected_sha256=record['sha256'],state='retired'),ctx=ctx)
        before=copy.deepcopy(self.native.rows)
        with self.assertRaisesRegex(ValueError,'incomplete'):self.accept(operation='refuse-role-lost',ctx=ctx)
        self.assertEqual(before,self.native.rows)

    def test_accepted_major_cannot_change_replaces_with_same_defining_owner(self):
        self.apply(payload());self.accept()
        self.apply(payload(key='iface.quotes.v2',replaces='iface.quotes.v1',operation_id='replacement-major'))
        second=ir.get(self.native.rows,'iface.quotes.v2',[OPERATOR])['proposed']
        self.accept(second,operation='replacement-accept')
        current=ir.get(self.native.rows,'iface.quotes.v2',[OPERATOR])['record']
        self.apply(payload(operation='revise',key=current['key'],operation_id='replacement-change',
            revision=current['revision']+1,expected_sha256=current['sha256'],replaces=None,
            compatibility={'rule':'additive-within-major','change':'additive'}))
        draft=ir.get(self.native.rows,current['key'],[OPERATOR])['proposed'];before=copy.deepcopy(self.native.rows)
        with self.assertRaisesRegex(ValueError,'replacement'):self.accept(draft,operation='replacement-refused')
        self.assertEqual(before,self.native.rows)

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
        with patch.object(rr.KIND,'catalog',return_value=(entries,False)),patch.object(context,'coverage_state',return_value='live'):
            context.prepare()
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
            # longer an owner makes the accepted admission incomplete.
            inactive=ir.ProgramContext('prog',lambda name:self.projects.get(name),operators=[OPERATOR],
                memberships={'prog':{uid:'viewer'}},users={uid:{'disabled':False}})
            with self.assertRaisesRegex(ValueError,'incomplete'):
                self.accept(operation='former-owner-incomplete',ctx=inactive)

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

    def test_owner_takeover_cannot_replace_first_or_pending_accepted_draft(self):
        other=RefNative();self.projects['other']=other.rows
        for native,path,key,text in [(self.native,self.path,'program.area.other','Area: other\nState: in'),
            (other,self.path.parent/'other','program.member-of','Program: prog\nState: in')]:
            path.mkdir(exist_ok=True);native.seed('decision-42',issue_type='decision');native.actor=OPERATOR
            rr.apply_native({'schema_version':1,'operation':'draft','operation_id':'takeover-link-'+key,
                'key':key,'title':'Other link','statement':text,'authority':{'type':'decision','id':'decision-42'},
                'owner':'person:example','review_by':payload()['review_by'],'tags':[],'decisions':[],
                'acceptance_state':'accepted','acceptance':acceptance()},OPERATOR,native,path,operator=True,operators=[OPERATOR])
        self.assertIn('other',self.context().areas()[0])
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                if not accepted:self.apply(payload())
                else:self.accept()
                current=self.entry()['record'] if accepted else self.entry()['proposed']
                if accepted:
                    self.apply(payload(operation='revise',operation_id='pending-owner',revision=3,
                        expected_sha256=current['sha256'],compatibility={'rule':'additive-within-major','change':'additive'}))
                    current=self.entry()['proposed']
                before=copy.deepcopy(self.native.rows)
                with self.assertRaisesRegex(ValueError,'defining area'):
                    self.apply(payload(operation='revise',operation_id='takeover-'+str(accepted),
                        revision=current['revision']+1,expected_sha256=current['sha256'],owner_project='other'),
                        ctx=self.context(lambda area:area=='other'))
                self.assertEqual(before,self.native.rows)

    def test_accept_rechecks_stored_person_proposer_demotion_and_disabled_account(self):
        uid='usr_'+'d'*16
        state={'users':{uid:{'id':uid,'disabled':False}},'memberships':{'area':{uid:'coordinator'}}}
        ctx=self.context();ctx.authority_state=state
        self.apply(payload(),actor=uid,ctx=ctx)
        for role,disabled in [('contributor',False),('coordinator',True)]:
            state['memberships']['area'][uid]=role;state['users'][uid]['disabled']=disabled
            with self.subTest(role=role,disabled=disabled),self.assertRaisesRegex(ValueError,'proposer no longer coordinates'):
                self.accept(operation='live-person-'+role+str(disabled),ctx=ctx)
        state['users'][uid]['disabled']=False;state['memberships']['area'][uid]='coordinator'
        self.accept(operation='live-person-restored',ctx=ctx)
        state['memberships']['area'][uid]='contributor'
        with self.assertRaisesRegex(ValueError,'proposer no longer coordinates'):
            self.accept(p=ir.KIND.existing_revisions(ir.KIND.live_row(next(r for r in self.native.rows if r.get('id')==self.entry()['native_id']),[OPERATOR])[0])[1],operation='live-person-restored',ctx=ctx)

    def test_accept_rechecks_stored_agent_grant_and_live_issuer(self):
        uid='usr_'+'d'*16;aid='agent_'+'e'*16
        state={'users':{uid:{'id':uid,'disabled':False}},'memberships':{'area':{uid:'owner'}},
            'agents':{aid:{'id':aid,'owner':uid,'enabled':True,'projects':['area']}},
            'coordinator_grants':{'area':{aid:{'granted_by':uid,'granted_at':'2026-01-01T00:00:00Z'}}}}
        ctx=self.context();ctx.authority_state=state
        self.apply(payload(),actor=aid,ctx=ctx)
        grant=state['coordinator_grants']['area'].pop(aid)
        with self.assertRaisesRegex(ValueError,'proposer no longer coordinates'):self.accept(operation='revoked-agent',ctx=ctx)
        state['coordinator_grants']['area'][aid]=grant;state['memberships']['area'][uid]='coordinator'
        with self.assertRaisesRegex(ValueError,'proposer no longer coordinates'):self.accept(operation='demoted-issuer',ctx=ctx)
        state['memberships']['area'][uid]='owner';self.accept(operation='restored-agent',ctx=ctx)

    def test_program_project_consumer_blocks_retirement(self):
        self.apply(payload());self.accept();record=self.entry()['record']
        cap=cap_entry(operation='draft',operation_id='program-consumer',interfaces=[{'iface':'iface:iface.quotes.v1@2','role':'consumes'}],
            acceptance_state='accepted',acceptance=acceptance())
        self.native.actor=OPERATOR
        cr.apply_native(cap,OPERATOR,self.native,self.path,operator=True,operators=[OPERATOR])
        self.apply(payload(operation='revise',operation_id='retire-local-consumer',revision=3,
            expected_sha256=record['sha256'],state='retired'))
        with self.assertRaisesRegex(ValueError,'known current consumers'):self.accept(operation='local-consumer-refusal')

    def test_retirement_owner_loss_or_disabled_accepted_link_is_incomplete(self):
        from unittest.mock import patch
        self.apply(payload());self.accept();record=self.entry()['record']
        self.apply(payload(operation='revise',operation_id='retire-failed-live-side',revision=3,
            expected_sha256=record['sha256'],state='retired'))
        # Keep the defining area linked; a second admitted side loses its live owner.
        self.native.actor=OPERATOR
        rr.apply_native({'schema_version':1,'operation':'draft','operation_id':'unreadable-admitted-side',
            'key':'program.area.other','title':'Other','statement':'Area: other\nState: in',
            'authority':{'type':'decision','id':'decision-42'},'owner':'person:example',
            'review_by':payload()['review_by'],'tags':[],'decisions':[],
            'acceptance_state':'accepted','acceptance':acceptance()},OPERATOR,self.native,self.path,operator=True,operators=[OPERATOR])
        uid='usr_'+'b'*16;original=rr.KIND.catalog
        def projected(rows,operators):
            entries,inc=original(rows,operators)
            for e in entries:
                if e.get('key')=='program.area.other':e['accepted_by']=uid
            return entries,inc
        with patch.object(rr.KIND,'catalog',side_effect=projected):
            for role,disabled in [('contributor',False),('owner',True)]:
                ctx=self.context();ctx.memberships={'prog':{uid:role}};ctx.users={uid:{'disabled':disabled}}
                with self.subTest(role=role,disabled=disabled),self.assertRaisesRegex(ValueError,'incomplete'):
                    self.accept(operation='failed-live-side-'+role,ctx=ctx)

    def test_v1_capability_rejects_v2_field_and_additive_stays_unverified(self):
        record=cr.entry_record(cap_entry(),1,'draft');record['interfaces']=[]
        record['sha256']=content_hash(record)
        with self.assertRaises(ValueError):cr.validate_entry(record)
        self.apply(payload());self.accept();old=self.entry()['record']
        self.apply(payload(operation='revise',operation_id='additive-unverified',revision=3,
            expected_sha256=old['sha256'],compatibility={'rule':'additive-within-major','change':'additive'}))
        self.accept(operation='accept-additive-unverified')
        self.assertEqual('compatibility-unverified',self.entry()['compatibility'])
        self.assertIn('compatibility-unverified',ir.flags(self.entry()['record'],[],True))

    def test_unreadable_area_does_not_assert_provider_absence_flags(self):
        record=ir.entry_record(payload(),2,'accepted')
        consumer={'role':'consumes','key':record['key'],'minimum':2}
        self.assertIn('provider-behind',ir.flags(record,[consumer],True))
        self.assertNotIn('provider-behind',ir.flags(record,[consumer],False))
        self.apply(payload());self.accept();self.projects['area']=None
        self.assertFalse(self.context().usage()[1]['complete'])

    def test_provider_pin_without_live_accepted_revision_does_not_count(self):
        self.apply(payload());self.accept()
        self.area.actor=OPERATOR
        cap=cap_entry(operation='draft',operation_id='unaccepted-provider-pin',interfaces=[{'iface':'iface:prog/iface.quotes.v1@99','role':'provides'}],
            acceptance_state='accepted',acceptance=acceptance())
        cr.apply_native(cap,OPERATOR,self.area,self.path.parent/'area',operator=True,operators=[OPERATOR])
        links,coverage=self.context().usage();self.assertTrue(coverage['complete']);self.assertEqual([],links)


    def test_operator_reconcile_completes_pending_second_leg_of_completed_accept(self):
        from unittest.mock import patch
        self.apply(payload());self.accept()
        self.apply(payload(key='iface.quotes.v2',replaces='iface.quotes.v1',operation_id='recover-major2'))
        proposed=ir.get(self.native.rows,'iface.quotes.v2',[OPERATOR])['proposed']
        original=self.native.__class__.__call__
        def cut(native,args):
            if args[:2]==['comments','add'] and len(args)>3 and args[3].startswith(ir.ENTRY_PREFIX):
                record=ir.parse_entry(args[3])
                if record and record['state']=='deprecated':raise RuntimeError('cut before predecessor revision')
            return original(native,args)
        with patch.object(self.native.__class__,'__call__',cut):
            with self.assertRaisesRegex(RuntimeError,'cut before'):self.accept(proposed,operation='recover-accept2')
        self.native.actor=OPERATOR
        with ir.context(self.context()):
            result=ir.reconcile(self.path,'recover-accept2',OPERATOR,'finish state-only leg','complete',self.native,operators=[OPERATOR])
        self.assertTrue(result['transition_complete']);self.assertEqual('deprecated',self.entry()['record']['state'])
        count=len(self.native.writes())
        with ir.context(self.context()):
            ir.reconcile(self.path,'recover-accept2',OPERATOR,'finish state-only leg','complete',self.native,operators=[OPERATOR])
        self.assertEqual(count,len(self.native.writes()))

    def test_retirement_member_of_owner_demotion_or_disabled_acceptor_is_incomplete(self):
        from unittest.mock import patch
        self.apply(payload());self.accept();record=self.entry()['record']
        other=RefNative();self.projects['other']=other.rows
        for native,path,key,text in [(self.native,self.path,'program.area.other','Area: other\nState: in'),
            (other,self.path.parent/'other','program.member-of','Program: prog\nState: in')]:
            path.mkdir(exist_ok=True);native.seed('decision-42',issue_type='decision');native.actor=OPERATOR
            rr.apply_native({'schema_version':1,'operation':'draft','operation_id':'accepted-side-'+key,
                'key':key,'title':'Other link','statement':text,'authority':{'type':'decision','id':'decision-42'},
                'owner':'person:example','review_by':payload()['review_by'],'tags':[],'decisions':[],
                'acceptance_state':'accepted','acceptance':acceptance()},OPERATOR,native,path,operator=True,operators=[OPERATOR])
        self.apply(payload(operation='revise',operation_id='retire-failed-member-of',revision=3,
            expected_sha256=record['sha256'],state='retired'))
        uid='usr_'+'b'*16;original=rr.KIND.catalog
        def projected(rows,operators):
            entries,inc=original(rows,operators)
            if rows is other.rows:
                for e in entries:
                    if e.get('key')=='program.member-of':e['accepted_by']=uid
            return entries,inc
        with patch.object(rr.KIND,'catalog',side_effect=projected):
            for role,disabled in [('contributor',False),('owner',True)]:
                ctx=self.context();ctx.memberships={'other':{uid:role}};ctx.users={uid:{'disabled':disabled}}
                with self.subTest(role=role,disabled=disabled),self.assertRaisesRegex(ValueError,'incomplete'):
                    self.accept(operation='failed-member-of-'+role,ctx=ctx)

    def test_only_stored_defining_area_coordinator_can_revise_same_owner(self):
        self.apply(payload());draft=self.entry()['proposed']
        with self.assertRaisesRegex(ValueError,'coordination'):
            self.apply(payload(operation='revise',operation_id='same-owner-wrong-coordinator',revision=2,
                expected_sha256=draft['sha256']),ctx=self.context(lambda area:False))
        self.assertEqual(draft,self.entry()['proposed'])

    def test_capability_mutex_selection_includes_implicit_withdrawals_and_pinned_accept(self):
        native=RefNative();native.actor=OPERATOR
        path=self.path.parent/'caps';path.mkdir()
        plain=cap_entry()
        self.assertFalse(cr.touches_interfaces(plain,native,[OPERATOR]))
        v2=cap_entry(operation='draft',operation_id='cap-mutex-v2',interfaces=[],acceptance_state='accepted',acceptance=acceptance())
        cr.apply_native(v2,OPERATOR,native,path,operator=True,operators=[OPERATOR])
        self.assertTrue(cr.touches_interfaces(dict(plain,operation='revise'),native,[OPERATOR]))
        self.assertTrue(cr.touches_interfaces(dict(plain,operation='accept'),native,[OPERATOR]))
        self.assertTrue(cr.touches_interfaces(dict(plain,operation='retire'),native,[OPERATOR]))

    def test_legacy_stored_draft_owner_disagreement_cannot_be_accepted(self):
        made=self.apply(payload());draft=self.entry()['proposed']
        changed=copy.deepcopy(draft);changed.update(revision=2,owner_project='other')
        changed['sha256']=content_hash(changed)
        # Simulate a pre-fix owner takeover already present in native history.
        self.native.actor=OPERATOR
        self.native(['comments','add',made['native_id'],ir.KIND.entry_comment(changed),'--json'])
        with self.assertRaisesRegex(ValueError,'Stored interface revisions disagree'):
            self.accept(changed,operation='legacy-takeover-refused')
        self.assertIsNone(self.entry()['record'])


    def test_malformed_batch_mutex_preflight_leaves_validation_to_writer_without_reads(self):
        def forbidden(args):raise AssertionError('malformed batch performed a native read')
        for items in (None,'bad',[None],['bad'],[1]):
            with self.subTest(items=items):
                self.assertTrue(cr.touches_interfaces({'items':items},forbidden,[OPERATOR]))

    def test_removed_operator_consumer_acceptance_makes_retirement_incomplete(self):
        self.apply(payload());self.accept();record=self.entry()['record']
        self.area.actor='ops2'
        cap=cap_entry(operation='draft',operation_id='ops2-consumer',interfaces=[{'iface':'iface:prog/iface.quotes.v1@2','role':'consumes'}],
            acceptance_state='accepted',acceptance=acceptance())
        cr.apply_native(cap,'ops2',self.area,self.path.parent/'area',operator=True,operators=[OPERATOR,'ops2'])
        before=self.context();before.operators=[OPERATOR,'ops2']
        self.assertTrue(before.usage()[1]['complete']);self.assertEqual(1,len(before.usage()[0]))
        self.apply(payload(operation='revise',operation_id='retire-inert-cap',revision=3,
            expected_sha256=record['sha256'],state='retired'))
        self.assertFalse(self.context().usage()[1]['complete'])
        with self.assertRaisesRegex(ValueError,'incomplete'):self.accept(operation='refuse-inert-cap')

    def test_removed_operator_bilateral_link_acceptances_make_retirement_incomplete(self):
        self.apply(payload());self.accept();record=self.entry()['record']
        other=RefNative();self.projects['other']=other.rows
        for native,path,key,text in [(self.native,self.path,'program.area.other','Area: other\nState: in'),
            (other,self.path.parent/'other','program.member-of','Program: prog\nState: in')]:
            path.mkdir(exist_ok=True);native.seed('decision-42',issue_type='decision');native.actor='ops2'
            rr.apply_native({'schema_version':1,'operation':'draft','operation_id':'ops2-side-'+key,
                'key':key,'title':'Other link','statement':text,'authority':{'type':'decision','id':'decision-42'},
                'owner':'person:example','review_by':payload()['review_by'],'tags':[],'decisions':[],
                'acceptance_state':'accepted','acceptance':acceptance()},'ops2',native,path,operator=True,operators=[OPERATOR,'ops2'])
        before=self.context();before.operators=[OPERATOR,'ops2'];self.assertIn('other',before.areas()[0])
        self.apply(payload(operation='revise',operation_id='retire-inert-sides',revision=3,
            expected_sha256=record['sha256'],state='retired'))
        self.assertFalse(self.context().usage()[1]['complete'])
        with self.assertRaisesRegex(ValueError,'incomplete'):self.accept(operation='refuse-inert-sides')

    def test_replaces_another_area_is_refused_independently_of_entry_owner_changes(self):
        self.apply(payload());self.accept()
        other=RefNative();self.projects['other']=other.rows
        for native,path,key,text in [(self.native,self.path,'program.area.other','Area: other\nState: in'),
            (other,self.path.parent/'other','program.member-of','Program: prog\nState: in')]:
            path.mkdir(exist_ok=True);native.seed('decision-42',issue_type='decision');native.actor=OPERATOR
            rr.apply_native({'schema_version':1,'operation':'draft','operation_id':'replace-other-'+key,
                'key':key,'title':'Other link','statement':text,'authority':{'type':'decision','id':'decision-42'},
                'owner':'person:example','review_by':payload()['review_by'],'tags':[],'decisions':[],
                'acceptance_state':'accepted','acceptance':acceptance()},OPERATOR,native,path,operator=True,operators=[OPERATOR])
        self.apply(payload(key='iface.quotes.v2',owner_project='other',replaces='iface.quotes.v1',operation_id='other-major'))
        new=ir.get(self.native.rows,'iface.quotes.v2',[OPERATOR])['proposed']
        before=copy.deepcopy(self.native.rows)
        with self.assertRaises(ValueError):self.accept(new,operation='reject-other-predecessor')
        self.assertEqual(before,self.native.rows,'replaces from another area must refuse before committing the new major')

    @unittest.skipIf(__import__('os').name=='nt','endpoint is POSIX-only')
    def test_endpoint_flags_use_caller_read_gate_and_never_read_hidden_area_counting_backend(self):
        import endpoint
        from unittest.mock import patch
        from types import SimpleNamespace
        self.apply(payload());self.accept()
        self.native.actor=OPERATOR
        cap=cap_entry(operation='draft',operation_id='counted-program-consumer',interfaces=[{'iface':'iface:iface.quotes.v1@2','role':'consumes'}],
            acceptance_state='accepted',acceptance=acceptance())
        cr.apply_native(cap,OPERATOR,self.native,self.path,operator=True,operators=[OPERATOR])
        root=self.path.parent
        for project in ('prog','area'):
            (root/project/'.beads').mkdir(exist_ok=True)
            (root/project/'.beads/metadata.json').write_text('{}')
        (root/'deployment.private.json').write_text(json.dumps({'password':'synthetic','unit':'none','operators':[OPERATOR]}))
        calls=[]
        def argv(root,path,actor,args,**kwargs):return [Path(path).name,*args]
        def run(command,env,**kwargs):
            project,*args=command;calls.append(project)
            rows=self.projects[project]
            if args==['export','--all']:stdout='\n'.join(json.dumps(row) for row in rows)+'\n'
            else:stdout=(self.native if project=='prog' else self.area)(args)
            return SimpleNamespace(returncode=0,stdout=stdout,stderr='')
        request={'project':'prog','actor':OPERATOR,'action':'interface','args':['list']}
        with patch.object(endpoint,'project_dir',side_effect=lambda root,name:root/name), \
             patch.object(endpoint,'environment',return_value={}), \
             patch.object(endpoint.native,'argv',side_effect=argv),patch.object(endpoint.native,'run',side_effect=run):
            hidden=json.loads(endpoint.execute(root,request,key_projects={'prog'})['stdout'])
            self.assertFalse(hidden['program_coverage']['complete']);self.assertNotIn('area',calls)
            self.assertNotIn('no-provider',hidden['items'][0]['flags'])
            self.assertNotIn('provider-behind',hidden['items'][0]['flags'])
            self.assertNotIn('orphan',hidden['items'][0]['flags'])
            calls.clear()
            visible=json.loads(endpoint.execute(root,request,key_projects={'prog','area'})['stdout'])
            self.assertTrue(visible['program_coverage']['complete']);self.assertEqual(1,calls.count('area'))
            self.assertIn('no-provider',visible['items'][0]['flags'])

    def test_frozen_graph_rechecks_cached_read_gate_without_backend_io_under_authority(self):
        self.apply(payload());self.accept();record=self.entry()['record'];calls=[];authority=[False]
        def read(project):
            self.assertFalse(authority[0],'a graph read acquired an area under authority')
            calls.append(project);return copy.deepcopy(self.projects.get(project))
        ctx=ir.ProgramContext('prog',read,operators=[OPERATOR],may_read=lambda project:True).prepare()
        self.assertEqual(['prog','area'],calls)
        authority[0]=True;ctx.accept(record)
        ctx.may_read=lambda project:project!='area'
        self.assertFalse(ctx.usage()[1]['complete']);self.assertEqual(['prog','area'],calls)
        self.assertIsNone(ctx.rows('newly-readable-area'));self.assertEqual(['prog','area'],calls)

    def test_explicit_operator_deprecation_recovery_after_original_proposer_demotion(self):
        from unittest.mock import patch
        uid='usr_'+'d'*16
        state={'users':{uid:{'id':uid,'disabled':False}},'memberships':{'area':{uid:'coordinator'}}}
        ctx=self.context();ctx.set_authority(state)
        self.apply(payload(),actor=uid,ctx=ctx);self.accept(ctx=ctx)
        self.apply(payload(key='iface.quotes.v2',replaces='iface.quotes.v1',operation_id='demoted-recovery-major'),actor=uid,ctx=ctx)
        proposed=ir.get(self.native.rows,'iface.quotes.v2',[OPERATOR])['proposed']
        original=self.native.__class__.__call__
        def cut(native,args):
            if args[:2]==['comments','add'] and len(args)>3 and args[3].startswith(ir.ENTRY_PREFIX):
                record=ir.parse_entry(args[3])
                if record and record['state']=='deprecated':raise RuntimeError('deprecation cut')
            return original(native,args)
        with patch.object(self.native.__class__,'__call__',cut):
            with self.assertRaisesRegex(RuntimeError,'deprecation cut'):self.accept(proposed,operation='demoted-recovery-accept',ctx=ctx)
        state['memberships']['area'][uid]='contributor';ctx.set_authority(state)
        with self.assertRaisesRegex(ValueError,'proposer no longer coordinates'):
            self.accept(proposed,operation='demoted-recovery-accept',ctx=ctx)
        before=self.entry()['record'];self.native.actor=OPERATOR
        denied=self.context();denied.operators=[]
        rows=copy.deepcopy(self.native.rows)
        with ir.context(denied),self.assertRaisesRegex(ValueError,'currently configured operator'):
            ir.reconcile(self.path,'demoted-recovery-accept',OPERATOR,'refused stale operator','complete',self.native,operators=[OPERATOR])
        self.assertEqual(rows,self.native.rows)
        with ir.context(ctx):
            result=ir.reconcile(self.path,'demoted-recovery-accept',OPERATOR,'explicit program recovery','complete',self.native,operators=[OPERATOR])
        self.assertTrue(result['transition_complete']);after=self.entry()['record']
        self.assertEqual('deprecated',after['state'])
        for field in ir.CONTENT_FIELDS:
            if field!='state':self.assertEqual(before[field],after[field],field)
        self.assertTrue(any('explicit program recovery' in c.get('text','') for row in self.native.rows for c in row.get('comments',[])))
        writes=len(self.native.writes())
        with ir.context(ctx):ir.reconcile(self.path,'demoted-recovery-accept',OPERATOR,'explicit program recovery','complete',self.native,operators=[OPERATOR])
        self.assertEqual(writes,len(self.native.writes()))

    def test_withdrawal_requires_live_approval_after_operator_removal_and_can_be_reaffirmed(self):
        self.apply(payload());self.accept();record=self.entry()['record'];self.area.actor='ops2'
        initial=cap_entry(operation='draft',operation_id='withdrawable-op2-consumer',interfaces=[{'iface':'iface:prog/iface.quotes.v1@2','role':'consumes'}],
            acceptance_state='accepted',acceptance=acceptance())
        cr.apply_native(initial,'ops2',self.area,self.path.parent/'area',operator=True,operators=[OPERATOR,'ops2'])
        current=cr.KIND.find_entry(self.area.rows,initial['key'],[OPERATOR,'ops2'])['record']
        cr.apply_native(cap_entry(operation='revise',operation_id='withdraw-op2',revision=2,expected_sha256=current['sha256'],interfaces=[]),
            'ops2',self.area,self.path.parent/'area',operators=[OPERATOR,'ops2'])
        draft=cr.KIND.find_entry(self.area.rows,initial['key'],[OPERATOR,'ops2'])['proposed']
        cr.apply_native({'schema_version':1,'operation':'accept','operation_id':'accept-withdraw-op2','key':initial['key'],
            'revision':draft['revision'],'record_sha256':draft['sha256'],'acceptance_state':'accepted','acceptance':acceptance()},
            'ops2',self.area,self.path.parent/'area',operator=True,operators=[OPERATOR,'ops2'])
        before=self.context();before.operators=[OPERATOR,'ops2']
        self.assertTrue(before.usage()[1]['complete']);self.assertEqual([],before.usage()[0])
        self.assertFalse(self.context().usage()[1]['complete'],'an inert withdrawal cannot hide a formerly accepted consumer')
        withdrawn=cr.KIND.find_entry(self.area.rows,initial['key'],[OPERATOR,'ops2'])['record']
        self.area.actor=OPERATOR
        cr.apply_native({'schema_version':1,'operation':'accept','operation_id':'reaffirm-withdrawal','key':initial['key'],
            'revision':withdrawn['revision'],'record_sha256':withdrawn['sha256'],'acceptance_state':'accepted','acceptance':acceptance()},
            OPERATOR,self.area,self.path.parent/'area',operator=True,operators=[OPERATOR])
        self.assertTrue(self.context().usage()[1]['complete']);self.assertEqual([],self.context().usage()[0])
        self.apply(payload(operation='revise',operation_id='retire-after-real-withdrawal',revision=3,
            expected_sha256=record['sha256'],state='retired'));self.accept(operation='real-withdrawal-retire')
        self.assertEqual('retired',self.entry()['lifecycle'])

    def test_draft_snapshot_reads_only_defining_pair_but_retirement_inventories_all_areas(self):
        self.apply(payload());self.accept();record=self.entry()['record']
        other=RefNative();self.projects['other']=other.rows
        for native,path,key,text in [(self.native,self.path,'program.area.other','Area: other\nState: in'),
            (other,self.path.parent/'other','program.member-of','Program: prog\nState: in')]:
            path.mkdir(exist_ok=True);native.seed('decision-42',issue_type='decision');native.actor=OPERATOR
            rr.apply_native({'schema_version':1,'operation':'draft','operation_id':'snapshot-scope-'+key,
                'key':key,'title':'Other link','statement':text,'authority':{'type':'decision','id':'decision-42'},
                'owner':'person:example','review_by':payload()['review_by'],'tags':[],'decisions':[],
                'acceptance_state':'accepted','acceptance':acceptance()},OPERATOR,native,path,operator=True,operators=[OPERATOR])
        calls=[]
        def read(project):calls.append(project);return copy.deepcopy(self.projects.get(project))
        draft=payload(key='iface.new.v1',operation_id='new-snapshot')
        ctx=ir.ProgramContext('prog',read,operators=[OPERATOR]).prepare(draft)
        ctx.propose(draft);self.assertEqual(['prog','area'],calls)
        self.apply(payload(operation='revise',operation_id='snapshot-retirement',revision=3,
            expected_sha256=record['sha256'],state='retired'))
        calls.clear();target=self.entry()['proposed']
        ctx=ir.ProgramContext('prog',read,operators=[OPERATOR]).prepare({'operation':'accept','key':target['key'],'revision':target['revision']})
        ctx.accept(target);self.assertEqual(['prog','area','other'],calls)

    @unittest.skipIf(__import__('os').name=='nt','endpoint is POSIX-only')
    def test_endpoint_prepares_foreign_snapshots_before_authority_and_does_no_project_io_in_effect(self):
        self._endpoint_snapshot_probe()

    @unittest.skipIf(__import__('os').name=='nt','endpoint is POSIX-only')
    def test_endpoint_refreshes_authority_after_snapshots_before_effect(self):
        self._endpoint_snapshot_probe(revoke=True)

    def _endpoint_snapshot_probe(self,revoke=False):
        import endpoint,http_authority,contextlib
        from unittest.mock import patch
        from types import SimpleNamespace
        root=self.path.parent;uid='usr_'+'d'*16;inside=[False];exports=[]
        for project in ('prog','area'):
            (root/project/'.beads').mkdir(exist_ok=True);(root/project/'.beads/metadata.json').write_text('{}')
        state={'users':{uid:{'id':uid,'disabled':False}},'memberships':{'prog':{uid:'coordinator'},'area':{uid:'coordinator'}}}
        current=[state]
        before=copy.deepcopy(self.native.rows)
        config=SimpleNamespace(store=str(root/'state.json'),lock=str(root/'state.json.lock'))
        body=payload()
        for field in ('operation',):body.pop(field)
        request={'project':'prog','actor':uid,'action':'interface','args':['propose','@attachment:p'],
            'attachments':{'p':{'flag':'--file','text':json.dumps(body)}},'authority':{'via':'session','user_id':uid}}
        original_lock=endpoint.held_coordination_lock
        @contextlib.contextmanager
        def project_lock(path,shared=False):
            self.assertFalse(inside[0],'project lock acquired under authority')
            with original_lock(path,shared=shared):yield
        def argv(root,path,actor,args,**kwargs):return [Path(path).name,actor,*args]
        def run(command,env,**kwargs):
            project,actor,*args=command;native=self.native if project=='prog' else self.area;native.actor=actor
            if args[:1]==['export']:
                self.assertFalse(inside[0],'project snapshot read under authority');exports.append(project)
            return SimpleNamespace(returncode=0,stdout=native(args),stderr='')
        def guarded(root,req,journal,fn,**kwargs):
            inside[0]=True
            try:
                kwargs['identity_check']()
                if revoke:
                    current[0]=copy.deepcopy(state)
                    current[0]['memberships']['area'][uid]='contributor'
                return fn()
            finally:inside[0]=False
        with contextlib.ExitStack() as stack:
            for name in ('http_actor_denial','descriptor_actor_denial','block_mismatch_denial'):
                stack.enter_context(patch.object(endpoint,name,return_value=None))
            stack.enter_context(patch.object(endpoint,'verified_web_authority',return_value=True))
            stack.enter_context(patch.object(endpoint,'configured_coordinator_rules',return_value={}))
            stack.enter_context(patch.object(endpoint,'configured_operators',return_value=[OPERATOR]))
            stack.enter_context(patch.object(endpoint,'project_dir',side_effect=lambda root,name:root/name))
            stack.enter_context(patch.object(endpoint,'environment',return_value={}))
            stack.enter_context(patch.object(endpoint,'held_coordination_lock',side_effect=project_lock))
            stack.enter_context(patch.object(endpoint,'guarded_write',side_effect=guarded))
            stack.enter_context(patch.object(http_authority,'read_state',side_effect=lambda *a:current[0]))
            stack.enter_context(patch.object(http_authority,'decide',return_value={}))
            stack.enter_context(patch.object(endpoint.native,'argv',side_effect=argv))
            stack.enter_context(patch.object(endpoint.native,'run',side_effect=run))
            result=endpoint.execute(root,request,authority_config=config,require_authority=True)
        if revoke:
            self.assertNotEqual(0,result['returncode'],result)
            self.assertIn('coordination',result['stderr'])
            self.assertEqual(before,self.native.rows)
        else:self.assertEqual(0,result['returncode'],result)
        self.assertEqual(['prog','area'],exports)
