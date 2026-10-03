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
import re

from briefing import PREFIX as CHECKPOINT_PREFIX
from export_requirements import (ACCEPTANCE_PREFIX,
                                 REVISION_PREFIX as REQUIREMENT_PREFIX)
from handoff import (
    COMPLETE_PREFIX as HANDOFF_COMPLETE_PREFIX,
    INTENT_PREFIX as HANDOFF_PREFIX,
    parse_identity as parse_handoff_identity,
)
from lifecycle import PREFIX as LIFECYCLE_PREFIX
from requirements import ACCEPTANCE_FIELDS, SHA256_TEXT
from recovery import PREFIX as VOID_PREFIX
from review_workflow import PREFIX as REVIEW_PREFIX, REVERT_PREFIX
from worker_gate import PREFIX as PLAN_PREFIX, parse_body as parse_plan_body


# The caller-supplied half of the contract acceptance object. `manifest_sha256`
# is bound by the command, never supplied: at record level the bound name is
# `record_sha256` (the accepted revision's content hash), because the hash of a
# whole publication manifest does not exist when a single record is accepted.
ACCEPTANCE_DECISION_FIELDS = tuple(name for name in ACCEPTANCE_FIELDS
                                   if name != 'manifest_sha256')
ACCEPTANCE_SOURCES = ('requirement-apply', 'requirement-backfill')
ACCEPTANCE_RECORD_FIELDS = ('schema_version', 'source', 'id', 'revision',
                            'record_sha256', 'acceptance_state', 'decision',
                            'evidence', 'operator', 'at', 'sha256')


def parse_acceptance_record(body):
    """Return the durable acceptance record iff it passes the full schema.

    Mirror of parse_requirement_record for `Kind: requirement-acceptance-v1`:
    exact canonical bytes, exact field set, a revision binding and a content
    hash, plus the contract's own F3 owner/approver/policy validation for an
    acceptance decision. Only a record the operator acceptance route could
    produce passes, so the raw comment path cannot forge acceptance evidence.
    """
    if not isinstance(body, str) or not body.startswith(ACCEPTANCE_PREFIX):
        return None
    rest = body[len(ACCEPTANCE_PREFIX):]
    try:
        from export_requirements import parse_json
        from requirements import canonical_bytes, content_hash
        record = parse_json(rest)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict) or set(record) != set(ACCEPTANCE_RECORD_FIELDS):
        return None
    if type(record.get('schema_version')) is not int or record['schema_version'] != 1:
        return None
    if record.get('source') not in ACCEPTANCE_SOURCES:
        return None
    if not isinstance(record.get('id'), str) or not record['id'].strip():
        return None
    revision = record.get('revision')
    digest = record.get('record_sha256')
    if revision is not None and (isinstance(revision, bool)
                                 or not isinstance(revision, int) or revision < 1):
        return None
    if digest is not None and (not isinstance(digest, str)
                               or not SHA256_TEXT.match(digest)):
        return None
    if (revision is None) != (digest is None):
        return None
    if record.get('acceptance_state') not in ('draft', 'accepted'):
        return None
    if not isinstance(record.get('operator'), str) or not record['operator'].strip():
        return None
    if not isinstance(record.get('at'), str) or not record['at'].strip():
        return None
    if record['source'] == 'requirement-apply':
        decision = record.get('decision')
        if revision is None or record.get('evidence') is not None:
            return None
        if not isinstance(decision, dict) or set(decision) != set(ACCEPTANCE_DECISION_FIELDS):
            return None
        try:
            from requirements import _validate_acceptance
            _validate_acceptance(dict(decision, manifest_sha256=digest),
                                 {'sha256': digest})
        except (ValueError, TypeError):
            return None
    else:
        if record.get('decision') is not None:
            return None
        if not isinstance(record.get('evidence'), str) or not record['evidence'].strip():
            return None
    try:
        if content_hash(record) != record.get('sha256'):
            return None
        if canonical_bytes(record).decode() != rest:
            return None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    return record


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

# kittrial-5bb.64, the shared slice 0 (tolerant reader) of the accepted designs
# docs/REFERENCE_CATALOG_DESIGN.md (.41), docs/REQUIREMENTS_GATHERING_DESIGN.md
# (.58) and docs/CAPABILITY_INDEX_DESIGN.md (.60). No writer exists yet: the
# prefixes are reserved first, so the later slices' records can never be forged
# through the raw path and a rollback to this kit stays safe. Only v1 is reserved;
# a v2 writer needs its own tolerant-reader step (.58 3.8).
REFERENCE_ENTRY_PREFIX = 'Kind: reference-entry-v1\n'
REFERENCE_ACCEPTANCE_PREFIX = 'Kind: reference-acceptance-v1\n'
PROPOSAL_PREFIX = 'Kind: requirement-proposal-v1\n'
PROPOSAL_DISPOSITION_PREFIX = 'Kind: proposal-disposition-v1\n'
CONTRIBUTION_SETTINGS_PREFIX = 'Kind: contribution-settings-v1\n'
CAPABILITY_ENTRY_PREFIX = 'Kind: capability-entry-v1\n'
CAPABILITY_ACCEPTANCE_PREFIX = 'Kind: capability-acceptance-v1\n'
CAPABILITY_VERIFICATION_PREFIX = 'Kind: capability-verification-v1\n'
CAPABILITY_ALIAS_PREFIX = 'Kind: capability-alias-v1\n'

RESERVED = (
    (REVIEW_PREFIX, 'contribution/review record', 'review TASK --file payload.json'),
    (CHECKPOINT_PREFIX, 'task checkpoint', 'checkpoint TASK --file checkpoint.json'),
    (LIFECYCLE_PREFIX, 'lifecycle evidence record', 'lifecycle.py record --file event.json'),
    (REQUIREMENT_PREFIX, 'requirement revision', 'requirement_records.py draft|revise'),
    (ACCEPTANCE_PREFIX, 'requirement acceptance evidence', 'admin.py requirement-apply'),
    (HANDOFF_PREFIX, 'handoff intent', 'handoff TASK --file handoff.json'),
    (HANDOFF_COMPLETE_PREFIX, 'handoff completion', 'handoff TASK --file handoff.json'),
    (PLAN_PREFIX, 'worker plan registration', 'worker_gate.py register'),
    (VOID_PREFIX, 'operator void record', 'admin.py void-record on the coordination host'),
    (REVERT_PREFIX, 'integration revert record', 'admin.py revert-record on the coordination host'),
    (REFERENCE_ENTRY_PREFIX, 'reference catalog entry', 'ref propose|revise'),
    (REFERENCE_ACCEPTANCE_PREFIX, 'reference acceptance evidence', 'admin.py reference-apply'),
    (PROPOSAL_PREFIX, 'requirement proposal', 'proposal submit|revise'),
    (PROPOSAL_DISPOSITION_PREFIX, 'proposal disposition', 'admin.py proposal-review|proposal-decide on the coordination host'),
    (CONTRIBUTION_SETTINGS_PREFIX, 'contribution settings', 'admin.py proposal-settings on the coordination host'),
    (CAPABILITY_ENTRY_PREFIX, 'capability entry', 'capability propose|revise'),
    (CAPABILITY_ACCEPTANCE_PREFIX, 'capability acceptance evidence', 'admin.py capability-apply'),
    (CAPABILITY_VERIFICATION_PREFIX, 'capability verification', 'capability check --record (capability verify)'),
    (CAPABILITY_ALIAS_PREFIX, 'capability alias', 'capability propose-alias|alias-reject'),
)

PREFIXES = tuple(prefix for prefix, _, _ in RESERVED)

# Every version of the nine record kinds is reserved, not only v1 (kittrial-5bb.64
# review item `smaller` b): otherwise a raw `Kind: capability-entry-v7` would be
# writable and then hidden. A later writer of vN ships its own tolerant-reader step.
_RECORD_KIND_RESERVATIONS = {
    'reference-entry': ('reference catalog entry', 'ref propose|revise'),
    'reference-acceptance': ('reference acceptance evidence', 'admin.py reference-apply'),
    'requirement-proposal': ('requirement proposal', 'proposal submit|revise'),
    'proposal-disposition': ('proposal disposition', 'admin.py proposal-review|proposal-decide on the coordination host'),
    'contribution-settings': ('contribution settings', 'admin.py proposal-settings on the coordination host'),
    'capability-entry': ('capability entry', 'capability propose|revise'),
    'capability-acceptance': ('capability acceptance evidence', 'admin.py capability-apply'),
    'capability-verification': ('capability verification', 'capability check --record (capability verify)'),
    'capability-alias': ('capability alias', 'capability propose-alias|alias-reject'),
}
_RECORD_KIND_ANY_VERSION = re.compile(
    r'Kind: (%s)-v[0-9]+\n' % '|'.join(re.escape(kind) for kind in _RECORD_KIND_RESERVATIONS))


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

# Per-command shorthand inventory, verified against the pinned bd 1.2.2 help
# output. A `bool` shorthand never takes a value; a `value` shorthand consumes
# the rest of the cluster (`-n5`) or the next argv token (`-n 5`). A shorthand
# not listed for the command is ambiguous and fails closed, so an unknown
# boolean shorthand can no longer smuggle an operator-only shorthand such as
# `-C` past the guard (`list -rC`, `ready -uC`).
BD_GLOBAL_SHORT_FLAGS = {'C': 'value', 'h': 'bool', 'q': 'bool',
                         'v': 'bool', 'V': 'bool'}
# `bd dep` has subcommands whose own shorthand inventories differ from the
# parent: `-b/--blocks` exists only on the parent (`bd dep <id> --blocks <id>`),
# `-t` is --type on add/list and `-d` is --max-depth on tree. Keying the whole
# command to the parent table refused every one of those legitimate short forms
# (kittrial-5bb.30). Verified against the pinned bd 1.2.2 help output, including
# native rejection of `-b` on `dep add`/`dep list`
# (`unknown shorthand flag: 'b' in -b`).
BD_DEP_PARENT_SHORT_FLAGS = {'b': 'value', 'h': 'bool'}
BD_DEP_SUBCOMMAND_ALIASES = {
    'add': 'add', 'cycles': 'cycles', 'list': 'list', 'relate': 'relate',
    'remove': 'remove', 'rm': 'remove', 'tree': 'tree',
    'unrelate': 'unrelate',
}
# `bd dep` long flags that take a value; used only to skip the value while
# locating the subcommand token.
BD_DEP_VALUE_LONG_FLAGS = {
    '--blocks', '--blocked-by', '--depends-on', '--file', '--type',
    '--direction', '--format', '--max-depth', '--status',
}
BD_COMMAND_SHORT_FLAGS = {
    'comments': {'add': {'a': 'value', 'f': 'value', 'h': 'bool'}},
    'list': {'a': 'value', 'l': 'value', 'n': 'value', 'p': 'value',
             'r': 'bool', 's': 'value', 't': 'value', 'w': 'bool'},
    'show': {'w': 'bool'},
    'ready': {'a': 'value', 'l': 'value', 'n': 'value', 'p': 'value',
              's': 'value', 't': 'value', 'u': 'bool'},
    'search': {'a': 'value', 'l': 'value', 'n': 'value', 's': 'value',
               't': 'value', 'r': 'bool'},
    'count': {'a': 'value', 'l': 'value', 'p': 'value', 's': 'value',
              't': 'value'},
    'create': {'a': 'value', 'd': 'value', 'e': 'value', 'f': 'value',
               'l': 'value', 'p': 'value', 't': 'value'},
    'update': {'a': 'value', 'd': 'value', 'e': 'value', 'p': 'value',
               's': 'value', 't': 'value'},
    'close': {'f': 'bool', 'r': 'value'},
    'reopen': {'r': 'value'},
    'dep': {
        'add': {'t': 'value', 'h': 'bool'},
        'cycles': {'h': 'bool'},
        'list': {'t': 'value', 'h': 'bool'},
        'relate': {'h': 'bool'},
        'remove': {'h': 'bool'},
        'tree': {'d': 'value', 'h': 'bool'},
        'unrelate': {'h': 'bool'},
    },
    'state': {},
    'lint': {'s': 'value', 't': 'value'},
}


def _short_flag_table(command, subcommand=None):
    """Shorthand table for a bd command, or None without command context.

    Without command context callers get the conservative command-agnostic
    union (`a`/`f`/`C` are operator-only), which is what the helper tests
    exercise. With command context the command's own inventory is used; for
    `comments` and `dep` the subcommand selects the inventory, and a `dep`
    invocation without a subcommand (`bd dep <id> --blocks <id>`) gets the
    parent table where `-b` lives.
    """
    if command is None:
        return None
    table = dict(BD_GLOBAL_SHORT_FLAGS)
    if command == 'comments':
        table.update(BD_COMMAND_SHORT_FLAGS['comments'].get(subcommand) or {})
    elif command == 'dep':
        if subcommand is None:
            table.update(BD_DEP_PARENT_SHORT_FLAGS)
        else:
            table.update(BD_COMMAND_SHORT_FLAGS['dep'].get(subcommand)
                         or BD_DEP_PARENT_SHORT_FLAGS)
    else:
        table.update(BD_COMMAND_SHORT_FLAGS.get(command) or {})
    return table


def _dep_subcommand(args):
    """Alias-normalized subcommand of a `bd dep` invocation, or None.

    `bd dep <issue-id> --blocks <id>` puts a positional issue ID where a
    subcommand would be, so an operand that is not a known subcommand resolves
    to None: the parent table, the only place `-b` is defined. Flag values are
    consumed so a value cannot be mistaken for the subcommand.
    """
    index = 1
    while index < len(args):
        token = args[index]
        if not isinstance(token, str):
            index += 1
            continue
        if token == '--':
            return None
        if len(token) > 1 and token.startswith('--'):
            name, sep, _ = token.partition('=')
            if name in BD_DEP_VALUE_LONG_FLAGS or name in BD_GLOBAL_VALUE_FLAGS:
                index += 1 if sep else 2
            else:
                index += 1
            continue
        if len(token) > 1 and token.startswith('-'):
            body = token[1:]
            eq = body.find('=')
            chars = body[:eq] if eq != -1 else body
            if eq == -1 and 'C' in chars and chars.index('C') == len(chars) - 1:
                index += 2
            else:
                index += 1
            continue
        return BD_DEP_SUBCOMMAND_ALIASES.get(token)
    return None


def _bd_command_context(args):
    """Best-effort (command, subcommand) for a bd argv list.

    The endpoint only reaches the guard after `args[0] in ALLOWED`, so the
    command is the first token. Transported `@attachment:` tokens are skipped
    while resolving the `comments` subcommand because the endpoint expands them
    into file flags in place.
    """
    command = args[0] if args and isinstance(args[0], str) else None
    subcommand = None
    if command == 'comments':
        for token in args[1:]:
            if (isinstance(token, str) and not token.startswith('-')
                    and not token.startswith('@attachment:')):
                subcommand = token
                break
    elif command == 'dep':
        subcommand = _dep_subcommand(args)
    return command, subcommand


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
    Raises ValueError when an unrecognized flag, or a transported attachment
    placed before the subcommand, could hide the subcommand or body
    (ambiguous), so no native write can be reached.

    The endpoint expands `@attachment:k` into its file flag in place, so an
    attachment before the subcommand becomes `comments --file PATH add TASK`,
    an ordering cobra accepts. That hid the body from the guard, so it is
    refused outright; attachments belong after the subcommand.
    """
    if not isinstance(args, list):
        return None
    tokens = _tokenize_bd_args(args)
    positional = [i for i, (kind, _) in enumerate(tokens) if kind == 'positional']
    if not positional or tokens[positional[0]][1] != 'comments':
        return None
    subcommand = None
    sub_index = None
    before = []
    after = []
    for index in positional[1:]:
        token = tokens[index][1]
        if subcommand is None:
            if isinstance(token, str) and token.startswith('@attachment:'):
                before.append(token)
                continue
            subcommand = token
            sub_index = index
        else:
            after.append(token)
    if before:
        raise ValueError(
            'Refusing comments request: attachment transport %r placed before '
            'the subcommand would expand to a file flag ahead of %r, hiding '
            'the body from the reserved-prefix guard; no native write was '
            'attempted. Place attachments after the subcommand.'
            % (before[0], subcommand))
    unknown = [token for kind, token in tokens if kind == 'unknown']
    if subcommand is None:
        if unknown:
            raise ValueError(
                'Refusing comments request: ambiguous flag %r before native '
                'write; the bd command could not be parsed structurally.'
                % (unknown[0],))
        return (None, [])
    before_subcommand = [
        token for i, (kind, token) in enumerate(tokens)
        if kind == 'unknown' and i < sub_index
    ]
    if before_subcommand or (subcommand == 'add' and unknown):
        raise ValueError(
            'Refusing comments request: ambiguous flag %r before native '
            'write; the bd command could not be parsed structurally.'
            % ((before_subcommand or unknown)[0],))
    return (subcommand, after)


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


def operator_only_flag(token, command=None, subcommand=None):
    """Canonical operator-only flag name for an argv token, or None.

    With command context the command's real shorthand inventory is used, so
    `-a` is --author for `comments add` but --assignee for
    list/ready/search/count/create/update, `-f` is --file for comments/create
    but --force for close, and a shorthand unknown to the command fails closed
    instead of hiding a later `-C`. Without command context every
    operator-only short spelling is refused. Joined, `=value` and clustered
    spellings are all covered (`-a`, `-aX`, `-a=X`, `--author=X`, `-qa`,
    `-C/tmp`, `-rC`).
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
    table = _short_flag_table(command, subcommand)
    for ch in chars:
        if table is None:
            if ch in OPERATOR_ONLY_SHORT_VALUE:
                spec = 'value'
            elif ch in BD_SHORT_BOOL_FLAGS:
                spec = 'bool'
            else:
                spec = None
        else:
            spec = table.get(ch)
        if spec is None:
            # Unknown shorthand for this command: ambiguous. Fail closed so an
            # unknown boolean cannot smuggle a later operator-only shorthand.
            return token
        if ch == 'C':
            return '--directory'
        if ch == 'a' and (command is None
                          or (command == 'comments' and subcommand == 'add')):
            return '--author'
        if ch == 'f' and (command is None or command in ('comments', 'create')):
            return '--file'
        if spec == 'bool':
            continue
        # A value-taking shorthand consumes the rest of the cluster (or the
        # next token): the following characters are its value, not flags.
        break
    return None


def operator_only_in_args(args):
    """First operator-only flag in an argv list, or None.

    `--` ends flag parsing exactly as in bd/pflag, so operands after it are
    ordinary body text, not flags. Command context selects the real shorthand
    inventory so `-a`/`-f` mean the command's own flags and an unknown
    shorthand fails closed.
    """
    if not isinstance(args, list):
        return None
    command, subcommand = _bd_command_context(args)
    end_of_flags = False
    for token in args:
        if token == '--':
            end_of_flags = True
            continue
        if end_of_flags:
            continue
        name = operator_only_flag(token, command, subcommand)
        if name is not None:
            return name
    return None


def _raw_file_flag_token(token, command, subcommand):
    """File-flag name for one raw server-path token, or None.

    Long file spellings are refused everywhere. `-f` is a file flag only where
    bd defines it as --file (`comments add` and `create`); on `close` it is
    --force, and treating the bare token as a path refused a legitimate
    force-close (kittrial-5bb.30). A shorthand unknown to the command is
    ambiguous and returns the token, matching operator_only_flag().
    """
    if not isinstance(token, str) or len(token) < 2 or not token.startswith('-'):
        return None
    if token.startswith('--'):
        name = token.partition('=')[0]
        return name if name in OPERATOR_ONLY_FILE_FLAGS else None
    body = token[1:]
    end = body.find('=')
    chars = body[:end] if end != -1 else body
    table = _short_flag_table(command, subcommand)
    for ch in chars:
        if ch == 'f' and (command is None or command in ('comments', 'create')):
            return '--file'
        spec = None if table is None else table.get(ch)
        if spec is None:
            return token
        if spec == 'bool':
            continue
        # A value-taking shorthand consumes the rest of the cluster.
        break
    return None


def raw_file_flag_in_args(args):
    """First raw server-side file-path flag in an argv list, or None.

    Command-aware replacement for endpoint's legacy `token in FILE_FLAGS`
    membership test, so `-f` means --force on `close` instead of a raw path.
    `--` ends flag parsing as in bd/pflag.
    """
    if not isinstance(args, list):
        return None
    command, subcommand = _bd_command_context(args)
    end_of_flags = False
    for token in args:
        if token == '--':
            end_of_flags = True
            continue
        if end_of_flags:
            continue
        name = _raw_file_flag_token(token, command, subcommand)
        if name is not None:
            return name
    return None


# ---------------------------------------------------------------------------
# Reserved coordination label namespaces.
#
# coordination.py writes `request:<request_id hash>` and
# `request-content:<content digest>` through its own internal run path. The raw
# contributor bd path must not be able to plant, replace or remove them: a
# planted `request:<hash>` label blocks a known request_id ("Native request
# content mismatch") and planting both labels can reconcile a victim
# create-child to an attacker issue. Reads (`list -l request:...`) stay usable
# because only the create/update label-writing flags are inspected.
#
# requirement_records.py is the only writer of the controlled requirement type
# and state labels (`requirement`, `brd-section`, `requirement:draft`,
# `requirement:accepted`). The raw path must not be able to accept a record
# (`update X --add-label requirement:accepted`) or flip its type label, so
# `requirement:` joins the reserved prefixes and the two type labels are
# reserved exactly.
#
# The same namespace feeds BOTH checks: the label-*value* check below and the
# read-before-write guard (`first_reserved_label`, endpoint._guard_reserved_labels).
# Extending it here therefore closes the two routes that do not name a reserved
# value: `create --parent X` inheriting `requirement`/`requirement:accepted`
# from a labelled parent (kittrial-pth.26 review P1), and `update X --set-labels`
# replacing the labels of a record that currently holds them (review P2). One
# guard, not a second parallel one.
# ---------------------------------------------------------------------------

# kittrial-5bb.64 adds the label PREFIXES of .41 (3.7), .58 (3.7) and .60 (3.1);
# `reference-key:` does not begin with `reference:`, so each is listed. The exact
# record type labels (`reference`, `proposal`, `contribution-settings`, `capability`)
# are deliberately NOT value-reserved: a project may already use them as ordinary
# labels (live: jjbp-j03.20 carries jjbp's own `proposal`). They are protected only on
# a real record anchor - a row that also carries a v1 record comment of the same
# family (see is_record_anchor) - where endpoint._guard_reserved_labels refuses
# replacing or removing its labels.
RESERVED_LABEL_PREFIXES = ('request:', 'request-content:', 'requirement:',
                           'reference:', 'reference-key:', 'proposal:', 'proposal-key:',
                           'capability:', 'capability-key:')
RESERVED_EXACT_LABELS = frozenset({'requirement', 'brd-section'})
# Every label-writing spelling accepted by the pinned bd 1.2.2, verified by
# driving the binary (`bd create --help` / `bd update --help` plus a behaviour
# probe of each candidate). `create` accepts the UNDOCUMENTED `--label` alias of
# `--labels` (it is hidden from --help; bd 1.2.2 review item hidden-label-alias),
# so a table built from --help alone missed `create --label requirement`.
# `--tag`/`--tags`, `--add-labels`, `--set-label` and `--remove-labels` are NOT
# bd 1.2.2 flags (`unknown flag`); `-l` and `--label(s)` are the only create
# spellings and `--add-label`/`--set-labels`/`--remove-label` the only update
# spellings. An unknown create/update flag still fails closed through
# unresolved_bd_flags()/label_guard_request(), so a future hidden alias cannot
# silently reopen this route.
LABEL_WRITE_FLAGS = {
    'create': {'--labels', '--label', '-l'},
    'update': {'--add-label', '--set-labels', '--remove-label'},
}


def _label_values(args):
    """Label values written by create/update label flags in an argv list."""
    command = args[0] if args and isinstance(args[0], str) else None
    flags = LABEL_WRITE_FLAGS.get(command)
    if not flags:
        return []
    table = _short_flag_table(command)
    values = []
    index = 0
    end_of_flags = False
    while index < len(args):
        token = args[index]
        if not isinstance(token, str):
            index += 1
            continue
        if end_of_flags:
            index += 1
            continue
        if token == '--':
            end_of_flags = True
            index += 1
            continue
        if token.startswith('--'):
            name, sep, value = token.partition('=')
            if name in flags:
                if sep:
                    values.append(value)
                else:
                    index += 1
                    if index < len(args) and isinstance(args[index], str):
                        values.append(args[index])
            index += 1
            continue
        if len(token) > 1 and token.startswith('-'):
            body = token[1:]
            end = body.find('=')
            chars = body[:end] if end != -1 else body
            attached = body[end + 1:] if end != -1 else None
            for position, ch in enumerate(chars):
                if ch == 'l' and command == 'create':
                    if attached is not None and position == len(chars) - 1:
                        values.append(attached)
                    elif position < len(chars) - 1:
                        values.append(chars[position + 1:])
                    else:
                        index += 1
                        if index < len(args) and isinstance(args[index], str):
                            values.append(args[index])
                    break
                spec = table.get(ch) if table else None
                if spec == 'bool':
                    continue
                # A value-taking (or unknown) shorthand consumes the rest.
                break
            index += 1
            continue
        index += 1
    return values


def reserved_label(label):
    """The label's own text when it sits in a reserved namespace, else None.

    One predicate shared by the value check (`reserved_label_in_args`) and the
    read-before-write guard (`first_reserved_label`), so a namespace extended
    here is enforced on every route at once. `requirement`/`brd-section` are
    reserved exactly; `requirement:draft`/`requirement:accepted` and the
    request namespaces are reserved by prefix.
    """
    if not isinstance(label, str):
        return None
    if label in RESERVED_EXACT_LABELS:
        return label
    for prefix in RESERVED_LABEL_PREFIXES:
        if label.startswith(prefix):
            return label
    return None


# ---------------------------------------------------------------------------
# The shared hidden-surface predicate (kittrial-5bb.64; .41 6.0, .58 8.5, .60 8).
#
# A row is a record anchor when it carries one of the type labels below, and a
# comment is a record comment when its body starts with one of the family
# prefixes, in ANY version: an unknown newer `Kind: <family>-vN` is hidden the
# same way and never fails a read. Every surface of the shared ten-surface list
# applies these two predicates and nothing else, so a later slice that adds a
# record kind extends one table.
# ---------------------------------------------------------------------------

# Each record type label and the v1 record prefixes that make a row carrying it an
# anchor. The first record a writer slice posts on a new anchor is one of these.
RECORD_ANCHOR_FAMILIES = {
    'reference': (REFERENCE_ENTRY_PREFIX, REFERENCE_ACCEPTANCE_PREFIX),
    'proposal': (PROPOSAL_PREFIX, PROPOSAL_DISPOSITION_PREFIX),
    'contribution-settings': (CONTRIBUTION_SETTINGS_PREFIX,),
    'capability': (CAPABILITY_ENTRY_PREFIX, CAPABILITY_ACCEPTANCE_PREFIX,
                   CAPABILITY_VERIFICATION_PREFIX, CAPABILITY_ALIAS_PREFIX),
}
RECORD_ANCHOR_LABELS = frozenset(RECORD_ANCHOR_FAMILIES)
RECORD_COMMENT_FAMILIES = ('Kind: reference-', 'Kind: requirement-proposal-',
                           'Kind: proposal-disposition-', 'Kind: contribution-settings-',
                           'Kind: capability-')
_RECORD_KIND = re.compile(r'Kind: ([a-z][a-z-]*?)-v([1-9][0-9]{0,5})\n')


def carries_record_label(row):
    """True when a row carries a record type label, so a surface that read it
    without comments must fetch them before it can decide is_record_anchor."""
    labels = row.get('labels') if isinstance(row, dict) else None
    return isinstance(labels, (list, tuple)) and any(
        isinstance(label, str) and label in RECORD_ANCHOR_LABELS for label in labels)


def is_record_anchor(row):
    """True for the native anchor of a reference, proposal, settings or capability.

    An anchor is a row carrying an exact record type label AND a v1 record comment
    of that label's family (kittrial-5bb.64 review, coordinator fix (a)). The label
    alone is not evidence - projects use these words as ordinary labels - and nor
    are request:/request-content: labels, which create-child also writes. After this
    kit deploys both halves are guard-protected: the record prefixes are reserved and
    the labels of a real anchor cannot be replaced or removed. Hiding grants no
    authority, so a forged pre-deploy pair only hides that one row.

    The later slices' readers use this same predicate. A writer that crashes after
    creating and labelling an anchor but before posting its first record leaves a row
    that reads as an ordinary closed task until the writer's reconcile posts that
    record; the writer slices own that recovery. `row['comments']` must be the row's
    comments: a caller that read rows without comments fetches them for rows where
    carries_record_label is true.
    """
    if not isinstance(row, dict):
        return False
    labels = row.get('labels')
    comments = row.get('comments')
    if not isinstance(labels, (list, tuple)) or not isinstance(comments, list):
        return False
    prefixes = tuple(prefix for label in labels if isinstance(label, str)
                     for prefix in RECORD_ANCHOR_FAMILIES.get(label, ()))
    if not prefixes:
        return False
    for comment in comments:
        text = comment.get('text') if isinstance(comment, dict) else None
        if isinstance(text, str) and any(view.startswith(prefixes)
                                         for view in (text, _reserved_prefix_view(text))):
            return True
    return False


def is_record_comment(text):
    """True for a reference/proposal/settings/capability record comment, any version.

    The view drops a leading BOM and folds CRLF, as the reserved-prefix guard
    does, so a lookalike is hidden too.
    """
    if not isinstance(text, str):
        return False
    return any(view.startswith(RECORD_COMMENT_FAMILIES)
               for view in (text, _reserved_prefix_view(text)))


def record_comment_kind(text):
    """(kind, version, state) for a record comment, else None.

    `state` is `supported` for a v1 record of one of the nine designed kinds and
    `unsupported` for anything else (an unknown version or kind), so a later slice's
    reader can report an unknown newer record per entry instead of failing the whole
    read (.41 3.8, .58 3.8). Every version is raw-write reserved either way.
    """
    if not is_record_comment(text):
        return None
    view = _reserved_prefix_view(text)
    match = _RECORD_KIND.match(view)
    if match is None:
        return ('unknown', None, 'unsupported')
    kind, version = match.group(1), int(match.group(2))
    supported = kind in _RECORD_KIND_RESERVATIONS and version == 1
    return (kind, version, 'supported' if supported else 'unsupported')


# The HTTP list asks for the anchors among at most this many labelled rows by id,
# in one `bd show --include-comments`; above it, one `bd export --all` is the cheaper
# read (kittrial-5bb.71 review 01a0f8b5, `scale-and-lock`).
ANCHOR_READ_IDS_MAX = 50


def record_anchor_ids(rows):
    """The sorted ids of the record anchors among full rows (with comments), by the
    same predicate every surface uses. One export answers a whole snapshot, so a
    reader that listed rows without comments pays one read, not one per labelled row
    (kittrial-5bb.71)."""
    return sorted(str(row['id']) for row in rows
                  if isinstance(row, dict) and row.get('id') is not None and is_record_anchor(row))


def hide_records(rows):
    """The rows a surface may show: record anchors dropped, and record comments
    removed from every other row (copied, never mutated in place)."""
    if not isinstance(rows, list):
        return rows
    shown = []
    for row in rows:
        if is_record_anchor(row):
            continue
        comments = row.get('comments') if isinstance(row, dict) else None
        if isinstance(comments, list) and any(
                isinstance(c, dict) and is_record_comment(c.get('text')) for c in comments):
            row = dict(row, comments=[c for c in comments
                                      if not (isinstance(c, dict) and is_record_comment(c.get('text')))])
        shown.append(row)
    return shown


def reserved_label_in_args(args):
    """First reserved-namespace label written by a create/update label flag."""
    if not isinstance(args, list):
        return None
    for value in _label_values(args):
        for part in value.split(','):
            label = reserved_label(part.strip())
            if label is not None:
                return label
    return None


def first_reserved_label(labels):
    """First reserved-namespace label in a native label list, or None."""
    if not isinstance(labels, (list, tuple)):
        return None
    for label in labels:
        found = reserved_label(label)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------
# Reserved-label read-before-write guard.
#
# Refusing a reserved label *value* is not enough. Two routes still moved the
# namespace on the contributor path (kittrial-5bb.30):
#   * bd copies parent labels onto a child created with `create --parent X`
#     unless `--no-inherit-labels` is given, so a contributor child of an
#     operator issue holding request:REAL,request-content:<sha> became a second
#     holder ("Duplicate native request records"); `--labels` does not prevent
#     it.
#   * `update X --set-labels ...` replaces the whole set, so a replacement that
#     names no reserved value stripped request:/request-content: from the
#     operator issue and left a planted child as the only holder.
# Both need the affected issue's current labels, so endpoint.execute reads them
# through the native client under the same project lock as the write.
# ---------------------------------------------------------------------------

# Long flags of the two guarded commands that take a value, verified against
# the pinned bd 1.2.2 help output. A value-taking long flag consumes the next
# argv token unless the value is `=joined`, which is what keeps a decoy token
# such as `--title --parent X` from being read as a real flag.
BD_LONG_VALUE_FLAGS = {
    'create': {
        '--acceptance', '--append-notes', '--assignee', '--body-file',
        '--context', '--defer', '--deps', '--description', '--design',
        '--design-file', '--due', '--estimate', '--event-actor',
        '--event-category', '--event-payload', '--event-target',
        '--external-ref', '--file', '--graph', '--id', '--label', '--labels',
        '--metadata', '--mol-type', '--notes', '--parent', '--priority',
        '--repo', '--skills', '--spec-id', '--title', '--type', '--waits-for',
        '--waits-for-gate', '--wisp-type',
    },
    'update': {
        '--acceptance', '--add-label', '--append-notes', '--assignee',
        '--await-id', '--body-file', '--defer', '--description', '--design',
        '--design-file', '--due', '--estimate', '--external-ref', '--metadata',
        '--notes', '--parent', '--priority', '--remove-label', '--session',
        '--set-labels', '--set-metadata', '--spec-id', '--status', '--title',
        '--type', '--unset-metadata',
    },
    # Verbatim from the pinned bd 1.2.2 `close --help`/`reopen --help`, for the
    # status-change guard on record anchors (kittrial-5bb.92 item 4).
    'close': {'--reason', '--reason-file', '--session'},
    'reopen': {'--reason'},
}
BD_LONG_BOOL_FLAGS = {
    'create': {
        '--dry-run', '--ephemeral', '--force', '--no-history',
        '--no-inherit-labels', '--silent', '--stdin', '--validate',
    },
    'update': {
        '--allow-empty-description', '--claim', '--ephemeral', '--history',
        '--no-history', '--persistent', '--stdin',
    },
    'close': {'--claim-next', '--continue', '--force', '--no-auto', '--suggest-next'},
}
# Label-replacing writes: `--set-labels` replaces the whole set and
# `--remove-label` drops named labels, so either can take the reserved
# namespace off a holder. `--add-label`/`--labels` can only add, and adding a
# reserved value is already refused by reserved_label_in_args().
LABEL_REPLACING_FLAGS = ('--set-labels', '--remove-label')


# pflag parses boolean flag values with Go's strconv.ParseBool, which accepts
# exactly these literals and rejects everything else (including `no`, `yes`,
# `2` and the empty value of `--flag=`). The guard must mirror that table
# exactly: a value the guard reads as "false" while bd reads it as invalid or
# true would let a reserved label through, so anything unrecognised fails
# closed rather than defaulting to either answer.
_GO_TRUE_LITERALS = frozenset(('1', 't', 'T', 'TRUE', 'true', 'True'))
_GO_FALSE_LITERALS = frozenset(('0', 'f', 'F', 'FALSE', 'false', 'False'))


def _parse_go_bool(value):
    """Tri-state pflag boolean: True, False, or None when the value is invalid.

    `_bd_scan()` records a bare `--flag` as Python True; an explicit value
    (`--flag=v`, including the empty `--flag=`) arrives as the raw string.
    """
    if value is True:
        return True
    if value is False:
        return False
    if isinstance(value, str):
        if value in _GO_TRUE_LITERALS:
            return True
        if value in _GO_FALSE_LITERALS:
            return False
    return None


def _bd_scan(args, command):
    """Structurally scan a create/update argv list.

    Returns (flags, operands, unknown): `flags` is an ordered list of
    (name, value) with the command's verified long and short inventories, so
    values are consumed and never mistaken for operands or flags; `operands`
    are the positional tokens; `unknown` holds tokens whose flag shape could
    not be resolved, which makes the scan ambiguous and fails closed.
    """
    value_long = BD_LONG_VALUE_FLAGS.get(command, set()) | BD_GLOBAL_VALUE_FLAGS
    bool_long = BD_LONG_BOOL_FLAGS.get(command, set()) | BD_GLOBAL_BOOL_FLAGS
    table = _short_flag_table(command)
    flags = []
    operands = []
    unknown = []
    index = 1
    end_of_flags = False
    while index < len(args):
        token = args[index]
        if not isinstance(token, str):
            unknown.append(token)
            index += 1
            continue
        if end_of_flags:
            operands.append(token)
            index += 1
            continue
        if token == '--':
            end_of_flags = True
            index += 1
            continue
        if len(token) > 1 and token.startswith('--'):
            name, sep, value = token.partition('=')
            if name in value_long:
                if not sep:
                    value = (args[index + 1]
                             if index + 1 < len(args)
                             and isinstance(args[index + 1], str) else None)
                    index += 1
                flags.append((name, value))
            elif name in bool_long:
                flags.append((name, value if sep else True))
            else:
                unknown.append(token)
            index += 1
            continue
        if len(token) > 1 and token.startswith('-'):
            body = token[1:]
            end = body.find('=')
            chars = body[:end] if end != -1 else body
            attached = body[end + 1:] if end != -1 else None
            position = 0
            while position < len(chars):
                ch = chars[position]
                spec = table.get(ch) if table else None
                if spec is None:
                    unknown.append(token)
                    break
                if spec == 'value':
                    if attached is not None and position == len(chars) - 1:
                        value = attached
                    elif position < len(chars) - 1:
                        value = chars[position + 1:]
                    else:
                        value = (args[index + 1]
                                 if index + 1 < len(args)
                                 and isinstance(args[index + 1], str) else None)
                        index += 1
                    flags.append(('-' + ch, value))
                    break
                flags.append(('-' + ch, True))
                position += 1
            index += 1
            continue
        operands.append(token)
        index += 1
    return flags, operands, unknown


def unresolved_bd_flags(args):
    """Unresolvable create/update flags; a non-empty list must fail the guard.

    The raw create/update path resolves its flag surface from the pinned bd
    1.2.2 inventory, which includes the undocumented `create --label` alias of
    `--labels`. A token outside that inventory could be another hidden or
    deprecated label-writing alias (or a value-taking flag whose value the scan
    would misread as a flag), so it is refused rather than assumed harmless.
    bd itself rejects a genuinely unknown flag, so this only tightens the guard.
    """
    if not isinstance(args, list) or not args:
        return []
    command = args[0] if isinstance(args[0], str) else None
    if command not in ('create', 'update'):
        return []
    _, _, unknown = _bd_scan(args, command)
    return list(unknown)


def label_guard_request(args):
    """Describe the read-before-write reserved-label check an argv list needs.

    Returns None when the invocation cannot move a reserved label, else a dict:

      {'kind': 'inherit', 'target': parent_id|None, 'ambiguous': bool}
          `create --parent X` without an effective --no-inherit-labels: X's
          labels must be read before the child inherits them.
      {'kind': 'replace', 'targets': [id, ...], 'ambiguous': bool}
          `update ... --set-labels/--remove-label`: every named target's labels
          must be read before the replacement.

    Anything the scan cannot resolve (unknown flag, repeated --parent, missing
    parent value, an unparseable --no-inherit-labels value, no target operand)
    is reported as ambiguous so the caller fails closed rather than guessing.
    An unknown flag is NEVER discarded before the branch decision: it could be
    an undocumented label alias (bd 1.2.2 accepts `create --label`) or a
    replacement switch, so it keeps the request ambiguous in both branches.
    """
    if not isinstance(args, list) or not args:
        return None
    command = args[0] if isinstance(args[0], str) else None
    if command not in ('create', 'update'):
        return None
    flags, operands, unknown = _bd_scan(args, command)
    ambiguous = bool(unknown)
    if command == 'create':
        parents = [value for name, value in flags if name == '--parent']
        # pflag parses every occurrence with strconv.ParseBool and the LAST one
        # wins, so a repeated flag is decided by its final value: an explicit
        # truthy value disables the inheritance guard, while `=f`/`=F`/`=0`/...
        # mean inheritance, exactly as strconv.ParseBool reads them. Any
        # occurrence bd cannot parse makes bd reject the whole command with an
        # error, so the guard refuses too instead of guessing which spelling bd
        # would have used.
        no_inherit = False
        invalid_bool = False
        for name, value in flags:
            if name != '--no-inherit-labels':
                continue
            parsed = _parse_go_bool(value)
            if parsed is None:
                invalid_bool = True
            else:
                no_inherit = parsed
        if invalid_bool or ambiguous:
            return {'kind': 'inherit',
                    'target': parents[0] if parents else None,
                    'ambiguous': True}
        if no_inherit or not parents:
            return None
        if len(parents) > 1 or not parents[0]:
            return {'kind': 'inherit', 'target': parents[0], 'ambiguous': True}
        return {'kind': 'inherit', 'target': parents[0], 'ambiguous': False}
    replacing = [name for name, _ in flags if name in LABEL_REPLACING_FLAGS]
    if not replacing and not ambiguous:
        return None
    # `@attachment:` tokens are transport placeholders, not issue IDs; the
    # endpoint expands them into file flags after this guard.
    targets = [token for token in operands
               if not token.startswith('@attachment:')]
    if not targets:
        # bd would fall back to the last touched issue, which the guard cannot
        # resolve: fail closed.
        ambiguous = True
    return {'kind': 'replace', 'targets': targets, 'ambiguous': ambiguous}


# Backwards-compatible aliases for the previous flag tables.
COMMENT_NO_VALUE_FLAGS = BD_GLOBAL_BOOL_FLAGS
COMMENT_FLAGS_WITH_VALUE = {'-f', '--file'}


def status_change_targets(args):
    """The ids a `close`/`reopen`/`update --status` invocation names, for the guard.

    Returns ``None`` when the invocation cannot move a status, else
    ``(command, targets)``. ``targets`` is the positional issue ids, or ``None`` when
    the scan is ambiguous (an unknown flag) or names no issue: bd would then act on the
    last touched issue, which the record-anchor guard cannot verify, so the caller
    fails closed. Verbatim pinned bd 1.2.2 close/reopen flag inventories are used, so a
    flag value is never mistaken for an issue id (kittrial-5bb.92 item 4).
    """
    if not isinstance(args, list) or not args:
        return None
    command = args[0] if isinstance(args[0], str) else None
    if command not in ('close', 'reopen', 'update'):
        return None
    flags, operands, unknown = _bd_scan(args, command)
    if command == 'update' and not any(name == '--status' for name, _ in flags):
        return None
    if unknown:
        return (command, None)
    targets = [token for token in operands if not token.startswith('@attachment:')]
    return (command, targets or None)


# Machine records are canonical UTF-8 with `\n` line endings. A client that
# prepends a UTF-8 BOM or sends CRLF produces a body the strict parsers ignore,
# so a "lookalike" acceptance/revision record would otherwise be posted as
# ordinary prose (kittrial-pth.26 review: BOM/CRLF lookalikes pass as ordinary
# comments). The raw path refuses such a body too: one reserved-prefix view is
# computed by dropping a single leading BOM and folding CRLF to LF, and a match
# there is reserved. Legitimate writers emit exact canonical bytes, so nothing
# valid is refused (a BOM/CRLF plan registration was never canonical).
def _reserved_prefix_view(body):
    """Drop one leading BOM and fold CRLF, for lookalike-prefix matching."""
    view = body[1:] if body.startswith('\ufeff') else body
    if '\r\n' in view:
        view = view.replace('\r\n', '\n')
    return view


def reserved_match(body):
    """Return (prefix, kind, operation) for a reserved body, else None.

    A body that is a reserved prefix only after dropping a leading BOM or
    folding CRLF is matched as well, so a lookalike cannot slip through as
    ordinary prose.
    """
    if not isinstance(body, str):
        return None
    for view in (body, _reserved_prefix_view(body)):
        for prefix, kind, operation in RESERVED:
            if view.startswith(prefix):
                return (prefix, kind, operation)
        versioned = _RECORD_KIND_ANY_VERSION.match(view)
        if versioned:
            kind, operation = _RECORD_KIND_RESERVATIONS[versioned.group(1)]
            return (versioned.group(0), kind, operation)
    return None


def is_legitimate_writer(body, actor=None, task=None):
    """True when a reserved-prefix body is a validated supported raw write.

    worker_gate plan registrations written through their documented route carry
    exact canonical bytes bound to the requesting actor/task; those must pass.
    Handoff, requirement revision and requirement acceptance records are never
    legitimate on the raw path: they use structured operations whose authority
    (ownership, kind/key/state, F3 acceptance evidence) cannot be established
    from self-asserted comment fields. Without actor/task context (pure helper
    use) only context-free canonical validity is checked; the endpoint always
    supplies context and fails closed on mismatch.
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
        # Requirement revisions are written only through the dedicated
        # requirement_records.py operation (internal run path, kind/key/state
        # and F3 acceptance authority checked). Raw endpoint revision comments
        # are rejected unconditionally, even canonical ones: a canonical
        # accepted revision must not be postable by an arbitrary actor.
        return False
    if body.startswith(ACCEPTANCE_PREFIX):
        # Durable F3 acceptance evidence is written only by the operator
        # acceptance route (admin.py requirement-apply) bound to the exact
        # revision hash; a self-asserted acceptance comment must never pass.
        return False
    if body.startswith(VOID_PREFIX):
        # Operator authority cannot be established from self-asserted comment
        # fields: a void record carries only its own payload, and its native
        # author is whatever `bd` was told at write time. Verified voids are
        # written by the host-side structured operation (admin.py void-record,
        # under the project lock); every raw path stays rejected, exactly like
        # handoff records.
        return False
    if body.startswith(REVERT_PREFIX):
        # Same reasoning for the audited integration revert record: operator
        # authority comes from the deployment allowlist and the host-side
        # admin.py revert-record command, never from the record's own fields.
        return False
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
    values are consumed as paths, not bodies. An unrecognized flag, or an
    attachment token placed before the subcommand, is ambiguous and rejected
    before any native write.
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


# ---------------------------------------------------------------------------
# HTTP-allocated actor ids (kittrial-5bb.70)
# ---------------------------------------------------------------------------
# The ids the HTTP service allocates and acts under (http_auth: `usr_` / `agent_` + 16
# hex), also as the head of a credential's sub-actor. They are reserved for that
# service: readers treat a proposal record authored under one as written with HTTP
# authority (proposal_records.disposition_authority, entry_view).
HTTP_ACTOR = re.compile(r'(?:usr|agent)_[0-9a-f]{16}(?:/.*)?')


def refuse_http_actor(actor, launched_by_service):
    """Refuse a declared actor that has an HTTP id shape, on every endpoint action,
    unless the endpoint was launched by the HTTP service.

    `launched_by_service` is the endpoint's `authority_config is not None`: it is true
    only when the process was started with `--authority-store`, a command-line flag the
    HTTP service passes and request data cannot set. A caller confined to the endpoint
    command (an authorized_keys `command=` entry that ignores the caller's command
    line) therefore cannot assert it. A caller with a shell on the service account can
    start the endpoint however it likes; that caller is inside the trust boundary of
    every check in this kit.
    """
    if not launched_by_service and isinstance(actor, str) and HTTP_ACTOR.fullmatch(actor):
        raise ValueError('Actor %s has the shape of an HTTP account or agent id, which only the HTTP service '
                         'acts under; declare your own session actor' % actor[:40])
