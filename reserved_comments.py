"""Reserved machine-record comment prefixes and the raw-comment guard.

Raw `comments add` (positional bodies and transported --file/-f/--body-file/
--design-file inputs) must not write bodies that start with a reserved
machine-record prefix. Structured records are written only through their
dedicated operations, which validate chain, ownership and schema before the
single native mutation. Ordinary prose -- including bodies that merely
mention "Kind:" mid-text -- remains valid.

This module is import-safe on all platforms (no fcntl). endpoint.py enforces
it on the contributor `bd` path before any native mutation.
"""
from briefing import PREFIX as CHECKPOINT_PREFIX
from export_requirements import REVISION_PREFIX as REQUIREMENT_PREFIX
from handoff import (
    COMPLETE_PREFIX as HANDOFF_COMPLETE_PREFIX,
    INTENT_PREFIX as HANDOFF_PREFIX,
    parse_identity as parse_handoff_identity,
)
from lifecycle import PREFIX as LIFECYCLE_PREFIX
from review_workflow import PREFIX as REVIEW_PREFIX
from worker_gate import PREFIX as PLAN_PREFIX, parse_body as parse_plan_body


def parse_requirement_record(body):
    """Return the requirement record iff it passes the full supported schema.

    Uses the publisher's own record validation (required fields, revision,
    acceptance_state, content hash) plus exact canonical bytes, so only a
    record a legitimate revision write could produce passes.
    """
    if not isinstance(body, str) or not body.startswith(REQUIREMENT_PREFIX):
        return None
    rest = body[len(REQUIREMENT_PREFIX):]
    try:
        from export_requirements import parse_json
        from requirements import canonical_bytes, content_hash
        record = parse_json(rest)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict) or not {'id', 'revision', 'sha256'} <= set(record):
        return None
    try:
        from requirements import _validate_record, REQUIREMENT_FIELDS, RECORD_FIELDS
        if record.get('key') is not None:
            _validate_record(record, 'revision-comment', has_key=True)
        else:
            _validate_record(record, 'revision-comment', has_key=False)
    except (ValueError, TypeError):
        return None
    if content_hash(record) != record.get('sha256'):
        return None
    if canonical_bytes(record).decode() != rest:
        return None
    return record


def raw_target(args):
    """First non-flag token after `comments add`, or None if unresolvable."""
    if not isinstance(args, list) or len(args) < 2:
        return None
    if args[0] != 'comments' or args[1] != 'add':
        return None
    skip_next = False
    found = False
    for token in args[2:]:
        if not isinstance(token, str):
            continue
        if token.startswith('@attachment:'):
            continue
        if skip_next:
            skip_next = False
            continue
        if token in COMMENT_FLAGS_WITH_VALUE:
            skip_next = True
            continue
        if token in COMMENT_NO_VALUE_FLAGS:
            continue
        if token.startswith('-') and len(token) > 1:
            return None
        if not found:
            found = True
            return token
    return None


def comment_target(args):
    """Resolve the `comments add` target task, or None when unresolvable."""
    return raw_target(args)

RESERVED = (
    (REVIEW_PREFIX, 'contribution/review record', 'review TASK --file payload.json'),
    (CHECKPOINT_PREFIX, 'task checkpoint', 'checkpoint TASK --file checkpoint.json'),
    (LIFECYCLE_PREFIX, 'lifecycle evidence record', 'lifecycle.py record --file event.json'),
    (REQUIREMENT_PREFIX, 'requirement revision', 'the requirement revision workflow'),
    (HANDOFF_PREFIX, 'handoff intent', 'handoff TASK --file handoff.json'),
    (HANDOFF_COMPLETE_PREFIX, 'handoff completion', 'handoff TASK --file handoff.json'),
    (PLAN_PREFIX, 'worker plan registration', 'worker_gate.py register'),
)

PREFIXES = tuple(prefix for prefix, _, _ in RESERVED)


# Known native no-value flags (verified against pinned bd --help on koopa).
# These are skipped when locating the positional body; an unrecognized
# flag before the body is ambiguous and rejected before any native write.
COMMENT_NO_VALUE_FLAGS = {
    '--json', '-h', '--help', '-q', '--quiet', '-v', '--verbose',
    '--global', '--profile', '--sandbox', '--readonly', '--local-time',
    '--ignore-schema-skew',
}
COMMENT_FLAGS_WITH_VALUE = {'-f', '--file'}


def reserved_match(body):
    """Return (prefix, kind, operation) for a reserved body, else None."""
    if not isinstance(body, str):
        return None
    for prefix, kind, operation in RESERVED:
        if body.startswith(prefix):
            return (prefix, kind, operation)
    return None


def is_legitimate_writer(body, actor=None, task=None):
    """True when a reserved-prefix body is a validated supported write.

    worker_gate plan registrations, handoff intents/completions and
    requirement revisions written through their documented routes carry
    exact canonical bytes bound to the requesting actor/task; those must
    pass. Anything else is forged. Without actor/task context (pure
    helper use) only context-free canonical validity is checked; the
    endpoint always supplies context and fails closed on mismatch.
    """
    if not isinstance(body, str):
        return False
    if body.startswith(PLAN_PREFIX):
        payload = parse_plan_body(body)
        if payload is None:
            return False
        if actor is not None and payload.get('actor') != actor:
            return False
        if task is not None and payload.get('task') != task:
            return False
        return True
    if body.startswith(HANDOFF_PREFIX) or body.startswith(HANDOFF_COMPLETE_PREFIX):
        # Handoff authority cannot be established from self-asserted comment
        # fields: verified handoff writes go through the structured handoff
        # operation (internal run path, ownership-checked). Raw endpoint
        # handoff records are rejected unconditionally.
        return False
    if body.startswith(REQUIREMENT_PREFIX):
        record = parse_requirement_record(body)
        if record is None:
            return False
        if task is not None and record.get('id') != task:
            return False
        return True
    return False


def handoff_context_ok(identity, actor, task):
    """Bind handoff records to the actual request: no self-asserted authority.

    The payload's task must equal the comment target; the initiator or the
    recipient must equal the requesting actor (the endpoint already
    authenticates actor as a trusted-team attribution string, and the
    structured handoff operation additionally checks current ownership).
    operator=true never confers authority through the raw path: without an
    actor binding it stays rejected.
    """
    payload = identity.get('payload', {})
    if task is not None and payload.get('task') != task:
        return False
    if actor is not None:
        if payload.get('from_actor') != actor and payload.get('to_actor') != actor:
            return False
        if identity.get('operator') and payload.get('from_actor') != actor:
            return False
    return True


def raw_comment_bodies(args, attachments):
    """Collect (body, source) pairs from a raw `comments add` request.

    Pure helper so the endpoint guard is unit-testable without fcntl.
    Returns [] for non-`comments add` commands. The positional body is
    the first non-flag token after `comments add TASK`, so supported
    flag orderings (e.g. `comments add --json TASK BODY`,
    `comments add TASK BODY --json`) are covered; `-f/--file` values
    are consumed as paths, not bodies.
    """
    if not isinstance(args, list) or len(args) < 2:
        return []
    if args[0] != 'comments' or args[1] != 'add':
        return []
    if not isinstance(attachments, dict):
        attachments = {}
    bodies = []
    file_body = None
    positional = list(args[2:])
    # Transported file inputs arrive as @attachment: tokens.
    remaining = []
    for token in positional:
        if isinstance(token, str) and token.startswith('@attachment:'):
            key = token.partition(':')[2]
            item = attachments.get(key)
            if isinstance(item, dict) and isinstance(item.get('text'), str):
                bodies.append((item['text'], 'file-transport'))
                if item.get('flag') in ('--file', '-f'):
                    file_body = item['text']
        else:
            remaining.append(token)
    # Positional body: first non-flag token after `comments add TASK`,
    # skipping flags and their values. An unrecognized flag before the
    # body is ambiguous and rejected before any native write.
    skip_next = False
    found_task = False
    for token in remaining:
        if not isinstance(token, str):
            continue
        if skip_next:
            skip_next = False
            continue
        if token in COMMENT_FLAGS_WITH_VALUE:
            skip_next = True
            continue
        if token in COMMENT_NO_VALUE_FLAGS:
            continue
        if token.startswith('-') and len(token) > 1:
            raise ValueError(
                'Refusing raw positional comment: ambiguous flag %r before '
                'native write; place the comment body as the first non-flag '
                'token after `comments add TASK`.' % (token,))
        if not found_task:
            found_task = True
            continue
        if file_body is None:
            bodies.append((token, 'positional'))
        break
    return bodies


def check_raw_request(args, attachments, actor=None, task=None):
    """Reject a raw `comments add` request carrying a forged reserved body.

    Context-bound: supported records must match the requesting actor and
    the resolved comment target. When the target is unresolvable and any
    body carries a reserved prefix, fail closed.
    """
    bodies = raw_comment_bodies(args, attachments)
    if task is None:
        task = comment_target(args)
    for body, source in bodies:
        if task is None and reserved_match(body) is not None:
            raise ValueError(
                'Refusing raw %s comment: unresolvable comment target for '
                'reserved record; use the dedicated structured operation.'
                % (source,))
        check_comment_body(body, source, actor=actor, task=task)


def check_comment_body(body, source, actor=None, task=None):
    """Reject forged reserved machine-record bodies with an actionable error."""
    match = reserved_match(body)
    if match is None:
        return
    if is_legitimate_writer(body, actor=actor, task=task):
        return
    _, kind, operation = match
    raise ValueError(
        'Refusing raw %s comment (%s): reserved %s write with '
        'structured validation; use %s. Ordinary prose remains valid.'
        % (source, kind, kind, operation)
    )
