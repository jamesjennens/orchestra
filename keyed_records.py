"""Shared keyed-record core: controlled labels, receipts, reconcile and F3 evidence.

One native issue per record carries controlled type/state labels and an append-only
ledger of reserved revision comments, plus operator acceptance evidence bound to an
exact revision content hash. Requirements (`requirement_records.py`), reference
catalog entries (`reference_records.py`) and the later proposal and capability
records share this machinery; each family is a `RecordSpec` (kittrial-5bb.66; the
.41 design section 12.1, .58 section 9.2, .60 section 3.1).

The core holds what is kind-independent:

- envelope checks (`refuse_injected_labels`, `checked_fields`, `text`,
  `positive_int`) and the deployment operator allowlist check;
- controlled labels (`controlled_labels`, `apply_controlled_labels`);
- the `.*-requests/` journal, its receipts (`validate_receipt` is the frozen receipt
  schema every record journal shares) and prior-receipt classification;
- revision and acceptance ledger reads over a spec's own strict parser;
- F3 acceptance binding (`bind_acceptance`, `bound_acceptance`);
- the single write entry point `apply_native(payload, actor, run, project, spec)`
  and `reconcile(..., spec)`.

Everything kind-specific - payload schema, record fields, how a new record is
created and selected, the revision and acceptance rules and the evidence record -
is a hook on the spec. The extraction is byte-neutral for requirements: the native
argv sequence, the revision and acceptance comment bytes and every receipt are
identical to the pre-extraction module (tests/test_keyed_records_bytes.py drives a
frozen copy of it and this core through the same scenarios).

Idempotency is native-first. A created record carries `request:<hash>` and
`request-content:<hash>` labels; the local receipts are the operator's recovery
cache, captured by the coordination backup sidecar, while the native record stays
authoritative. Preflight reads run BEFORE any receipt is written, so a refusal
reserves nothing; a receipt stays pending only for an uncertain real-write failure.
"""
import json
import time
from pathlib import Path

import record_json
from coordination import atomic, identifier
from recovery import configured_operators
from requirements import ACCEPTANCE_FIELDS, SHA256_TEXT, content_hash, load_json

ACCEPTANCE_EVIDENCE_FIELDS = tuple(name for name in ACCEPTANCE_FIELDS
                                   if name != 'manifest_sha256')
RECONCILE_RELEASABLE = ('failed', 'released')
RECEIPT_STATUSES = ('pending', 'complete', 'failed', 'released')
STAMP = '%Y-%m-%dT%H:%M:%SZ'


def now():
    return time.strftime(STAMP, time.gmtime())


class RecordSpec:
    """One keyed record family: its names, labels, journal and kind-specific steps.

    Values: `kind`, `noun` (used in messages), `type_labels`, `state_labels`
    (acceptance state -> label), `revision_prefix`, `acceptance_prefix`, `journal`,
    `key_regex`, `fields`, `allow_accepted_first_revision`, `supports_retire`,
    `accept_action`, `apply_command`, `reconcile_command`.

    Hooks, each a callable taking the arguments named in `apply_native`:
    `validate`, `refuse_before_journal`, `explicit_task`, `read_rows`, `read_created`, `resolve_task`,
    `check_key_unique`, `create_revision`, `create_args`, `after_create`,
    `prepare_row`, `existing_revisions`, `require_selectable`, `build_record`,
    `require_bound_key`, `check_revision`, `check_acceptance`, `acceptance_evidence`,
    `existing_acceptances`, `live_acceptances`, `revision_comment`, `apply_labels`, `result`.
    """

    VALUES = ('kind', 'noun', 'type_labels', 'state_labels', 'revision_prefix',
              'acceptance_prefix', 'journal', 'key_regex', 'fields',
              'allow_accepted_first_revision', 'supports_retire', 'accept_action',
              'apply_command', 'reconcile_command')
    HOOKS = ('validate', 'refuse_before_journal', 'explicit_task', 'read_rows', 'read_created',
             'resolve_task', 'check_key_unique', 'create_revision', 'create_args',
             'after_create', 'prepare_row', 'existing_revisions', 'require_selectable',
             'build_record', 'require_bound_key', 'check_revision', 'check_acceptance',
             'acceptance_evidence', 'existing_acceptances', 'live_acceptances', 'revision_comment',
             'apply_labels', 'result')

    def __init__(self, **values):
        missing = [name for name in self.VALUES + self.HOOKS if name not in values]
        extra = sorted(set(values) - set(self.VALUES + self.HOOKS))
        if missing or extra:
            raise TypeError('RecordSpec missing %s, unknown %s' % (missing, extra))
        self.__dict__.update(values)
        self.type_labels = frozenset(self.type_labels)
        self.controlled = frozenset(self.type_labels) | frozenset(self.state_labels.values())


# -- envelope ----------------------------------------------------------------------

def refuse_injected_labels(payload, where):
    """Fail closed on caller-supplied labels before any other validation."""
    if not isinstance(payload, dict):
        return
    if 'labels' in payload or 'add_labels' in payload:
        raise ValueError('Labels are controlled by the %s; arbitrary label writes are refused. '
                         'The operation derives the type/state labels from kind and acceptance_state.' % where)


def text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(where + ' must be a nonempty string')
    return value


def positive_int(value, where):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(where + ' must be a positive integer (not boolean)')
    return value


def checked_fields(payload, allowed, where):
    extra = sorted(set(payload) - set(allowed))
    if extra:
        raise ValueError('%s has unknown field(s): %s' % (where, ', '.join(extra)))


def require_configured_operator(actor, operators, action):
    """Refuse an actor outside the deployment operator allowlist before any write.

    The admin CLI reads the authority set from `deployment.private.json` with
    `operators(root, strict=True)` and supplies it here, exactly as `void-record`
    does: an empty authority authorizes nobody, and the shell-only
    `ORCHESTRA_OPERATORS` value that disagrees is refused by the strict read before
    this point. The check runs before the receipt journal directory or any native
    read/write, so a refused actor reserves nothing.

    `operators=None` is the direct library/contributor path and keeps the documented
    shell boundary; the admin CLI always supplies the set, so the operator route
    cannot skip the check.
    """
    if operators is None:
        return
    authority = configured_operators(operators)
    if not authority:
        raise ValueError('No operator allowlist is configured on the coordination host; add the acting '
                         'operator to deployment.private.json before you ' + action)
    if actor not in authority:
        raise ValueError('Actor ' + str(actor) + ' is not a server-side configured operator; only a '
                         'configured operator may ' + action)


# -- F3 acceptance binding ------------------------------------------------------------

def validate_acceptance_shape(acceptance):
    """Structural check for caller-supplied F3 acceptance evidence.

    The content hash is not caller-supplied: the command binds the acceptance object
    to the exact revision content hash it is about to write, stored as
    `record_sha256` (not `manifest_sha256`, which names a whole publication manifest
    and cannot exist when one record is accepted).
    """
    if not isinstance(acceptance, dict):
        raise ValueError('acceptance must be an object naming owners, approvers, policy, '
                         'decision_id and evidence')
    extra = sorted(set(acceptance) - set(ACCEPTANCE_EVIDENCE_FIELDS))
    if extra:
        raise ValueError('acceptance has unknown field(s): %s (record_sha256 is bound by the command)'
                         % ', '.join(extra))
    for name in ACCEPTANCE_EVIDENCE_FIELDS:
        if name not in acceptance:
            raise ValueError('acceptance is missing field ' + name)
    return acceptance


def bound_acceptance(acceptance):
    """Validate one record-level F3 decision bound to a revision content hash."""
    if not isinstance(acceptance, dict):
        raise ValueError('invalid acceptance evidence: acceptance must be an object')
    extra = sorted(set(acceptance) - set(ACCEPTANCE_EVIDENCE_FIELDS) - {'record_sha256'})
    if extra:
        raise ValueError('invalid acceptance evidence: unknown field(s) ' + ', '.join(extra))
    digest = acceptance.get('record_sha256')
    if not isinstance(digest, str) or not SHA256_TEXT.match(digest):
        raise ValueError('invalid acceptance evidence: record_sha256 must be the accepted '
                         'revision content hash')
    for name in ACCEPTANCE_EVIDENCE_FIELDS:
        if name not in acceptance:
            raise ValueError('invalid acceptance evidence: missing field ' + name)
    from requirements import _validate_acceptance
    full = {name: acceptance[name] for name in ACCEPTANCE_EVIDENCE_FIELDS}
    full['manifest_sha256'] = digest
    try:
        _validate_acceptance(full, {'sha256': digest})
    except ValueError as exc:
        raise ValueError('invalid acceptance evidence: %s' % exc) from None
    return acceptance


def bind_acceptance(acceptance, record):
    """Bind F3 evidence to the exact revision content hash via the contract validator.

    The contract validator checks owners/approvers/policy/decision/evidence and that
    the bound hash matches the accepted content. The stored object names the binding
    `record_sha256`, because the thing being accepted is one record revision, not a
    publication manifest.
    """
    validate_acceptance_shape(acceptance)
    bound = dict(acceptance)
    bound['record_sha256'] = record['sha256']
    bound_acceptance(bound)
    return bound


# -- labels and native reads ------------------------------------------------------------

def controlled_labels(spec, type_label, acceptance_state):
    return {type_label, spec.state_labels[acceptance_state]}


def apply_controlled_labels(run, task, current, spec, desired):
    """Move a record's controlled labels to exactly `desired`, removals first."""
    present = set(current or [])
    changed = False
    for label in sorted(spec.controlled - desired):
        if label in present:
            run(['update', task, '--remove-label', label, '--json'])
            changed = True
    for label in sorted(desired - present):
        run(['update', task, '--add-label', label, '--json'])
        changed = True
    return changed


def read_rows(run):
    return record_json.loads_rows(run(['export', '--all']))


def find(rows, task):
    return next((row for row in rows if row.get('id') == task), None)


def existing_ledger(row, prefix, parse, noun, what, key, belongs=None, keep=None):
    """A validated ledger of one reserved record kind on one row, keyed by `key`.

    Malformed input fails closed: a comment that claims the prefix must parse with the
    kind's own strict parser, must belong to this row (`belongs(record, row)`; by
    default its `id` is the row id), and must not conflict with another record for
    the same key.

    `keep(comment, record)`, when given, decides whether a well-formed record takes part
    in the ledger at all (an acceptance whose native author is not a configured operator
    does not). It runs after the strict parse, so a malformed comment still fails closed.

    For acceptance evidence (`what == 'acceptance'`) a second record for one revision is
    only a conflict when it carries a DIFFERENT decision; two records with the same
    decision are one decision recorded twice, and the first in native order stands
    (kittrial-5bb.92 review item 3).
    """
    found = {}
    task = row.get('id')
    if belongs is None:
        belongs = lambda record, row: record.get('id') == row.get('id')
    for comment in row.get('comments') or []:
        if not isinstance(comment, dict):
            raise ValueError('malformed comment on %s record %s' % (noun, task))
        body = comment.get('text')
        if not isinstance(body, str) or not body.startswith(prefix):
            continue
        record = parse(body)
        if record is None:
            raise ValueError('malformed %s %s comment on %s' % (noun, what, task))
        if not belongs(record, row):
            raise ValueError('%s %s comment on %s belongs to another record' % (noun, what, task))
        if keep is not None and not keep(comment, record):
            continue
        slot = record[key]
        prior = found.get(slot)
        if prior is not None:
            if prior != record and not (what == 'acceptance' and same_acceptance_decision(prior, record)):
                raise ValueError(conflict_message(noun, what, task))
            continue   # a duplicate of one decision: the first in native order stands
        found[slot] = record
    return found


def conflict_message(noun, what, task):
    if what == 'revision':
        return 'conflicting content for one id/revision: ' + str(task)
    return 'conflicting acceptance evidence for one revision on ' + str(task)


# The fields that identify one F3 acceptance decision. Two evidence records for one
# revision can both be live when an operator wrote the first, was removed, and another
# operator accepted: the writer must not rewrite that decision, but a second record that
# carries the SAME decision is not a conflict - the first in native order stands. Only a
# differing record hash or decision object is a conflict (kittrial-5bb.92 review item 3).
ACCEPTANCE_DECISION_FIELDS = ('record_sha256', 'decision')


def same_acceptance_decision(first, second):
    """True when two acceptance records carry the same decision for one revision."""
    return all(first.get(name) == second.get(name) for name in ACCEPTANCE_DECISION_FIELDS)


def latest_revision(existing):
    return existing[max(existing)] if existing else None


# -- journal and receipts ------------------------------------------------------------------

def journal_dir(project, name):
    journal = Path(project) / name
    if journal.is_symlink():
        raise ValueError('Refusing %s: the path is a symlink; no native write was attempted.' % name)
    try:
        journal.mkdir(exist_ok=True)
    except OSError as exc:
        raise ValueError('Cannot create %s: %s' % (name, exc))
    if not journal.is_dir():
        raise ValueError('%s is not a directory; refusing to write a receipt.' % name)
    return journal


def receipt_path(journal, identity, noun):
    receipt = journal / (identity + '.json')
    if receipt.is_symlink():
        raise ValueError('Refusing %s receipt %s: it is a symlink; no native write was attempted.'
                         % (noun, receipt.name))
    return receipt


def validate_receipt(record, backfill=False):
    """Structural check for one record journal receipt (backup/restore path).

    The frozen receipt schema every record journal shares (.41 10.3, .58 8.4, .60 9):
    a 64-hex `sha256`, a known `status`, optional nonempty `actor`/`id`, an optional
    `revision >= 1` and an optional bound `acceptance`. Unknown keys are kept. The
    journals are the operator's recovery cache; a backup must not carry a malformed
    receipt, or a restore would resurrect an unreadable one.
    """
    if not isinstance(record, dict):
        raise ValueError('Invalid requirement receipt')
    digest = record.get('sha256')
    if not isinstance(digest, str) or not SHA256_TEXT.match(digest):
        raise ValueError('Invalid requirement receipt content hash')
    if record.get('status') not in RECEIPT_STATUSES:
        raise ValueError('Invalid requirement receipt status')
    actor = record.get('actor')
    if actor is not None and (not isinstance(actor, str) or not actor.strip()):
        raise ValueError('Invalid requirement receipt actor')
    if backfill:
        records = record.get('records')
        if not isinstance(records, list) or not records or any(
                not isinstance(value, str) or not value.strip() for value in records):
            raise ValueError('Invalid requirement backfill receipt records')
    else:
        issue = record.get('id')
        if issue is not None and (not isinstance(issue, str) or not issue.strip()):
            raise ValueError('Invalid requirement receipt record id')
        revision = record.get('revision')
        if revision is not None and (isinstance(revision, bool)
                                     or not isinstance(revision, int) or revision < 1):
            raise ValueError('Invalid requirement receipt revision')
    if record.get('acceptance') is not None:
        bound_acceptance(record['acceptance'])
    return record


def prior_state(prior, digest, actor):
    """Classify an existing receipt for this operation ID.

    Returns 'none', 'same' (identical retry) or 'reusable' (operator released or
    marked failed). A conflict raises before any native write.
    """
    if not isinstance(prior, dict):
        return 'none'
    if prior.get('actor') and prior['actor'] != actor:
        raise ValueError('Operation ID already used by actor %r; use a new operation ID or have the operator '
                         'reconcile it.' % (prior['actor'],))
    if prior.get('status') in RECONCILE_RELEASABLE:
        return 'reusable'
    if prior.get('sha256') == digest:
        return 'same'
    raise ValueError('Operation ID already used for different content; use a new operation ID or have the '
                     'operator reconcile it.')


def uncertain(message, action, spec):
    return ValueError('%s The native %s did not confirm; the outcome is uncertain, so this operation ID stays '
                      'pending. Inspect native state and complete it with %s before '
                      'retrying.' % (message, action, spec.reconcile_command))


# -- the single write entry point ------------------------------------------------------------

def apply_native(payload, actor, run, project, spec, operator=False, operators=None):
    """Caller holds the canonical project lock; run(argv) invokes pinned bd.

    `operator=True` is the owner/operator route; only it may accept a record (or
    demote one, where the spec allows), and only with F3 acceptance evidence. An
    operator acceptance writes a durable acceptance record bound to the exact revision
    hash beside the revision comment, BEFORE the accepted revision and its state
    label, so a failed evidence write leaves the record reading as unaccepted rather
    than accepted without evidence.

    `operators` is the deployment operator allowlist; the admin CLI supplies
    `operators(root, strict=True)` and an actor outside it is refused before any
    journal or native write.
    """
    if operator:
        require_configured_operator(actor, operators, spec.accept_action)
    spec.validate(payload, operator)
    spec.refuse_before_journal(payload, operator)
    revision = spec.create_revision(payload)
    identity = content_hash({'operation_id': payload['operation_id']})
    digest = content_hash({'actor': actor, 'payload': payload})
    journal = journal_dir(project, spec.journal)
    receipt = receipt_path(journal, identity, spec.noun)
    prior = load_json(receipt) if receipt.exists() else None
    state = prior_state(prior, digest, actor)
    reusable = state == 'reusable'
    reconciled = state == 'same'
    explicit = spec.explicit_task(payload)
    if prior is not None and prior.get('id') and explicit and not reusable \
            and prior['id'] != explicit:
        raise ValueError('Operation ID already used for a different record')
    # `read_rows(run, payload)` is the kind's one preflight read for this write: a kind
    # whose records are found by label reads only the rows this payload can touch.
    rows = spec.read_rows(run, payload)
    created = False
    task = (prior.get('id') if prior and not reusable and prior.get('id') else None) or explicit
    if task is None:
        task = spec.resolve_task(rows, payload, operator)
    spec.check_key_unique(rows, payload, task)
    if task is None:
        request_label = 'request:' + identity
        matches = [row for row in rows if request_label in (row.get('labels') or [])]
        if len(matches) > 1:
            raise ValueError('Duplicate native %s records; operator reconciliation required' % spec.noun)
        if matches:
            if 'request-content:' + digest not in (matches[0].get('labels') or []):
                raise ValueError('Native %s content mismatch' % spec.noun)
            task = matches[0]['id']
            reconciled = True
        else:
            if prior is not None and not reusable:
                raise ValueError('Reserved %s request has no visible record; outcome uncertain. '
                                 'Operator must reconcile before any new request; do not allocate another ID.'
                                 % spec.noun)
            args = spec.create_args(payload, request_label, 'request-content:' + digest)
            # Preflight the identical create natively BEFORE any receipt or native
            # mutation: a bad parent or invalid field is refused here, so a refusal
            # reserves nothing and cannot burn the ID.
            run(args + ['--dry-run'])
            atomic(receipt, {'sha256': digest, 'status': 'pending', 'actor': actor,
                             'operation': payload['operation'], 'revision': revision})
            try:
                raw = run(args)
            except (ValueError, OSError) as refusal:
                raise uncertain(str(refusal), 'create', spec) from None
            try:
                issue = json.loads(raw)
            except (TypeError, ValueError):
                issue = None
            if not isinstance(issue, dict) or not issue.get('id'):
                raise uncertain('Create response was not a record.', 'create', spec)
            task = issue['id']
            created = True
            atomic(receipt, {'sha256': digest, 'status': 'pending', 'actor': actor, 'id': task,
                             'created': True, 'operation': payload['operation'], 'revision': revision})
            spec.after_create(run, task)
            rows = spec.read_created(run, task, payload)
    row = find(rows, task)
    if row is None:
        if prior is not None:
            raise ValueError('Recorded native %s record %s is not visible; outcome uncertain. '
                             'Operator must reconcile this operation ID.' % (spec.noun, task))
        raise ValueError('Unknown %s record: %s' % (spec.noun, task))
    existing = spec.existing_revisions(row)
    if not created:
        spec.require_selectable(row, payload, operator, existing)
        if prior is not None and prior.get('created') \
                and 'request-content:' + digest not in (row.get('labels') or []):
            raise ValueError('Native %s content mismatch' % spec.noun)
    revision, record = spec.build_record(payload, task, existing)
    spec.require_bound_key(task, payload, existing)
    spec.check_revision(payload, revision, existing, record, task)
    bound = spec.check_acceptance(payload, existing, record, operator, row)
    # Durable F3 acceptance evidence: a second reserved machine record bound to the
    # revision hash. Planned before any write so a conflicting decision is refused
    # with zero native writes, and skipped when this exact decision is already
    # recorded, so an idempotent retry adds nothing.
    evidence_body = None
    if bound is not None:
        evidence_record, candidate = spec.acceptance_evidence(
            bound, task, revision, record, actor)
        # Only acceptance evidence whose stored native author is a live configured
        # operator counts as a prior decision the writer must not rewrite. The
        # reference and capability readers apply the same author rule when they select
        # the accepted revision; a requirement record's reader does NOT - it reads the
        # controlled state label and the latest revision's `acceptance_state`, and the
        # durable evidence is only the binding an operator's apply left behind. A
        # planted acceptance (a contributor cannot write one, but a raw nudge can)
        # therefore never makes the operator's own apply return 0 without evidence
        # (kittrial-5bb.92 item 1, p74 5c; reader note corrected in item 3).
        prior_evidence = spec.live_acceptances(row, operators).get(revision)
        if prior_evidence is not None:
            if (prior_evidence.get('record_sha256') != evidence_record['record_sha256']
                    or prior_evidence.get('decision') != evidence_record['decision']):
                raise ValueError('Revision %d on %s already carries different acceptance evidence; '
                                 'operator reconciliation required before the decision can be '
                                 'rewritten.' % (revision, task))
            evidence_body = None
        else:
            evidence_body = candidate
    if prior is None or prior.get('status') != 'complete' or created:
        # A pending receipt is written only immediately before a real native write,
        # so every refusal above reserves nothing.
        atomic(receipt, {'sha256': digest, 'status': 'pending', 'actor': actor, 'id': task,
                         'created': created, 'operation': payload['operation'], 'revision': revision})
    # A crash between creating an anchor and its first record leaves an ordinary
    # row; the same operation's retry reaches this point and finishes it.
    spec.prepare_row(run, row)
    # Acceptance evidence is written BEFORE the accepted revision comment and the
    # accepted state label: with the evidence first, an uncertain evidence write
    # leaves the record reading as unaccepted, and the pending receipt is completed
    # by retry or reconcile.
    if evidence_body is not None:
        try:
            run(['comments', 'add', task, evidence_body, '--json'])
        except (ValueError, OSError) as refusal:
            raise uncertain(str(refusal), 'acceptance evidence', spec) from None
    body = spec.revision_comment(record)
    comment_id = next((comment.get('id') for comment in row.get('comments') or []
                       if isinstance(comment, dict) and comment.get('text') == body), None)
    if revision not in existing:
        try:
            raw = run(['comments', 'add', task, body, '--json'])
        except (ValueError, OSError) as refusal:
            raise uncertain(str(refusal), 'revision comment', spec) from None
        comment_id = _comment_id(raw)
    spec.apply_labels(run, task, row.get('labels') or [], payload, record)
    complete = {'sha256': digest, 'status': 'complete', 'actor': actor, 'id': task,
                'operation': payload['operation'], 'revision': revision}
    if bound is not None:
        complete['acceptance'] = bound
        complete['acceptance_record'] = True
    atomic(receipt, complete)
    return spec.result(payload, task, revision, record, created, reconciled, bound, comment_id)


def _comment_id(raw):
    """The id bd reports for a comment it just added, or None."""
    try:
        reply = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    reply = reply[0] if isinstance(reply, list) and reply else reply
    return reply.get('id') if isinstance(reply, dict) and isinstance(reply.get('id'), str) else None


def reconcile(project, operation_id, actor, reason, disposition, run, spec, issue_id=None,
              confirm=None):
    """Operator-only: resolve a stuck receipt from native state.

    * `complete` requires an explicit `--issue-id` and confirms that exact native
      record (and, for created records, its `request:` label) before the receipt is
      marked complete. `confirm(row)`, when given, is the kind's extra check of that
      row (a reference anchor must already hold its first revision record).
    * `failed`/`released` is allowed only when the receipt records no native record
      and no record carries the request label, so a released operation ID can be
      resubmitted.
    * Repeating an identical reconciliation is idempotent.
    """
    noun, title = spec.noun, spec.noun[:1].upper() + spec.noun[1:]
    identifier(operation_id)
    identifier(actor)
    if disposition not in ('failed', 'released', 'complete'):
        raise ValueError('Disposition must be failed, released or complete')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('A reconciliation reason is required')
    if disposition == 'complete' and not issue_id:
        raise ValueError('A complete disposition requires an explicit --issue-id naming the native record to confirm')
    if issue_id is not None and disposition != 'complete':
        raise ValueError('An issue ID applies only to a complete disposition')
    if issue_id is not None and (not isinstance(issue_id, str) or not issue_id.strip()):
        raise ValueError('Invalid issue ID')
    journal = journal_dir(project, spec.journal)
    identity = content_hash({'operation_id': operation_id})
    receipt = receipt_path(journal, identity, noun)
    if not receipt.exists():
        raise ValueError('No %s operation receipt exists for that operation ID' % noun)
    prior = load_json(receipt)
    if not isinstance(prior, dict) or not isinstance(prior.get('status'), str):
        raise ValueError('Malformed %s receipt; inspect before reconciling' % noun)
    at = now()
    audit = prior.get('reconciliation') or {}
    if prior['status'] in RECONCILE_RELEASABLE:
        same = (audit.get('actor') == actor and audit.get('reason') == reason
                and audit.get('disposition') == disposition)
        if same:
            return {'operation_id': operation_id, 'status': prior['status'], 'reconciled': False,
                    'already': True, 'reconciliation': audit}
        raise ValueError('Reconciliation conflict: operation %s is already %s by actor %r (reason %r); the '
                         'recorded audit is kept.' % (operation_id, prior['status'], audit.get('actor'),
                                                      audit.get('reason')))
    if prior['status'] == 'complete':
        same = (audit.get('actor') == actor and audit.get('reason') == reason
                and audit.get('disposition') == 'complete' and prior.get('id') == issue_id)
        if same:
            return {'operation_id': operation_id, 'status': 'complete', 'reconciled': False,
                    'already': True, 'id': prior.get('id'), 'reconciliation': audit}
        raise ValueError('Reconciliation conflict: operation %s is already complete by actor %r (reason %r, '
                         'id %r); the recorded audit is kept.' % (operation_id, audit.get('actor'),
                                                                  audit.get('reason'), prior.get('id')))
    if prior['status'] != 'pending':
        raise ValueError('%s operation is not pending; nothing to reconcile' % title)
    rows = spec.read_rows(run, None)
    found = [row for row in rows if 'request:' + identity in (row.get('labels') or [])]
    if len(found) > 1:
        raise ValueError('Duplicate native request records; manual operator reconciliation required')
    if disposition == 'complete':
        if prior.get('id') and prior['id'] != issue_id:
            raise ValueError('--issue-id %s does not match the receipt record %s' % (issue_id, prior['id']))
        if found and found[0].get('id') != issue_id:
            raise ValueError('--issue-id %s does not match the labelled native record %s'
                             % (issue_id, found[0].get('id')))
        if not found and not prior.get('id'):
            raise ValueError('No labelled native record exists for this request; completion needs one, so use '
                             '--disposition failed or released')
        row = find(rows, issue_id)
        if row is None:
            raise ValueError('Could not confirm native record %s; refusing to complete an unconfirmed receipt'
                             % issue_id)
        if confirm is not None:
            confirm(row)
        audit = {'actor': actor, 'reason': reason, 'disposition': 'complete', 'at': at,
                 'completed_from': 'native', 'issue': {'id': issue_id, 'title': row.get('title')}}
        updated = {'sha256': prior.get('sha256'), 'status': 'complete', 'actor': prior.get('actor') or actor,
                   'id': issue_id, 'reconciliation': audit}
        for name in ('operation', 'revision', 'created', 'acceptance'):
            if name in prior:
                updated[name] = prior[name]
        atomic(receipt, updated)
        return {'operation_id': operation_id, 'status': 'complete', 'reconciled': True, 'id': issue_id,
                'issue': audit['issue'], 'reconciliation': audit}
    if prior.get('id'):
        raise ValueError('The receipt records native record %s; releasing would discard the binding, so '
                         'complete it with --disposition complete --issue-id instead.' % prior['id'])
    if found:
        raise ValueError('A native record already carries request:%s; complete the receipt instead of '
                         'releasing it.' % identity)
    audit = {'actor': actor, 'reason': reason, 'disposition': disposition, 'at': at}
    updated = {'sha256': prior.get('sha256'), 'status': disposition, 'actor': prior.get('actor'),
               'reconciliation': audit}
    for name in ('operation', 'revision', 'error'):
        if name in prior:
            updated[name] = prior[name]
    if 'error' not in updated:
        updated['error'] = ('Released by the operator after confirming no native record was created.'
                            if disposition == 'released' else
                            'Marked failed by the operator after confirming no native record was created.')
    atomic(receipt, updated)
    return {'operation_id': operation_id, 'status': disposition, 'reconciled': True, 'reconciliation': audit}
