"""Operator void records for malformed reserved machine history.

A comment that claims a reserved machine format but fails schema validation used
to make every read and every new write on the task fail, leaving destructive
native-database editing as the only escape. A void record is an append-only
native comment, written only by the operator CLI, that names one offending
comment, preserves its exact original bytes and their SHA-256, and carries the
disposition, reason, target identity and - through the native comment - the
operator actor and timestamp. Nothing is deleted or rewritten: the original
stays in native history, in history reads and in rendered views, so the incident
remains inspectable and a reviewer can audit the decision.

A malformed, forged or stale void record never takes effect. It is ignored and
reported, exactly like an invalid checkpoint, so recovery can never dead-end on
its own bookkeeping and a void can never suppress history by accident. Validity
is bound to native provenance, not payload shape: the record names the operator
that issued it and its stored native author must match, and the prefix is
reserved on the contributor transport so a raw `comments add` can never write
one. Applying a void to a record that is part of the contribution chain the
surviving records currently form is refused by the owning module, so an
unauthorised void cannot remove a current approval.
"""
import hashlib
import json
import re
from requirements import canonical_bytes

PREFIX = 'Kind: record-void-v1\n'
OPERATION = 'void-record'
DISPOSITIONS = ('void',)
KIND_PREFIXES = {'contribution-review': 'Kind: contribution-review-v1\n'}
FIELDS = {'schema_version', 'operation', 'operation_id', 'task', 'target', 'target_kind',
          'target_sha256', 'original', 'reason', 'disposition', 'operator'}
ORIGINAL_LIMIT = 60000
BYTE_LIMIT = 80000


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def identity(value, label='Invalid void record ID'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}', value):
        raise ValueError(label)
    return value


def text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError(f'{name}: expected nonempty text up to {limit} characters')
    return value


def issued(p, task):
    """Validate one void payload. Raises for anything that is not an exact record."""
    if not isinstance(p, dict) or set(p) != FIELDS:
        raise ValueError('Invalid operator void record fields')
    if type(p['schema_version']) is not int or p['schema_version'] != 1 or p['task'] != task:
        raise ValueError('Invalid operator void record version/task')
    if p['operation'] != OPERATION:
        raise ValueError('Invalid operator void record operation')
    identity(task, 'Invalid void record task')
    identity(p['operation_id'], 'Invalid void record operation ID')
    identity(p['target'], 'Invalid void record target')
    identity(p['operator'], 'Invalid void record operator')
    if p['target_kind'] not in KIND_PREFIXES:
        raise ValueError('Unsupported operator void target kind')
    if p['disposition'] not in DISPOSITIONS:
        raise ValueError('Invalid operator void disposition')
    if not isinstance(p['target_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', p['target_sha256']):
        raise ValueError('Require the original record SHA256')
    if not isinstance(p['original'], str) or not p['original'].strip() or '\x00' in p['original']:
        raise ValueError('Preserve the exact original record bytes')
    if len(p['original']) > ORIGINAL_LIMIT:
        raise ValueError(f'Original record exceeds {ORIGINAL_LIMIT} characters; split the incident')
    if digest(p['original']) != p['target_sha256']:
        raise ValueError('Void record original bytes do not match target_sha256')
    text(p['reason'], 'void reason', 1000)
    if len(canonical_bytes(p)) > BYTE_LIMIT:
        raise ValueError('Void record exceeds 80 KB')


def validate(p, task):
    return issued(p, task)


def preserves(raw, p):
    """True when the void payload still carries the target's exact current bytes."""
    return raw == p['original'] and digest(raw) == p['target_sha256']


def target_text(issue, target):
    """Exact stored bytes of the named comment; one match required."""
    found = [c for c in issue.get('comments') or [] if str(c.get('id')) == target]
    if len(found) != 1:
        raise ValueError('Operator void target comment is missing or duplicated: ' + target)
    raw = found[0].get('text', '')
    if not isinstance(raw, str):
        raise ValueError('Operator void target comment has no text: ' + target)
    return raw


def records(issue):
    """Read the void records on one issue as (voids, targets, invalid).

    `voids`/`targets` hold only records that are individually valid, whose
    target still exists with exactly the preserved bytes and matching kind,
    and whose declared operator matches the native comment's author. Validity
    is therefore bound to native provenance, not to the payload shape alone:
    a void whose stored author is not the operator it names never takes
    effect. Anything else is listed in `invalid` and has no effect on the
    projection.
    """
    voids = []
    targets = {}
    invalid = []
    operations = set()
    for comment in issue.get('comments') or []:
        raw = comment.get('text', '')
        if not isinstance(raw, str) or not raw.startswith(PREFIX):
            continue
        cid = str(comment.get('id'))
        try:
            p = json.loads(raw[len(PREFIX):])
            issued(p, issue['id'])
            identity(cid, 'Invalid void record comment ID')
            author = text(comment.get('author'), 'void record author', 300)
            text(comment.get('created_at'), 'void record timestamp', 100)
            if author != p['operator']:
                raise ValueError('Operator void record provenance does not match its native author')
            if p['target'] in targets or p['operation_id'] in operations:
                raise ValueError('Conflicting operator void record')
            original = target_text(issue, p['target'])
            if not preserves(original, p):
                raise ValueError('Operator void record does not preserve the exact target bytes')
            if not original.startswith(KIND_PREFIXES[p['target_kind']]):
                raise ValueError('Operator void target is not a ' + p['target_kind'] + ' record')
        except (ValueError, KeyError, TypeError):
            invalid.append(cid)
            continue
        operations.add(p['operation_id'])
        targets[p['target']] = (p, comment)
        voids.append((p, comment))
    return voids, targets, invalid
