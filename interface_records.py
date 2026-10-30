"""Program interface-v1 register (PROGRAM_DESIGN §4, slice 3a).

Schema compatibility verification belongs to slice 3b. Every revision is explicitly
compatibility-unverified here. Reads never fetch a schema or follow a URL.
"""
import contextvars
import json
import re
import time
from datetime import date
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import keyed_entries
import keyed_records as core
import reference_records as refs
from export_requirements import parse_json
from requirements import canonical_bytes, content_hash

ENTRY_PREFIX = 'Kind: interface-entry-v1\n'
ACCEPTANCE_PREFIX = 'Kind: interface-acceptance-v1\n'
KEY = re.compile(r'iface\.([a-z][a-z0-9]*(?:[.-][a-z0-9]+)*)\.v([1-9][0-9]{0,5})\Z')
TOKEN_VALUE = re.compile(r'(iface\.[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*\.v[1-9][0-9]{0,5})@([1-9][0-9]{0,8})\Z')
STATES = ('draft', 'accepted', 'deprecated', 'retired')
CONTENT_FIELDS = ('key', 'name', 'meaning', 'owner_project', 'owner', 'schema', 'compatibility',
                  'replaces', 'state', 'review_by', 'tags')
ENTRY_FIELDS = ('schema_version', 'revision', *CONTENT_FIELDS, 'acceptance_state', 'successor', 'sha256')
MAX_INTERFACES = 32
PAGE_MAX = 50
AREA_MAX = 50
_CONTEXT = contextvars.ContextVar('interface_context', default=None)


class Refusal(ValueError):
    status = 409


@contextmanager
def context(value):
    token = _CONTEXT.set(value)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def valid_key(value, where='key'):
    if not isinstance(value, str) or len(value) > 80 or not KEY.fullmatch(value):
        raise ValueError(where + ' must be iface.<name>.v<positive-major>, at most 80 characters')
    return value


def token(value, here=None):
    if not isinstance(value, str) or not value.startswith('iface:') or len(value) > 200:
        raise ValueError('iface must be a pinned iface:<project>/<key>@<revision> token')
    local = value[6:]
    project = here
    if '/' in local:
        project, local = local.split('/', 1)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}', project):
            raise ValueError('Invalid interface project')
    match = TOKEN_VALUE.fullmatch(local)
    if not match:
        raise ValueError('iface must name an interface key and a positive pinned @revision')
    valid_key(match[1])
    return project, match[1], int(match[2])


def valid_interfaces(value, here=None):
    if not isinstance(value, list) or len(value) > MAX_INTERFACES:
        raise ValueError('interfaces must be a list of at most 32 entries')
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {'iface', 'role'}:
            raise ValueError('Each interface link is exactly {iface, role}')
        token(item['iface'])
        if item['role'] not in ('provides', 'consumes'):
            raise ValueError('Interface role must be provides or consumes')
        # A capability cannot claim two minima or opposite roles for the same major.
        project, key, _ = token(item['iface'],here)
        if (project, key) in seen:
            raise ValueError('Duplicate or conflicting interface major on one capability')
        seen.add((project, key))
    return value


def schema(value):
    if not isinstance(value, dict) or value.get('type') not in ('repo-path', 'url'):
        raise ValueError('schema must be a repo-path or HTTPS url pointer')
    fields = ({'type', 'path', 'commit', 'anchor'} if value['type'] == 'repo-path'
              else {'type', 'url', 'retrieved'}) | {'media_type', 'sha256'}
    if set(value) != fields:
        raise ValueError('schema has the wrong field set')
    pointer = {k: v for k, v in value.items() if k not in ('media_type', 'sha256') and v is not None}
    refs.valid_authority(pointer)
    if value['type'] == 'repo-path' and not refs.COMMIT.fullmatch(value.get('commit') or ''):
        raise ValueError('schema.commit must pin a full commit')
    if not isinstance(value['media_type'], str) or not re.fullmatch(r'[a-zA-Z0-9.+-]{1,100}/[a-zA-Z0-9.+-]{1,100}', value['media_type']):
        raise ValueError('schema.media_type must be a bounded media type')
    if not isinstance(value['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['sha256']):
        raise ValueError('schema.sha256 must pin the schema bytes')
    return value


def validate_content(payload):
    valid_key(payload.get('key'))
    for name, limit in (('name', 200), ('meaning', 2000)):
        refs._bounded_text(payload.get(name), name, limit)
    if not isinstance(payload.get('owner_project'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}', payload['owner_project']):
        raise ValueError('owner_project must name the exact defining area project')
    refs.valid_owner(payload.get('owner'))
    schema(payload.get('schema'))
    comp = payload.get('compatibility')
    if not isinstance(comp, dict) or set(comp) != {'rule', 'change'} or comp['rule'] != 'additive-within-major' or comp['change'] not in ('initial', 'additive'):
        raise ValueError('compatibility must declare additive-within-major and initial or additive')
    if payload.get('replaces') is not None:
        valid_key(payload['replaces'], 'replaces')
        a, b = KEY.fullmatch(payload['key']), KEY.fullmatch(payload['replaces'])
        if a[1] != b[1] or int(a[2]) != int(b[2]) + 1:
            raise ValueError('A new major replaces exactly the preceding major of the same interface')
    if payload.get('state') not in STATES:
        raise ValueError('state must be draft, accepted, deprecated or retired')
    if payload.get('review_by') is not None:
        refs._date(payload['review_by'], 'review_by')
    refs.valid_tags(payload.get('tags', []))


def validate_entry(record):
    if not isinstance(record, dict) or set(record) != set(ENTRY_FIELDS) or type(record['schema_version']) is not int or record['schema_version'] != 1:
        raise ValueError('interface entry has the wrong version or field set')
    core.positive_int(record['revision'], 'revision')
    validate_content(record)
    if record['acceptance_state'] == 'accepted' and record['review_by'] is None:
        raise ValueError('An accepted interface needs review_by')
    if record['acceptance_state'] not in ('draft', 'accepted') or record['successor'] is not None:
        raise ValueError('Interface lifecycle is state; shared acceptance must be draft or accepted')
    if content_hash(record) != record['sha256']:
        raise ValueError('Interface hash mismatch')
    return record


def parse_entry(body):
    if not isinstance(body, str) or not body.startswith(ENTRY_PREFIX):
        return None
    try:
        record = parse_json(body[len(ENTRY_PREFIX):])
        validate_entry(record)
        return record if canonical_bytes(record).decode() == body[len(ENTRY_PREFIX):] else None
    except (ValueError, TypeError, KeyError):
        return None


def entry_record(payload, revision, acceptance_state):
    record = {name: deepcopy(payload.get(name)) for name in CONTENT_FIELDS}
    record['tags'] = list(payload.get('tags', []))
    record.update(schema_version=1, revision=revision, acceptance_state=acceptance_state, successor=None)
    record['sha256'] = content_hash(record)
    return validate_entry(record)


def write_time_rules(record):
    if record.get('review_by') is not None:
        today=date(*time.gmtime()[:3])
        review=refs._date(record['review_by'],'review_by')
        if review < today or review > refs._months_ahead(today,24):
            raise ValueError('review_by must be current and at most 24 months ahead')
    if record['schema']['type']=='url' and refs._date(record['schema']['retrieved'],'schema.retrieved') > date(*time.gmtime()[:3]):
        raise ValueError('schema.retrieved cannot be in the future')


def pre_write(payload, run, operators=None, project=None):
    if payload['operation'] in ('propose', 'revise', 'draft'):
        ctx = _CONTEXT.get()
        if ctx is None:
            raise Refusal('Interface writes need an authorized project context')
        ctx.propose(payload)


def acceptance_rules(record, acceptance, run):
    ctx = _CONTEXT.get()
    if ctx is None:
        raise Refusal('Interface acceptance needs complete caller-authorized program coverage')
    ctx.accept(record)


KIND = keyed_entries.AnchoredKind(
    noun='interface', title='Interface', command='interface', type_label='interface',
    state_labels={'draft': 'interface:draft', 'accepted': 'interface:accepted'},
    key_prefix='interface-key:', family='interface-', entry_prefix=ENTRY_PREFIX,
    acceptance_prefix=ACCEPTANCE_PREFIX, journal='.interface-requests', source='interface-apply',
    accept_action='accept an interface', apply_command='admin.py interface-apply',
    reconcile_command='admin.py interface-reconcile', anchor_title='Interface: %s',
    anchor_description='Interface register anchor for %s. Use interface get %s; not a work item.',
    close_reason='interface register anchor (not a work item)', valid_key=valid_key,
    parse_entry=parse_entry, validate_entry=validate_entry, entry_record=entry_record,
    validate_content=validate_content, content_fields=CONTENT_FIELDS,
    write_time_rules=write_time_rules, pre_write=pre_write, acceptance_rules=acceptance_rules)


def catalog(rows, operators=None):
    return KIND.catalog(rows, operators or ())


def view(entry, entries=()):
    record = entry.get('record') or entry.get('proposed')
    replacement=next((e['key'] for e in entries if e.get('state')=='accepted'
                      and e.get('record',{}).get('replaces')==entry.get('key')),None)
    return dict(entry, replacement=replacement, lifecycle=(record['state'] if entry.get('record') else 'draft') if record else None,
                compatibility='compatibility-unverified', trust='data')


def get(rows, key, operators=None):
    entries,_=catalog(rows,operators)
    return view(KIND.find_entry(rows, key, operators or ()),entries)


def read(args, run, operators=None):
    rest = [v for v in args if v != '--json']
    if rest[:1] == ['get'] and len(rest) == 2:
        return get(KIND.read_rows(run), valid_key(rest[1]), operators)
    if not rest or rest[0] != 'list':
        raise ValueError('interface get KEY | list [--offset N --limit N] | propose|revise --file FILE')
    opts = {'offset': 0, 'limit': 20}
    if len(rest[1:]) % 2:
        raise ValueError('interface list needs --offset/--limit values')
    for flag, val in zip(rest[1::2], rest[2::2]):
        if flag not in ('--offset', '--limit') or not re.fullmatch(r'[0-9]{1,6}', val):
            raise ValueError('Invalid interface page')
        opts[flag[2:]] = int(val)
    if not 1 <= opts['limit'] <= PAGE_MAX:
        raise ValueError('Interface limit must be 1..50')
    entries, incomplete = catalog(KIND.read_rows(run), operators)
    entries = sorted(entries, key=lambda e: e.get('key') or '')
    page = entries[opts['offset']:opts['offset'] + opts['limit']]
    return {'schema_version': 1, 'items': [view(e,entries) for e in page], 'total': len(entries),
            'next_offset': opts['offset'] + len(page) if opts['offset'] + len(page) < len(entries) else None,
            'coverage': {'complete': not incomplete and all(e['state'] not in ('malformed', 'unsupported', 'conflicted') for e in entries),
                         'incomplete': incomplete}, 'compatibility': 'compatibility-unverified'}


class ProgramContext:
    """Authorized read adapter. None means unreadable, failed or unavailable, never empty.

    read_project must check the caller BEFORE I/O; it returns a complete exported snapshot.
    The outer deployment mutation lock serializes graph/capability changes with accept.
    """
    def __init__(self, project, read_project, *, operators=(), memberships=None, users=None, may_propose=None):
        self.project, self.read_project = project, read_project
        self.operators = operators
        self.memberships, self.users = memberships, users
        self.may_propose = may_propose
        self.cache = {}

    def rows(self, project):
        if project not in self.cache:
            try:
                rows = self.read_project(project)
                # Native exported rows without project_id are scoped by this adapter's reader.
                if not isinstance(rows, list) or any(not isinstance(r, dict) or r.get('malformed') or r.get('project_id', project) != project for r in rows):
                    rows = None
                self.cache[project] = rows
            except (ValueError, OSError, RuntimeError):
                self.cache[project] = None
        return self.cache[project]

    def own(self):
        rows = self.rows(self.project)
        if rows is None:
            raise Refusal('Program snapshot is unavailable; no interface acceptance was written')
        return rows

    def areas(self):
        import program_links
        entries, incomplete = refs.KIND.catalog(self.own(), self.operators)
        candidates = []
        complete = not incomplete
        for e in entries:
            if e['state'] in ('malformed','unsupported','conflicted'):
                complete=False
            key = e.get('key') or ''
            side = program_links.side_of(key)
            if not side or side[0] != 'program':
                continue
            status, _ = program_links.side_status(e, self.project, side[1], self.memberships, self.users)
            if e['state'] in ('malformed', 'unsupported', 'conflicted'):
                complete = False
            if status == 'in':
                candidates.append(side[1])
        if len(candidates) > AREA_MAX:
            return [], False, len(candidates)
        linked, unavailable = [], 0
        for area in candidates:
            rows = self.rows(area)
            if rows is None:
                unavailable += 1
                complete = False
                continue
            others, inc = refs.KIND.catalog(rows, self.operators)
            selected = [e for e in others if e.get('key') == program_links.MEMBER_OF]
            if inc or any(e['state'] in ('malformed', 'unsupported', 'conflicted') for e in others):
                complete = False
            if len(selected) == 1 and program_links.side_status(selected[0], area, self.project, self.memberships, self.users)[0] == 'in':
                linked.append(area)
        return linked, complete, unavailable

    def usage(self):
        import capability_records
        areas, complete, hidden = self.areas()
        links = []
        for area in areas:
            entries, incomplete = capability_records.catalog(self.rows(area), self.operators)
            if incomplete or any(e['state'] in ('malformed', 'unsupported', 'conflicted') for e in entries):
                complete = False
            for e in entries:
                record = e.get('record')
                if e['state'] == 'accepted' and record:
                    try: valid_interfaces(record.get('interfaces',[]),area)
                    except ValueError:
                        complete=False
                        continue
                    for item in record.get('interfaces', []):
                        project, key, revision = token(item['iface'], area)
                        if project == self.project:
                            if item['role']=='provides' and not self.accepted_pin(key,revision):
                                continue
                            links.append(dict(item, project=area, key=key, minimum=revision))
        return links, {'complete': complete, 'read_areas': len(areas), 'unavailable_areas': hidden}

    def accepted_pin(self,key,revision):
        try:
            entry=KIND.find_entry(self.own(),key,self.operators)
            if entry['state']!='accepted':return False
            row=next(r for r in self.own() if r.get('id')==entry.get('native_id'))
            row=KIND.live_row(row,self.operators)[0]
            rec=KIND.existing_revisions(row).get(revision)
            evidence=KIND.live_acceptances(row,self.operators).get(revision)
            return bool(rec and rec['acceptance_state']=='accepted' and evidence and evidence['record_sha256']==rec['sha256'])
        except (ValueError,StopIteration):return False

    def propose(self, payload):
        area = payload['owner_project']
        if self.may_propose is not None and not self.may_propose(area):
            raise Refusal('Proposing an interface needs coordination in its defining area')
        areas, complete, _ = self.areas()
        if area not in areas:
            raise Refusal('The defining area must be a currently linked readable program member')

    def accept(self, record):
        entries, incomplete = catalog(self.own(), self.operators)
        if incomplete or any(e['state'] in ('malformed', 'unsupported', 'conflicted') for e in entries):
            raise Refusal('Interface catalog is incomplete; acceptance refused')
        selected = next((e for e in entries if e['key'] == record['key']), None)
        if selected is not None:
            row = next((r for r in self.own() if r.get('id') == selected['native_id']), None)
            if row is not None:
                row = KIND.live_row(row,self.operators)[0]
                stored = KIND.existing_revisions(row).get(record['revision'])
                evidence = KIND.live_acceptances(row,self.operators).get(record['revision'])
                if stored == record and evidence and evidence['record_sha256'] == record['sha256']:
                    return  # native-identical replay; core still checks identity, actor and F3
        areas, _complete, _hidden = self.areas()
        if record['owner_project'] not in areas:
            raise Refusal('Defining area is not a currently linked readable program member')
        current = next((e.get('record') for e in entries if e['key'] == record['key']), None)
        lifecycle_only = (current is not None and record['state'] in ('deprecated','retired')
                          and record['state'] != current['state']
                          and all(record[k] == current[k] for k in CONTENT_FIELDS if k != 'state'))
        if current is None:
            if record['compatibility']['change'] != 'initial' or record['state'] != 'accepted':
                raise Refusal('The first accepted interface declares initial and state accepted')
            major = int(KEY.fullmatch(record['key'])[2])
            if any(e.get('record') and KEY.fullmatch(e['key'])[1]==KEY.fullmatch(record['key'])[1] for e in entries) and record['replaces'] is None:
                raise Refusal('A new major must name the preceding major in replaces')
            if record['replaces'] is not None:
                predecessor = next((e for e in entries if e['key'] == record['replaces']), None)
                if predecessor and predecessor.get('proposed') is not None:
                    raise Refusal('Predecessor has a pending draft; new major acceptance refused')
                prior = predecessor.get('record') if predecessor else None
                if prior is None or prior['owner_project'] != record['owner_project']:
                    raise Refusal('replaces must name an accepted preceding major of the same defining area')
        else:
            if current['state'] == 'retired':
                raise Refusal('A retired interface major cannot be reactivated')
            if record['owner_project'] != current['owner_project'] or record['replaces'] != current['replaces']:
                raise Refusal('An interface revision cannot change its defining area or replacement')
            if not lifecycle_only and record['compatibility']['change'] != 'additive':
                raise Refusal('Later revisions within a major must declare additive')
            if record['state'] == 'draft':
                raise Refusal('An accepted revision cannot have draft lifecycle')
        if record['state'] == 'retired':
            if not lifecycle_only:
                raise Refusal('Retirement changes only state')
            usage, coverage = self.usage()
            consumers = [u for u in usage if u['key'] == record['key'] and u['role'] == 'consumes']
            if not coverage['complete'] or consumers:
                raise Refusal('Retirement refused: %s coverage; %d areas unavailable; %d known current consumers' %
                              ('complete' if coverage['complete'] else 'incomplete',coverage['unavailable_areas'],len(consumers)))


def flags(record, usage, complete):
    same = [u for u in usage if u['key'] == record['key']]
    consumers = [u for u in same if u['role'] == 'consumes']
    providers = [u for u in same if u['role'] == 'provides']
    result = ['compatibility-unverified']
    if consumers and record['state'] in ('deprecated', 'retired'):
        result.append('consumes-' + record['state'])
    if complete:
        if not same:
            result.append('orphan')
        if any(not any(p['minimum'] >= c['minimum'] for p in providers) for c in consumers):
            result.append('no-provider')
        if any(c['minimum'] >= record['revision'] for c in consumers) and not any(p['minimum'] >= record['revision'] for p in providers):
            result.append('provider-behind')
    return result


def apply_native(payload, actor, run, project, operator=False, operators=None, web_coordinator=False, party_check=None, web_service=False):
    # The route supplies context; the core owns all revision/evidence/CAS/replay writes.
    return KIND.apply_native(payload, actor, run, project, operator=operator, operators=operators,
                             web_coordinator=web_coordinator, party_check=party_check, web_service=web_service)


def validate_transition(plan):
    fields={'schema_version','operation_id','sha256','actor','id','revision','status','record','evidence'}
    if not isinstance(plan,dict) or set(plan)!=fields or type(plan['schema_version']) is not int or plan['schema_version']!=1:
        raise ValueError('Malformed interface transition receipt')
    core.validate_receipt(plan)
    core.text(plan['operation_id'],'operation_id')
    if plan['status'] not in ('pending','complete'):
        raise ValueError('Interface transition cannot be released or discarded')
    validate_entry(plan['record'])
    evidence=KIND.parse_acceptance(plan['evidence'])
    if (evidence is None or evidence['id']!=plan['id'] or evidence['revision']!=plan['revision']
            or evidence['record_sha256']!=plan['record']['sha256'] or evidence['operator']!=plan['actor']
            or plan['record']['state']!='deprecated' or plan['record']['acceptance_state']!='accepted'
            or plan['record']['revision']!=plan['revision']):
        raise ValueError('Interface transition evidence is not bound to its exact record')
    return plan


def _finish_deprecation(payload,actor,run,project,operators,accepted):
    """Retryable second leg of new-major acceptance, never a cross-entry atomicity claim.

    The caller already passed ordinary F3/G1 checks for the new major. This leg can
    change ONLY the predecessor's lifecycle state; it cannot replace schema/meaning.
    Evidence is posted before the revision just like the shared core. The persistent
    plan fixes exact bytes across a crash between those two native comments.
    """
    import hashlib
    from coordination import atomic
    if not accepted.get('replaces'):
        return
    directory=Path(project)/'.interface-transitions'
    if directory.is_symlink():raise ValueError('Interface transition directory must not be a symlink')
    directory.mkdir(mode=0o700,exist_ok=True)
    identity=content_hash({'operation_id':payload['operation_id']})
    path=directory/(identity+'.json')
    digest=content_hash(payload)
    if path.exists() or path.is_symlink():
        plan=validate_transition(core.read_host_json(path))
        if plan['operation_id']!=payload['operation_id'] or plan['sha256']!=digest or (plan['status']!='complete' and plan['actor']!=actor):
            raise Refusal('Interface transition identity differs; use the original operation and actor')
    else:
        key=accepted['replaces']
        rows=KIND.read_key_rows(run,key)
        predecessor=KIND.find_entry(rows,key,operators or ())
        previous=predecessor.get('record')
        if previous is None or predecessor['state']!='accepted':
            raise Refusal('Predecessor is not readable and accepted; deprecation is pending')
        if previous['state'] in ('deprecated','retired'):
            return
        if predecessor.get('proposed') is not None:
            raise Refusal('Predecessor has a pending draft; deprecation is pending, not complete')
        row=next(r for r in rows if r['id']==predecessor['native_id'])
        revisions=KIND.existing_revisions(KIND.live_row(row,operators or ())[0])
        record=deepcopy(previous)
        record.update(revision=max(revisions)+1,state='deprecated')
        record['sha256']=content_hash(record)
        validate_entry(record)
        bound=core.bind_acceptance(payload['acceptance'],record)
        _,body=KIND.acceptance_evidence(bound,row['id'],record['revision'],record,actor)
        plan={'schema_version':1,'operation_id':payload['operation_id'],'sha256':digest,'actor':actor,
              'id':row['id'],'revision':record['revision'],'status':'pending','record':record,'evidence':body}
        validate_transition(plan)
        atomic(path,plan,mode=0o600)
    return resume_transition(path,plan,run,operators)


def resume_transition(path,plan,run,operators):
    from coordination import atomic
    validate_transition(plan)
    actor=plan['actor']
    if plan['status']=='complete':return
    rows=KIND.shown(run,[plan['id']]);row=rows[0] if len(rows)==1 else None
    if row is None:raise Refusal('Predecessor disappeared; transition remains pending')
    revisions=KIND.existing_revisions(KIND.live_row(row,operators or ())[0])
    wanted=plan['record'];number=wanted['revision']
    if number in revisions and revisions[number]!=wanted:
        raise Refusal('Predecessor changed at the deprecation revision; transition remains pending')
    if any(n>number for n in revisions):
        raise Refusal('Predecessor advanced beyond pending transition; reconcile before retrying')
    if number not in revisions:
        prior=revisions.get(number-1)
        if prior is None or any(prior[k]!=wanted[k] for k in CONTENT_FIELDS if k!='state'):
            raise Refusal('Predecessor content changed; transition remains pending')
    # Re-read each leg to reconcile a lost write response without duplicating a comment.
    for body in (plan['evidence'],KIND.entry_comment(wanted)):
        current=KIND.shown(run,[plan['id']])[0]
        if not any(c.get('text')==body and c.get('author')==actor for c in current.get('comments',[]) if isinstance(c,dict)):
            run(['comments','add',plan['id'],body,'--json'])
    plan['status']='complete';atomic(path,plan,mode=0o600)


# Retain the core as the single native write entry, then complete the state-only second leg.
_core_apply=apply_native


def apply_native(payload,actor,run,project,operator=False,operators=None,web_coordinator=False,party_check=None,web_service=False):
    result=_core_apply(payload,actor,run,project,operator=operator,operators=operators,
                       web_coordinator=web_coordinator,party_check=party_check,web_service=web_service)
    row=KIND.shown(run,[result['native_id']])[0]
    record=KIND.existing_revisions(KIND.live_row(row,operators or ())[0]).get(result['revision'])
    if record is not None:
        result.update(sha256=record['sha256'],lifecycle=record['state'] if record['acceptance_state']=='accepted' else 'draft',
                      compatibility='compatibility-unverified')
        if payload['operation'] in ('accept','draft'):
            _finish_deprecation(payload,actor,run,project,operators,record)
    return result


def reconcile(project,operation_id,actor,reason,disposition,run,issue_id=None,operators=None):
    """Configured-operator recovery; never release the core's completed first leg."""
    from coordination import atomic
    core.require_configured_operator(actor,operators,'reconcile an interface operation')
    path=Path(project)/'.interface-transitions'/(content_hash({'operation_id':operation_id})+'.json')
    plan=validate_transition(core.read_host_json(path)) if path.exists() else None
    if plan is not None and plan['operation_id']!=operation_id:
        raise Refusal('Interface transition receipt identity does not match its path')
    if plan is not None and disposition!='complete':
        raise Refusal('A compound interface transition must be completed, never released or discarded')
    result=KIND.reconcile(project,operation_id,actor,reason,disposition,run,issue_id=issue_id,operators=operators)
    if plan is not None and plan['status']!='complete':
        old=KIND.parse_acceptance(plan['evidence'])
        bound=core.bind_acceptance(old['decision'],plan['record'])
        _,body=KIND.acceptance_evidence(bound,plan['id'],plan['revision'],plan['record'],actor)
        plan.update(actor=actor,evidence=body)
        atomic(path,plan,mode=0o600)
        resume_transition(path,plan,run,operators)
        result['transition_complete']=True
    return result


def help_payload():
    return {'command':'interface','usage':'interface list [--offset N --limit N] | get KEY | propose|revise --file FILE',
            'limits':{'page':PAGE_MAX,'areas':AREA_MAX,'capability_interfaces':MAX_INTERFACES},
            'writes':'Propose/revise require authenticated web authority; a program coordinator accepts; configured host operators use interface-apply.',
            'retirement':'Revise only state to retired and accept with a complete, consumer-free view.',
            'compatibility':'compatibility-unverified; schema verifier is a later slice'}
