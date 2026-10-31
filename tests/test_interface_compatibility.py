"""Conservative schema proof boundaries; no tracker or external schema execution."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
KIT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(KIT))
import interface_compatibility as c


def schema(kind='object', **changes):
    return dict({'$schema':c.DIALECT,'type':kind},**changes)


def raw(value): return json.dumps(value,separators=(',',':')).encode()


def api(value=None):
    body={'content':{'application/json':{'schema':value or {'type':'string'}}}}
    return {'openapi':'3.1.0','info':{'title':'Synthetic','version':'1'},'paths':{'/items':{'post':{
        'requestBody':copy.deepcopy(body),'responses':{'200':dict(copy.deepcopy(body),description='Synthetic response')}}}}}


class CompatibilityTests(unittest.TestCase):
    def check(self, old, new, expected, **options):
        result=c.classify(raw(old),raw(new),**options)
        self.assertEqual(expected,result['classification'],result)
        self.assertEqual('compatibility-unverified' if expected=='unknown' else expected,result['compatibility'])
        return result

    def test_supported_addition_and_breaking_controls_in_both_directions(self):
        old=schema('string',minLength=2,maxLength=4);new=schema('string',minLength=1,maxLength=5)
        self.check(old,new,'additive',direction='request');self.check(old,new,'breaking',direction='response')
        self.check(new,old,'breaking',direction='request');self.check(new,old,'additive',direction='response')

    def test_required_and_additional_properties_are_directional(self):
        old=schema(properties={'a':{'type':'string'}},required=['a'],additionalProperties=False)
        new=schema(properties={'a':{'type':'string'},'b':{'type':'integer'}},additionalProperties=True)
        self.check(old,new,'additive');self.check(old,new,'breaking',direction='response')
        self.check(new,old,'breaking');self.check(new,old,'additive',direction='response')

    def test_boolean_is_not_a_json_schema_number_even_inside_finite_values(self):
        self.check(schema('boolean',const=True),schema('boolean',enum=[1]),'breaking')
        a=schema('object',const={'x':True});b=schema('object',const={'x':1})
        self.check(a,b,'breaking')

    def test_enum_const_and_integer_number_containment(self):
        self.check(schema('integer',enum=[1,2]),schema('number',enum=[1.0,2.0,3]),'additive')
        self.check(schema('number'),schema('integer'),'breaking')
        self.check(schema('integer'),schema('number'),'additive')
        self.check(schema('string',enum=['x','y']),schema('string',const='x'),'breaking')

    def test_array_items_and_limits_are_not_ignored(self):
        old=schema('array',items={'type':'integer'},maxItems=2)
        new=schema('array',items={'type':'number'},maxItems=3)
        self.check(old,new,'additive');self.check(old,new,'breaking',direction='response')

    def test_numeric_exclusive_boundaries_and_equal_spelling(self):
        self.check(schema('number',minimum=0),schema('number',exclusiveMinimum=0),'breaking')
        self.check(schema('number',exclusiveMinimum=0),schema('number',minimum=0),'additive')
        self.check(schema('number',minimum=0.0),schema('number',minimum=0),'additive')

    def test_unproved_containment_is_unknown_not_a_guessed_break(self):
        old=schema('string',minLength=2000,maxLength=4000);new=schema('string',minLength=2001,maxLength=4000)
        self.check(old,new,'unknown')

    def test_unknown_keyword_ref_yaml_dialect_and_duplicate_keys_unknown(self):
        base=schema('string')
        for change in ({'pattern':'x'},{'$ref':'#/$defs/x'},{'$dynamicRef':'x'},
                       {'anyOf':[{'type':'string'}]},{'$schema':'https://json-schema.org/draft-07/schema#'}):
            with self.subTest(change=change):self.check(base,dict(base,**change),'unknown')
        for value in (b'type: string',b'{"type":"string","type":"integer"}',b'[]',b'null',b'{"type":"number","const":NaN}'):
            with self.subTest(value=value):self.assertEqual('unknown',c.classify(raw(base),value)['classification'])
        self.check(base,base,'unknown',media_type='application/yaml')

    def test_bytes_depth_nodes_operations_budget_unknown(self):
        base=raw(schema('string'))
        self.assertEqual('unknown',c.classify(base,b' '* (c.BYTE_MAX+1))['classification'])
        deep=b'['* (c.DEPTH_MAX+1)+b'0'+b']'*(c.DEPTH_MAX+1)
        self.assertEqual('unknown',c.classify(base,deep)['classification'])
        nodes=schema('string',enum=['x']*c.NODE_MAX)
        self.assertEqual('unknown',c.classify(base,raw(nodes))['classification'])
        huge=api();huge['paths']={f'/p{i}':huge['paths']['/items'] for i in range(c.OP_MAX+1)}
        self.check(huge,huge,'unknown',media_type='application/vnd.oai.openapi+json')

    def test_comparison_budget_counts_finite_value_comparisons(self):
        finite=schema('string',enum=[str(i) for i in range(150)])
        self.check(finite,finite,'unknown')

    def test_no_network_or_execution(self):
        with patch('subprocess.run',side_effect=AssertionError('execution')),patch('socket.create_connection',side_effect=AssertionError('network')):
            self.check(schema('string'),schema('string'),'additive')
            self.check(schema('string'),dict(schema('string'),**{'$ref':'https://invalid.example/s'}),'unknown')

    def test_openapi_request_and_response_directions(self):
        old=api({'type':'string','minLength':2});new=copy.deepcopy(old)
        op=new['paths']['/items']['post'];op['requestBody']['content']['application/json']['schema']['minLength']=1
        self.check(old,new,'additive',media_type='application/vnd.oai.openapi+json')
        op['responses']['200']['content']['application/json']['schema']['minLength']=1
        self.check(old,new,'breaking',media_type='application/vnd.oai.openapi+json')

    def test_openapi_removed_operations_added_status_and_required_body(self):
        old=api();new=copy.deepcopy(old);new['paths']={}
        self.check(old,new,'breaking',media_type='application/vnd.oai.openapi+json')
        new=copy.deepcopy(old);new['paths']['/items']['post']['responses']['500']=copy.deepcopy(new['paths']['/items']['post']['responses']['200'])
        self.check(old,new,'breaking',media_type='application/vnd.oai.openapi+json')
        new=copy.deepcopy(old);new['paths']['/items']['post']['requestBody']['required']=True
        self.check(old,new,'breaking',media_type='application/vnd.oai.openapi+json')

    def test_openapi_unknown_context_prevents_an_additive_claim(self):
        old=api()
        for change in ({'security':[]},{'components':{}},{'openapi':'3.0.3'}):
            self.check(old,dict(old,**change),'unknown',media_type='application/vnd.oai.openapi+json')
        new=copy.deepcopy(old);new['paths']['/items']['parameters']=[]
        self.check(old,new,'unknown',media_type='application/vnd.oai.openapi+json')

    def test_inexact_decimal_lexemes_never_gain_compatibility_or_become_integers(self):
        # This real number is not an integer. A float-only loader rounded it to
        # 1.0 and falsely proved a request restriction additive, including OpenAPI.
        old=raw(schema('number',const='LEXICAL')).replace(b'"LEXICAL"',b'1.0000000000000001')
        self.assertEqual('unknown',c.classify(old,raw(schema('integer')))['classification'])
        old=raw(api({'type':'number','const':'LEXICAL'})).replace(b'"LEXICAL"',b'1.0000000000000001')
        self.assertEqual('unknown',c.classify(old,raw(api({'type':'integer'})),media_type='application/vnd.oai.openapi+json')['classification'])
        self.check(schema('number',minimum=0.1),schema('number',minimum=0.1),'unknown')
        self.check(schema('number',minimum=0.5),schema('number',minimum=0.25),'additive')

    def test_openapi_empty_domains_never_invent_structural_breaks(self):
        media='application/vnd.oai.openapi+json'
        impossible={'type':'string','const':7}
        old=api();old['paths']['/items']['post']['requestBody'].update(required=True)
        old['paths']['/items']['post']['requestBody']['content']['application/json']['schema']=impossible
        new=copy.deepcopy(old);new['paths']={}
        self.check(old,new,'unknown',media_type=media)
        old=api();new=copy.deepcopy(old)
        new['paths']['/items']['post']['responses']['201']={'description':'Unreachable','content':{'application/json':{'schema':impossible}}}
        self.check(old,new,'unknown',media_type=media)
        old=api(impossible);new=copy.deepcopy(old);new['paths']={}
        self.check(old,new,'unknown',media_type=media)

    def test_openapi_ambiguous_identifiers_and_invalid_context_are_unknown(self):
        media='application/vnd.oai.openapi+json';old=api();new=copy.deepcopy(old)
        new['paths']['/items']['post']['requestBody']['description']=4
        self.check(old,new,'unknown',media_type=media)
        new=copy.deepcopy(old);new['paths']['/items']['post']['operationId']='new-sdk-method'
        self.check(old,new,'unknown',media_type=media)
        new=copy.deepcopy(old);new['paths']['/items']['post']['operationId']='duplicate'
        new['paths']['/items']['put']=copy.deepcopy(new['paths']['/items']['post'])
        self.check(old,new,'unknown',media_type=media)
        new=copy.deepcopy(old);new['paths']['/items\nInjected']=new['paths'].pop('/items')
        result=self.check(old,new,'unknown',media_type=media)
        self.assertTrue(all(reason.isascii() and not any(ord(c)<32 for c in reason) for reason in result['reasons']))

    def test_each_input_budget_refuses_otherwise_supported_valid_json(self):
        base=schema('string')
        with patch.object(c,'BYTE_MAX',len(raw(base))-1):self.check(base,base,'unknown')
        nested=schema('object',properties={'item':{'type':'object','properties':{'value':{'type':'string'}}}})
        with patch.object(c,'DEPTH_MAX',3):self.check(nested,nested,'unknown')
        finite=schema('string',enum=['x','y','z'])
        with patch.object(c,'NODE_MAX',4):self.check(finite,finite,'unknown')
        document=api()
        with patch.object(c,'OP_MAX',1):self.check(document,document,'unknown',media_type='application/vnd.oai.openapi+json')

    def test_openapi_ambiguous_http_body_and_path_context_stays_unknown(self):
        media='application/vnd.oai.openapi+json';old=api()
        for method in ('get','delete','options','head','trace'):
            new=copy.deepcopy(old);new['paths']['/items'][method]=new['paths']['/items'].pop('post')
            with self.subTest(method=method):self.check(new,new,'unknown',media_type=media)
        for status in ('100','204','205','304'):
            new=copy.deepcopy(old);new['paths']['/items']['post']['responses'][status]=new['paths']['/items']['post']['responses'].pop('200')
            with self.subTest(status=status):self.check(new,new,'unknown',media_type=media)
        for path in ('/a/../b','//items','/a/./items'):
            new=copy.deepcopy(old);new['paths'][path]=new['paths'].pop('/items')
            with self.subTest(path=path):self.check(new,new,'unknown',media_type=media)

    def test_default_depth_budget_rejects_supported_nested_schema(self):
        nested=schema('string')
        for _ in range(17):nested=schema('object',properties={'value':nested})
        # Valid supported JSON; using nesting()'s default64 shortcut incorrectly
        # returned zero, bypassing this hook's explicit32-level contract.
        self.check(nested,nested,'unknown')
