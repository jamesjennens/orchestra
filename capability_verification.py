#!/usr/bin/env python3
"""Capability verification records and what readers conclude from them (.60 section 5).

Slice 1b of docs/CAPABILITY_INDEX_DESIGN.md (kittrial-5bb.69). A verification says
"at this commit, these pointers of this capability revision did (not) resolve". The
check itself runs in the caller's checkout (`capability check`, client-side); the
server can never repeat it, so everything a reader concludes rests on WHO wrote the
record.

Records are `Kind: capability-verification-v1` comments on the capability's closed
anchor. The closed field set: `schema_version`, `key`, `revision`, `record_sha256`,
`commit` (40 or 64 lowercase hex), `checked_at`, `source` (`ast` or `graphify`),
`graph_built_at_commit` (or null), `tool` (`{name, version}`), `results`
(`[{pointer, resolved, reason}]`, exactly the revision's pointers), `passed`,
`submitter` and `sha256`. `submitter` and `sha256` are written by the operation.

Two routes write them, exactly as for aliases (kittrial-5bb.67 review 01a0fc55):
- the endpoint (`capability verify`, what `capability check --record` posts) always
  writes `identity: unverified`. Over SSH the actor is self-declared, so the native
  author proves nothing there. Such a record can raise drift and reads `reported`;
  it never reads `verified` and never clears drift.
- the host command `admin.py capability-verify` writes `identity: verified`, after
  checking the actor against the operator allowlist or the `verifiers` list.

A reader TRUSTS a record only when it says `verified` AND its stored native author is
the record's actor AND that actor is on the operator allowlist or the verifiers list.

A revision's `verification`, in this order (.60 section 5.2):
1. `drifted`: some record for this revision failed, from anyone, and no trusted pass
   at an integrated commit comes after it in native order.
2. `verified`: the newest trusted record passed.
3. `reported`: there are passing reports, none trusted.
4. `unverified`: no record for this revision.
5. `superseded-revision`: records exist only for an older revision.

"After" is the record's position in the anchor's native comment sequence, never the
client-stamped `checked_at`, which is shown for information only.

An INTEGRATED commit is one some task's trusted lifecycle fact records as
`integration_commit` with `integrated=passed` (`lifecycle.integration_evidence`, the
reader `review` and `work` use), unless an honoured host-issued revert names that
commit on that task (`review_workflow.revert_records`: a retracted revert no longer
counts, so the integration counts again).

Bounds at write: an untrusted failing report is refused when its submitter already
holds 10 open failing reports in the project, or, for unverified submitters, when
their shared pool of 5 is full ("open" means not cleared by a trusted pass); at most
20 untrusted PASSING reports per capability revision. The two bounds are separate on
purpose: passing reports never use up room a failing report needs, so junk passes
cannot stop anyone from raising drift. Trusted records are never capped. Report text
(`reason`) is untrusted and never enters an error message.
"""
import json
import re

import keyed_records as core
from coordination import identifier
from export_requirements import parse_json
from recovery import configured_operators
from requirements import SHA256_TEXT, canonical_bytes, content_hash
from reserved_comments import CAPABILITY_VERIFICATION_PREFIX as PREFIX

FIELDS = ('schema_version', 'key', 'revision', 'record_sha256', 'commit', 'checked_at', 'source',
          'graph_built_at_commit', 'tool', 'results', 'passed', 'submitter', 'sha256')
PAYLOAD_FIELDS = FIELDS[:-2]
SUBMITTER_FIELDS = ('actor', 'person', 'identity', 'submitted_by_agent')
COMMIT = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
CHECKED_AT = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z')
TOOL_TEXT = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._+-]{0,39}')
REASON = re.compile(r'[a-z][a-z0-9-]{0,39}')
SOURCES = ('ast', 'graphify')
RESULTS_MAX = 80
BATCH_MAX = 500
# Open failing reports allowed (.60 section 5.1), keyed on the resolved person.
CAP_PERSON_FAILING = 10
CAP_UNVERIFIED_FAILING = 5
# Untrusted PASSING reports allowed per capability revision (plan answer b). Failing
# reports are bounded only by the open-failing pool above (review 01a0fe9e).
CAP_UNTRUSTED_REVISION = 20
# A read tests at most this many commits with narrow native reads before it falls
# back to one full export; a commit recorded by more tasks than this also falls back.
NARROW_COMMITS = 3
NARROW_TASKS = 5
STATES = ('drifted', 'verified', 'reported', 'unverified', 'superseded-revision')


# -- the record ---------------------------------------------------------------------------------

def _check_results(results):
    if not isinstance(results, list) or len(results) > RESULTS_MAX:
        raise ValueError('results: expected a list of at most %d pointer results' % RESULTS_MAX)
    seen = set()
    for item in results:
        if not isinstance(item, dict) or set(item) != {'pointer', 'resolved', 'reason'}:
            raise ValueError('results: each item needs exactly pointer, resolved and reason')
        if not isinstance(item['pointer'], str) or not item['pointer'] or item['pointer'] in seen:
            raise ValueError('results: each pointer is a nonempty text, named once')
        seen.add(item['pointer'])
        if item['resolved'] is not None and type(item['resolved']) is not bool:
            raise ValueError('results: resolved is true, false or null')
        if item['resolved'] is True:
            if item['reason'] is not None:
                raise ValueError('results: a resolved pointer carries no reason')
        elif not isinstance(item['reason'], str) or not REASON.fullmatch(item['reason']):
            raise ValueError('results: a pointer that did not resolve needs a short reason code')


def validate_payload(payload):
    """The caller's part of a verification: every field but `submitter` and `sha256`."""
    if not isinstance(payload, dict):
        raise ValueError('verification payload must be an object')
    core.checked_fields(payload, set(PAYLOAD_FIELDS), 'verification payload')
    missing = [name for name in PAYLOAD_FIELDS if name not in payload]
    if missing:
        raise ValueError('verification payload is missing ' + ', '.join(missing))
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        raise ValueError('schema_version must be the integer 1')
    import capability_records
    capability_records.valid_key(payload['key'])
    if type(payload['revision']) is not int or payload['revision'] < 1:
        raise ValueError('revision must be a positive integer')
    if not isinstance(payload['record_sha256'], str) or not SHA256_TEXT.fullmatch(payload['record_sha256']):
        raise ValueError('record_sha256 must be the SHA-256 of the revision that was checked')
    if not isinstance(payload['commit'], str) or not COMMIT.fullmatch(payload['commit']):
        raise ValueError('commit must be a full 40 or 64 character lowercase hex commit')
    if not isinstance(payload['checked_at'], str) or not CHECKED_AT.fullmatch(payload['checked_at']):
        raise ValueError('checked_at must be a UTC timestamp like 2026-10-02T12:00:00Z')
    if payload['source'] not in SOURCES:
        raise ValueError('source must be ast or graphify')
    built = payload['graph_built_at_commit']
    if built is not None and (not isinstance(built, str) or not re.fullmatch(r'[0-9a-f]{7,64}', built)):
        raise ValueError('graph_built_at_commit must be a hex commit or null')
    tool = payload['tool']
    if not isinstance(tool, dict) or set(tool) != {'name', 'version'} or any(
            not isinstance(tool[name], str) or not TOOL_TEXT.fullmatch(tool[name]) for name in ('name', 'version')):
        raise ValueError('tool must be {name, version}, each a short plain text')
    _check_results(payload['results'])
    resolved = [item['resolved'] for item in payload['results']]
    if type(payload['passed']) is not bool or payload['passed'] != all(value is True for value in resolved):
        raise ValueError('passed must be true exactly when every pointer resolved')
    if not payload['passed'] and False not in resolved:
        raise ValueError('a verification with pointers that could not be checked (resolved null) and none missing '
                         'is neither a pass nor a failure, and is not recorded')
    return payload


def parse(body):
    """The verification record iff it passes its full schema, else None."""
    if not isinstance(body, str) or not body.startswith(PREFIX):
        return None
    rest = body[len(PREFIX):]
    try:
        record = parse_json(rest)
        if not isinstance(record, dict) or set(record) != set(FIELDS):
            return None
        validate_payload({name: record[name] for name in PAYLOAD_FIELDS})
        submitter = record['submitter']
        if not isinstance(submitter, dict) or set(submitter) != set(SUBMITTER_FIELDS):
            return None
        identifier(submitter['actor'])
        if submitter['identity'] not in ('verified', 'unverified') or submitter['submitted_by_agent'] is not False:
            return None
        if (submitter['identity'] == 'verified') != isinstance(submitter['person'], str):
            return None
        if content_hash(record) != record['sha256'] or canonical_bytes(record).decode('utf-8') != rest:
            return None
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return None
    return record


def build(payload, actor, verified):
    """(record, body). `verified` is decided by the ROUTE, never by the actor's name."""
    record = {name: payload[name] for name in PAYLOAD_FIELDS}
    record['submitter'] = {'actor': actor, 'person': 'operator:' + actor if verified else None,
                           'identity': 'verified' if verified else 'unverified', 'submitted_by_agent': False}
    record['sha256'] = content_hash(record)
    body = PREFIX + canonical_bytes(record).decode('utf-8')
    if parse(body) != record:
        raise ValueError('Refusing to write a verification record that does not pass its own schema')
    return record, body


def read_record(view, body, comment):
    """The entry reader's hook: one verification in native order; a malformed one is
    reported and ignored, never fatal to the entry."""
    record = parse(body)
    if record is None:
        view['warnings'].append({'code': 'malformed-verification',
                                 'detail': 'verification record %s is malformed and ignored' % comment.get('id')})
        return
    records = view.setdefault('verification_records', [])
    records.append({'record': record, 'author': comment.get('author'), 'comment_id': comment.get('id'),
                    'position': len(records)})


# -- trust --------------------------------------------------------------------------------------

def trusted_actors(operators, verifiers):
    """The actors whose verified records count: the operator allowlist plus the verifiers list."""
    return configured_operators(operators if operators is not None else ()) | \
        configured_operators(verifiers if verifiers is not None else ())


def is_trusted(item, trusted):
    record = item['record']
    return (record['submitter']['identity'] == 'verified' and item['author'] == record['submitter']['actor']
            and item['author'] in trusted)


def is_lifecycle_row(row):
    """Whether an exported row can matter to the integrated-commit test: a lifecycle
    event row, or a task carrying a native `integrated:` label (no other task can hold a
    trusted integrated fact). A reader that streams an export keeps only these."""
    return isinstance(row, dict) and (row.get('issue_type') == 'event' or any(
        isinstance(label, str) and label.startswith('integrated:') for label in row.get('labels') or []))


def integrated_commits(rows, operators=None, journal=None):
    """Every integrated commit the given native rows record (lowercase).

    `rows` must hold each task with its lifecycle event rows (a full export, or the
    narrow per-task read `Integrated` makes). A scope counts when it records a trusted
    `integrated=passed` with an `integration_commit`, unless an honoured revert on that
    task names the commit.
    """
    from lifecycle import integration_evidence
    from review_state import reverts_by_task
    reverts, _ = reverts_by_task(rows, operators, journal)
    found = set()
    for task in integration_evidence(rows):
        removed = {str(item.get('integration_commit') or '').lower() for item in reverts.get(task['id']) or []}
        for scope in task['scopes']:
            commit = str((scope.get('scope') or {}).get('integration_commit') or '').lower()
            if commit and (scope.get('integrated') or {}).get('value') == 'passed' and commit not in removed:
                found.add(commit)
    return found


class Integrated:
    """`commit -> bool`: whether a commit is integrated, read as cheaply as the call allows.

    One instance serves one endpoint call, and nothing is read until a commit is asked
    about (only a trusted pass asks). Given the lifecycle rows of an export the caller
    already made (`refresh`, or a `list`/`find` whose catalog read exported), it answers
    from them and makes no native read at all. Otherwise each commit is tested with
    narrow native reads: one `bd list --desc-contains COMMIT` finds the lifecycle events
    that name it, then one `bd list --parent TASK` per task and one `bd show` of those
    tasks give exactly the rows `integrated_commits` needs. After NARROW_COMMITS
    distinct commits, or for a commit more than NARROW_TASKS tasks name, it makes ONE
    `bd export --all`, keeps only the lifecycle rows of it, and answers everything else
    from that. Results are memoised, so the set is computed at most once per call and
    never per row.
    """

    def __init__(self, run, operators=None, journal=None, export_rows=None):
        self.run, self.operators, self.journal = run, operators, journal
        self.known = {}
        self.export_rows = export_rows
        self.everything = None

    def _export(self):
        rows = self.export_rows
        if rows is None:
            # Only lifecycle rows are kept while the export streams by, so memory does
            # not grow with the project.
            rows = []
            for line in self.run(['export', '--all']).splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if is_lifecycle_row(row):
                    rows.append(row)
        self.everything = integrated_commits(rows, self.operators, self.journal)
        self.export_rows = None

    def _narrow(self, commit):
        listed = json.loads(self.run(['list', '--all', '--desc-contains', commit, '--limit', '0', '--json']) or '[]')
        tasks = []
        for row in listed or []:
            if not isinstance(row, dict) or row.get('issue_type') != 'event':
                continue
            for dependency in row.get('dependencies') or []:
                if not isinstance(dependency, dict) or dependency.get('type') != 'parent-child':
                    continue
                parent = dependency.get('depends_on_id')
                if isinstance(parent, str) and parent not in tasks:
                    tasks.append(parent)
        if not tasks:
            return False
        if len(tasks) > NARROW_TASKS:
            return None
        rows = []
        for task in tasks:
            children = json.loads(self.run(['list', '--all', '--parent', task, '--limit', '0', '--json']) or '[]')
            rows.extend(row for row in children or [] if isinstance(row, dict) and row.get('issue_type') == 'event')
        shown = json.loads(self.run(['show', *tasks, '--json', '--include-comments']) or '[]')
        if isinstance(shown, dict):
            shown = [shown]
        rows.extend(row for row in shown or [] if isinstance(row, dict) and row.get('id') in tasks)
        return commit in integrated_commits(rows, self.operators, self.journal)

    def __call__(self, commit):
        commit = str(commit or '').lower()
        if self.everything is None and self.export_rows is not None:
            self._export()
        if self.everything is not None:
            return commit in self.everything
        if commit not in self.known:
            answer = self._narrow(commit) if len(self.known) < NARROW_COMMITS else None
            if answer is None:
                self._export()
                return commit in self.everything
            self.known[commit] = answer
        return self.known[commit]


def _report(item):
    record = item['record']
    return {'commit': record['commit'], 'checked_at': record['checked_at'], 'passed': record['passed'],
            'source': record['source'], 'comment_id': item['comment_id'],
            'missing': [result['pointer'] for result in record['results'] if result['resolved'] is False][:10],
            'submitter': {'person': record['submitter']['person'] if item.get('trusted') else None,
                          'identity': 'verified' if item.get('trusted') else 'unverified'}}


def derive(records, revision, trusted, integrated):
    """The `verification` block of one revision (.60 section 5.2).

    `records` are the entry's parsed verification records in native order, `revision`
    the revision record a reader describes, `trusted` the actor set and `integrated`
    a `commit -> bool` callable, asked only about trusted passes.
    """
    block = {'state': 'unverified', 'revision': revision['revision'] if revision else None, 'verified_at': None,
             'report': None, 'drift': None, 'records': 0, 'open_failing': []}
    if revision is None:
        return block
    mine = [dict(item, trusted=is_trusted(item, trusted)) for item in records
            if item['record']['key'] == revision['key'] and item['record']['revision'] == revision['revision']
            and item['record']['record_sha256'] == revision['sha256']]
    block['records'] = len(mine)
    if not mine:
        if any(item['record']['key'] == revision['key'] and item['record']['revision'] < revision['revision']
               for item in records):
            block['state'] = 'superseded-revision'
        return block
    failing = [item for item in mine if not item['record']['passed']]
    cleared_at = -1
    if failing:
        # The newest trusted pass at an integrated commit clears every failure before it.
        for item in reversed(mine):
            if item['position'] < failing[0]['position']:
                break
            if item['trusted'] and item['record']['passed'] and integrated(item['record']['commit']):
                cleared_at = item['position']
                break
    open_failing = [item for item in failing if item['position'] > cleared_at]
    block['open_failing'] = open_failing
    trusted_records = [item for item in mine if item['trusted']]
    newest_trusted = trusted_records[-1] if trusted_records else None
    if open_failing:
        block.update(state='drifted', drift=_report(open_failing[-1]))
    elif newest_trusted is not None and newest_trusted['record']['passed']:
        record = newest_trusted['record']
        block.update(state='verified', verified_at={
            'commit': record['commit'], 'checked_at': record['checked_at'],
            'person': record['submitter']['person'], 'integrated': bool(integrated(record['commit']))})
    else:
        block['state'] = 'reported'
    block['report'] = _report(mine[-1])
    return block


def public(block):
    """The block as reads return it (the open failing records are an internal detail)."""
    shown = dict(block)
    shown['open_failing'] = len(block['open_failing'])
    return shown


# -- the write ----------------------------------------------------------------------------------

def _same_check(record, payload):
    """Whether a repeat says the same thing: the same pointers resolved. The stamp, the
    tool version and the index source may differ between two runs at one commit."""
    return record['passed'] == payload['passed'] and record['results'] == payload['results']


def verify(payload, actor, run, operators=None, verifiers=None, journal=None, operator=False):
    """Record one verification. The caller holds the project lock.

    `operator=False` is the endpoint route (`capability verify`): the record is written
    `unverified`, whatever actor the caller names. `operator=True` is the host route
    (`admin.py capability-verify`): the actor must be on the operator allowlist or the
    verifiers list, and the record is written `verified`.

    The record is bound to the revision a reader describes (the accepted revision, or
    the newest draft when none is accepted) by its exact `record_sha256`, and its
    results must name exactly that revision's pointers. Idempotent per
    `(key, revision, commit, actor, route)`: an identical repeat returns the first
    record, a different result under the same tuple is refused.
    """
    import capability_records as records
    trusted = trusted_actors(operators, verifiers)
    if operator and actor not in trusted:
        raise ValueError('Actor %s is not on the deployment operator allowlist or the verifiers list; only a '
                         'listed actor may record a verified capability check' % actor)
    validate_payload(payload)
    key = payload['key']
    rows = records.KIND.read_key_rows(run, key)
    entry = records.KIND.find_entry(rows, key, operators, view=records.entry_view)
    if entry['state'] in ('malformed', 'unsupported'):
        raise ValueError('Capability %s cannot be read (%s); it cannot be verified' % (key, entry['state']))
    if entry['state'] == 'superseded':
        raise ValueError('Capability %s is retired; verify its successor' % key)
    revision = records._newest(entry)
    if revision['revision'] != payload['revision'] or revision['sha256'] != payload['record_sha256']:
        raise ValueError('The check does not match the current revision of capability %s (revision %d); run '
                         'capability check again' % (key, revision['revision']))
    pointers = list(revision['code']) + list(revision['tests']) + list(revision['anchors'])
    if not pointers:
        raise ValueError('Capability %s revision %d has no code, test or anchor pointer, so there is nothing to '
                         'verify' % (key, revision['revision']))
    if sorted(item['pointer'] for item in payload['results']) != sorted(set(pointers)):
        raise ValueError('results must name exactly the pointers of revision %d of capability %s'
                         % (revision['revision'], key))
    existing = entry.get('verification_records') or []
    identity = 'verified' if operator else 'unverified'
    mine = [item for item in existing if item['record']['revision'] == revision['revision']
            and item['record']['record_sha256'] == revision['sha256']]
    for item in mine:
        record = item['record']
        if record['commit'] == payload['commit'] and item['author'] == actor \
                and record['submitter']['actor'] == actor and record['submitter']['identity'] == identity:
            if _same_check(record, payload):
                return {'key': key, 'revision': record['revision'], 'commit': record['commit'],
                        'passed': record['passed'], 'identity': identity, 'reconciled': True,
                        'comment_id': item['comment_id']}
            raise ValueError('You already recorded a different result for capability %s revision %d at this '
                             'commit; check at a new commit' % (key, revision['revision']))
    if not operator:
        # Passing and failing reports are bounded separately: a failing report is never
        # refused because passes filled the revision, so junk passes cannot silence
        # drift (.60 section 5.2). It answers only to the open-failing pool.
        if payload['passed']:
            passes = [item for item in mine if item['record']['passed'] and not is_trusted(item, trusted)]
            if len(passes) >= CAP_UNTRUSTED_REVISION:
                raise ValueError('Capability %s revision %d already has %d untrusted passing reports (the cap); '
                                 'an operator or listed verifier must verify it. A failing report is still '
                                 'accepted' % (key, revision['revision'], CAP_UNTRUSTED_REVISION))
        else:
            _check_failing_caps(run, operators, trusted, journal)
    record, body = build(payload, actor, operator)
    raw = run(['comments', 'add', entry['native_id'], body, '--json'])
    return {'key': key, 'revision': record['revision'], 'commit': record['commit'], 'passed': record['passed'],
            'identity': identity, 'reconciled': False, 'comment_id': core._comment_id(raw)}


def _check_failing_caps(run, operators, trusted, journal):
    """Refuse an untrusted failing report beyond the caps of .60 section 5.1.

    Every endpoint submitter is `unverified` until kittrial-5bb.68 binds SSH actors to
    people, so they share one pool; the per-person cap applies to a submitter a reader
    resolves to a person without trusting them as a verifier.
    """
    import capability_records as records
    rows, lifecycle = records.read_catalog(run)
    integrated = Integrated(run, operators, journal, export_rows=lifecycle)
    entries, _ = records.catalog(rows, operators)
    pool = 0
    for entry in entries:
        if entry['state'] in ('malformed', 'unsupported', 'superseded'):
            continue
        block = derive(entry.get('verification_records') or [], records._newest(entry), trusted, integrated)
        pool += sum(1 for item in block['open_failing'] if not item['trusted'])
    if pool >= CAP_UNVERIFIED_FAILING:
        raise ValueError('The shared pool of open failing reports from unverified submitters is full (%d per '
                         'project) until an operator or listed verifier records a passing check at an integrated '
                         'commit; a verified person may hold %d'
                         % (CAP_UNVERIFIED_FAILING, CAP_PERSON_FAILING))


def validate_batch(payload):
    """`admin.py capability-verify --file`: one payload, or `{schema_version, items: [...]}`."""
    if isinstance(payload, dict) and 'items' in payload:
        core.checked_fields(payload, {'schema_version', 'items'}, 'verification batch')
        if type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
            raise ValueError('schema_version must be the integer 1')
        items = payload['items']
        if not isinstance(items, list) or not 1 <= len(items) <= BATCH_MAX:
            raise ValueError('items: expected 1..%d verification payloads' % BATCH_MAX)
    else:
        items = [payload]
    for item in items:
        validate_payload(item)
    keys = [item['key'] for item in items]
    if len(set(keys)) != len(keys):
        raise ValueError('items must not repeat a capability key')
    return items


def verify_batch(payload, actor, run, operators=None, verifiers=None, journal=None, lock=None):
    """`admin.py capability-verify`: record trusted verifications, one lock hold per item.

    The actor is checked against the operator allowlist and the verifiers list before
    anything is read. Each item is one capability; a refusal (a stale revision, an
    unknown key) is reported for that item and the others still run. Re-running the
    same file is safe: an item already recorded is reported `already-recorded`.
    """
    import contextlib
    if actor not in trusted_actors(operators, verifiers):
        raise ValueError('Actor %s is not on the deployment operator allowlist or the verifiers list; only a '
                         'listed actor may record a verified capability check' % actor)
    items = validate_batch(payload)
    lock = lock or contextlib.nullcontext
    results = []
    for item in items:
        try:
            with lock():
                done = verify(item, actor, run, operators=operators, verifiers=verifiers, journal=journal,
                              operator=True)
        except ValueError as error:
            results.append({'key': item['key'], 'result': 'refused', 'reason': str(error)[:300]})
            continue
        results.append({'key': item['key'], 'result': 'already-recorded' if done['reconciled'] else 'recorded',
                        'revision': done['revision'], 'commit': done['commit'], 'passed': done['passed'],
                        'comment_id': done['comment_id']})
    return {'items': results, 'recorded': sum(1 for item in results if item['result'] != 'refused'),
            'refused': sum(1 for item in results if item['result'] == 'refused')}
