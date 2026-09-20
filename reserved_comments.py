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
    is_valid_completion as handoff_completion_valid,
    is_valid_intent as handoff_intent_valid,
)
from lifecycle import PREFIX as LIFECYCLE_PREFIX
from review_workflow import PREFIX as REVIEW_PREFIX
from worker_gate import PREFIX as PLAN_PREFIX, parse_body as parse_plan_body

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


def is_legitimate_writer(body):
    """True when a reserved-prefix body is a validated supported write.

    worker_gate plan registrations and handoff intents/completions
    written through their documented routes carry exact canonical
    bytes; those must pass. Requirement revisions are documented
    additive raw comments (export_requirements REVISION_PREFIX); the
    publisher validates manifests before writing, so the prefix alone
    cannot distinguish them here. Anything else is forged.
    """
    if not isinstance(body, str):
        return False
    if body.startswith(PLAN_PREFIX) and parse_plan_body(body) is not None:
        return True
    if body.startswith(HANDOFF_PREFIX) and handoff_intent_valid(body):
        return True
    if body.startswith(HANDOFF_COMPLETE_PREFIX) and handoff_completion_valid(body):
        return True
    if body.startswith(REQUIREMENT_PREFIX):
        return True
    return False


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


def check_raw_request(args, attachments):
    """Reject a raw `comments add` request carrying a forged reserved body."""
    for body, source in raw_comment_bodies(args, attachments):
        check_comment_body(body, source)


def check_comment_body(body, source):
    """Reject forged reserved machine-record bodies with an actionable error."""
    match = reserved_match(body)
    if match is None:
        return
    if is_legitimate_writer(body):
        return
    _, kind, operation = match
    raise ValueError(
        'Refusing raw %s comment (%s): reserved %s write with '
        'structured validation; use %s. Ordinary prose remains valid.'
        % (source, kind, kind, operation)
    )
