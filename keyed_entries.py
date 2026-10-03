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
  incomplete;
- the reader view: an acceptance counts only while its evidence comment's stored
  native author equals the evidence `operator` and is on the live deployment
  operator allowlist, otherwise it is inert (named, never silent); every entry fails
  alone (`malformed`, `unsupported`).
"""
import json
import re
import subprocess

import keyed_records as core
from coordination import identifier
from export_requirements import parse_json
from recovery import configured_operators
from requirements import SHA256_TEXT, canonical_bytes, content_hash
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
    names), `pre_write(payload, run)` (extra reads before the journal, e.g. link
    checks), and `extra_records` (a mapping of further record kinds on the anchor to
    a reader `(view, body, comment) -> None` that may raise to mark the entry
    malformed).
    """

    def __init__(self, **values):
        defaults = {'pre_write': lambda payload, run: None, 'extra_records': {}, 'supports_retire': False}
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
        label = self.key_label(key)
        for row in rows:
            if self.type_label not in (row.get('labels') or []) or label not in self.key_labels(row):
                continue
            if not is_record_anchor(row):
                return row, 'incomplete'
            return row, 'entry'
        return None, None

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
                             'to reconcile it.' % (self.title, payload['key'], row['id']))
        if payload['key'] not in {record['key'] for record in self.existing_revisions(row).values()}:
            # The lookup label is shared with a different key (a.b-c and a.b.c).
            raise ValueError('Unknown %s key %s; use %s list to see the catalog.'
                             % (self.noun, payload['key'], self.command))
        return row['id']

    def check_key_unique(self, rows, payload, task):
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
                                 'its operation_id, or reconciled by the operator.'
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
        if operator:
            core.require_configured_operator(actor, operators, self.spec.accept_action)
        self.validate_payload(payload, operator=operator)
        self.pre_write(payload, run)
        return core.apply_native(payload, actor, run, project, self.spec, operator=operator, operators=operators)

    def confirm_anchor(self, row):
        if not is_record_anchor(row):
            raise ValueError('Anchor %s has no %s revision record yet; re-run the original %s propose '
                             'with the same operation_id to finish it, then reconcile.'
                             % (row.get('id'), self.noun, self.command))

    def reconcile(self, project, operation_id, actor, reason, disposition, run, issue_id=None):
        return core.reconcile(project, operation_id, actor, reason, disposition, run, self.spec,
                              issue_id=issue_id, confirm=self.confirm_anchor)

    # -- the reader view ------------------------------------------------------------------------

    def entry_view(self, row, operators):
        """One anchor as readers see it. Never raises: a bad entry reads malformed.

        The kind adds its own fields (a reference's `due`) to the returned view; records
        of `extra_records` kinds are passed to their readers in native order.
        """
        view = {'key': None, 'native_id': row.get('id'), 'state': None, 'record': None,
                'record_comment_id': None, 'acceptance': None, 'acceptance_inert': False,
                'inert_operator': None, 'proposed': None, 'proposed_comment_id': None, 'warnings': []}
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

    def catalog(self, rows, operators, view=None):
        """(entries, incomplete ids) over the kind's labelled rows; ordinary labelled tasks are skipped."""
        view = view or self.entry_view
        entries, incomplete = [], []
        for row in rows:
            if not isinstance(row, dict) or self.type_label not in (row.get('labels') or []):
                continue
            if not is_record_anchor(row):
                if self.key_labels(row):
                    incomplete.append(row.get('id'))
                continue
            entries.append(view(row, operators))
        # Group by the record's exact key, never its lossy lookup slug. A bare
        # accepted label/comment cannot displace an anchor with live evidence.
        grouped, unknown = {}, []
        for entry in entries:
            if entry['key'] is None:
                unknown.append(entry)
            else:
                grouped.setdefault(entry['key'], []).append(entry)
        return [self.select_entry(group) for group in grouped.values()] + unknown, incomplete

    @staticmethod
    def select_entry(entries):
        """Prefer live acceptance evidence; break ties by native id, independent of read order.

        Do not combine revisions/evidence from different anchors. Duplicate anchors
        require operator reconciliation even when a trusted record can be selected.
        """
        chosen = min(entries, key=lambda entry: (entry['record'] is None, str(entry['native_id'])))
        if len(entries) > 1:
            chosen['warnings'].insert(0, {'code': 'duplicate-key',
                                         'detail': '%d anchors match this key; selected %s; operator '
                                                   'reconciliation required' % (len(entries), chosen['native_id'])})
        return chosen

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
            if not is_record_anchor(row):
                incomplete.append(row.get('id'))
                continue
            entry = view(row, operators)
            if entry['key'] in (key, None):
                candidates.append(entry)
        if candidates:
            return self.select_entry(candidates)
        if incomplete:
            raise ValueError('%s key %s has no revision record yet (incomplete anchor %s); re-run its '
                             'propose or ask the operator to reconcile it' % (self.title, key, incomplete[0]))
        raise ValueError('Unknown %s key %s; use %s list to see the catalog' % (self.noun, key, self.command))

    @staticmethod
    def coverage(entries, incomplete, base):
        notes = [base]
        duplicates = [entry for entry in entries
                      if any(warning['code'] == 'duplicate-key' for warning in entry['warnings'])]
        if duplicates:
            notes.append('%d duplicate key(s); operator reconciliation required (%s)' % (
                len(duplicates), ', '.join(entry['native_id'] for entry in duplicates[:COVERAGE_IDS])))
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
