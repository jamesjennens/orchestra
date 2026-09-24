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
    """First non-flag operand after `comments add`, or None if unresolvable.

    Uses the structural parser, so global flags placed before the `add`
    subcommand (`comments --json add TASK BODY`) resolve the same target.
    """
    parts = _comments_parts(args)
    if parts is None or parts[0] != 'add':
        return None
    for token in parts[1]:
        if not isinstance(token, str):
            continue
        if token.startswith('@attachment:'):
            continue
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


# ---------------------------------------------------------------------------
# Structural bd argv parsing.
#
# bd (cobra/pflag) accepts global flags before the subcommand, so
# `comments --json add TASK BODY`, `comments -q add ...`, `comments -v add ...`
# and `comments --sandbox add ...` are all valid. The old guard assumed
# args[1] == 'add', so those orderings hid the body from the reserved-prefix
# check and a forged machine record was written. The tokenizer below locates
# the subcommand regardless of preceding global flags. Unrecognized flags make
# the structure ambiguous (an unknown value-taking flag could consume the
# apparent subcommand or body), so a `comments` invocation that contains one
# fails closed before any native write.
#
# Flag inventory verified against the pinned bd 1.2.2 help output:
#   global:  --actor --db -C/--directory --dolt-auto-commit --global
#            --ignore-schema-skew --json --profile -q/--quiet --readonly
#            --sandbox -v/--verbose -h/--help
#   comments add: -a/--author -f/--file -h/--help
# `--local-time` is retained as a tolerated legacy spelling (bd 1.2.2 rejects
# it natively, so no write results).
# ---------------------------------------------------------------------------

BD_GLOBAL_BOOL_FLAGS = {
    '--json', '--quiet', '-q', '--verbose', '-v', '--global',
    '--ignore-schema-skew', '--readonly', '--sandbox', '--profile',
    '--help', '-h', '--local-time',
}
BD_GLOBAL_VALUE_FLAGS = {
    '--actor', '--db', '--directory', '-C', '--dolt-auto-commit',
}
BD_COMMENT_ADD_BOOL_FLAGS = {'--help', '-h'}
BD_COMMENT_ADD_VALUE_FLAGS = {'--author', '-a', '--file', '-f'}

# Short flags that take a value (pflag allows -xVALUE and -x=VALUE).
BD_SHORT_VALUE_FLAGS = frozenset('afC')
BD_SHORT_BOOL_FLAGS = frozenset('hqv')


def _classify_bd_flag(token):
    """Classify a bd flag token as ('value'|'bool', attached) or None.

    `attached` is True when the value is joined to the flag (`-aX`, `-a=X`,
    `--author=X`); otherwise the next argv token is the flag's value.
    """
    if token.startswith('--'):
        name, sep, _ = token.partition('=')
        if name in BD_GLOBAL_VALUE_FLAGS or name in BD_COMMENT_ADD_VALUE_FLAGS:
            return ('value', bool(sep))
        if name in BD_GLOBAL_BOOL_FLAGS or name in BD_COMMENT_ADD_BOOL_FLAGS:
            return ('bool', bool(sep))
        return None
    body = token[1:]
    if not body:
        return None
    end = body.find('=')
    if end != -1:
        chars, attached = body[:end], True
    else:
        chars, attached = body, False
    for index, ch in enumerate(chars):
        if ch in BD_SHORT_VALUE_FLAGS:
            return ('value', attached or index < len(chars) - 1)
        if ch in BD_SHORT_BOOL_FLAGS:
            continue
        return None
    return ('bool', attached)


def _tokenize_bd_args(args):
    """Split argv into ('positional'|'flag'|'unknown', token) pairs.

    Known flag values are consumed so they are never mistaken for operands;
    `--` ends flag parsing exactly as pflag does.
    """
    tokens = []
    end_of_flags = False
    index = 0
    while index < len(args):
        token = args[index]
        if not isinstance(token, str):
            index += 1
            continue
        if end_of_flags:
            tokens.append(('positional', token))
            index += 1
            continue
        if token == '--':
            end_of_flags = True
            index += 1
            continue
        if len(token) > 1 and token.startswith('-'):
            spec = _classify_bd_flag(token)
            if spec is None:
                tokens.append(('unknown', token))
                index += 1
                continue
            kind, attached = spec
            tokens.append(('flag', token))
            index += 2 if kind == 'value' and not attached else 1
            continue
        tokens.append(('positional', token))
        index += 1
    return tokens


def _comments_parts(args):
    """Return (subcommand, operands) for a bd `comments` invocation.

    `operands` are the positional tokens after the subcommand, with flag
    values removed. Returns None when this is not a `comments` command.
    Raises ValueError when an unrecognized flag could hide the subcommand or
    body (ambiguous), so no native write can be reached.
    """
    if not isinstance(args, list):
        return None
    tokens = _tokenize_bd_args(args)
    positional = [i for i, (kind, _) in enumerate(tokens) if kind == 'positional']
    if not positional or tokens[positional[0]][1] != 'comments':
        return None
    if len(positional) < 2:
        unknown = [token for kind, token in tokens if kind == 'unknown']
        if unknown:
            raise ValueError(
                'Refusing comments request: ambiguous flag %r before native '
                'write; the bd command could not be parsed structurally.'
                % (unknown[0],))
        return (None, [])
    sub_index = positional[1]
    subcommand = tokens[sub_index][1]
    operands = [tokens[i][1] for i in positional[2:]]
    unknown = [token for kind, token in tokens if kind == 'unknown']
    before_subcommand = [
        token for i, (kind, token) in enumerate(tokens)
        if kind == 'unknown' and i < sub_index
    ]
    if before_subcommand or (subcommand == 'add' and unknown):
        raise ValueError(
            'Refusing comments request: ambiguous flag %r before native '
            'write; the bd command could not be parsed structurally.'
            % ((before_subcommand or unknown)[0],))
    return (subcommand, operands)


# Operator-only flags: identity/connection/file configuration that a
# contributor must not set. Matched in every pflag spelling, including the
# short form (`-a`), joined value (`-aX`, `-C/tmp`), and `=value`
# (`-a=X`, `--author=X`), plus boolean clusters (`-qa`).
OPERATOR_ONLY_LONG_FLAGS = {
    '--directory', '--db', '--repo', '--global', '--actor', '--author',
    '--profile', '--graph', '--config', '--metadata',
}
OPERATOR_ONLY_FILE_FLAGS = {'--file', '--body-file', '--design-file'}
OPERATOR_ONLY_SHORT_VALUE = {'a': '--author', 'C': '--directory', 'f': '--file'}


def operator_only_flag(token):
    """Canonical operator-only flag name for an argv token, or None.

    Unlike a `token.split('=')[0]` denylist this catches joined short forms:
    `-a`, `-a operator`, `-aoperator`, `-a=operator`, `-aoperator=...`,
    `--author=...`, `--actor`, `-C/tmp`, `-qa` (cluster), `-fnotes.txt`.
    """
    if not isinstance(token, str) or len(token) < 2 or not token.startswith('-'):
        return None
    if token.startswith('--'):
        name = token.partition('=')[0]
        if name in OPERATOR_ONLY_LONG_FLAGS or name in OPERATOR_ONLY_FILE_FLAGS:
            return name
        return None
    body = token[1:]
    end = body.find('=')
    chars = body[:end] if end != -1 else body
    for ch in chars:
        name = OPERATOR_ONLY_SHORT_VALUE.get(ch)
        if name is not None:
            return name
        if ch in BD_SHORT_BOOL_FLAGS:
            continue
        return None
    return None


def operator_only_in_args(args):
    """First operator-only flag in an argv list, or None.

    `--` ends flag parsing exactly as in bd/pflag, so operands after it are
    ordinary body text, not flags.
    """
    if not isinstance(args, list):
        return None
    end_of_flags = False
    for token in args:
        if token == '--':
            end_of_flags = True
            continue
        if end_of_flags:
            continue
        name = operator_only_flag(token)
        if name is not None:
            return name
    return None


# Backwards-compatible aliases for the previous flag tables.
COMMENT_NO_VALUE_FLAGS = BD_GLOBAL_BOOL_FLAGS
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
    Returns [] for non-`comments add` commands. The bd command is parsed
    structurally, so global flags before `add` (`comments --json add TASK
    BODY`) and flags anywhere after the operand are handled; `-f/--file`
    values are consumed as paths, not bodies. An unrecognized flag is
    ambiguous and rejected before any native write.
    """
    parts = _comments_parts(args)
    if parts is None or parts[0] != 'add':
        return []
    if not isinstance(attachments, dict):
        attachments = {}
    bodies = []
    file_body = None
    # Transported file inputs arrive as @attachment: tokens.
    remaining = []
    for token in parts[1]:
        if isinstance(token, str) and token.startswith('@attachment:'):
            key = token.partition(':')[2]
            item = attachments.get(key)
            if isinstance(item, dict) and isinstance(item.get('text'), str):
                bodies.append((item['text'], 'file-transport'))
                if item.get('flag') in ('--file', '-f'):
                    file_body = item['text']
        else:
            remaining.append(token)
    # Positional body: the second operand after `comments add TASK`.
    if file_body is None and len(remaining) >= 2:
        token = remaining[1]
        if isinstance(token, str):
            bodies.append((token, 'positional'))
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
