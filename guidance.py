"""Versioned, operator-set, per-project guidance for every actor run.

Guidance is an INSTRUCTION channel to agents, so its only writer is the operator
host command (``admin.py set-guidance``), which checks the deployment operator
allowlist before it writes. The endpoint can only read it and record that an
actor has acknowledged a version; contributor-written text (task titles,
proposal text, capability summaries) can never reach this file. A read always
reports who set the current text and when, and the text never overrides the
user's authorization or the worker safety rules.

Storage is two files in the project directory, deliberately separate from the
onboarding entry point (``ONBOARDING.md``):

* ``GUIDANCE.md`` - the bounded plain text. The version is the SHA-256 of its
  exact UTF-8 bytes, computed by every reader, so a crash between the two writes
  cannot make a reader report a version that does not describe the text it read.
* ``.guidance.json`` - the audit record: version, set_by, set_at, the previous
  version and text, a bounded history of earlier versions, and one
  acknowledgement per actor. A reader tolerates a missing, older or malformed
  metadata file by still reading the text and reporting what it can; the version
  stays authoritative because it comes from the text.

Every reader is tolerant of an older kit's project: no ``GUIDANCE.md`` means
"no guidance set", never an error, and unknown metadata keys are ignored.
"""
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from coordination import atomic

GUIDANCE_NAME = 'GUIDANCE.md'
META_NAME = '.guidance.json'
LIMIT = 8000
HISTORY_LIMIT = 50
ACK_LIMIT = 500
VERSION = re.compile(r'[a-f0-9]{64}')
ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}')
# Plain text only: no NUL or other C0 control characters except tab/newline/carriage
# return, and no DEL. A binary or terminal-control payload is refused rather than
# stored as instructions.
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

def version_of(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def validate_actor(actor):
    if not isinstance(actor,str) or not ACTOR.fullmatch(actor):
        raise ValueError('Guidance needs a valid actor identity')
    return actor

def validate_text(text):
    """A bounded, nonempty, plain-text guidance payload."""
    if not isinstance(text,str) or not text.strip():
        raise ValueError('Guidance must be nonempty UTF-8 text')
    if CONTROL.search(text):
        raise ValueError('Guidance must be plain text (no control characters)')
    if len(text.encode('utf-8'))>LIMIT:
        raise ValueError('Guidance exceeds %d bytes; shorten it or keep the long material in the '
                         'project onboarding entry point'%LIMIT)
    return text

def _paths(path):
    path=Path(path)
    if path.is_symlink():
        raise ValueError('Guidance path must not be a symlink')
    text=path/GUIDANCE_NAME
    meta=path/META_NAME
    for candidate in (text,meta,meta.with_suffix('.tmp')):
        if candidate.is_symlink():
            raise ValueError('Guidance paths must not be symlinks')
    return text,meta

def read_text(path):
    """The current guidance text, or None when the project has none.

    Over-limit or undecodable text is refused, never truncated: a worker must not
    follow a partial instruction.
    """
    text,_=_paths(path)
    if not text.exists():
        return None
    data=text.read_bytes()
    if len(data)>LIMIT*4:
        raise ValueError('Guidance is far over its %d-byte limit; ask the operator to shorten it'%LIMIT)
    try:
        value=data.decode('utf-8-sig')
    except UnicodeError:
        raise ValueError('Guidance is not valid UTF-8 text; ask the operator to reinstall it') from None
    validate_text(value)
    return value

def _valid_meta(value):
    """Tolerant shape check: the fields every reader needs, unknown keys ignored."""
    if not isinstance(value,dict):return False
    if type(value.get('schema_version')) is not int or value['schema_version']!=1:return False
    if not isinstance(value.get('version'),str) or not VERSION.fullmatch(value['version']):return False
    return True

def read_meta(path):
    """The audit metadata, or None when it is absent or unreadable.

    A reader never fails on a missing or malformed metadata file: it still has the
    text and its computed version. ``validate_meta`` is the strict form used by
    the writer and by backup validation.
    """
    _,meta=_paths(path)
    if not meta.exists():
        return None
    try:
        value=json.loads(meta.read_text(encoding='utf-8'))
    except (OSError,UnicodeError,ValueError):
        return None
    return value if _valid_meta(value) else None

def validate_meta(meta):
    """Strict validation of a record this kit wrote (writer and backup path)."""
    fields={'schema_version','version','set_by','set_at','previous_version','previous_text',
            'history','acknowledged'}
    if not isinstance(meta,dict):raise ValueError('Invalid guidance record')
    missing=sorted({'schema_version','version','set_by','set_at','previous_version','history',
                    'acknowledged'}-set(meta))
    if missing:raise ValueError('Invalid guidance record: missing fields '+', '.join(missing))
    unknown=sorted(set(meta)-fields)
    if unknown:raise ValueError('Invalid guidance record: unknown fields '+', '.join(unknown))
    if type(meta['schema_version']) is not int or meta['schema_version']!=1:
        raise ValueError('Invalid guidance record: schema_version must be integer 1')
    if not isinstance(meta['version'],str) or not VERSION.fullmatch(meta['version']):
        raise ValueError('Invalid guidance record: version must be a sha256 hex digest')
    if not isinstance(meta['set_by'],str) or not meta['set_by'].strip():
        raise ValueError('Invalid guidance record: set_by')
    if not isinstance(meta['set_at'],str) or not meta['set_at'].strip():
        raise ValueError('Invalid guidance record: set_at')
    previous=meta['previous_version']
    if previous is not None and (not isinstance(previous,str) or not VERSION.fullmatch(previous)):
        raise ValueError('Invalid guidance record: previous_version')
    if meta.get('previous_text') is not None:
        validate_text(meta['previous_text'])
    history=meta['history']
    if not isinstance(history,list) or len(history)>HISTORY_LIMIT:
        raise ValueError('Invalid guidance record: history must be a list of at most %d entries'%HISTORY_LIMIT)
    for entry in history:
        if not isinstance(entry,dict) or set(entry)!={'version','set_by','set_at','previous_version'}:
            raise ValueError('Invalid guidance record: malformed history entry')
        if not isinstance(entry['version'],str) or not VERSION.fullmatch(entry['version']):
            raise ValueError('Invalid guidance record: malformed history version')
        for key in ('set_by','set_at'):
            if entry[key] is not None and not isinstance(entry[key],str):
                raise ValueError('Invalid guidance record: malformed history '+key)
        if entry['previous_version'] is not None and (not isinstance(entry['previous_version'],str)
                                                      or not VERSION.fullmatch(entry['previous_version'])):
            raise ValueError('Invalid guidance record: malformed history previous_version')
    acknowledged=meta['acknowledged']
    if not isinstance(acknowledged,dict) or len(acknowledged)>ACK_LIMIT:
        raise ValueError('Invalid guidance record: acknowledged must be a map of at most %d actors'%ACK_LIMIT)
    for actor,entry in acknowledged.items():
        if not isinstance(actor,str) or not ACTOR.fullmatch(actor) or not isinstance(entry,dict):
            raise ValueError('Invalid guidance record: malformed acknowledgement')
        if set(entry)!={'version','acknowledged_at'}:
            raise ValueError('Invalid guidance record: malformed acknowledgement fields')
        if not isinstance(entry['version'],str) or not VERSION.fullmatch(entry['version']):
            raise ValueError('Invalid guidance record: malformed acknowledgement version')
        if not isinstance(entry['acknowledged_at'],str) or not entry['acknowledged_at'].strip():
            raise ValueError('Invalid guidance record: malformed acknowledgement time')
    return meta

def write_text(target,text):
    """Atomic sibling-temporary write of the guidance text (also used by restore)."""
    target=Path(target)
    if target.is_symlink():
        raise ValueError('Guidance path must not be a symlink')
    fd,tmp=tempfile.mkstemp(prefix='.'+target.name+'.',dir=str(target.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            stream.write(text);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,target)
    finally:
        if os.path.exists(tmp):
            try:os.unlink(tmp)
            except OSError:pass

def _acknowledgement(meta,actor):
    if not isinstance(meta,dict) or not isinstance(actor,str):
        return None
    table=meta.get('acknowledged')
    if not isinstance(table,dict):
        return None
    entry=table.get(actor)
    return entry if isinstance(entry,dict) else None

def state(path,actor=None):
    """The version block shared by the read, brief, work and resume responses.

    ``attention`` is true exactly when guidance is set and the calling actor has
    not acknowledged the current version, so a coordinator's new guidance is
    visible on the next run without any push or polling.
    """
    text=read_text(path)
    meta=read_meta(path) if text is not None else None
    current=version_of(text) if text is not None else None
    meta_version=meta.get('version') if isinstance(meta,dict) else None
    entry=_acknowledgement(meta,actor)
    acknowledged=bool(entry) and entry.get('version')==current
    result={'schema_version':1,'present':text is not None,'version':current,
            'set_at':meta.get('set_at') if isinstance(meta,dict) else None,
            'set_by':meta.get('set_by') if isinstance(meta,dict) else None,
            'previous_version':meta.get('previous_version') if isinstance(meta,dict) else None,
            'meta_version':meta_version,
            'acknowledged':acknowledged if current is not None else None,
            'acknowledged_version':entry.get('version') if entry else None,
            'acknowledged_at':entry.get('acknowledged_at') if entry else None,
            'attention':bool(current is not None and not acknowledged)}
    if meta_version is not None and current is not None and meta_version!=current:
        result['warning']=('Guidance audit metadata records a different version than the text; '
                           'the operator should set the guidance again')
    if result['attention']:
        result['next_action']='guidance get'
    return result

def read(path,args,actor=None):
    """``guidance get [--since VERSION]``: the text plus what changed since a version."""
    since=None;index=0
    if args[:1]==['get']:args=args[1:]
    if args[:1]==['version']:
        if len(args)!=1:raise ValueError('Use guidance version without arguments')
        return state(path,actor)
    while index<len(args):
        token=args[index]
        if token=='--since' or token.startswith('--since='):
            if since is not None:raise ValueError('Give --since at most once')
            if token=='--since':
                index+=1
                if index>=len(args):raise ValueError('--since needs a version')
                since=args[index]
            else:since=token.split('=',1)[1]
        else:raise ValueError('Use guidance get [--since VERSION], guidance version, guidance ack or guidance status')
        index+=1
    if since is not None and not VERSION.fullmatch(since):
        raise ValueError('--since must be a guidance version (sha256 hex digest)')
    text=read_text(path)
    if text is None:
        return {'schema_version':1,'present':False,'version':None,'set_at':None,'set_by':None,
                'text':None,'changed':False,'since':since,'previous_version':None,
                'previous_text':None,'acknowledged':None,'attention':False}
    current=version_of(text)
    meta=read_meta(path)
    previous=meta.get('previous_version') if isinstance(meta,dict) else None
    changed=since is not None and since!=current
    entry=_acknowledgement(meta,actor)
    return {'schema_version':1,'present':True,'version':current,
            'set_at':meta.get('set_at') if isinstance(meta,dict) else None,
            'set_by':meta.get('set_by') if isinstance(meta,dict) else None,
            'text':text,'changed':changed,'since':since,'previous_version':previous,
            # "What changed since a version": the immediately previous text is kept
            # beside its version, so a caller that names it gets the exact prior text.
            'previous_text':(meta.get('previous_text') if isinstance(meta,dict) and since==previous else None),
            'acknowledged':bool(entry) and entry.get('version')==current,
            'attention':not bool(entry) or entry.get('version')!=current}

def write_guidance(path,text,actor,now=None):
    """The operator write. Returns the new audit summary; idempotent on unchanged text."""
    validate_text(text);validate_actor(actor)
    target,meta_path=_paths(path)
    old_text=read_text(path)
    old_meta=read_meta(path)
    current=version_of(text)
    previous=version_of(old_text) if old_text is not None else None
    if previous==current:
        return {'version':current,'set_by':(old_meta or {}).get('set_by') or actor,
                'set_at':(old_meta or {}).get('set_at'),
                'previous_version':(old_meta or {}).get('previous_version'),
                'changed':False}
    stamp=now or datetime.now(timezone.utc).isoformat()
    history=list((old_meta or {}).get('history') or []) if isinstance(old_meta,dict) else []
    if old_text is not None:
        history.append({'version':previous,
                        'set_by':(old_meta or {}).get('set_by') if isinstance(old_meta,dict) else None,
                        'set_at':(old_meta or {}).get('set_at') if isinstance(old_meta,dict) else None,
                        'previous_version':(old_meta or {}).get('previous_version') if isinstance(old_meta,dict) else None})
    acknowledged=(old_meta or {}).get('acknowledged') if isinstance(old_meta,dict) else None
    meta={'schema_version':1,'version':current,'set_by':actor,'set_at':stamp,
          'previous_version':previous,'previous_text':old_text,
          'history':history[-HISTORY_LIMIT:],
          'acknowledged':dict(acknowledged) if isinstance(acknowledged,dict) else {}}
    validate_meta(meta)
    # The text is written first: a crash before the metadata replace leaves the new
    # version already authoritative (readers derive it from the text) with stale
    # audit fields, which the next set repairs. The reverse order would let a
    # crashed write advertise a version whose text was never installed.
    write_text(target,text)
    atomic(meta_path,meta)
    return {'version':current,'set_by':actor,'set_at':stamp,'previous_version':previous,
            'changed':True}

def acknowledge(path,actor,version=None):
    """Record that one actor has read the current guidance version.

    A stale ``version`` (one that is no longer current) is refused, so an actor
    cannot mark a version it did not read as picked up; the caller re-reads.
    """
    validate_actor(actor)
    text=read_text(path)
    if text is None:
        raise ValueError('No guidance is set for this project yet; nothing to acknowledge')
    current=version_of(text)
    if version is not None:
        if not isinstance(version,str) or not VERSION.fullmatch(version):
            raise ValueError('A guidance version is a sha256 hex digest')
        if version!=current:
            raise ValueError('That guidance version is no longer current; read the current guidance first')
    meta=read_meta(path)
    if meta is None:
        raise ValueError('Guidance audit metadata is missing or unreadable; ask the operator to set the '
                         'guidance again before acknowledging it')
    table=meta.get('acknowledged')
    if not isinstance(table,dict):
        raise ValueError('Guidance audit metadata has no acknowledgement table; ask the operator to set the '
                         'guidance again')
    stamp=datetime.now(timezone.utc).isoformat()
    prior=table.get(actor)
    if isinstance(prior,dict) and prior.get('version')==current:
        return {'acknowledged':True,'version':current,'acknowledged_at':prior.get('acknowledged_at'),
                'reconciled':True}
    if len(table)>=ACK_LIMIT and actor not in table:
        raise ValueError('Guidance acknowledgement table is full; ask the operator to compact it')
    table[actor]={'version':current,'acknowledged_at':stamp}
    meta['acknowledged']=table
    validate_meta(meta)
    atomic(path/META_NAME,meta)
    return {'acknowledged':True,'version':current,'acknowledged_at':stamp,'reconciled':False}

def status(path,actor,operators=None):
    """Operator-only: which lanes have acknowledged which version."""
    from keyed_records import require_configured_operator
    require_configured_operator(actor,operators,'read the guidance acknowledgement status')
    text=read_text(path)
    meta=read_meta(path) if text is not None else None
    current=version_of(text) if text is not None else None
    table=meta.get('acknowledged') if isinstance(meta,dict) else None
    rows=[]
    if isinstance(table,dict):
        for name,entry in sorted(table.items()):
            if not isinstance(entry,dict):continue
            rows.append({'actor':name,'version':entry.get('version'),
                         'acknowledged_at':entry.get('acknowledged_at'),
                         'current':entry.get('version')==current})
    return {'schema_version':1,'present':text is not None,'version':current,
            'set_by':meta.get('set_by') if isinstance(meta,dict) else None,
            'set_at':meta.get('set_at') if isinstance(meta,dict) else None,
            'previous_version':meta.get('previous_version') if isinstance(meta,dict) else None,
            'history':meta.get('history') if isinstance(meta,dict) else [],
            'acknowledged':rows,'acknowledged_total':len(rows),
            'up_to_date':sorted(row['actor'] for row in rows if row['current']),
            'behind':sorted(row['actor'] for row in rows if not row['current'])}

def brief_block(path,actor=None):
    """The compact version block for brief/work/resume; never raises on old projects."""
    if path is None:return None
    try:
        return state(path,actor)
    except (OSError,ValueError):
        # Guidance is an instruction channel: if it cannot be read reliably, say so
        # instead of silently reporting "no guidance".
        return {'schema_version':1,'present':None,'version':None,'set_at':None,'set_by':None,
                'previous_version':None,'acknowledged':None,'attention':False,
                'warning':'Guidance could not be read; ask the operator to check the guidance files'}
