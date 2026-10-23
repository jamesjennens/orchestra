"""Strict HTTP adapter over the canonical release payloads (kittrial-5bb.378).

The endpoint holds the project, the configured authority store and the installation's
``lifecycle_authors`` rule. This module validates exactly the field set the web route
accepts and turns it into the unchanged native ``release-deploy`` / ``release-query``
payloads. It also resolves the caller's named targets against one native export, so a
target that is not integrated at the commits it names is reported as skipped with its
reason instead of refusing the whole group (the host's own writer refuses a NAMED target
that is not integrated; the web route is where a client's selection can be stale).

Nothing here decides authority: the route holds ``CAP_RELEASE`` (deploy) or
``CAP_COORDINATE`` (query), and the endpoint re-checks the live descriptor before any
write through ``http_authority.verified_web_authority`` / ``decide``.
"""
import lifecycle


class ReleaseError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


QUERY_FIELDS = frozenset({'scope'})
DEPLOY_FIELDS = frozenset({'scope', 'evidence', 'provenance', 'live_verified', 'targets'})
DEPLOY_OPTIONAL = frozenset(lifecycle.RELEASE_OPTIONAL_FIELDS)

#: The read-only query's operation id. It writes nothing, so a caller needs no
#: idempotency key: the canonical validator still wants a label and this constant one
#: can never collide with a deploy identity.
QUERY_OPERATION = 'release-query'

#: What a skipped target says, in the row the page and the caller both read.
SKIPPED_UNKNOWN = 'the task does not exist in this project'
SKIPPED_MALFORMED = 'the task row cannot be read'
SKIPPED_NOT_INTEGRATED = 'no passing integration at the named source and integration commit'


def fields(body):
    """Validate the HTTP body shape; return ``(operation, fields)`` or raise 422.

    ``body`` is the route's JSON object. ``operation`` is ``query`` or ``deploy``.
    The returned ``fields`` are copied; no authority or identity is accepted from the
    body (the server supplies the actor and the operation id).
    """
    if not isinstance(body, dict):
        raise ReleaseError(422, 'A release request takes one JSON object')
    operation = body.get('operation')
    if operation == 'query':
        if set(body) != {'operation'} | QUERY_FIELDS:
            raise ReleaseError(422, 'A release query takes exactly: scope')
        return 'query', {'scope': body['scope']}
    if operation != 'deploy':
        raise ReleaseError(422, 'Supply a release operation: query or deploy')
    for name in ('actor', 'operation_id', 'schema_version', 'dimension', 'value'):
        if name in body:
            raise ReleaseError(422, 'The server supplies %s; do not send it' % name)
    allowed = {'operation'} | DEPLOY_FIELDS | DEPLOY_OPTIONAL
    if not {'operation'} | DEPLOY_FIELDS <= set(body) or set(body) - allowed:
        raise ReleaseError(422, 'A release deploy takes: '
                           + ', '.join(sorted(DEPLOY_FIELDS | DEPLOY_OPTIONAL)))
    result = {key: body[key] for key in DEPLOY_FIELDS}
    for key in DEPLOY_OPTIONAL:
        if key in body:
            result[key] = body[key]
    # Validate every shape now, with a placeholder identity, so a malformed request is
    # refused before an endpoint round trip. The real identity is supplied by validate().
    deploy_native(result, 'validate', 'validate')
    return 'deploy', result


def deploy_native(fields, actor, operation_id):
    """Build and validate the unchanged native release-deploy payload."""
    payload = dict(schema_version=1, operation_id=operation_id, actor=actor,
                   dimension=lifecycle.RELEASE, value='passed',
                   scope=fields['scope'], evidence=fields['evidence'],
                   provenance=fields['provenance'], live_verified=fields['live_verified'],
                   targets=fields['targets'])
    for key in DEPLOY_OPTIONAL:
        if key in fields:
            payload[key] = fields[key]
    try:
        lifecycle.validate_release_payload(payload)
        # Bound new HTTP inputs without changing how historical native events validate.
        if len(payload['evidence']) > 50:
            raise ValueError('At most 50 evidence pointers may be supplied')
        for pointer in payload['evidence']:
            lifecycle.one_line(pointer, 'evidence pointer', 2000)
        for target in payload['targets']:
            if 'note' in target:
                lifecycle.one_line(target['note'], 'note')
    except (ValueError, TypeError, KeyError) as error:
        raise ReleaseError(422, str(error)) from None
    return payload


def query_native(fields, actor):
    """Build and validate the unchanged native release-query payload (writes nothing)."""
    payload = dict(schema_version=1, operation_id=QUERY_OPERATION, actor=actor,
                   dimension=lifecycle.RELEASE_QUERY, value='passed',
                   scope=fields['scope'], evidence=['HTTP release query'],
                   provenance='performed', targets=[], live_verified=False)
    try:
        lifecycle.validate_query_payload(payload)
    except (ValueError, TypeError, KeyError) as error:
        raise ReleaseError(422, str(error)) from None
    return payload


def validate(body, actor, operation_id=None):
    """Return ``(operation, native payload)`` for the endpoint.

    A deploy needs the endpoint request's stable operation identity; a query writes
    nothing and carries the constant read-only identity.
    """
    operation, values = fields(body)
    if operation == 'query':
        return operation, query_native(values, actor)
    if not isinstance(operation_id, str) or not lifecycle.LABEL.fullmatch(operation_id):
        raise ReleaseError(422, 'A stable operation identity is required for a release deploy')
    return operation, deploy_native(values, actor, operation_id)


def _issue(rows, task):
    for row in rows:
        if isinstance(row, dict) and row.get('id') == task and row.get('issue_type') != 'event':
            return row
    return None


def resolve_targets(rows, targets):
    """Split named targets into ``(resolved, skipped)`` against ONE native export.

    A target is resolved only when the export carries a passing ``integrated`` fact for
    that task at the exact ``source_commit`` and ``integration_commit`` it names. A
    missing or malformed task, or one without that integration, is skipped with a
    reason; it is not written and does not refuse the other targets. Everything else -
    a hold, a host-issued revert, a conflicting operation id - is left to
    ``lifecycle.apply_release``, which refuses the group exactly as the host command does.
    """
    evidence = {}
    for entry in lifecycle.scoped_evidence(rows, ('integrated',)):
        evidence[entry['id']] = entry
    resolved = []
    skipped = []
    for target in targets:
        task = target['task']
        issue = _issue(rows, task)
        if issue is None:
            skipped.append({'task': task, 'reason': SKIPPED_UNKNOWN})
            continue
        if issue.get('malformed'):
            skipped.append({'task': task, 'reason': SKIPPED_MALFORMED})
            continue
        scopes = (evidence.get(task) or {}).get('scopes') or []
        match = next((candidate for candidate in scopes
                      if (candidate.get('integrated') or {}).get('value') == 'passed'
                      and candidate['scope']['source_commit'] == target['source_commit']
                      and candidate['scope']['integration_commit'] == target['integration_commit']),
                     None)
        if match is None:
            skipped.append({'task': task, 'reason': SKIPPED_NOT_INTEGRATED})
            continue
        resolved.append(target)
    return resolved, skipped


def flags(receipts):
    """The per-target warning rows a release result carries, as plain dicts."""
    return [{'task': row.get('task'), 'flag': row.get('flag'),
             'reason': row.get('flag_reason'), 'current_contribution': row.get('current_contribution')}
            for row in receipts or [] if isinstance(row, dict) and row.get('flag')]
