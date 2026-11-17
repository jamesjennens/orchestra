"""Bounded compatibility proofs for the explicitly supported schema subset.

No source loading, references, schema extension execution, or network access.
Request compatibility proves OLD values are accepted by NEW; response compatibility
proves NEW values are accepted by OLD. Failure to prove containment is unknown
unless a bounded concrete counterexample proves a break.
"""
import hashlib
from decimal import Decimal,InvalidOperation
import math
import re
import record_json
from export_requirements import _unique
from requirements import canonical_bytes

# Earlier evidence can claim additive for a removed OpenAPI response code.
VERSION = 'interface-subset-2'
DIALECT = 'https://json-schema.org/draft/2020-12/schema'
BYTE_MAX, DEPTH_MAX, NODE_MAX, OP_MAX, STEP_MAX = 1048576, 32, 10000, 1000, 10000
TYPES = ('object', 'array', 'string', 'number', 'integer', 'boolean', 'null')
OBJECT_SAMPLE_MAX = 64
ANNOTATIONS = {'$schema', 'title', 'description', '$comment'}
FACETS = {'object': {'properties', 'required', 'additionalProperties'},
          'array': {'items', 'minItems', 'maxItems'}, 'string': {'minLength', 'maxLength'},
          'number': {'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'},
          'integer': {'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'},
          'boolean': set(), 'null': set()}


class Unsupported(ValueError):
    pass


class Budget:
    def __init__(self): self.left = STEP_MAX
    def tick(self, amount=1):
        self.left -= amount
        if self.left < 0: raise Unsupported('comparison-budget')


def exact_float(text):
    value=float(text)
    try:
        if not math.isfinite(value) or Decimal(text)!=Decimal.from_float(value):
            raise Unsupported('inexact-number-unsupported')
    except (InvalidOperation,ValueError,OverflowError):
        raise Unsupported('inexact-number-unsupported') from None
    return value


def decode(raw):
    if not isinstance(raw, bytes) or len(raw) > BYTE_MAX: raise Unsupported('byte-budget')
    text = raw.decode('utf-8')
    if record_json.nesting(text, DEPTH_MAX) > DEPTH_MAX: raise Unsupported('depth-budget')
    value = record_json.loads(text, object_pairs_hook=_unique, parse_float=exact_float)
    todo, count = [value], 0
    while todo:
        item = todo.pop(); count += 1
        if count > NODE_MAX: raise Unsupported('node-budget')
        if isinstance(item, dict):
            if '$ref' in item or '$dynamicRef' in item: raise Unsupported('references-unsupported')
            todo.extend(item.values())
        elif isinstance(item, list): todo.extend(item)
        elif isinstance(item, float) and not math.isfinite(item): raise Unsupported('nonfinite-number')
    canonical_bytes(value)  # Unicode/non-finite validation AFTER the node budget.
    return value


def validate(schema, budget):
    budget.tick()
    if not isinstance(schema, dict) or schema.get('type') not in TYPES: raise Unsupported('simple-type-required')
    kind = schema['type']
    if set(schema) - (ANNOTATIONS | FACETS[kind] | {'type', 'enum', 'const'}): raise Unsupported('unsupported-keyword')
    if any(not isinstance(schema[k], str) for k in ANNOTATIONS if k in schema): raise Unsupported('invalid-annotation')
    if '$schema' in schema and schema['$schema'] != DIALECT: raise Unsupported('unsupported-dialect')
    if 'enum' in schema and (not isinstance(schema['enum'], list) or not schema['enum']): raise Unsupported('invalid-enum')
    if 'enum' in schema and 'const' in schema: raise Unsupported('ambiguous-finite-set')
    if kind == 'object':
        props = schema.get('properties', {})
        required = schema.get('required', [])
        if not isinstance(props, dict) or not isinstance(required, list) or any(not isinstance(v, str) for v in required): raise Unsupported('invalid-object')
        if len(set(required)) != len(required) or not set(required) <= set(props): raise Unsupported('unsupported-required')
        if type(schema.get('additionalProperties', True)) is not bool: raise Unsupported('schema-additional-properties')
        for value in props.values(): validate(value, budget)
    if kind == 'array' and 'items' in schema: validate(schema['items'], budget)
    for name in FACETS[kind] - {'properties', 'required', 'additionalProperties', 'items'}:
        if name not in schema: continue
        value = schema[name]
        if kind in ('string', 'array'):
            if type(value) is not int or value < 0: raise Unsupported('invalid-length-bound')
        elif type(value) not in (int, float) or not math.isfinite(value): raise Unsupported('invalid-number-bound')
    for low, high in (('minLength', 'maxLength'), ('minItems', 'maxItems')):
        if schema.get(low, 0) > schema.get(high, math.inf): raise Unsupported('contradictory-length-bounds')


def json_equal(a, b, budget):
    budget.tick(1 + (len(a) if isinstance(a, (dict, list, str)) else 0) + (len(b) if isinstance(b, (dict, list, str)) else 0))
    # JSON Schema distinguishes booleans from numbers, including recursively.
    if type(a) is bool or type(b) is bool: return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict): return set(a) == set(b) and all(json_equal(a[k], b[k], budget) for k in a)
    if isinstance(a, list) and isinstance(b, list): return len(a) == len(b) and all(json_equal(x, y, budget) for x, y in zip(a, b))
    return type(a) is type(b) and a == b or type(a) in (int, float) and type(b) in (int, float) and a == b


def accepts(s, value, budget):
    budget.tick(1 + (len(value) if isinstance(value, (dict, list, str)) else 0)); kind = s['type']
    if kind in ('number', 'integer'):
        if type(value) not in (int, float) or not math.isfinite(value) or kind == 'integer' and value != int(value): return False
    elif not {'object': lambda: isinstance(value, dict), 'array': lambda: isinstance(value, list),
              'string': lambda: isinstance(value, str), 'boolean': lambda: type(value) is bool,
              'null': lambda: value is None}[kind](): return False
    if 'const' in s and not json_equal(value, s['const'], budget): return False
    if 'enum' in s and not any(json_equal(value, v, budget) for v in s['enum']): return False
    if kind in ('number', 'integer'):
        for name, check in [('minimum', lambda a, b: a >= b), ('maximum', lambda a, b: a <= b),
                            ('exclusiveMinimum', lambda a, b: a > b), ('exclusiveMaximum', lambda a, b: a < b)]:
            if name in s and not check(value, s[name]): return False
    if kind in ('string', 'array'):
        low, high = ('minLength', 'maxLength') if kind == 'string' else ('minItems', 'maxItems')
        if not s.get(low, 0) <= len(value) <= s.get(high, math.inf): return False
    if kind == 'object':
        if not set(s.get('required', [])) <= set(value): return False
        props = s.get('properties', {})
        if not s.get('additionalProperties', True) and set(value) - set(props): return False
        if any(k in props and not accepts(props[k], v, budget) for k, v in value.items()): return False
    if kind == 'array' and 'items' in s and any(not accepts(s['items'], v, budget) for v in value): return False
    return True


def interval(s):
    low = max((s.get('minimum', -math.inf), True), (s.get('exclusiveMinimum', -math.inf), False), key=lambda x: (x[0], not x[1]))
    high = min((s.get('maximum', math.inf), True), (s.get('exclusiveMaximum', math.inf), False), key=lambda x: (x[0], x[1]))
    return low, high


def includes(wide, narrow, budget):
    """Sufficient containment proof only; False does not itself prove a break."""
    budget.tick()
    finite = [narrow['const']] if 'const' in narrow else narrow.get('enum')
    if finite is not None: return all(not accepts(narrow, v, budget) or accepts(wide, v, budget) for v in finite)
    if 'enum' in wide or 'const' in wide: return False
    if wide['type'] != narrow['type'] and (wide['type'], narrow['type']) != ('number', 'integer'): return False
    kind = narrow['type']
    if kind in ('number', 'integer'):
        (wl, wi), (wh, wj) = interval(wide); (nl, ni), (nh, nj) = interval(narrow)
        return (wl < nl or wl == nl and (wi or not ni)) and (wh > nh or wh == nh and (wj or not nj))
    if kind in ('string', 'array'):
        lo, hi = ('minLength', 'maxLength') if kind == 'string' else ('minItems', 'maxItems')
        if wide.get(lo, 0) > narrow.get(lo, 0) or wide.get(hi, math.inf) < narrow.get(hi, math.inf): return False
        if kind == 'array' and 'items' in wide and narrow.get('maxItems', math.inf) != 0:
            return 'items' in narrow and includes(wide['items'], narrow['items'], budget)
    if kind == 'object':
        wp, np = wide.get('properties', {}), narrow.get('properties', {})
        if not set(wide.get('required', [])) <= set(narrow.get('required', [])): return False
        if not wide.get('additionalProperties', True) and (narrow.get('additionalProperties', True) or set(np) - set(wp)): return False
        for key, value in wp.items():
            if key not in np:
                if narrow.get('additionalProperties', True): return False
            elif not includes(value, np[key], budget): return False
    return True


def samples(s, budget):
    """Small bounded witness pool; no inferred break without checking the witness."""
    budget.tick()
    if 'const' in s: return [s['const']]
    if 'enum' in s: return s['enum'][:32]
    kind = s['type']
    if kind == 'null': return [None]
    if kind == 'boolean': return [False, True]
    if kind in ('integer', 'number'):
        values = [0, 1, -1, 0.5]
        for key in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'):
            if key in s: values.extend([s[key], s[key] - 1, s[key] + 1])
        return values
    if kind == 'string':
        lengths = {0, 1, s.get('minLength', 0), s.get('maxLength', 1)}
        return ['x' * n for n in sorted(lengths) if n <= 1024]
    if kind == 'array':
        values = samples(s['items'], budget) if 'items' in s else [None]
        lengths = {0, 1, s.get('minItems', 0), s.get('maxItems', 1)}
        return [[v] * n for n in sorted(lengths) if n <= 16 for v in values[:4]]
    props = s.get('properties', {}); base = {}
    for key in s.get('required', []):
        found = [v for v in samples(props[key], budget) if accepts(props[key], v, budget)]
        if not found: return []
        budget.tick()
        base[key] = found[0]
    values = [base]
    for key, child in props.items():
        for v in samples(child, budget)[:4]:
            budget.tick(len(base) + 1)
            values.append(dict(base, **{key: v}))
            if len(values) == OBJECT_SAMPLE_MAX:
                return values
    if s.get('additionalProperties', True):
        key = '__extra'
        while key in props: key += '_'
        budget.tick(len(base) + 1)
        values.append(dict(base, **{key: None}))
    return values


def compare_schema(old, new, direction, budget, evidence=None):
    narrow, wide = (old, new) if direction == 'request' else (new, old)
    if includes(wide, narrow, budget):
        if evidence is not None and len(evidence)<7:evidence.append(direction+': supported containment proof')
        return 'additive'
    for value in samples(narrow, budget) + samples(wide, budget):
        if accepts(narrow, value, budget) and not accepts(wide, value, budget):
            if evidence is not None and len(evidence)<7:evidence.append(direction+': checked counterexample sha256 '+hashlib.sha256(canonical_bytes(value)).hexdigest())
            return 'breaking'
    return 'unknown'



def openapi_operations(doc, budget):
    if not isinstance(doc, dict) or doc.get('openapi') not in ('3.1.0', '3.1.1'):
        raise Unsupported('openapi-3.1-required')
    if set(doc) - {'openapi', 'info', 'paths', 'jsonSchemaDialect'}:
        raise Unsupported('unsupported-openapi-context')
    if doc.get('jsonSchemaDialect', DIALECT) != DIALECT:
        raise Unsupported('unsupported-openapi-dialect')
    if not isinstance(doc.get('info'), dict) or not isinstance(doc.get('paths'), dict):
        raise Unsupported('invalid-openapi-document')
    info = doc['info']
    if set(info) - {'title', 'version', 'description'} or any(not isinstance(v, str) for v in info.values()) or not {'title', 'version'} <= set(info):
        raise Unsupported('unsupported-info')
    # Templated paths need parameter interpretation; this first subset refuses them.
    operations = {}; operation_ids = set()
    for path, methods in doc['paths'].items():
        if (not re.fullmatch(r'/[A-Za-z0-9_./-]*',path) or '//' in path or any(part in ('.','..') for part in path.split('/'))
                or not isinstance(methods, dict)):
            raise Unsupported('unsupported-path')
        if len(doc['paths']) + len(operations) > OP_MAX: raise Unsupported('operation-budget')
        for method, operation in methods.items():
            budget.tick()
            if method not in ('get', 'put', 'post', 'delete', 'options', 'patch'):
                raise Unsupported('unsupported-path-context')
            if not isinstance(operation, dict) or set(operation) - {'summary', 'description', 'operationId', 'tags', 'requestBody', 'responses'}:
                raise Unsupported('unsupported-operation-context')
            if any(not isinstance(operation[k], str) for k in ('summary', 'description', 'operationId') if k in operation): raise Unsupported('invalid-operation-annotation')
            if 'tags' in operation and (not isinstance(operation['tags'], list) or any(not isinstance(v, str) for v in operation['tags'])): raise Unsupported('invalid-operation-tags')
            if 'operationId' in operation:
                if not operation['operationId'] or operation['operationId'] in operation_ids: raise Unsupported('ambiguous-operation-id')
                operation_ids.add(operation['operationId'])
            body = operation.get('requestBody')
            if 'requestBody' in operation and body is None: raise Unsupported('invalid-request-body')
            if body is not None:
                if method not in ('post','put','patch'): raise Unsupported('ambiguous-method-request-body')
                if not isinstance(body, dict) or set(body) - {'description', 'required', 'content'}:
                    raise Unsupported('unsupported-request-body')
                if type(body.get('required', False)) is not bool: raise Unsupported('invalid-request-required')
                if 'description' in body and not isinstance(body['description'],str): raise Unsupported('invalid-request-description')
                request = content_schema(body, budget)
            else: request = None
            responses = operation.get('responses')
            if not isinstance(responses, dict) or not responses: raise Unsupported('responses-required')
            normalized = {}
            for status, response in responses.items():
                if len(status) != 3 or not status.isascii() or not status.isdigit() or not 200 <= int(status) <= 599 or status in ('204','205','304'):
                    raise Unsupported('explicit-status-required')
                if not isinstance(response, dict) or set(response) - {'description', 'content'}:
                    raise Unsupported('unsupported-response-context')
                if not isinstance(response.get('description'), str): raise Unsupported('response-description-required')
                normalized[status] = content_schema(response, budget)
            operations[(path, method)] = (request, body.get('required', False) if body else False, normalized)
            if len(doc['paths']) + len(operations) > OP_MAX: raise Unsupported('operation-budget')
    return operations


def content_schema(body, budget):
    content = body.get('content')
    if not isinstance(content, dict) or set(content) != {'application/json'}:
        raise Unsupported('json-content-only')
    media = content['application/json']
    if not isinstance(media, dict) or set(media) != {'schema'}: raise Unsupported('inline-schema-only')
    validate(media['schema'], budget)
    return media['schema']


def schema_witness(schema,budget):
    return any(accepts(schema,value,budget) for value in samples(schema,budget))


def compare_openapi(old, new, budget, evidence):
    # Validate the WHOLE documents before any positive or negative inference.
    before, after = openapi_operations(old, budget), openapi_operations(new, budget)
    outcomes = []
    for identity, (oreq, orequired, ores) in before.items():
        if identity not in after:
            # A structural change is not a break when its domain may be empty.
            # Prove a bounded reachable old request/response before claiming one.
            reachable = (oreq is None or not orequired or schema_witness(oreq,budget))
            observed = reachable and any(schema_witness(schema,budget) for schema in ores.values())
            outcomes.append('breaking' if observed else 'unknown')
            if observed and len(evidence)<7:evidence.append(('removed operation '+identity[1]+' '+identity[0])[:160])
            continue
        nreq, nrequired, nres = after[identity]
        if old['paths'][identity[0]][identity[1]].get('operationId') != new['paths'][identity[0]][identity[1]].get('operationId'):
            outcomes.append('unknown')  # SDK identifier compatibility is not proved.
        if oreq is None and nreq is None: pass
        elif oreq is None or nreq is None: outcomes.append('unknown')
        else:
            outcomes.append('breaking' if nrequired and not orequired else compare_schema(oreq, nreq, 'request', budget, evidence))
        removed = ores.keys() - nres.keys()
        if removed:
            reachable = oreq is None or not orequired or schema_witness(oreq, budget)
            for status in sorted(removed):
                budget.tick()
                observed = reachable and schema_witness(ores[status], budget)
                outcomes.append('breaking' if observed else 'unknown')
                if observed and len(evidence) < 7:
                    evidence.append(('removed response %s %s %s' % (status, identity[1], identity[0]))[:160])
        for status, schema in nres.items():
            if status in ores: outcomes.append(compare_schema(ores[status], schema, 'response', budget, evidence))
            else:
                reachable = nreq is None or not nrequired or schema_witness(nreq,budget)
                outcomes.append('breaking' if reachable and schema_witness(schema,budget) else 'unknown')
    # Added operations do not alter an existing operation's contract. Every new
    # operation was still validated above; no unknown extension is silently ignored.
    return 'unknown' if 'unknown' in outcomes else 'breaking' if 'breaking' in outcomes else 'additive'

def classify(old_bytes, new_bytes, *, media_type='application/schema+json', direction='request'):
    """Classify exact byte inputs. Source/commit/record trust is a separate hook."""
    result = {'algorithm': VERSION, 'classification': 'unknown', 'compatibility': 'compatibility-unverified',
              'media_type': media_type, 'direction': direction, 'reasons': [],
              'old_sha256': hashlib.sha256(old_bytes).hexdigest() if isinstance(old_bytes, bytes) else None,
              'new_sha256': hashlib.sha256(new_bytes).hexdigest() if isinstance(new_bytes, bytes) else None}
    try:
        if direction not in ('request', 'response'): raise Unsupported('direction-required')
        old, new = decode(old_bytes), decode(new_bytes); budget = Budget(); evidence=[]
        if media_type == 'application/schema+json':
            if old.get('$schema') != DIALECT or new.get('$schema') != DIALECT: raise Unsupported('explicit-2020-12-dialect-required')
            validate(old, budget); validate(new, budget)
            status = compare_schema(old, new, direction, budget, evidence)
        elif media_type in ('application/vnd.oai.openapi+json', 'application/vnd.oai.openapi'):
            status = compare_openapi(old, new, budget, evidence)
        else: raise Unsupported('unsupported-media-type')
        result.update(classification=status, compatibility=status if status != 'unknown' else 'compatibility-unverified',
                      reasons=['supported-containment-proof' if status == 'additive' else 'proven-contract-break' if status == 'breaking' else 'containment-unproved']+evidence[:7])
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, UnicodeError, RecursionError) as error:
        result['reasons'] = [''.join(ch if ch.isascii() and 32 <= ord(ch) < 127 else '?' for ch in str(error))[:160] or 'unsupported-input']
    return result
