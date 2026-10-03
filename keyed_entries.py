"""Closed-anchor keyed entries: the layer reference and capability records share.

A keyed entry is one native `task` issue - an *anchor* - created and **closed** by
its `propose` before the first revision, carrying the controlled type and state
labels, a lookup label `<kind>-key:<key with . as ->` and the `request:` /
`request-content:` idempotency labels, with append-only revision comments and
operator acceptance evidence bound to the accepted revision's content hash (the .41
design sections 3-7, reused by .60 section 3.1).

This module is the kind-independent part of what kittrial-5bb.66 first wrote inside
`reference_records.py`, moved here unchanged in behaviour (kittrial-5bb.67) so the
capability records do not become a second copy (.60 section 11). An `AnchoredKind`
holds one kind's names and hooks; it builds that kind's `keyed_records.RecordSpec`
and gives it:

- the reads, which cost what they touch and take no coordination lock: `get` and
  every write read only their own key (one `bd list` by lookup label, one `bd show`);
  only `list` and reconcile read the catalog, with one `bd show` up to
  CATALOG_SHOW_MAX entries and one `bd export --all` above (kittrial-5bb.66 review
  01a0fbfd, kittrial-5bb.71);
- the write steps: create and close the anchor (and close it on a retry), the
  compare-and-swap `revise`, `accept` of the reviewed draft as revision+1 with
  identical content, the direct accepted revision 1 (`draft`), the evidence record
  and its strict parser, the controlled labels;
- the crash rule: readers recognise an entry only through
  `reserved_comments.is_record_anchor`, unchanged, so an anchor whose writer stopped
  before the first record is an ordinary closed row; the same operation's retry
  finishes it, another operation is refused that key, and readers report it as
  incomplete. When its payload is lost, the operator's `release` (`admin.py
  anchor-release`, kittrial-5bb.74) closes it and frees the key;
- the reader view: an acceptance counts only while its evidence comment's stored
  native author equals the evidence `operator` and is on the live deployment
  operator allowlist, otherwise it is inert (named, never silent); every entry fails
  alone (`malformed`, `unsupported`);
- operator voids (kittrial-5bb.74, the .41 design's section 3.7 repair): `admin.py
  void-record` with a target kind of this kind's family appends a `record-void-v1`
  comment (`apply_void`), and every read and write of the kind sees the anchor as
  `live_row` does, without the comments applied voids name. Validity is
  `recovery.records`, the review voids' own trust rule; a void of a record the entry
  reads is refused, so a void repairs a malformed, foreign or conflicting record and
  never withdraws one.
"""
import copy
import json
import re
import subprocess
from pathlib import Path

import keyed_records as core
import recovery
from coordination import atomic, identifier
from export_requirements import parse_json
from recovery import configured_operators
from requirements import SHA256_TEXT, canonical_bytes, content_hash, load_json
from reserved_comments import is_record_anchor, record_comment_kind

SHOW_CHUNK = 50
# The whole-catalog read uses one `bd show --include-comments` up to this many
# entries and one `bd export --all` above it (kittrial-5bb.66 review 01a0fbfd): on
# real bd a show costs about 45 ms per id (2.2 s per 50), so past a couple of dozen
# entries one export of the project is the cheaper read, as kittrial-5bb.71 found for
# the anchors read.
CATALOG_SHOW_MAX = 20
ISSUE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}')
COVERAGE_IDS = 10
RELEASE_REASON_MAX = 1000
CONTRIBUTOR_OPERATIONS = ('propose', 'revise')
OPERATOR_OPERATIONS = ('accept', 'draft')
WRITTEN_BY_THE_OPERATION = ('acceptance_state', 'successor', 'sha256', 'acceptance', 'labels')
ACCEPTANCE_RECORD_FIELDS = ('schema_version', 'source', 'id', 'key', 'revision', 'record_sha256',
                            'acceptance_state', 'decision', 'operator', 'at', 'sha256')
# A native read failure: the endpoint's runner raises ValueError, admin's run_bd
# raises CalledProcessError.
NATIVE_FAILURES = (ValueError, OSError, subprocess.CalledProcessError)


def read_labelled(run, label):
    """The rows carrying `label`, with their comments, in two native reads.

    One label-filtered `bd list` (no comments), then one `bd show --include-comments`
    when there are at most CATALOG_SHOW_MAX rows, or one `bd export --all` above that.
    """
    listed = json.loads(run(['list', '--label', label, '--all', '--limit', '0', '--json']) or '[]')
    ids = [row['id'] for row in listed or [] if isinstance(row, dict) and isinstance(row.get('id'), str)]
    if len(ids) <= CATALOG_SHOW_MAX:
        return AnchoredKind.shown(run, ids)
    wanted = set(ids)
    exported = (json.loads(line) for line in run(['export', '--all']).splitlines() if line.strip())
    return [row for row in exported if isinstance(row, dict) and row.get('id') in wanted]


def all_missing(error):
    """bd 1.2.2 `show` fails only when every id is missing ("no issue(s) found ...")."""
    text = ' '.join(str(part) for part in (error, getattr(error, 'stderr', ''), getattr(error, 'stdout', ''))
                    if part)
    return 'no issue' in text and 'found' in text


class AnchoredKind:
    """One keyed-entry kind: its names, its record parsers and its content hooks.

    Names: `noun` ('reference'), `title` ('Reference'), `command` ('ref'),
    `type_label`, `state_labels`, `key_prefix` ('reference-key:'), `family`
    ('reference-', the record-kind prefix family), `entry_prefix`,
    `acceptance_prefix`, `journal`, `source` (the evidence source, e.g.
    'reference-apply'), `accept_action`, `apply_command`, `reconcile_command`,
    `anchor_title`, `anchor_description` and `close_reason`.

    Hooks: `valid_key(value, where)`, `parse_entry(body)`, `validate_entry(record)`,
    `entry_record(payload, revision, state)`, `validate_content(payload)` (the
    kind's content fields, called for propose/revise/draft), `write_time_rules(record)`
    (date rules, checked before any write), `content_fields` (the payload content
    names), `pre_write(payload, run, operators)` (extra reads before the journal, e.g.
    link checks), `extra_records` (a mapping of further record kinds on the anchor to
    a reader `(view, body, comment) -> None` that may raise to mark the entry
    malformed) and `extra_parsers` (the same kinds to their strict parser
    `body -> record or None`, which `void_refusal` uses to tell a well-formed record
    from a malformed one).
    """

    def __init__(self, **values):
        defaults = {'pre_write': lambda payload, run, operators: None, 'extra_records': {}, 'extra_parsers': {},
                    'supports_retire': False}
        defaults.update(values)
        self.__dict__.update(defaults)
        self.propose_fields = frozenset(('schema_version', 'operation_id', 'operation', 'revision',
                                         'expected_sha256') + tuple(self.content_fields))
        self.accept_fields = frozenset(('schema_version', 'operation_id', 'operation', 'key', 'revision',
                                        'record_sha256', 'acceptance_state', 'acceptance'))
        self.direct_fields = self.propose_fields | {'acceptance_state', 'acceptance'}
        self.retire_fields = self.accept_fields | {'successor'}
        self.operator_operations = OPERATOR_OPERATIONS + (('retire',) if self.supports_retire else ())
        self.spec = core.RecordSpec(
            kind=self.noun, noun=self.noun, type_labels={self.type_label}, state_labels=self.state_labels,
            revision_prefix=self.entry_prefix, acceptance_prefix=self.acceptance_prefix, journal=self.journal,
            key_regex=None, fields=self.propose_fields, allow_accepted_first_revision=True,
            supports_retire=False, accept_action=self.accept_action, apply_command=self.apply_command,
            reconcile_command=self.reconcile_command,
            validate=lambda payload, operator: self.validate_payload(payload, operator=operator),
            refuse_before_journal=lambda payload, operator: None,
            explicit_task=lambda payload: None,
            read_rows=lambda run, payload=None: self.read_rows(run) if payload is None else self.read_key_rows(
                run, payload['key'], payload['operation_id']),
            read_created=lambda run, task, payload: self.shown(run, [task]),
            resolve_task=self.resolve_task,
            check_key_unique=self.check_key_unique,
            create_revision=self.create_revision,
            create_args=self.create_args,
            after_create=self.close,
            prepare_row=self.prepare_row,
            existing_revisions=self.existing_revisions,
            require_selectable=self.require_selectable,
            build_record=self.build_record,
            require_bound_key=self.require_bound_key,
            check_revision=self.check_revision,
            check_acceptance=self.check_acceptance,
            acceptance_evidence=lambda bound, task, revision, record, actor: self.acceptance_evidence(
                bound, task, revision, record, actor),
            existing_acceptances=self.existing_acceptances,
            revision_comment=self.entry_comment,
            apply_labels=self.apply_labels,
            result=self.result,
        )

    # -- labels and reads -------------------------------------------------------------------

    def key_label(self, key):
        return self.key_prefix + key.replace('.', '-')

    def key_labels(self, row):
        return [label for label in row.get('labels') or []
                if isinstance(label, str) and label.startswith(self.key_prefix)]

    def entry_belongs(self, record, row):
        return self.key_label(record['key']) in self.key_labels(row)

    def listed_ids(self, run, extra):
        listed = json.loads(run(['list', '--label', self.type_label, *extra, '--all', '--limit', '0',
                                 '--json']) or '[]')
        return [row['id'] for row in listed or [] if isinstance(row, dict) and isinstance(row.get('id'), str)]

    @staticmethod
    def shown(run, ids):
        """The named rows with their comments, in one `bd show --include-comments`."""
        if not ids:
            return []
        try:
            shown = json.loads(run(['show', *ids, '--json', '--include-comments']) or '[]')
        except NATIVE_FAILURES as error:
            if all_missing(error):
                return []   # every named row was deleted after the list
            raise
        shown = shown if isinstance(shown, list) else [shown]
        return [row for row in shown if isinstance(row, dict) and row.get('id') in ids]

    def read_rows(self, run):
        """The whole catalog: every labelled row with its comments (`read_labelled`).

        `list` and reconcile use it; `get` and the writes never do (they read by key
        label, `read_key_rows`).
        """
        return read_labelled(run, self.type_label)

    def read_key_rows(self, run, key, operation_id=None):
        """Only the rows one key can touch: its lookup label, plus this operation's request label.

        One `bd list` (type label AND (key label OR request label)) and one `bd show` of
        the one or two rows it names. This is the whole preflight read of a propose,
        revise or accept, and the read of `get`: its cost does not grow with the catalog.
        """
        labels = [self.key_label(key)]
        if operation_id is not None:
            labels.append('request:' + content_hash({'operation_id': operation_id}))
        return self.shown(run, self.listed_ids(run, ['--label-any', ','.join(labels)]))

    def existing_revisions(self, row):
        return core.existing_ledger(row, self.entry_prefix, self.parse_entry, self.noun, 'revision', 'revision',
                                    belongs=self.entry_belongs)

    def existing_acceptances(self, row):
        return core.existing_ledger(row, self.acceptance_prefix, self.parse_acceptance, self.noun, 'acceptance',
                                    'revision', belongs=lambda record, row: record['id'] == row.get('id')
                                    and self.key_label(record['key']) in self.key_labels(row))

    def anchor_for(self, rows, key):
        """(row, status) for a key: status is `entry`, `incomplete` or None."""
        matches = self.require_unique_key(rows, key)
        for row in matches:
            if not is_record_anchor(row):
                return row, 'incomplete'
            return row, 'entry'
        return None, None

    def require_unique_key(self, rows, key):
        """Every write refuses duplicate anchors, including incomplete/malformed ones.

        A readable different key sharing the lossy lookup slug is not this key.
        Unknown content cannot establish that distinction and requires repair.
        """
        matches = []
        for row in rows:
            if not isinstance(row, dict) or self.type_label not in (row.get('labels') or []) \
                    or self.key_label(key) not in self.key_labels(row):
                continue
            if is_record_anchor(row) and self.entry_view(row, ())['key'] not in (key, None):
                continue
            matches.append(row)
        if len(matches) > 1:
            raise ValueError('%s key %s has duplicate anchors (%s); every write is refused until an '
                             'operator reconciles them' % (self.title, key,
                                                          ', '.join(str(row['id']) for row in matches)))
        return matches

    # -- records -----------------------------------------------------------------------------

    def parse_acceptance(self, body):
        """The acceptance evidence iff it passes its full schema."""
        if not isinstance(body, str) or not body.startswith(self.acceptance_prefix):
            return None
        rest = body[len(self.acceptance_prefix):]
        try:
            record = parse_json(rest)
            if not isinstance(record, dict) or set(record) != set(ACCEPTANCE_RECORD_FIELDS):
                return None
            if type(record['schema_version']) is not int or record['schema_version'] != 1:
                return None
            if record['source'] != self.source:
                return None
            if not isinstance(record['id'], str) or not ISSUE_ID.fullmatch(record['id']):
                return None
            self.valid_key(record['key'])
            core.positive_int(record['revision'], 'revision')
            if not isinstance(record['record_sha256'], str) or not SHA256_TEXT.match(record['record_sha256']):
                return None
            if record['acceptance_state'] not in ('accepted', 'superseded'):
                return None
            decision = record['decision']
            if not isinstance(decision, dict) or set(decision) != set(core.ACCEPTANCE_EVIDENCE_FIELDS):
                return None
            core.bound_acceptance(dict(decision, record_sha256=record['record_sha256']))
            identifier(record['operator'])
            if not isinstance(record['at'], str) or not record['at'].strip():
                return None
            if content_hash(record) != record['sha256'] or canonical_bytes(record).decode('utf-8') != rest:
                return None
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            return None
        return record

    def entry_comment(self, record):
        body = self.entry_prefix + canonical_bytes(record).decode('utf-8')
        if self.parse_entry(body) != record:
            raise ValueError('Refusing to write a %s revision that does not pass its own schema' % self.noun)
        return body

    def acceptance_evidence(self, bound, task, revision, record, actor, at=None):
        """The durable acceptance record bound to one revision hash."""
        decision = {name: bound[name] for name in core.ACCEPTANCE_EVIDENCE_FIELDS}
        evidence = {'schema_version': 1, 'source': self.source, 'id': task, 'key': record['key'],
                    'revision': revision, 'record_sha256': bound['record_sha256'],
                    'acceptance_state': record['acceptance_state'], 'decision': decision,
                    'operator': actor, 'at': at or core.now()}
        evidence['sha256'] = content_hash(evidence)
        body = self.acceptance_prefix + canonical_bytes(evidence).decode('utf-8')
        if self.parse_acceptance(body) != evidence:
            raise ValueError('Refusing to write acceptance evidence that does not pass its own schema')
        return evidence, body

    # -- operator voids (kittrial-5bb.74) ------------------------------------------------------

    def record_slot(self, kind, comment, row):
        """The place one record comment holds in the entry, or None when the entry cannot read it.

        A revision holds `('revision', N)` and acceptance evidence `('acceptance', N)`;
        an alias or verification record holds a place of its own. A comment that fails
        its kind's strict parser, or that belongs to another anchor or key, holds none.
        """
        body = comment.get('text')
        if kind == self.family + 'entry':
            record = self.parse_entry(body)
            if record is None or not self.entry_belongs(record, row):
                return None
            return ('revision', record['revision'])
        if kind == self.family + 'acceptance':
            record = self.parse_acceptance(body)
            if record is None or record['id'] != row.get('id') \
                    or self.key_label(record['key']) not in self.key_labels(row):
                return None
            return ('acceptance', record['revision'])
        parse = self.extra_parsers.get(kind)
        if parse is not None and parse(body) is None:
            return None
        return ('record', str(comment.get('id')))

    def void_refusal(self, row, payload, dropped=()):
        """Why a valid void of a comment on this anchor does not apply, else None.

        `dropped` are the comments earlier applied voids already name. A void applies to
        a comment of one of this kind's record kinds that the entry cannot read
        (malformed, or another anchor's or key's), or to a LATER holder of a place an
        earlier live comment already holds (a conflicting or duplicated revision or
        acceptance). The earliest holder in native order is never voided: the writer
        never writes a second holder of a place, so only the first can be the legitimate
        record, and a void cannot itself be voided (kittrial-5bb.74 review, P2). A record
        the entry reads is the ledger itself, so a void of it is refused: withdrawing an
        entry is a new revision or a retirement, which keeps revision numbers monotonic
        for the writer and for an older kit.
        """
        kind = payload['target_kind']
        if not kind.startswith(self.family):
            return 'it targets a %s record, not a %s record' % (kind, self.noun)
        comments = [comment for comment in row.get('comments') or []
                    if isinstance(comment, dict) and str(comment.get('id')) not in dropped]
        target = next((comment for comment in comments if str(comment.get('id')) == payload['target']), None)
        if target is None:
            return 'its target %s is not on the anchor' % payload['target']
        slot = self.record_slot(kind, target, row)
        if slot is None:
            return None
        holders = [comment for comment in comments if isinstance(comment.get('text'), str)
                   and recovery.claims_kind(comment['text'], kind) and self.record_slot(kind, comment, row) == slot]
        if holders[0] is not target:
            return None
        if len(holders) > 1:
            place = ('revision %s' if slot[0] == 'revision' else 'the acceptance evidence for revision %s') % slot[1]
            return ('record %s is the earliest holder of %s, so it is the only one the writer can have written; '
                    'void the later holder %s instead' % (payload['target'], place, holders[1].get('id')))
        return ('record %s is a well-formed %s record the entry reads; a void repairs a malformed, foreign or '
                'conflicting record and never withdraws one' % (payload['target'], kind))

    def live_row(self, row, operators):
        """(row, notes): the anchor as this kind's readers and writers see it.

        A comment an applied operator void names is left out. Whether a void is valid is
        `recovery.records`, the trust rule review voids use: an exact record whose stored
        native author is the payload's operator and on the deployment allowlist
        (`operators`; None authorizes nobody), which follows its target in native order
        and preserves the target's exact bytes. A valid void then applies unless
        `void_refusal` refuses it. Nothing is deleted: the row returned is a copy, and the
        void and its target stay in native history, so the anchor stays hidden on every
        surface (`reserved_comments.is_record_anchor` reads the stored row). `notes` are
        entry warnings: `record-voided`, `void-refused` and `void-invalid`. Never raises.
        """
        applied, notes = self.applied_voids(row, operators)
        if not applied:
            return row, notes
        dropped = {payload['target'] for payload, _ in applied}
        return dict(row, comments=[comment for comment in row['comments']
                                   if not (isinstance(comment, dict) and str(comment.get('id')) in dropped)]), notes

    def applied_voids(self, row, operators):
        """(applied, notes): the voids that apply on this anchor, as (payload, comment) in
        native order, and the entry warnings `live_row` reports. Never raises."""
        comments = row.get('comments') if isinstance(row, dict) else None
        if not isinstance(comments, list) or not any(
                isinstance(comment, dict) and isinstance(comment.get('text'), str)
                and comment['text'].startswith(recovery.PREFIX) for comment in comments):
            return [], []
        try:
            voids, _, invalid = recovery.records(row, operators if operators is not None else ())
        except (AttributeError, TypeError, KeyError):
            return [], [{'code': 'void-invalid', 'detail': 'the void records on this anchor cannot be read'}]
        notes = [{'code': 'void-invalid',
                  'detail': 'void record %s is not applied: it is malformed, stale, out of order or not written by a '
                            'configured operator' % comment_id} for comment_id in invalid]
        applied, dropped = [], set()
        for payload, comment in voids:
            reason = self.void_refusal(row, payload, dropped)
            if reason is not None:
                notes.append({'code': 'void-refused',
                              'detail': 'void record %s is not applied: %s' % (comment.get('id'), reason)})
                continue
            dropped.add(payload['target'])
            applied.append((payload, comment))
            notes.append({'code': 'record-voided',
                          'detail': '%s record %s is voided by operator %s (void record %s)'
                                    % (payload['target_kind'], payload['target'], payload['operator'],
                                       comment.get('id'))})
        return applied, notes

    def live_rows(self, rows, operators):
        return [self.live_row(row, operators)[0] if isinstance(row, dict) else row for row in rows]

    def has_live_record(self, row, operators):
        """Whether the anchor still holds a record once applied voids are left out."""
        return is_record_anchor(self.live_row(row, operators)[0])

    # -- payloads ------------------------------------------------------------------------------

    def validate_payload(self, payload, operator=False):
        if not isinstance(payload, dict):
            raise ValueError('%s payload must be an object' % self.noun)
        core.refuse_injected_labels(payload, '%s operation' % self.noun)
        operation = payload.get('operation')
        allowed_operations = self.operator_operations if operator else CONTRIBUTOR_OPERATIONS
        if operation not in allowed_operations:
            raise ValueError('operation must be one of ' + ', '.join(allowed_operations))
        if not operator:
            supplied = [name for name in WRITTEN_BY_THE_OPERATION if name in payload]
            if supplied:
                raise ValueError('%s %s written by the operation, never caller-supplied; acceptance is the '
                                 'operator route (%s).'
                                 % (', '.join(supplied), 'is' if len(supplied) == 1 else 'are', self.apply_command))
        fields = {'propose': self.propose_fields, 'revise': self.propose_fields, 'accept': self.accept_fields,
                  'draft': self.direct_fields, 'retire': self.retire_fields}[operation]
        core.checked_fields(payload, fields, '%s payload' % self.noun)
        if type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
            raise ValueError('schema_version must be the integer 1')
        if 'operation_id' not in payload:
            raise ValueError('%s payload is missing field operation_id' % self.noun)
        identifier(payload['operation_id'])
        if 'key' not in payload:
            raise ValueError('%s payload is missing field key' % self.noun)
        self.valid_key(payload['key'])
        if operation == 'retire':
            if 'successor' not in payload:
                raise ValueError('retire needs successor, the key that replaces this one')
            self.valid_key(payload['successor'], 'successor')
            if payload['successor'] == payload['key']:
                raise ValueError('a key cannot be its own successor')
        if operation in ('accept', 'retire'):
            for name in ('revision', 'record_sha256', 'acceptance_state', 'acceptance'):
                if name not in payload:
                    raise ValueError('%s needs %s' % (self.apply_command.split()[-1], name))
            core.positive_int(payload['revision'], 'revision')
            if not isinstance(payload['record_sha256'], str) or not SHA256_TEXT.match(payload['record_sha256']):
                raise ValueError('record_sha256 must be the content hash of the revision being accepted')
        else:
            self.validate_content(payload)
            revision = payload.get('revision', 1 if operation != 'revise' else None)
            if operation == 'revise':
                if revision is None:
                    raise ValueError('revise needs the next revision number')
                core.positive_int(revision, 'revision')
                if revision < 2:
                    raise ValueError('revise writes revision 2 or later; use propose for revision 1')
                digest = payload.get('expected_sha256')
                if not isinstance(digest, str) or not SHA256_TEXT.match(digest):
                    raise ValueError('revise needs expected_sha256, the content hash of the newest revision '
                                     'it replaces')
            else:
                if revision != 1:
                    raise ValueError('%s creates revision 1; use revise for later revisions' % operation)
                if payload.get('expected_sha256') is not None:
                    raise ValueError('%s creates a new entry, so expected_sha256 must be null' % operation)
        if operator and operation == 'retire':
            if payload.get('acceptance_state') != 'superseded':
                raise ValueError('retire writes a superseded revision (acceptance_state must be superseded)')
            core.validate_acceptance_shape(payload.get('acceptance'))
            core.bound_acceptance(dict(payload['acceptance'], record_sha256='0' * 64))
        elif operator:
            if payload.get('acceptance_state') != 'accepted':
                raise ValueError('%s writes an accepted revision (acceptance_state must be accepted)'
                                 % self.apply_command.split()[-1])
            core.validate_acceptance_shape(payload.get('acceptance'))
            core.bound_acceptance(dict(payload['acceptance'], record_sha256='0' * 64))
        if operation not in ('accept', 'retire'):
            # Every content rule, including the date rules, is checked here, BEFORE the
            # anchor is created: a refusal after the create would leave an anchor with no
            # record. (An acceptance's content is the reviewed draft's; it is checked
            # before its writes, on an anchor that already exists.)
            state = 'accepted' if operation == 'draft' else 'draft'
            self.write_time_rules(self.entry_record(payload, payload.get('revision', 1), state))
        return payload

    # -- spec hooks ------------------------------------------------------------------------------

    def resolve_task(self, rows, payload, operator):
        if payload['operation'] in ('propose', 'draft'):
            return None
        row, status = self.anchor_for(rows, payload['key'])
        if row is None:
            raise ValueError('Unknown %s key %s; use %s list to see the catalog, or %s propose to '
                             'create it.' % (self.noun, payload['key'], self.command, self.command))
        if status == 'incomplete':
            raise ValueError('%s key %s is held by anchor %s, which has no revision record yet (an '
                             'interrupted propose); re-run that propose with its operation_id, or ask the operator '
                             'to reconcile it, or to release the anchor (admin.py anchor-release) if its payload is '
                             'lost.' % (self.title, payload['key'], row['id']))
        if payload['key'] not in {record['key'] for record in self.existing_revisions(row).values()}:
            # The lookup label is shared with a different key (a.b-c and a.b.c).
            raise ValueError('Unknown %s key %s; use %s list to see the catalog.'
                             % (self.noun, payload['key'], self.command))
        return row['id']

    def check_key_unique(self, rows, payload, task):
        # Also check receipt-bound retries: resolve_task may have been bypassed.
        self.require_unique_key(rows, payload['key'])
        if task is not None:
            return
        label = self.key_label(payload['key'])
        request = 'request:' + content_hash({'operation_id': payload['operation_id']})
        for row in rows:
            if self.type_label not in (row.get('labels') or []) or label not in self.key_labels(row):
                continue
            if request in (row.get('labels') or []):
                continue   # this operation's own interrupted anchor; the retry finishes it
            if not is_record_anchor(row):
                raise ValueError('%s key %s is held by anchor %s, which has no revision record yet (an '
                                 'interrupted propose by another operation); that operation must be re-run with '
                                 'its operation_id, or reconciled by the operator, who releases the anchor '
                                 '(admin.py anchor-release) if its payload is lost.'
                                 % (self.title, payload['key'], row['id']))
            revisions = self.existing_revisions(row)
            keys = {record['key'] for record in revisions.values()}
            if payload['key'] in keys or not keys:
                raise ValueError('%s key %s already exists (%s); use %s revise.'
                                 % (self.title, payload['key'], row['id'], self.command))
            raise ValueError('%s key %s collides with existing key %s (both use the lookup label %s); '
                             'choose a different key.' % (self.title, payload['key'], sorted(keys)[0], label))

    def create_args(self, payload, request_label, content_label):
        labels = sorted({self.type_label, self.state_labels['draft'], self.key_label(payload['key']),
                         request_label, content_label})
        return ['create', '--title', self.anchor_title % payload['key'],
                '--description', self.anchor_description % (payload['key'], payload['key']), '--type', 'task',
                '--no-inherit-labels', '--labels', ','.join(labels), '--json']

    def close(self, run, task):
        run(['close', task, '--reason', self.close_reason, '--json'])

    def prepare_row(self, run, row):
        """Close an anchor whose create committed but whose close did not (a retry)."""
        if row.get('status') != 'closed':
            self.close(run, row['id'])

    def require_selectable(self, row, payload, operator, existing):
        if self.type_label not in (row.get('labels') or []) or self.key_label(payload['key']) not in self.key_labels(row):
            raise ValueError('Record %s is not the %s anchor for key %s' % (row.get('id'), self.noun, payload['key']))
        unsupported = [kind for kind in (record_comment_kind(comment.get('text'))
                                         for comment in row.get('comments') or [] if isinstance(comment, dict))
                       if kind and kind[0].startswith(self.family) and kind[2] == 'unsupported']
        if unsupported:
            raise ValueError('%s %s carries a record kind this kit does not support (%s-v%s); an operator '
                             'must handle it with a kit that does.' % (self.title, payload['key'], unsupported[0][0],
                                                                       unsupported[0][1]))

    def build_record(self, payload, task, existing):
        operation = payload['operation']
        if operation == 'propose':
            record = self.entry_record(payload, 1, 'draft')
        elif operation == 'draft':
            record = self.entry_record(payload, 1, 'accepted')
        elif operation == 'revise':
            record = self.entry_record(payload, payload['revision'], 'draft')
        else:
            reviewed = existing.get(payload['revision'])
            if reviewed is None:
                raise ValueError('%s %s has no revision %d to %s' % (self.title, payload['key'], payload['revision'],
                                                                     operation))
            record = {name: value for name, value in reviewed.items() if name != 'sha256'}
            if operation == 'retire':
                record.update(revision=payload['revision'] + 1, acceptance_state='superseded',
                              successor=payload['successor'])
            else:
                record.update(revision=payload['revision'] + 1, acceptance_state='accepted', successor=None)
            record['sha256'] = content_hash(record)
            self.validate_entry(record)
        self.write_time_rules(record)
        return record['revision'], record

    def require_bound_key(self, task, payload, existing):
        keys = {record['key'] for record in existing.values()}
        if len(keys) > 1:
            raise ValueError('%s anchor %s has conflicting keys across revisions; operator reconciliation '
                             'required' % (self.title, task))
        if keys and keys != {payload['key']}:
            raise ValueError('%s anchor %s is keyed %s; a key swap is refused' % (self.title, task, next(iter(keys))))

    def check_revision(self, payload, revision, existing, record, task):
        operation = payload['operation']
        newest = max(existing) if existing else None
        if revision in existing:
            if existing[revision] == record:
                return   # an identical retry of a completed write
            if operation in ('accept', 'retire'):
                raise ValueError('Revision %d of %s is not the newest (revision %d exists); review the newest '
                                 'revision and accept that one' % (payload['revision'], payload['key'], newest))
            raise ValueError('revision %d of %s already exists with different content' % (revision, payload['key']))
        if operation in ('propose', 'draft'):
            if newest is not None:
                raise ValueError('%s key %s already exists; use %s revise' % (self.title, payload['key'], self.command))
            return
        if any(item['acceptance_state'] == 'superseded' for item in existing.values()):
            raise ValueError('%s %s is retired; propose a new key instead' % (self.title, payload['key']))
        if operation == 'revise':
            if revision != newest + 1:
                raise ValueError('revise must write revision %d of %s (requested %d)'
                                 % (newest + 1, payload['key'], revision))
            if existing[newest]['sha256'] != payload['expected_sha256']:
                raise ValueError('expected_sha256 does not match revision %d of %s; re-read it with %s get and '
                                 'revise from the current content' % (newest, payload['key'], self.command))
            return
        # accept: the reviewed revision must still be the newest, with the reviewed hash.
        if newest != payload['revision']:
            raise ValueError('Revision %d of %s is not the newest (revision %d exists); review the newest revision '
                             'and accept that one' % (payload['revision'], payload['key'], newest))
        if existing[payload['revision']]['sha256'] != payload['record_sha256']:
            raise ValueError('record_sha256 does not match revision %d of %s' % (payload['revision'], payload['key']))

    def check_acceptance(self, payload, existing, record, operator, row):
        if not operator:
            return None
        return core.bind_acceptance(payload['acceptance'], record)

    def apply_labels(self, run, task, current, payload, record):
        if record['acceptance_state'] == 'superseded':
            desired = {self.type_label, self.state_labels['superseded']}
            core.apply_controlled_labels(run, task, current, self.spec, desired)
            return
        accepted = record['acceptance_state'] == 'accepted' or self.state_labels['accepted'] in (current or [])
        desired = {self.type_label, self.state_labels['accepted' if accepted else 'draft']}
        core.apply_controlled_labels(run, task, current, self.spec, desired)

    def result(self, payload, task, revision, record, created, reconciled, bound, comment_id=None):
        result = {'key': payload['key'], 'revision': revision, 'native_id': task,
                  'record_comment_id': comment_id, 'state': record['acceptance_state'],
                  'created': created, 'reconciled': reconciled}
        if bound is not None:
            result['acceptance'] = bound
        return result

    def create_revision(self, payload):
        if payload['operation'] in ('accept', 'retire'):
            return payload['revision'] + 1
        return payload.get('revision', 1)

    # -- write entry points ----------------------------------------------------------------------

    def apply_native(self, payload, actor, run, project, operator=False, operators=None):
        """`operators` is the deployment allowlist. On the operator route it authorizes the
        actor; on both routes it decides which operator voids apply, so the endpoint
        supplies it to contributor writes too (it authorizes nothing there)."""
        if operator:
            core.require_configured_operator(actor, operators, self.spec.accept_action)
        self.validate_payload(payload, operator=operator)
        self.pre_write(payload, run, operators)
        return core.apply_native(payload, actor, run, project, self.write_spec(operators), operator=operator,
                                 operators=operators)

    def write_spec(self, operators):
        """The kind's spec for one write, whose reads see each row as `live_row` does.

        The writer then agrees with the readers: a comment an applied operator void names
        takes no part in compare-and-swap, the key checks or the acceptance ledger.
        """
        spec = copy.copy(self.spec)
        spec.read_rows = lambda run, payload=None: self.live_rows(self.spec.read_rows(run, payload), operators)
        spec.read_created = lambda run, task, payload: self.live_rows(self.spec.read_created(run, task, payload),
                                                                      operators)
        return spec

    def apply_void(self, payload, actor, run, operators):
        """`admin.py void-record` for a record on this kind's anchor `payload['task']`.

        Host route only, exactly like a review void: the actor must be on the deployment
        operator allowlist before any read (`recovery.authorize`), the payload is bound to
        that actor, and an identical retry is idempotent while a reused operation ID or a
        second void of one target is refused (`recovery.earlier`). The target must be a
        comment of the declared kind, one of this kind's, on this kind's anchor, with its
        exact bytes preserved, and `void_refusal` must not refuse it. The void is one
        appended native comment. Like a contribution-review void it needs no host
        journal: its own native record is all a reader checks, and a new sidecar path
        would make a backup unrestorable by an older kit.
        """
        recovery.authorize(actor, operators)
        payload = recovery.bound(payload, actor, payload.get('task') if isinstance(payload, dict) else None)
        if not payload['target_kind'].startswith(self.family):
            raise ValueError('Operator void target kind %s is not a %s record kind' % (payload['target_kind'],
                                                                                    self.noun))
        rows = self.shown(run, [payload['task']])
        if len(rows) != 1 or rows[0].get('issue_type') == 'event':
            raise ValueError('Task missing, duplicated or is an event')
        row = rows[0]
        if self.type_label not in (row.get('labels') or []) or not self.key_labels(row):
            raise ValueError('%s is not a %s anchor; a %s void targets a record on one'
                             % (row.get('id'), self.noun, payload['target_kind']))
        prior = recovery.earlier(recovery.records(row, operators)[0], payload, actor)
        if prior is not None:
            return dict(comment_id=str(prior[1]['id']), reconciled=True, target=prior[0]['target'])
        raw = recovery.target_text(row, payload['target'])
        if not recovery.claims_kind(raw, payload['target_kind']):
            raise ValueError('Operator void target is not a %s record: %s' % (payload['target_kind'],
                                                                              payload['target']))
        if not recovery.preserves(raw, payload):
            raise ValueError('Operator void record must preserve the exact current bytes of ' + payload['target'])
        dropped = {applied['target'] for applied, _ in self.applied_voids(row, operators)[0]}
        reason = self.void_refusal(row, payload, dropped)
        if reason is not None:
            raise ValueError('Operator void refused for %s: %s' % (payload['target'], reason))
        raw = run(['comments', 'add', row['id'], recovery.PREFIX + canonical_bytes(payload).decode('utf-8'), '--json'])
        return dict(comment_id=core._comment_id(raw), reconciled=False, target=payload['target'])

    def confirm_anchor(self, row, operators=None):
        """A `complete` reconcile needs the anchor to hold a live record: one no applied
        operator void names (`operators` is the deployment allowlist)."""
        if not self.has_live_record(row, operators):
            raise ValueError('Anchor %s has no %s revision record yet (or every record it held is voided); re-run '
                             'the original %s propose with the same operation_id to finish it, then reconcile. If '
                             'that payload is lost, release the anchor with admin.py anchor-release.'
                             % (row.get('id'), self.noun, self.command))

    def reconcile(self, project, operation_id, actor, reason, disposition, run, issue_id=None, operators=None):
        return core.reconcile(project, operation_id, actor, reason, disposition, run, self.spec,
                              issue_id=issue_id, confirm=lambda row: self.confirm_anchor(row, operators))

    def release(self, project, issue_id, actor, reason, run, operators=None, duplicate=False,
                set_aside_evidence=False):
        """`admin.py anchor-release` (operator): close an anchor that holds no record and free its key.

        With `duplicate` it is the other mode, `release_duplicate`: a NAMED anchor of a
        duplicated key that may hold well-formed records (kittrial-5bb.91). What follows
        describes the plain mode.

        An anchor holds no record when its propose stopped between the create and the first
        record (still open if it stopped before the close), or when operator voids name
        every record it carried. Only the same operation's retry can finish it, so once
        the original payload is lost its lookup label blocks the key for good, and an
        open one is claimable as an ordinary task. The release:

        - is refused for an actor outside the deployment allowlist before any read, for a
          row that is not this kind's anchor, and for one that still holds a live record
          (or a record this kit cannot read);
        - marks each pending receipt the row's `request:` labels name as `released`, with
          the audit, so the operation ID is settled and a retry of it starts a new anchor;
        - closes the row if it is open, so it is never claimable;
        - appends one plain audit comment, then removes the state, `request:` and
          `request-content:` labels and, last, the lookup label, which frees the key.

        The type label stays: an anchor whose records were voided keeps those comments
        and stays hidden as an anchor, and a row that never held a record reads as an
        ordinary closed task, as it already did. Nothing is deleted. Each step is skipped
        when already done, so a re-run after an uncertain write finishes the release;
        once the lookup label is gone the row is no longer an anchor and a re-run is
        refused.
        """
        core.require_configured_operator(actor, operators, 'release a %s anchor' % self.noun)
        if not isinstance(issue_id, str) or not ISSUE_ID.fullmatch(issue_id):
            raise ValueError('Invalid issue ID')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > RELEASE_REASON_MAX \
                or '\x00' in reason:
            raise ValueError('A release reason is required (at most %d characters)' % RELEASE_REASON_MAX)
        if set_aside_evidence and not duplicate:
            raise ValueError('--set-aside-evidence applies only with --duplicate')
        rows = self.shown(run, [issue_id])
        if len(rows) != 1:
            raise ValueError('Unknown %s anchor %s' % (self.noun, issue_id))
        row = rows[0]
        labels = [label for label in row.get('labels') or [] if isinstance(label, str)]
        keys = sorted(self.key_labels(row))
        if self.type_label not in labels or not keys:
            raise ValueError('%s carries no %s and %s labels: it is not a %s anchor, or it was already released'
                             % (issue_id, self.type_label, self.key_prefix, self.noun))
        extra = {}
        if duplicate:
            note, extra = self.duplicate_release(row, keys, actor, reason, run, operators, set_aside_evidence)
            close_reason = '%s anchor released by the operator (duplicate of its key)' % self.title
        else:
            live, _ = self.live_row(row, operators)
            for comment in live.get('comments') or []:
                kind = record_comment_kind(comment.get('text') if isinstance(comment, dict) else None)
                if kind and (kind[0].startswith(self.family) or kind[0] == 'unknown'):
                    raise ValueError('%s anchor %s still holds a record, so it is an entry, not an orphan; an '
                                     'operator void (admin.py void-record) repairs a malformed record; a duplicate '
                                     'of its key is released with --duplicate' % (self.title, issue_id))
            note = ('Released by operator %s: this %s anchor held no record, so %s no longer holds its key. '
                    'Reason: %s' % (actor, self.noun, ', '.join(keys), reason))
            close_reason = '%s anchor released by the operator (no record)' % self.title
        journal = Path(project) / self.journal
        pending = []
        for label in sorted(labels):
            identity = label[len('request:'):] if label.startswith('request:') else None
            if identity is None or not SHA256_TEXT.match(identity) or not journal.is_dir() or journal.is_symlink():
                continue
            receipt = core.receipt_path(journal, identity, self.noun)
            prior = load_json(receipt) if receipt.exists() else None
            if not isinstance(prior, dict) or prior.get('status') != 'pending':
                continue
            if prior.get('id') not in (None, issue_id):
                raise ValueError('The receipt for %s records native record %s, not %s; reconcile it with %s first'
                                 % (label, prior['id'], issue_id, self.reconcile_command))
            pending.append((identity, receipt, prior))
        at = core.now()
        for identity, receipt, prior in pending:
            audit = {'actor': actor, 'reason': reason, 'disposition': 'released', 'at': at,
                     'released_anchor': issue_id}
            updated = {'sha256': prior.get('sha256'), 'status': 'released', 'actor': prior.get('actor'),
                       'reconciliation': audit,
                       'error': 'Released by the operator with anchor %s, %s.'
                                % (issue_id, 'a duplicate of its key' if duplicate else 'which held no record')}
            for name in ('operation', 'revision'):
                if name in prior:
                    updated[name] = prior[name]
            atomic(receipt, updated)
        closed = row.get('status') != 'closed'
        if closed:
            run(['close', issue_id, '--reason', close_reason, '--json'])
        if not any(isinstance(comment, dict) and comment.get('text') == note for comment in row.get('comments') or []):
            run(['comments', 'add', issue_id, note, '--json'])
        removed = sorted(label for label in labels if label in self.state_labels.values()
                         or label.startswith(('request:', 'request-content:'))) + keys
        for label in removed:
            run(['update', issue_id, '--remove-label', label, '--json'])
        return dict({'released': issue_id, 'kind': self.noun, 'closed': closed, 'labels_removed': removed,
                     'receipts_released': [identity for identity, _, _ in pending]}, **extra)

    def key_anchors(self, run, row):
        """(key, anchors): every anchor of the key the named `row` holds, counted exactly as
        every write counts them (`require_unique_key`).

        The rows carrying the type label and the row's lookup label, leaving out a record
        anchor whose readable record names a different key (a slug collision). `key` is the
        exact key when one of them can state it, else None.
        """
        label = self.key_labels(row)[0]
        rows = self.shown(run, self.listed_ids(run, ['--label', label]))
        rows = [item for item in rows if self.type_label in (item.get('labels') or [])
                and label in self.key_labels(item)]
        if not any(item.get('id') == row.get('id') for item in rows):
            rows.append(row)
        readable = {item.get('id'): self.entry_view(item, ())['key'] if is_record_anchor(item) else None
                    for item in rows}
        key = readable.get(row.get('id')) or next((value for value in readable.values() if value), None)
        return key, sorted((item for item in rows if readable[item.get('id')] in (key, None)),
                           key=lambda item: str(item.get('id')))

    def readable_revisions(self, row, operators):
        """[{revision, sha256}] of the revisions one anchor holds that this kit reads and no
        applied void names; [] when it holds none or they do not parse."""
        try:
            return [{'revision': number, 'sha256': record['sha256']}
                    for number, record in sorted(self.existing_revisions(self.live_row(row, operators)[0]).items())]
        except ValueError:
            return []

    def acceptance_on(self, row, operators):
        """The acceptance evidence on one anchor that no applied void names:
        [{revision, record_sha256, operator, decision_id, at, live}]. `live` is the reader's
        rule: the stored native author is the record's operator and is on the allowlist."""
        allowlist = configured_operators(operators if operators is not None else ())
        found = []
        for comment in self.live_row(row, operators)[0].get('comments') or []:
            record = self.parse_acceptance(comment.get('text') if isinstance(comment, dict) else None)
            if record is None or record['id'] != row.get('id'):
                continue
            author = comment.get('author')
            found.append({'revision': record['revision'], 'record_sha256': record['record_sha256'],
                          'operator': record['operator'], 'decision_id': record['decision'].get('decision_id'),
                          'at': record['at'], 'live': author == record['operator'] and author in allowlist})
        return found

    def selected_anchor(self, anchors, operators):
        """The native id of the anchor a reader selects for the key among `anchors`, or None
        when it reads conflicted (or nothing is left)."""
        entries, _ = self.catalog(anchors, operators)
        chosen = [entry for entry in entries if entry['state'] not in ('conflicted', 'incomplete')]
        return chosen[0]['native_id'] if len(entries) == 1 and chosen else None

    def duplicate_release(self, row, keys, actor, reason, run, operators, set_aside_evidence):
        """The checks of `anchor-release --duplicate` (kittrial-5bb.91): (audit note, result fields).

        Since kittrial-5bb.83 every write on a key with more than one anchor is refused. A
        duplicate that holds no record is released by the plain mode; this mode releases a
        NAMED duplicate that holds well-formed records, which a void cannot repair (a void
        never withdraws a record the entry reads). Refused, before any write:

        - the row carries more than one lookup label;
        - the key is not duplicated (fewer than two anchors, counted as every write counts);
        - the named anchor holds a record this kit cannot read;
        - the named anchor holds a readable record and no remaining anchor does (a
          record-less, voided-only or malformed anchor does not count): the key is never
          left with nothing readable. Release the other anchor first;
        - the named anchor carries acceptance evidence that no void names, live or inert,
          unless `set_aside_evidence`. That covers the anchor the reader selects: among
          duplicates only one live acceptance selects an anchor (`select_entries`), so the
          selected anchor always carries live evidence and needs no rule of its own;
        - the named anchor READS an accepted record (its `entry_view`) and no remaining
          anchor reads one, so the key would lose its only accepted record. Live evidence
          on a remaining anchor is not enough: an accept that died after its evidence and
          before the accepted revision leaves evidence and a draft (review of febaad7).

        At least one other anchor always remains: that is what "duplicated" means, and the
        caller holds the project coordination lock from this read to the label removal.
        A released row is no longer an anchor of the key and is never read for it again,
        so evidence set aside stays set aside even if its operator is re-added.
        """
        issue_id = row.get('id')
        if len(keys) != 1:
            raise ValueError('%s carries more than one %s label (%s), so the key it duplicates is ambiguous; '
                             'use the host procedure' % (issue_id, self.key_prefix, ', '.join(keys)))
        key, anchors = self.key_anchors(run, row)
        remaining = [item for item in anchors if item.get('id') != issue_id]
        shown_key = key or keys[0]
        if not remaining:
            raise ValueError('%s key %s is not duplicated: %s is its only anchor, and the last anchor of a key is '
                             'never released this way. An orphan is released without --duplicate; an entry is '
                             'withdrawn by a new revision or a retirement' % (self.title, shown_key, issue_id))
        view = self.entry_view(row, operators)
        if view['state'] == 'unsupported':
            raise ValueError('%s anchor %s holds a record this kit cannot read, so it cannot be judged; use a kit '
                             'that reads it' % (self.title, issue_id))
        if self.readable_revisions(row, operators) \
                and not any(self.readable_revisions(other, operators) for other in remaining):
            raise ValueError('No remaining anchor of %s %s holds a readable record (%s), so releasing %s would leave '
                             'the key with nothing readable. Deal with the other anchor(s) first: release a '
                             'record-less one without --duplicate, or void its malformed records and then release '
                             'it; %s is then the only anchor of the key'
                             % (self.noun, shown_key, ', '.join(str(item.get('id')) for item in remaining),
                                issue_id, issue_id))
        evidence = self.acceptance_on(row, operators)
        before = self.selected_anchor(anchors, operators)
        after = self.selected_anchor(remaining, operators)
        if evidence and not set_aside_evidence:
            raise ValueError('%s anchor %s carries acceptance evidence (%s)%s. It is released only with '
                             '--set-aside-evidence, which sets that evidence aside for good and lists it in the '
                             'audit comment'
                             % (self.title, issue_id,
                                ', '.join('revision %d by %s, %s' % (item['revision'], item['operator'],
                                                                     'live' if item['live'] else 'inert')
                                          for item in evidence[:5]),
                                '; it is the anchor readers select for %s' % shown_key if before == issue_id else ''))
        if view['record'] is not None \
                and not any(self.entry_view(other, operators)['record'] is not None for other in remaining):
            raise ValueError('%s anchor %s holds the only accepted record of %s: no remaining anchor reads an '
                             'accepted record, so releasing it would leave the key with none. Release the other '
                             'anchor(s) instead (%s)'
                             % (self.title, issue_id, shown_key,
                                ', '.join(str(item.get('id')) for item in remaining)))
        # Malformed records on the duplicate read as none: it leaves the key anyway.
        revisions = self.readable_revisions(row, operators)
        others = {}
        for comment in self.live_row(row, operators)[0].get('comments') or []:
            kind = record_comment_kind(comment.get('text') if isinstance(comment, dict) else None)
            if kind and kind[0] in self.extra_records:
                others[kind[0]] = others.get(kind[0], 0) + 1
        note = ('Released by operator %s: this %s anchor was a duplicate of key %s and no longer holds it. '
                'Remaining anchor(s): %s. Readers selected %s before and select %s now. Records set aside: %s. '
                'Acceptance evidence set aside: %s. Other records that stop counting for the key: %s. Reason: %s'
                % (actor, self.noun, shown_key, ', '.join(str(item.get('id')) for item in remaining),
                   before or 'no anchor (conflicted)', after or 'no anchor (conflicted)',
                   '; '.join('revision %d sha256 %s' % (item['revision'], item['sha256']) for item in revisions)
                   or 'none readable',
                   '; '.join('revision %d record %s by %s, decision %s, %s'
                             % (item['revision'], item['record_sha256'], item['operator'], item['decision_id'],
                                'live' if item['live'] else 'inert') for item in evidence) or 'none',
                   ', '.join('%d %s' % (count, kind) for kind, count in sorted(others.items())) or 'none', reason))
        return note, {'duplicate': True, 'key': shown_key,
                      'remaining': [item.get('id') for item in remaining],
                      'selected_before': before, 'selected_after': after, 'records': revisions,
                      'evidence_set_aside': evidence, 'stop_counting': others}

    # -- the reader view ------------------------------------------------------------------------

    def entry_view(self, row, operators):
        """One anchor as readers see it, through `live_row`. Never raises: a bad entry reads malformed.

        The kind adds its own fields (a reference's `due`) to the returned view; records
        of `extra_records` kinds are passed to their readers in native order.
        """
        view = {'key': None, 'native_id': row.get('id'), 'state': None, 'record': None,
                'record_comment_id': None, 'acceptance': None, 'acceptance_inert': False,
                'inert_operator': None, 'proposed': None, 'proposed_comment_id': None, 'warnings': []}
        row, notes = self.live_row(row, operators)
        view['warnings'].extend(notes)
        try:
            revisions, acceptances, extras = {}, {}, []
            for comment in row.get('comments') or []:
                body = comment.get('text') if isinstance(comment, dict) else None
                kind = record_comment_kind(body)
                if not kind or not kind[0].startswith(self.family):
                    continue
                if kind[2] == 'unsupported':
                    view.update(state='unsupported')
                    view['warnings'].append({'code': 'unsupported-record',
                                             'detail': '%s-v%s is newer than this kit' % (kind[0], kind[1])})
                    return view
                if kind[0] == self.family + 'entry':
                    record = self.parse_entry(body)
                    if record is None or not self.entry_belongs(record, row):
                        raise ValueError('malformed %s revision' % self.noun)
                    prior = revisions.get(record['revision'])
                    if prior is not None and prior[0] != record:
                        raise ValueError('conflicting content for one revision')
                    revisions[record['revision']] = (record, comment.get('id'))
                elif kind[0] == self.family + 'acceptance':
                    record = self.parse_acceptance(body)
                    if record is None or record['id'] != row.get('id'):
                        raise ValueError('malformed %s acceptance evidence' % self.noun)
                    acceptances.setdefault(record['revision'], []).append((record, comment.get('author')))
                elif kind[0] in self.extra_records:
                    extras.append((kind[0], body, comment))
            if not revisions:
                raise ValueError('no %s revision record' % self.noun)
            keys = {record['key'] for record, _ in revisions.values()}
            if len(keys) != 1:
                raise ValueError('conflicting keys across revisions')
            view['key'] = next(iter(keys))
            allowlist = configured_operators(operators if operators is not None else ())
            chosen = inert = None
            for number in sorted(revisions, reverse=True):
                record, comment_id = revisions[number]
                if record['acceptance_state'] == 'draft':
                    continue
                evidence = [(item, author) for item, author in acceptances.get(number, [])
                            if item['record_sha256'] == record['sha256']
                            and item['acceptance_state'] == record['acceptance_state'] and item['key'] == record['key']]
                if not evidence:
                    view['warnings'].append({'code': 'accepted-without-evidence',
                                             'detail': 'revision %d reads accepted but has no acceptance evidence'
                                                       % number})
                    continue
                live = [(item, author) for item, author in evidence
                        if author == item['operator'] and author in allowlist]
                if live:
                    chosen = (number, live[0][0])
                    break
                if inert is None:
                    inert = (number, evidence[0][1] or evidence[0][0]['operator'])
            if chosen is not None:
                number, item = chosen
                record, comment_id = revisions[number]
                view.update(record=record, record_comment_id=comment_id, state=record['acceptance_state'],
                            acceptance=dict(item['decision'], record_sha256=item['record_sha256'],
                                            operator=item['operator'], at=item['at']))
            else:
                view['state'] = 'draft-only'
            if inert is not None and (chosen is None or inert[0] > chosen[0]):
                view.update(acceptance_inert=True, inert_operator=inert[1])
                view['warnings'].append({'code': 'acceptance-inert',
                                         'detail': 'revision %d carries acceptance evidence written by %s, who '
                                                   'is not on the deployment operator allowlist' % inert})
            floor = chosen[0] if chosen is not None else 0
            drafts = [number for number, (record, _) in revisions.items()
                      if record['acceptance_state'] == 'draft' and number > floor]
            if drafts:
                record, comment_id = revisions[max(drafts)]
                view.update(proposed=record, proposed_comment_id=comment_id)
            for kind, body, comment in extras:
                self.extra_records[kind](view, body, comment)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            view.update(state='malformed', record=None, acceptance=None, proposed=None)
            view['warnings'].append({'code': 'malformed', 'detail': str(error)[:200]})
        return view

    def released(self, row):
        """Whether a row with no lookup label carries the audit comment `anchor-release`
        writes. Such a row was an anchor and is not one any more: it is not read for any
        key. The record prefixes are reserved, so a contributor cannot make a typed row
        that holds records and lacks its lookup label; anything else in that shape stays
        visible as a malformed entry."""
        marker = ': this %s anchor ' % self.noun
        return any(isinstance(comment, dict) and isinstance(comment.get('text'), str)
                   and comment['text'].startswith('Released by operator ') and marker in comment['text'][:200]
                   for comment in row.get('comments') or [])

    def catalog(self, rows, operators, view=None):
        """(entries, incomplete ids) over the kind's labelled rows; ordinary labelled tasks are skipped.

        An anchor whose every record an applied operator void names holds no live record,
        so it is reported as incomplete, exactly like an anchor whose propose stopped
        before its first record.
        """
        view = view or self.entry_view
        entries, incomplete = [], []
        for row in rows:
            if not isinstance(row, dict) or self.type_label not in (row.get('labels') or []):
                continue
            if not self.key_labels(row) and self.released(row):
                continue   # released by an operator: no longer an anchor of any key
            if not self.has_live_record(row, operators):
                if self.key_labels(row):
                    incomplete.append(row.get('id'))
                    entry = view(row, operators)
                    entry['state'] = 'incomplete'
                    entries.append((entry, self.key_labels(row)))
                continue
            entries.append((view(row, operators), self.key_labels(row)))
        # Group by the record's exact key, never its lossy lookup slug. A bare
        # accepted label/comment cannot displace an anchor with live evidence.
        grouped, unknown = {}, []
        for entry, labels in entries:
            if entry['key'] is None:
                unknown.append((entry, labels))
            else:
                grouped.setdefault(entry['key'], []).append(entry)
        # A malformed row's label can identify its readable siblings, but cannot
        # be inverted to invent a dotted key. Include it in every plausible group
        # if distinct known keys share the same slug.
        for entry, labels in unknown:
            keys = [key for key in grouped if isinstance(key, str) and self.key_label(key) in labels]
            if keys:
                for key in keys:
                    grouped[key].append(dict(entry, key=key, warnings=list(entry['warnings'])))
            else:
                group = ('lookup', tuple(sorted(labels))) if labels else ('native', entry['native_id'])
                grouped.setdefault(group, []).append(entry)
        return [entry for group in grouped.values() for entry in self.select_entries(group)
                if entry['state'] != 'incomplete'], incomplete

    @staticmethod
    def select_entries(entries):
        """Only one uniquely live acceptance can select a duplicate key's record.

        Otherwise retain every anchor as conflicted. Rank is display order only:
        live evidence, readable content, then malformed/unsupported content.
        """
        if len(entries) == 1:
            return entries
        ordered = sorted(entries, key=lambda entry: (
            0 if entry['record'] is not None else 2 if entry['state'] in ('malformed', 'unsupported', 'incomplete') else 1,
            str(entry['native_id'])))
        anchors = [{'native_id': entry['native_id'], 'state': entry['state'],
                    'trust': 'accepted' if entry['record'] is not None else
                    entry['state'] if entry['state'] in ('malformed', 'unsupported', 'incomplete') else 'draft'}
                   for entry in ordered]
        warning = {'code': 'duplicate-key', 'detail': 'Duplicate anchors (%s); operator reconciliation required'
                   % ', '.join(str(anchor['native_id']) for anchor in anchors)}
        live = [entry for entry in ordered if entry['record'] is not None]
        if len(live) == 1:
            chosen = live[0]
            chosen.update(duplicate_anchors=anchors)
            chosen['warnings'].insert(0, warning)
            return [chosen]
        for entry, anchor in zip(ordered, anchors):
            entry.update(candidate=entry['record'] or entry['proposed'] or entry.get('newest'),
                         anchor_trust=anchor['trust'], state='conflicted', record=None, acceptance=None,
                         record_comment_id=None, proposed=None, proposed_comment_id=None,
                         acceptance_inert=False, inert_operator=None, duplicate_anchors=anchors)
            if 'aliases_pending' in entry:
                entry.update(aliases_pending=[], aliases_rejected=set())
            entry['warnings'].insert(0, dict(warning))
        return ordered

    def find_entry(self, rows, key, operators, view=None):
        """The view for one key, found through its lookup label; the record's own key must match.

        A malformed or unsupported entry is returned with that state (its key is known only
        from the label); an anchor with no record yet, or an unknown key, is a refusal.
        """
        self.valid_key(key)
        view = view or self.entry_view
        label = self.key_label(key)
        incomplete, candidates = [], []
        for row in rows:
            if not isinstance(row, dict) or self.type_label not in (row.get('labels') or []) \
                    or label not in self.key_labels(row):
                continue
            if not self.has_live_record(row, operators):
                incomplete.append(row.get('id'))
                entry = view(row, operators)
                entry.update(key=key, state='incomplete')
                candidates.append(entry)
                continue
            entry = view(row, operators)
            if entry['key'] in (key, None):
                entry['key'] = key
                candidates.append(entry)
        if candidates and not (len(candidates) == 1 and candidates[0]['state'] == 'incomplete'):
            selected = self.select_entries(candidates)
            if len(selected) == 1:
                return selected[0]
            # A shape-compatible conflict response carries no selected anchor or
            # record. The anchors' individual trust is evidence for reconciliation.
            result = dict(selected[0], native_id=None, candidate=None)
            result.update(aliases_pending=[], aliases_rejected=set(), newest=None, verification_records=[])
            return result
        if incomplete:
            raise ValueError('%s key %s has no revision record yet (incomplete anchor %s); re-run its '
                             'propose, or ask the operator to reconcile it or release the anchor'
                             % (self.title, key, incomplete[0]))
        raise ValueError('Unknown %s key %s; use %s list to see the catalog' % (self.noun, key, self.command))

    @staticmethod
    def coverage(entries, incomplete, base):
        notes = [base]
        duplicates = {tuple(anchor['native_id'] for anchor in entry['duplicate_anchors'])
                      for entry in entries if entry.get('duplicate_anchors')}
        if duplicates:
            notes.append('%d duplicate key(s); operator reconciliation required (%s)' % (
                len(duplicates), '; '.join(', '.join(str(task) for task in group) for group in sorted(duplicates))))
        for state in ('malformed', 'unsupported'):
            ids = [entry['native_id'] for entry in entries if entry['state'] == state]
            if ids:
                notes.append('%d entr%s skipped as %s (%s)' % (len(ids), 'y' if len(ids) == 1 else 'ies', state,
                                                               ', '.join(ids[:COVERAGE_IDS])))
        if incomplete:
            notes.append('%d incomplete anchor%s with no record yet (%s)' % (
                len(incomplete), '' if len(incomplete) == 1 else 's', ', '.join(incomplete[:COVERAGE_IDS])))
        return '; '.join(notes)

    def write_payload(self, args, attachments, commands=CONTRIBUTOR_OPERATIONS):
        """The payload of `<command> propose|revise --file entry.json` (the client's attachment transport)."""
        command, rest = args[0], [token for token in args[1:] if token != '--json']
        if command not in commands:
            raise ValueError('%s: unknown command %s' % (self.command, command))
        if len(rest) != 1 or not rest[0].startswith('@attachment:'):
            raise ValueError('%s %s takes --file entry.json' % (self.command, command))
        item = (attachments or {}).get(rest[0].partition(':')[2])
        if not isinstance(item, dict) or item.get('flag') not in ('--file', '-f') or not isinstance(item.get('text'), str):
            raise ValueError('%s %s takes --file entry.json' % (self.command, command))
        payload = parse_json(item['text'])
        if not isinstance(payload, dict):
            raise ValueError('%s payload must be an object' % self.noun)
        if 'operation' in payload:
            raise ValueError('the payload must not set operation; use the propose or revise command')
        return dict(payload, operation=command)
