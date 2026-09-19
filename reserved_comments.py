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
from handoff import INTENT_PREFIX as HANDOFF_PREFIX, COMPLETE_PREFIX as HANDOFF_COMPLETE_PREFIX
from lifecycle import PREFIX as LIFECYCLE_PREFIX
from review_workflow import PREFIX as REVIEW_PREFIX
from worker_gate import PREFIX as PLAN_PREFIX

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


def reserved_match(body):
    """Return (prefix, kind, operation) for a reserved body, else None."""
    if not isinstance(body, str):
        return None
    for prefix, kind, operation in RESERVED:
        if body.startswith(prefix):
            return (prefix, kind, operation)
    return None


def raw_comment_bodies(args, attachments):
    """Collect (body, source) pairs from a raw `comments add` request.

    Pure helper so the endpoint guard is unit-testable without fcntl.
    Returns [] for non-`comments add` commands.
    """
    if not isinstance(args, list) or len(args) < 2:
        return []
    if args[0] != 'comments' or args[1] != 'add':
        return []
    if not isinstance(attachments, dict):
        attachments = {}
    bodies = []
    file_body = None
    for i, a in enumerate(args):
        if not isinstance(a, str):
            continue
        if a.startswith('@attachment:'):
            key = a.partition(':')[2]
            item = attachments.get(key)
            if isinstance(item, dict) and isinstance(item.get('text'), str):
                bodies.append((item['text'], 'file-transport'))
                if item.get('flag') in ('--file', '-f'):
                    file_body = item['text']
        elif i == 3 and file_body is None:
            bodies.append((a, 'positional'))
    return bodies


def check_raw_request(args, attachments):
    """Reject a raw `comments add` request carrying a reserved body."""
    for body, source in raw_comment_bodies(args, attachments):
        check_comment_body(body, source)


def check_comment_body(body, source):
    """Reject reserved machine-record bodies with a precise actionable error."""
    match = reserved_match(body)
    if match is None:
        return
    _, kind, operation = match
    raise ValueError(
        'Refusing raw %s comment (%s): reserved %s write with '
        'structured validation; use %s. Ordinary prose remains valid.'
        % (source, kind, kind, operation)
    )
