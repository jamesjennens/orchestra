"""Project review standards Phase 1 (kittrial-5bb.570): versioned register,
kit-validated check records, warn/block approval gate, waivers, and sign-offs.

All JSON decoding uses ``export_requirements.parse_json`` (depth-guarded, duplicate-key
refusing, canonicalizable). Host filesystem reads and writes refuse symlinks via
``O_NOFOLLOW`` / ``regular_file`` and commit atomically via temporary files + ``fsync``
+ ``os.replace``. Reads never mutate disk or state.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fnmatch
import hashlib
import os
import posixpath
import re
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

from export_requirements import parse_json
from field_limits import check_text
from regular_file import _is_reparse_point, read_regular_text
from requirements import canonical_bytes
import review_workflow
from review_workflow import (
    BUNDLE_GIT_TIMEOUT,
    CoordinatorRuleRefusal,
    DELIVERY_SERVE_MAX_BYTES,
    DeliveryCorruptError,
    DeliveryTooLargeError,
    DeliveryUnavailableError,
    author_key,
    identity,
    plain_text,
    resolve_delivery_bundle_path,
    same_party,
    verify_served_bundle,
)

SCHEMA_VERSION = 1
STANDARDS_DIRNAME = '.standards'
BUNDLE_FETCH_LOCK_NAME = '.standards-bundle-fetch.lock'
BUNDLE_LOCK_WAIT_SECONDS = 15.0
BUNDLE_INSPECT_SEMAPHORE = threading.BoundedSemaphore(1)
STAGED_TTL_SECONDS = 24 * 3600

MAX_FILE_BYTES = 512 * 1024
MAX_COMMIT_OBSERVATION_BYTES = 4 * 1024 * 1024
MAX_COMMIT_OBSERVATION_FILE_BYTES = 8 * 1024 * 1024
COMMIT_OBSERVATIONS_PER_PARTY_MAX_BYTES = 512 * 1024
COMMIT_FAIL_ONLY_ENTRY_MAX_BYTES = 16 * 1024
COMMIT_FAIL_ALLOWANCE_PER_PARTY_MAX = 8
COMMIT_FAIL_ALLOWANCE_TOTAL_MAX = 256
MAX_BLOB_BYTES = 10 * 1024 * 1024
BINARY_SNIFF_BYTES = 8192
NOTIFICATIONS_MAX = 500
GATE_HISTORY_MAX = 50
COMMIT_OBSERVATIONS_MAX = 256
COMMIT_OBSERVATIONS_PER_RECORDER_MAX = 32
REVIEW_PREFIX = 'Kind: contribution-review-v1\n'
ALL_CHANGES_MUST_DEFAULT = 5
RULES_MAX = 200
REFS_MAX = 16
SELECTORS_MAX = 64
CITATIONS_MAX = 32
CHECKS_MAX = 200
SUMMARY_REASON_MAX = 1200
STATEMENT_MAX = 600
RATIONALE_MAX = 2000
CHECK_REASON_MAX = 1000
COMMAND_MAX = 1000
TOOL_NAME_MAX = 120
DECLARED_MODEL_MAX = 120

RULE_ID_RE = re.compile(r'^[A-Z][A-Z0-9]{1,15}-[0-9]{1,6}$')
PROJECT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
COMMIT_RE = re.compile(r'^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
HTTP_PERSON_RE = re.compile(r'^usr_[0-9a-f]{16}$')
HOST_PERSON_RE = re.compile(r'^person:[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$')
AGENT_LIKE_RE = re.compile(
    r'^(?:agent_[0-9a-f]{16}|lane:[A-Za-z0-9._/-]+|cred_[0-9a-f]{16}|worker(?:/.*)?)$',
    re.IGNORECASE,
)
REF_KEY_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{1,127}$')
DRAFT_ID_RE = re.compile(r'^draft-[0-9a-f]{16}$')
WAIVER_ID_RE = re.compile(r'^waiver-[0-9a-f]{16}$')
CITATION_RE = re.compile(r'^(?P<path>.+):(?P<start>-?\d+)(?:-(?P<end>-?\d+))?$')

SEVERITIES = ('must', 'should')
CHECK_KINDS = ('mechanical', 'judgement')
CHECK_RESULTS = ('pass', 'fail', 'not_applicable')
GATE_MODES = ('warn', 'block')
DRAFT_STATUSES = ('pending', 'accepted', 'rejected')
REVIEW_STANDARDS_KEYS = ('checks', 'adopt_recommendation', 'declared_tool', 'declared_model')

PERSON_ONLY_SUBCOMMANDS = frozenset({
    'put',
    'rule-edit',
    'set-gate',
    'waive',
    'draft-decide',
    'record-commit',
    'owner-departed',
})
CONTRIBUTOR_SUBCOMMANDS = frozenset({
    'show',
    'history',
    'lint',
    'draft-propose',
    'draft-list',
    'waivers',
    'sign-off',
    'notifications',
})

#: Closed extension and basename to language mapping for R4 applicability.
EXTENSION_LANGUAGES = {
    '.py': 'python',
    '.pyi': 'python',
    '.js': 'javascript',
    '.mjs': 'javascript',
    '.cjs': 'javascript',
    '.jsx': 'javascript',
    '.ts': 'typescript',
    '.mts': 'typescript',
    '.cts': 'typescript',
    '.tsx': 'typescript',
    '.sh': 'shell',
    '.bash': 'shell',
    '.ps1': 'powershell',
    '.go': 'go',
    '.rs': 'rust',
    '.c': 'c',
    '.h': 'c',
    '.cc': 'cpp',
    '.cpp': 'cpp',
    '.cxx': 'cpp',
    '.hpp': 'cpp',
    '.hh': 'cpp',
    '.java': 'java',
    '.kt': 'kotlin',
    '.kts': 'kotlin',
    '.rb': 'ruby',
    '.php': 'php',
    '.cs': 'csharp',
    '.swift': 'swift',
    '.sql': 'sql',
    '.html': 'html',
    '.htm': 'html',
    '.css': 'css',
    '.json': 'json',
    '.yml': 'yaml',
    '.yaml': 'yaml',
    '.toml': 'toml',
    '.md': 'markdown',
    '.markdown': 'markdown',
}
BASENAME_LANGUAGES = {
    'dockerfile': 'dockerfile',
    'containerfile': 'dockerfile',
    'makefile': 'make',
    'gnumakefile': 'make',
}
KNOWN_LANGUAGES = frozenset(set(EXTENSION_LANGUAGES.values()) | set(BASENAME_LANGUAGES.values()))

REVIEWER_ANTI_PREFILL_NOTICE = (
    "The author's delivery summary and report are claims, not evidence; the kit never "
    "pre-fills check records from them, and you must verify and record each check yourself."
)


class StandardsRefusal(CoordinatorRuleRefusal):
    """Pre-effect standards refusal carrying an explicit HTTP status code."""

    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


class StandardsDamagedError(ValueError):
    """Raised internally when head.json or versions/ exists on disk/state and is invalid."""


class BundleLockTimeoutError(RuntimeError):
    """Raised when the installation-wide bundle fetch lock wait times out."""


class SignoffCommitError(RuntimeError):
    """Raised post-approval when finalizing a committed sign-off cannot complete safely."""


# -- Verbatim pinned refusal sentences (#1..#24) ------------------------------

def refusal_agent_register_edit():
    # Sentence #1 (updated in Plan 2 Item 12)
    return (
        "Standards register edits require a person's session; agent and worker credentials "
        "cannot edit rules directly \u2014 submit a draft instead."
    )


def refusal_agent_rule_owner(rule_id, owner):
    # Sentence #2
    return (
        "Rule %s owner %s is not a person account; rule owners must be people, never agents."
        % (rule_id, owner)
    )


def refusal_unauthorized_structural_edit(rule_id):
    # Sentence #3
    return (
        "Only a project coordinator, project owner, or named register editor can add or "
        "remove rules or change a rule's owner (rule %s)." % rule_id
    )


def refusal_unauthorized_rule_edit(rule_id, owner):
    # Sentence #4
    return (
        "Only rule %s's owner (%s), a project coordinator, or a project owner can edit rule %s."
        % (rule_id, owner, rule_id)
    )


def refusal_inherited_must_readonly(pid, rule_id, shared_pid):
    # Sentence #5 (updated in Plan 2 Item 7 & 12)
    return (
        "Project %s cannot modify or drop inherited 'must' rule %s from %s; inherited "
        "'must' rules are read-only." % (pid, rule_id, shared_pid)
    )


def refusal_inheritance_not_shared(shared_pid):
    # Sentence #6a
    return (
        "Standards register inheritance refused: project %s does not publish a shared "
        "standards register." % shared_pid
    )


def refusal_inheritance_transitive(shared_pid):
    # Sentence #6b
    return (
        "Standards register inheritance refused: transitive inheritance through %s is not allowed."
        % shared_pid
    )


def refusal_gate_switch_authority():
    # Sentence #7
    return (
        "Only a person who is a project coordinator or owner can change the standards gate "
        "mode; agents cannot switch the gate."
    )


def refusal_mechanical_reviewer_pass(rule_id, tool, command, commit):
    # Sentence #8 (updated in Plan 2 Item 12)
    return (
        "Rule %s is a mechanical check (%s: %s); a verifier records its CI result for commit "
        "%s, so a reviewer cannot assert 'pass'." % (rule_id, tool, command, commit)
    )


def refusal_judgement_pass_evidence(rule_id, commit):
    # Sentence #9
    return (
        "Check record for judgement rule %s with result 'pass' requires at least one "
        "file:line citation in commit %s or a command with its integer exit_code."
        % (rule_id, commit)
    )


def refusal_citation_unresolved(citation, rule_id, commit, detail):
    # Sentence #10
    return (
        "Citation '%s' for rule %s does not resolve in delivered commit %s: %s."
        % (citation, rule_id, commit, detail)
    )


def refusal_not_applicable_reason(rule_id):
    # Sentence #11
    return (
        "Check record for rule %s with result 'not_applicable' requires a non-empty reason."
        % rule_id
    )


def refusal_fail_item_id(rule_id, contribution_id):
    # Sentence #12 (updated in Plan 2 Item 10 & 12)
    return (
        "Check record for rule %s with result 'fail' must cite a valid review item_id on "
        "contribution %s." % (rule_id, contribution_id)
    )


def refusal_commit_observation_authority(commit):
    # Sentence #13 (updated in Plan 2 Item 6 & 12)
    return (
        "Commit standards observation refused: only a listed verifier, listed host operator, "
        "coordinator person session, or dedicated verifier credential independent of the "
        "contribution's author, assignee, reviewer, and approver may record commit or CI "
        "observations for %s." % commit
    )


def refusal_waiver_agent():
    # Sentence #14a
    return "Waivers require a person's session; agents cannot grant waivers."


def refusal_waiver_unauthorized(rule_id, owner):
    # Sentence #14b
    return (
        "Only rule %s's owner (%s) or a project coordinator can grant a waiver for rule %s."
        % (rule_id, owner, rule_id)
    )


def refusal_waiver_same_party(rule_id, actor):
    # Sentence #15
    return (
        "Waiver refused for rule %s: %s is of the delivery author's or assignee's party, "
        "and an author cannot waive a rule on their own delivery." % (rule_id, actor)
    )


def refusal_block_gate(version, details):
    # Sentence #16
    return (
        "Approval refused by standards gate (mode: block, register v%d): unresolved 'must' "
        "rule(s): %s." % (version, ', '.join(details))
    )


def refusal_draft_decide_rule_owner(rule_id, owner, draft_id):
    # Sentence #17a
    return (
        "Only rule %s's owner (%s), a project coordinator, or a project owner can decide "
        "draft %s." % (rule_id, owner, draft_id)
    )


def refusal_draft_decide_structural(draft_id):
    # Sentence #17b
    return (
        "Only a project coordinator, project owner, or named register editor can decide "
        "draft %s because it adds, removes, or re-owns a rule." % draft_id
    )


def refusal_version_conflict(pid, expected, actual):
    # Sentence #18
    return (
        "Standards register version conflict on project %s: expected version %d, current "
        "version is %d; re-read the register and retry." % (pid, expected, actual)
    )


def refusal_damaged_register_block(pid, detail):
    # Sentence #19
    return (
        "Approval refused: standards register in project %s is damaged (%s); repair or "
        "restore .standards/ before approving." % (pid, detail)
    )


def refusal_expired_waiver(waiver_id, rule_id, contribution_id, date_str):
    # Sentence #20
    return (
        "Waiver %s for rule %s on delivery %s expired on %s and no longer applies."
        % (waiver_id, rule_id, contribution_id, date_str)
    )


def refusal_inheritance_no_read(shared_pid):
    # Sentence #21
    return (
        "Standards register inheritance refused: you do not have read access to project %s."
        % shared_pid
    )


def refusal_draft_reject_reason(draft_id):
    # Sentence #22a
    return "Rejecting draft %s requires a non-empty reason." % draft_id


def refusal_draft_already_decided(draft_id, status):
    # Sentence #22b
    return "Draft %s has already been %s and cannot be decided again." % (draft_id, status)


def refusal_ssh_person_only(subcommand):
    # Sentence #23
    return (
        "Standards operation '%s' is a person-only action and is not accepted on the "
        "contributor SSH endpoint; use the web interface signed in as a person, or "
        "'admin.py standards %s' as a listed host operator." % (subcommand, subcommand)
    )


def refusal_invalid_adopt_recommendation(rec_id, contribution_id, commit):
    # Sentence #24a
    return (
        "Cannot adopt recommendation %s: it is not a standing independent recommendation "
        "with check records for contribution %s at commit %s."
        % (rec_id, contribution_id, commit)
    )


def refusal_detach_inherited_must(pid, shared_pid, rule_ids):
    return (
        "Project %s cannot remove or change 'inherits' from shared project %s while %s defines "
        "inherited 'must' rule(s) (%s); inherited 'must' rules are read-only in the child."
        % (pid, shared_pid, shared_pid, ', '.join(rule_ids))
    )


def refusal_put_register_authority():
    return (
        "Only a project coordinator, project owner, or named register editor can create or "
        "replace a project standards register."
    )


def refusal_na_over_mechanical_fail(rule_id, commit):
    return (
        "Check record for mechanical 'must' rule %s cannot assert 'not_applicable' when a "
        "failing CI result is recorded for commit %s; fix the failure or request a single-delivery waiver."
        % (rule_id, commit)
    )


def refusal_commit_observation_cap(commit, limit):
    return (
        "Commit standards observation refused for %s: commit has reached the maximum of %d "
        "recorded observations; existing observations are never evicted." % (commit, limit)
    )


def refusal_commit_observation_recorder_cap(commit, limit):
    return (
        "Commit standards observation refused for %s: recorder's party has reached the per-party "
        "limit of %d observations on this commit." % (commit, limit)
    )


def refusal_commit_observation_size_cap(commit, limit):
    return (
        "Commit standards observation refused for %s: stored commit observations would exceed "
        "the %d-byte limit; existing observations are never evicted." % (commit, limit)
    )


def refusal_commit_observation_party_bytes_cap(commit, limit):
    return (
        "Commit standards observation refused for %s: recorder's party would exceed the "
        "per-party %d-byte observation budget on this commit." % (commit, limit)
    )


def refusal_commit_fail_allowance_cap(commit, limit):
    return (
        "Commit standards observation refused for %s: recorder's party has reached the "
        "per-party reserve of %d compact failing CI observations on this commit."
        % (commit, limit)
    )


# -- Time & Digest Helpers ----------------------------------------------------

def utc_now_iso(now=None):
    if isinstance(now, str) and now.strip():
        return now
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        dt = datetime.fromtimestamp(now, tz=timezone.utc)
    elif isinstance(now, datetime):
        dt = now.astimezone(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def utc_today_str(now=None):
    iso = utc_now_iso(now)
    return iso[:10]


def _parse_iso_epoch(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _record_sha256(payload):
    body = {k: v for k, v in payload.items() if k != 'sha256'}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _random_hex(nbytes=8):
    return os.urandom(nbytes).hex()


# -- Symlink-Refusing Filesystem Helpers --------------------------------------

def _lstat_or_none(path):
    try:
        return os.lstat(str(path))
    except FileNotFoundError:
        return None


def _require_real_dir(path, label):
    info = _lstat_or_none(path)
    if info is None:
        return False
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise StandardsDamagedError('%s is not a regular directory' % label)
    return True


def _ensure_real_dir(base_dir, *parts):
    cur = Path(base_dir)
    info = _lstat_or_none(cur)
    if info is None or stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError('Project directory is not a regular directory')
    for part in parts:
        cur = cur / part
        st = _lstat_or_none(cur)
        if st is None:
            cur.mkdir(mode=0o755)
            st = os.lstat(str(cur))
        if stat.S_ISLNK(st.st_mode) or _is_reparse_point(st) or not stat.S_ISDIR(st.st_mode):
            raise ValueError('%s is not a regular directory' % part)
    return cur


def _list_regular_names(dir_path, label):
    if not _require_real_dir(dir_path, label):
        return []
    names = []
    with os.scandir(str(dir_path)) as it:
        for entry in it:
            if entry.name.startswith('.tmp-'):
                continue
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode) or _is_reparse_point(st) or not stat.S_ISREG(st.st_mode):
                raise StandardsDamagedError('%s/%s is not a regular file' % (label, entry.name))
            names.append((entry.name, st))
    names.sort(key=lambda item: item[0])
    return names


def _read_json_file(file_path, label, *, limit=MAX_FILE_BYTES):
    st = _lstat_or_none(file_path)
    if st is None:
        return None
    if stat.S_ISLNK(st.st_mode) or _is_reparse_point(st) or not stat.S_ISREG(st.st_mode):
        raise StandardsDamagedError('%s is not a regular file' % label)
    try:
        raw = read_regular_text(str(file_path), limit=limit)
        data = parse_json(raw)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise StandardsDamagedError('%s cannot be read: %s' % (label, exc)) from exc
    if not isinstance(data, dict):
        raise StandardsDamagedError('%s is not a JSON object' % label)
    return data


def _signoff_filename(
    contribution_id=None,
    operation_id=None,
    *,
    stage='committed',
    parse_name=None,
    staged_payload=None,
):
    """Produce or parse a sign-off filename for ``(contribution_id, operation_id)``.

    Inline names (``<= 200`` chars) use ``<cid>.<stage>.<op_id>.json``. Longer names use
    ``<cid[:64]>.<stage>._sha256-<24hex>.json``; the leading ``_`` on ``_sha256-`` is
    disjoint from every valid ``identity()`` (which starts with ``[A-Za-z0-9]``), and one
    function both emits and parses names so ``reconcile_or_clean_staged`` cannot drift.
    """
    if stage not in ('staged', 'committed'):
        raise ValueError('Invalid sign-off filename stage: %r' % (stage,))
    marker = '.%s.' % stage
    if parse_name is not None:
        if (
            not isinstance(parse_name, str)
            or not parse_name.endswith('.json')
            or marker not in parse_name
        ):
            return None
        stem = parse_name[:-5]
        cid_part, _, op_part = stem.partition(marker)
        if not cid_part or not op_part:
            return None
        if op_part.startswith('_sha256-'):
            if not isinstance(staged_payload, dict):
                return ('hashed_unverified', None, None)
            cid = str(staged_payload.get('contribution_id') or '')
            op_id = str(staged_payload.get('operation_id') or '')
            try:
                expected = _signoff_filename(cid, op_id, stage=stage)
            except ValueError:
                return ('hashed_invalid', None, None)
            if expected != parse_name:
                return ('hashed_invalid', None, None)
            return ('ok', cid, op_id)
        try:
            identity(cid_part)
            identity(op_part)
        except ValueError:
            return None
        if _signoff_filename(cid_part, op_part, stage=stage) != parse_name:
            return None
        return ('ok', cid_part, op_part)

    cid = str(contribution_id)
    op_id = str(operation_id)
    identity(cid)
    identity(op_id)
    name = '%s%s%s.json' % (cid, marker, op_id)
    if len(name) <= 200:
        return name
    digest = hashlib.sha256(('%s\0%s' % (cid, op_id)).encode('utf-8')).hexdigest()[:24]
    return '%s%s_sha256-%s.json' % (cid[:64], marker, digest)


def _signoff_staged_filename(contribution_id, operation_id):
    return _signoff_filename(contribution_id, operation_id, stage='staged')


def _signoff_committed_filename(contribution_id, operation_id):
    return _signoff_filename(contribution_id, operation_id, stage='committed')


def _atomic_write_json(dir_path, filename, payload):
    if '/' in filename or '\\' in filename or filename in ('', '.', '..'):
        raise ValueError('Invalid standards filename')
    target = Path(dir_path) / filename
    st = _lstat_or_none(target)
    if st is not None and (stat.S_ISLNK(st.st_mode) or _is_reparse_point(st) or not stat.S_ISREG(st.st_mode)):
        raise ValueError('Refusing to overwrite non-regular file %s' % filename)
    encoded = canonical_bytes(payload) + b'\n'
    fd, tmp_path = tempfile.mkstemp(prefix='.tmp-', dir=str(dir_path))
    try:
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, str(target))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def _safe_unlink(dir_path, filename):
    target = Path(dir_path) / filename
    st = _lstat_or_none(target)
    if st is None:
        return
    if stat.S_ISLNK(st.st_mode) or _is_reparse_point(st) or not stat.S_ISREG(st.st_mode):
        return
    try:
        os.unlink(str(target))
    except FileNotFoundError:
        pass


@contextmanager
def held_bundle_fetch_lock(lock_dir, wait_seconds=BUNDLE_LOCK_WAIT_SECONDS):
    """Acquire both the in-process semaphore and the host O_NOFOLLOW flock with bounded wait."""
    deadline = time.monotonic() + max(0.05, float(wait_seconds))
    remaining = max(0.05, deadline - time.monotonic())
    if not BUNDLE_INSPECT_SEMAPHORE.acquire(timeout=remaining):
        raise BundleLockTimeoutError('Timed out waiting for bundle inspection semaphore')
    fd = None
    try:
        if lock_dir is not None and fcntl is not None:
            base = Path(lock_dir)
            base_st = _lstat_or_none(base)
            if base_st is None or stat.S_ISLNK(base_st.st_mode) or _is_reparse_point(base_st) or not stat.S_ISDIR(base_st.st_mode):
                raise ValueError('Bundle lock root is not a regular directory')
            lock_path = base / BUNDLE_FETCH_LOCK_NAME
            existing = _lstat_or_none(lock_path)
            if existing is not None and (
                stat.S_ISLNK(existing.st_mode)
                or _is_reparse_point(existing)
                or not stat.S_ISREG(existing.st_mode)
            ):
                raise ValueError('Refusing symlink or non-regular bundle fetch lock file')
            flags = (
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, 'O_NOFOLLOW', 0)
                | getattr(os, 'O_CLOEXEC', 0)
            )
            try:
                fd = os.open(str(lock_path), flags, 0o600)
            except OSError as exc:
                raise ValueError('Cannot open bundle fetch lock file: %s' % exc) from exc
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or _is_reparse_point(opened):
                os.close(fd)
                fd = None
                raise ValueError('Refusing non-regular bundle fetch lock descriptor')
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BundleLockTimeoutError('Timed out waiting for bundle fetch lock')
                    time.sleep(min(0.05, max(0.005, deadline - time.monotonic())))
        yield
    finally:
        if fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        BUNDLE_INSPECT_SEMAPHORE.release()


# -- Storage Adapters (Host .standards/ and InProcessBackend dict) ------------

class HostStandardsStore:
    """Symlink-refusing adapter over ``<project>/.standards/``."""

    def __init__(self, project_path, project_id, shared_loader=None):
        self.project_path = Path(project_path)
        self.project_id = str(project_id)
        self.root = self.project_path / STANDARDS_DIRNAME
        self.shared_loader = shared_loader

    def _ensure_subdir(self, *parts):
        return _ensure_real_dir(self.project_path, STANDARDS_DIRNAME, *parts)

    def inspect_register_raw(self):
        """Return ``('none', None, None)`` | ``('damaged', None, detail)`` | ``('active', head, versions_map)``.

        Binding guarantee (kittrial-5bb.570 VERIFIED): "no register" means NO ``head.json``
        AND NO ``versions/`` entries; ``drafts/``, ``gate.json``, ``commits/``, ``waivers/``,
        ``departed_owners.json``, and ``notifications.jsonl`` are ignored for this check.
        """
        root_st = _lstat_or_none(self.root)
        if root_st is None:
            return ('none', None, None)
        if stat.S_ISLNK(root_st.st_mode) or _is_reparse_point(root_st) or not stat.S_ISDIR(root_st.st_mode):
            return ('damaged', None, '.standards is not a regular directory')

        head_path = self.root / 'head.json'
        head_st = _lstat_or_none(head_path)
        versions_dir = self.root / 'versions'
        versions_st = _lstat_or_none(versions_dir)

        if versions_st is not None and (
            stat.S_ISLNK(versions_st.st_mode)
            or _is_reparse_point(versions_st)
            or not stat.S_ISDIR(versions_st.st_mode)
        ):
            return ('damaged', None, '.standards/versions is not a regular directory')

        version_entries = []
        if versions_st is not None:
            try:
                with os.scandir(str(versions_dir)) as it:
                    for entry in it:
                        if entry.name.startswith('.tmp-'):
                            continue
                        version_entries.append(entry)
            except OSError as exc:
                return ('damaged', None, 'cannot scan .standards/versions: %s' % exc)

        if head_st is None and not version_entries:
            return ('none', None, None)

        if head_st is None:
            return ('damaged', None, 'head.json is missing while versions/ contains entries')
        try:
            head = _read_json_file(head_path, 'head.json')
            _validate_head_object(head)
            current = head['current_version']
            if not version_entries:
                raise StandardsDamagedError('versions/ is empty while head.json names v%d' % current)
            versions_map = {}
            for entry in sorted(version_entries, key=lambda e: e.name):
                st = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode) or _is_reparse_point(st) or not stat.S_ISREG(st.st_mode):
                    raise StandardsDamagedError('versions/%s is not a regular file' % entry.name)
                m = re.fullmatch(r'v(\d{6})\.json', entry.name)
                if not m:
                    raise StandardsDamagedError('unexpected file versions/%s' % entry.name)
                ver_num = int(m.group(1))
                vdata = _read_json_file(versions_dir / entry.name, 'versions/%s' % entry.name)
                _validate_stored_version_object(vdata, self.project_id, ver_num)
                versions_map[ver_num] = vdata
            for expected_v in range(1, current + 1):
                if expected_v not in versions_map:
                    raise StandardsDamagedError('missing version file v%06d.json' % expected_v)
            if max(versions_map) != current:
                raise StandardsDamagedError('versions/ contains version beyond head current_version')
            head_ver = versions_map[current]
            if head_ver['sha256'] != head['current_sha256']:
                raise StandardsDamagedError('head.json sha256 does not match v%06d.json' % current)
        except StandardsDamagedError as exc:
            return ('damaged', None, str(exc))
        return ('active', head, versions_map)

    def write_version_and_head(self, version_obj, head_obj):
        try:
            versions_dir = self._ensure_subdir('versions')
            base_dir = self._ensure_subdir()
            ver_name = 'v%06d.json' % version_obj['version']
            _atomic_write_json(versions_dir, ver_name, version_obj)
            _atomic_write_json(base_dir, 'head.json', head_obj)
        except (StandardsDamagedError, ValueError, OSError) as exc:
            self._raise_damaged_subdir(exc)

    def read_gate_raw(self):
        root_st = _lstat_or_none(self.root)
        if root_st is None:
            return ('default', None, None)
        if stat.S_ISLNK(root_st.st_mode) or _is_reparse_point(root_st) or not stat.S_ISDIR(root_st.st_mode):
            return ('damaged', None, '.standards is not a regular directory')
        gate_path = self.root / 'gate.json'
        if _lstat_or_none(gate_path) is None:
            return ('default', None, None)
        try:
            data = _read_json_file(gate_path, 'gate.json')
            if data.get('mode') not in GATE_MODES:
                raise StandardsDamagedError('invalid gate mode')
            return ('stored', data, None)
        except StandardsDamagedError as exc:
            return ('damaged', None, str(exc))

    def write_gate(self, gate_obj):
        try:
            base_dir = self._ensure_subdir()
            _atomic_write_json(base_dir, 'gate.json', gate_obj)
        except (StandardsDamagedError, ValueError, OSError) as exc:
            self._raise_damaged_subdir(exc)

    def list_drafts(self):
        if not _require_real_dir(self.root, '.standards'):
            return []
        drafts_dir = self.root / 'drafts'
        items = []
        for name, _ in _list_regular_names(drafts_dir, 'drafts'):
            if not name.endswith('.json'):
                continue
            items.append(_read_json_file(drafts_dir / name, 'drafts/%s' % name))
        items.sort(key=lambda d: (str(d.get('created_at') or ''), str(d.get('id') or '')))
        return items

    def read_draft(self, draft_id):
        if not isinstance(draft_id, str) or not DRAFT_ID_RE.fullmatch(draft_id):
            return None
        if not _require_real_dir(self.root, '.standards'):
            return None
        drafts_dir = self.root / 'drafts'
        if not _require_real_dir(drafts_dir, 'drafts'):
            return None
        path = drafts_dir / ('%s.json' % draft_id)
        if _lstat_or_none(path) is None:
            return None
        return _read_json_file(path, 'drafts/%s.json' % draft_id)

    def write_draft(self, draft_obj):
        try:
            drafts_dir = self._ensure_subdir('drafts')
            _atomic_write_json(drafts_dir, '%s.json' % draft_obj['id'], draft_obj)
        except (StandardsDamagedError, ValueError, OSError) as exc:
            self._raise_damaged_subdir(exc)

    def list_waivers(self, task_id=None):
        if not _require_real_dir(self.root, '.standards'):
            return []
        waivers_dir = self.root / 'waivers'
        items = []
        for name, _ in _list_regular_names(waivers_dir, 'waivers'):
            if not name.endswith('.json'):
                continue
            item = _read_json_file(waivers_dir / name, 'waivers/%s' % name)
            if task_id is None or item.get('task_id') == task_id:
                items.append(item)
        items.sort(key=lambda w: (str(w.get('granted_at') or ''), str(w.get('id') or '')))
        return items

    def write_waiver(self, waiver_obj):
        try:
            waivers_dir = self._ensure_subdir('waivers')
            _atomic_write_json(waivers_dir, '%s.json' % waiver_obj['id'], waiver_obj)
        except (StandardsDamagedError, ValueError, OSError) as exc:
            self._raise_damaged_subdir(exc)

    def read_commit_observation(self, commit):
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
            return None
        if not _require_real_dir(self.root, '.standards'):
            return None
        commits_dir = self.root / 'commits'
        if not _require_real_dir(commits_dir, 'commits'):
            return None
        path = commits_dir / ('%s.json' % commit.lower())
        if _lstat_or_none(path) is None:
            return None
        return _read_json_file(
            path, 'commits/%s.json' % commit.lower(), limit=MAX_COMMIT_OBSERVATION_FILE_BYTES
        )

    def write_commit_observation(self, commit, obs_obj):
        try:
            commits_dir = self._ensure_subdir('commits')
            _atomic_write_json(commits_dir, '%s.json' % commit.lower(), obs_obj)
        except (StandardsDamagedError, ValueError, OSError) as exc:
            self._raise_damaged_subdir(exc)

    def read_pin(self, contribution_id):
        identity(str(contribution_id))
        if not _require_real_dir(self.root, '.standards'):
            return None
        pins_dir = self.root / 'pins'
        if not _require_real_dir(pins_dir, 'pins'):
            return None
        path = pins_dir / ('%s.json' % contribution_id)
        if _lstat_or_none(path) is None:
            return None
        return _read_json_file(path, 'pins/%s.json' % contribution_id)

    def _raise_damaged_subdir(self, exc):
        raise StandardsRefusal(
            'Standards directory in project %s is damaged (%s); repair or restore .standards/ before reviewing.'
            % (self.project_id, exc),
            status=409,
        ) from None

    def write_pin(self, contribution_id, pin_obj):
        identity(str(contribution_id))
        try:
            pins_dir = self._ensure_subdir('pins')
            _atomic_write_json(pins_dir, '%s.json' % contribution_id, pin_obj)
        except (StandardsDamagedError, ValueError, OSError) as exc:
            self._raise_damaged_subdir(exc)

    def stage_recommendation(self, operation_id, staged_obj):
        identity(str(operation_id))
        try:
            rec_dir = self._ensure_subdir('recommendations')
            _atomic_write_json(rec_dir, 'op-%s.staged.json' % operation_id, staged_obj)
        except (StandardsDamagedError, ValueError, OSError) as exc:
            self._raise_damaged_subdir(exc)

    def commit_recommendation(self, operation_id, comment_id, final_obj):
        identity(str(operation_id))
        identity(str(comment_id))
        try:
            rec_dir = self._ensure_subdir('recommendations')
            _atomic_write_json(rec_dir, '%s.json' % comment_id, final_obj)
            _safe_unlink(rec_dir, 'op-%s.staged.json' % operation_id)
        except (StandardsDamagedError, ValueError, OSError) as exc:
            self._raise_damaged_subdir(exc)

    def read_recommendation_checks(self, comment_id, operation_id=None):
        """Return ``('ok', dict)`` | ``('missing', None)`` | ``('corrupt', detail)``."""
        try:
            if not _require_real_dir(self.root, '.standards'):
                return ('missing', None)
            rec_dir = self.root / 'recommendations'
            if not _require_real_dir(rec_dir, 'recommendations'):
                return ('missing', None)
        except StandardsDamagedError as exc:
            return ('corrupt', str(exc))
        try:
            identity(str(comment_id))
        except ValueError:
            return ('missing', None)
        committed_path = rec_dir / ('%s.json' % comment_id)
        if _lstat_or_none(committed_path) is not None:
            try:
                return ('ok', _read_json_file(committed_path, 'recommendations/%s.json' % comment_id))
            except StandardsDamagedError as exc:
                return ('corrupt', str(exc))
        if operation_id is not None:
            try:
                identity(str(operation_id))
            except ValueError:
                return ('missing', None)
            staged_path = rec_dir / ('op-%s.staged.json' % operation_id)
            if _lstat_or_none(staged_path) is not None:
                try:
                    return ('ok', _read_json_file(staged_path, 'recommendations/op-%s.staged.json' % operation_id))
                except StandardsDamagedError as exc:
                    return ('corrupt', str(exc))
        return ('missing', None)

    def stage_signoff(self, contribution_id, operation_id, staged_obj):
        staged_name = _signoff_staged_filename(contribution_id, operation_id)
        try:
            so_dir = self._ensure_subdir('signoffs')
            _atomic_write_json(so_dir, staged_name, staged_obj)
        except (StandardsDamagedError, ValueError, OSError) as exc:
            self._raise_damaged_subdir(exc)

    def commit_signoff(self, contribution_id, operation_id, final_obj):
        cid = str(contribution_id)
        op_id = str(operation_id)
        committed_name = _signoff_committed_filename(cid, op_id)
        staged_name = _signoff_staged_filename(cid, op_id)
        try:
            so_dir = self._ensure_subdir('signoffs')
            committed_path = so_dir / committed_name
            if _lstat_or_none(committed_path) is not None:
                existing = _read_json_file(committed_path, 'signoffs/%s' % committed_name)
                if (
                    not isinstance(existing, dict)
                    or str(existing.get('contribution_id') or '') != cid
                    or str(existing.get('operation_id') or '') != op_id
                ):
                    raise StandardsDamagedError(
                        'signoffs/%s does not match (%s, %s)' % (committed_name, cid, op_id)
                    )
            else:
                _atomic_write_json(so_dir, committed_name, final_obj)
            _safe_unlink(so_dir, staged_name)
        except (StandardsDamagedError, ValueError, OSError) as exc:
            raise SignoffCommitError(
                'Cannot finalize committed sign-off in project %s (%s)' % (self.project_id, exc)
            ) from None

    def read_signoff(self, contribution_id, approve_operation_id=None):
        try:
            if not _require_real_dir(self.root, '.standards'):
                return None
            so_dir = self.root / 'signoffs'
            if not _require_real_dir(so_dir, 'signoffs'):
                return None
        except StandardsDamagedError:
            return None
        cid = str(contribution_id)
        try:
            identity(cid)
        except ValueError:
            return None
        if approve_operation_id is not None:
            op_id = str(approve_operation_id)
            try:
                committed_name = _signoff_committed_filename(cid, op_id)
                staged_name = _signoff_staged_filename(cid, op_id)
            except ValueError:
                return None
            committed_path = so_dir / committed_name
            if _lstat_or_none(committed_path) is not None:
                try:
                    so = _read_json_file(committed_path, 'signoffs/%s' % committed_name)
                    if (
                        isinstance(so, dict)
                        and str(so.get('contribution_id') or '') == cid
                        and str(so.get('operation_id') or '') == op_id
                    ):
                        return so
                except StandardsDamagedError:
                    pass
            staged_path = so_dir / staged_name
            if _lstat_or_none(staged_path) is not None:
                try:
                    so = _read_json_file(staged_path, 'signoffs/%s' % staged_name)
                    if (
                        isinstance(so, dict)
                        and str(so.get('contribution_id') or '') == cid
                        and str(so.get('operation_id') or '') == op_id
                    ):
                        return so
                except StandardsDamagedError:
                    pass
            legacy_path = so_dir / ('%s.json' % cid)
            if _lstat_or_none(legacy_path) is not None:
                try:
                    legacy = _read_json_file(legacy_path, 'signoffs/%s.json' % cid)
                    if (
                        isinstance(legacy, dict)
                        and str(legacy.get('contribution_id') or '') == cid
                        and str(legacy.get('operation_id') or '') == op_id
                    ):
                        return legacy
                except StandardsDamagedError:
                    return None
            return None
        for_cid = [
            so for so in self.list_committed_signoffs()
            if str(so.get('contribution_id') or '') == cid
        ]
        if not for_cid:
            return None
        for_cid.sort(
            key=lambda s: (
                str(s.get('created_at') or ''),
                str(s.get('approval_comment_id') or ''),
                str(s.get('operation_id') or ''),
            )
        )
        return for_cid[-1]

    def list_committed_signoffs(self):
        try:
            if not _require_real_dir(self.root, '.standards'):
                return []
            so_dir = self.root / 'signoffs'
            entries = _list_regular_names(so_dir, 'signoffs')
        except (StandardsDamagedError, OSError):
            return []
        items = []
        for name, _ in entries:
            if not name.endswith('.json') or '.staged.' in name:
                continue
            try:
                items.append(_read_json_file(so_dir / name, 'signoffs/%s' % name))
            except StandardsDamagedError:
                continue
        return items

    def reconcile_or_clean_staged(self, rows, now_epoch=None):
        """Promote staged sign-offs/recommendations whose bd comment landed; delete >24h orphans."""
        try:
            if not _require_real_dir(self.root, '.standards'):
                return
        except StandardsDamagedError:
            return
        now_val = time.time() if now_epoch is None else float(now_epoch)
        approve_ops, rec_ops = _extract_tracker_operation_maps(rows)

        so_dir = self.root / 'signoffs'
        try:
            so_entries = _list_regular_names(so_dir, 'signoffs')
        except (StandardsDamagedError, OSError):
            so_entries = ()
        for name, st in so_entries:
            if '.staged.' not in name or not name.endswith('.json'):
                continue
            try:
                staged = _read_json_file(so_dir / name, 'signoffs/%s' % name)
            except StandardsDamagedError:
                staged = None
            parsed = _signoff_filename(stage='staged', parse_name=name, staged_payload=staged)
            if parsed is None:
                continue
            pstatus, cid, op_id = parsed
            if pstatus != 'ok':
                if now_val - st.st_mtime >= STAGED_TTL_SECONDS:
                    _safe_unlink(so_dir, name)
                continue
            matched = approve_ops.get(op_id)
            if matched is not None and matched[0] == cid and isinstance(staged, dict):
                try:
                    promoted = dict(staged, status='committed', approval_comment_id=matched[1])
                    self.commit_signoff(cid, op_id, promoted)
                    if _lstat_or_none(so_dir / name) is not None:
                        _safe_unlink(so_dir, name)
                except (StandardsDamagedError, StandardsRefusal, SignoffCommitError, OSError):
                    pass
            elif now_val - st.st_mtime >= STAGED_TTL_SECONDS and op_id not in approve_ops:
                _safe_unlink(so_dir, name)

        rec_dir = self.root / 'recommendations'
        try:
            rec_entries = _list_regular_names(rec_dir, 'recommendations')
        except (StandardsDamagedError, OSError):
            rec_entries = ()
        for name, st in rec_entries:
            if not (name.startswith('op-') and name.endswith('.staged.json')):
                continue
            op_id = name[3:-12]
            try:
                identity(op_id)
            except ValueError:
                continue
            comment_id = rec_ops.get(op_id)
            if comment_id is not None:
                try:
                    staged = _read_json_file(rec_dir / name, 'recommendations/%s' % name)
                    promoted = dict(staged, status='committed', recommendation_id=comment_id)
                    self.commit_recommendation(op_id, comment_id, promoted)
                except (StandardsDamagedError, StandardsRefusal, OSError):
                    pass
            elif now_val - st.st_mtime >= STAGED_TTL_SECONDS and op_id not in rec_ops:
                _safe_unlink(rec_dir, name)

    def read_departed_owners(self):
        if not _require_real_dir(self.root, '.standards'):
            return set()
        path = self.root / 'departed_owners.json'
        if _lstat_or_none(path) is None:
            return set()
        data = _read_json_file(path, 'departed_owners.json')
        owners = data.get('departed_owners') or []
        return {str(x) for x in owners if isinstance(x, str)}

    def mark_owner_departed(self, owner_id, actor, reason, now=None):
        try:
            base_dir = self._ensure_subdir()
            existing = sorted(self.read_departed_owners() | {owner_id})
            payload = {
                'schema_version': SCHEMA_VERSION,
                'departed_owners': existing,
                'updated_by': actor,
                'updated_at': utc_now_iso(now),
                'reason': reason,
            }
            _atomic_write_json(base_dir, 'departed_owners.json', payload)
            return payload
        except (StandardsDamagedError, ValueError, OSError) as exc:
            self._raise_damaged_subdir(exc)

    def append_notification(self, entry):
        try:
            base_dir = self._ensure_subdir()
            items = self.read_notifications()
            items.append(entry)
            if len(items) > NOTIFICATIONS_MAX:
                items = items[-NOTIFICATIONS_MAX:]
            lines = b''.join(canonical_bytes(item) + b'\n' for item in items)
            target = base_dir / 'notifications.jsonl'
            st = _lstat_or_none(target)
            if st is not None and (stat.S_ISLNK(st.st_mode) or _is_reparse_point(st) or not stat.S_ISREG(st.st_mode)):
                raise ValueError('Refusing non-regular notifications.jsonl')
            fd, tmp_path = tempfile.mkstemp(prefix='.tmp-', dir=str(base_dir))
            try:
                try:
                    os.write(fd, lines)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp_path, str(target))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (StandardsDamagedError, ValueError, OSError) as exc:
            self._raise_damaged_subdir(exc)

    def read_notifications(self):
        if not _require_real_dir(self.root, '.standards'):
            return []
        path = self.root / 'notifications.jsonl'
        if _lstat_or_none(path) is None:
            return []
        raw = read_regular_text(str(path), limit=MAX_FILE_BYTES * 2)
        items = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            parsed = parse_json(line)
            if isinstance(parsed, dict):
                items.append(parsed)
        return items


class _DamagedStandardsStore:
    """Sentinel store returned when a sibling project path is a symlink or non-directory."""

    def __init__(self, project_id, detail):
        self.project_id = str(project_id)
        self.detail = str(detail)
        self.shared_loader = None

    def inspect_register_raw(self):
        return ('damaged', None, self.detail)


def host_shared_loader_for(project_path):
    """Build a ``shared_loader(shared_pid)`` resolving sibling host projects under ``<root>/projects``."""
    base = Path(project_path)
    projects_dir = base.parent

    def _load(shared_pid):
        if not isinstance(shared_pid, str) or not PROJECT_ID_RE.fullmatch(shared_pid):
            return None
        target = projects_dir / shared_pid
        st = _lstat_or_none(target)
        if st is None:
            return None
        if stat.S_ISLNK(st.st_mode) or _is_reparse_point(st) or not stat.S_ISDIR(st.st_mode):
            return _DamagedStandardsStore(
                shared_pid, 'shared project path %s is a symlink or non-directory' % shared_pid
            )
        return HostStandardsStore(target, shared_pid)

    return _load


def dict_shared_loader_for(standards_root, project_exists=None):
    """Build a ``shared_loader(shared_pid)`` resolving sibling projects in ``standards_root``."""

    def _load(shared_pid):
        if not isinstance(shared_pid, str) or not PROJECT_ID_RE.fullmatch(shared_pid):
            return None
        if project_exists is not None:
            if not project_exists(shared_pid):
                return None
        elif not isinstance(standards_root, dict) or shared_pid not in standards_root:
            return None
        return DictStandardsStore(standards_root, shared_pid)

    return _load



class DictStandardsStore:
    """In-memory / JSON-dict adapter for ``InProcessBackend`` sharing 100% of logic."""

    def __init__(self, standards_root, project_id, shared_loader=None):
        self.standards_root = standards_root
        self.project_id = str(project_id)
        self.shared_loader = shared_loader

    def _bucket(self, write=False):
        if write:
            bucket = self.standards_root.setdefault(self.project_id, {})
            if not isinstance(bucket, dict):
                raise StandardsDamagedError('standards state is not a mapping')
            return bucket
        return self.standards_root.get(self.project_id)

    def inspect_register_raw(self):
        bucket = self._bucket(write=False)
        if bucket is None:
            return ('none', None, None)
        if not isinstance(bucket, dict):
            return ('damaged', None, 'standards state is not a JSON object')
        head = bucket.get('head')
        versions = bucket.get('versions')
        if versions is not None and not isinstance(versions, dict):
            return ('damaged', None, 'versions state is not a JSON object')
        head_present = head is not None
        versions_present = bool(versions)
        if not head_present and not versions_present:
            return ('none', None, None)
        if not head_present:
            return ('damaged', None, 'head.json is missing while versions/ contains entries')
        try:
            if not isinstance(head, dict):
                raise StandardsDamagedError('head.json is not a JSON object')
            _validate_head_object(head)
            current = head['current_version']
            if not versions_present:
                raise StandardsDamagedError('versions/ is empty while head.json names v%d' % current)
            versions_map = {}
            for k, vdata in versions.items():
                try:
                    ver_num = int(k)
                except (TypeError, ValueError) as exc:
                    raise StandardsDamagedError('invalid version key %r' % (k,)) from exc
                if not isinstance(vdata, dict):
                    raise StandardsDamagedError('version v%06d is not a JSON object' % ver_num)
                _validate_stored_version_object(vdata, self.project_id, ver_num)
                versions_map[ver_num] = parse_json(canonical_bytes(vdata).decode('utf-8'))
            for expected_v in range(1, current + 1):
                if expected_v not in versions_map:
                    raise StandardsDamagedError('missing version file v%06d.json' % expected_v)
            if max(versions_map) != current:
                raise StandardsDamagedError('versions/ contains version beyond head current_version')
            if versions_map[current]['sha256'] != head['current_sha256']:
                raise StandardsDamagedError('head.json sha256 does not match v%06d.json' % current)
        except StandardsDamagedError as exc:
            return ('damaged', None, str(exc))
        return ('active', dict(head), versions_map)

    def write_version_and_head(self, version_obj, head_obj):
        bucket = self._bucket(write=True)
        versions = bucket.setdefault('versions', {})
        versions[str(version_obj['version'])] = parse_json(canonical_bytes(version_obj).decode('utf-8'))
        bucket['head'] = parse_json(canonical_bytes(head_obj).decode('utf-8'))

    def read_gate_raw(self):
        bucket = self._bucket(write=False)
        if bucket is None:
            return ('default', None, None)
        if not isinstance(bucket, dict):
            return ('damaged', None, 'standards state is not a JSON object')
        if 'gate' not in bucket or bucket['gate'] is None:
            return ('default', None, None)
        gate = bucket['gate']
        if not isinstance(gate, dict) or gate.get('mode') not in GATE_MODES:
            return ('damaged', None, 'invalid gate mode')
        return ('stored', dict(gate), None)

    def write_gate(self, gate_obj):
        bucket = self._bucket(write=True)
        bucket['gate'] = parse_json(canonical_bytes(gate_obj).decode('utf-8'))

    def list_drafts(self):
        bucket = self._bucket(write=False)
        if not isinstance(bucket, dict):
            return []
        drafts = bucket.get('drafts') or {}
        items = [dict(v) for v in drafts.values() if isinstance(v, dict)]
        items.sort(key=lambda d: (str(d.get('created_at') or ''), str(d.get('id') or '')))
        return items

    def read_draft(self, draft_id):
        if not isinstance(draft_id, str) or not DRAFT_ID_RE.fullmatch(draft_id):
            return None
        bucket = self._bucket(write=False)
        if not isinstance(bucket, dict):
            return None
        item = (bucket.get('drafts') or {}).get(draft_id)
        return dict(item) if isinstance(item, dict) else None

    def write_draft(self, draft_obj):
        bucket = self._bucket(write=True)
        bucket.setdefault('drafts', {})[draft_obj['id']] = parse_json(
            canonical_bytes(draft_obj).decode('utf-8')
        )

    def list_waivers(self, task_id=None):
        bucket = self._bucket(write=False)
        if not isinstance(bucket, dict):
            return []
        waivers = bucket.get('waivers') or {}
        items = [
            dict(v)
            for v in waivers.values()
            if isinstance(v, dict) and (task_id is None or v.get('task_id') == task_id)
        ]
        items.sort(key=lambda w: (str(w.get('granted_at') or ''), str(w.get('id') or '')))
        return items

    def write_waiver(self, waiver_obj):
        bucket = self._bucket(write=True)
        bucket.setdefault('waivers', {})[waiver_obj['id']] = parse_json(
            canonical_bytes(waiver_obj).decode('utf-8')
        )

    def read_commit_observation(self, commit):
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
            return None
        bucket = self._bucket(write=False)
        if not isinstance(bucket, dict):
            return None
        item = (bucket.get('commits') or {}).get(commit.lower())
        return dict(item) if isinstance(item, dict) else None

    def write_commit_observation(self, commit, obs_obj):
        bucket = self._bucket(write=True)
        bucket.setdefault('commits', {})[commit.lower()] = parse_json(
            canonical_bytes(obs_obj).decode('utf-8')
        )

    def read_pin(self, contribution_id):
        identity(str(contribution_id))
        bucket = self._bucket(write=False)
        if not isinstance(bucket, dict):
            return None
        item = (bucket.get('pins') or {}).get(str(contribution_id))
        return dict(item) if isinstance(item, dict) else None

    def write_pin(self, contribution_id, pin_obj):
        identity(str(contribution_id))
        bucket = self._bucket(write=True)
        bucket.setdefault('pins', {})[str(contribution_id)] = parse_json(
            canonical_bytes(pin_obj).decode('utf-8')
        )

    def stage_recommendation(self, operation_id, staged_obj):
        identity(str(operation_id))
        bucket = self._bucket(write=True)
        bucket.setdefault('recommendations_staged', {})[str(operation_id)] = parse_json(
            canonical_bytes(staged_obj).decode('utf-8')
        )

    def commit_recommendation(self, operation_id, comment_id, final_obj):
        identity(str(operation_id))
        identity(str(comment_id))
        bucket = self._bucket(write=True)
        bucket.setdefault('recommendations', {})[str(comment_id)] = parse_json(
            canonical_bytes(final_obj).decode('utf-8')
        )
        (bucket.get('recommendations_staged') or {}).pop(str(operation_id), None)

    def read_recommendation_checks(self, comment_id, operation_id=None):
        bucket = self._bucket(write=False)
        if not isinstance(bucket, dict):
            return ('missing', None)
        recs = bucket.get('recommendations')
        if isinstance(recs, dict) and str(comment_id) in recs:
            val = recs[str(comment_id)]
            if not isinstance(val, dict):
                return ('corrupt', 'recommendation check record is corrupt')
            return ('ok', dict(val))
        staged = bucket.get('recommendations_staged')
        if operation_id is not None and isinstance(staged, dict) and str(operation_id) in staged:
            val = staged[str(operation_id)]
            if not isinstance(val, dict):
                return ('corrupt', 'staged recommendation check record is corrupt')
            return ('ok', dict(val))
        return ('missing', None)

    def stage_signoff(self, contribution_id, operation_id, staged_obj):
        identity(str(contribution_id))
        identity(str(operation_id))
        bucket = self._bucket(write=True)
        bucket.setdefault('signoffs_staged', {})['%s:%s' % (contribution_id, operation_id)] = parse_json(
            canonical_bytes(staged_obj).decode('utf-8')
        )

    def commit_signoff(self, contribution_id, operation_id, final_obj):
        cid = str(contribution_id)
        op_id = str(operation_id)
        identity(cid)
        identity(op_id)
        bucket = self._bucket(write=True)
        key = '%s:%s' % (cid, op_id)
        so_map = bucket.setdefault('signoffs', {})
        if not isinstance(so_map, dict):
            raise SignoffCommitError('signoffs store in project %s is not a mapping' % self.project_id)
        if key in so_map:
            existing = so_map[key]
            if (
                not isinstance(existing, dict)
                or str(existing.get('contribution_id') or '') != cid
                or str(existing.get('operation_id') or '') != op_id
            ):
                raise SignoffCommitError(
                    'existing sign-off entry %s in project %s is invalid' % (key, self.project_id)
                )
        else:
            so_map[key] = parse_json(canonical_bytes(final_obj).decode('utf-8'))
        staged_map = bucket.get('signoffs_staged')
        if isinstance(staged_map, dict):
            staged_map.pop(key, None)

    def read_signoff(self, contribution_id, approve_operation_id=None):
        bucket = self._bucket(write=False)
        if not isinstance(bucket, dict):
            return None
        cid = str(contribution_id)
        try:
            identity(cid)
        except ValueError:
            return None
        so_map = bucket.get('signoffs') or {}
        if approve_operation_id is not None:
            op_id = str(approve_operation_id)
            try:
                identity(op_id)
            except ValueError:
                return None
            key = '%s:%s' % (cid, op_id)
            committed = so_map.get(key)
            if (
                isinstance(committed, dict)
                and str(committed.get('contribution_id') or '') == cid
                and str(committed.get('operation_id') or '') == op_id
            ):
                return dict(committed)
            staged = (bucket.get('signoffs_staged') or {}).get(key)
            if (
                isinstance(staged, dict)
                and str(staged.get('contribution_id') or '') == cid
                and str(staged.get('operation_id') or '') == op_id
            ):
                return dict(staged)
            legacy = so_map.get(cid)
            if (
                isinstance(legacy, dict)
                and str(legacy.get('contribution_id') or '') == cid
                and str(legacy.get('operation_id') or '') == op_id
            ):
                return dict(legacy)
            return None
        for_cid = [
            dict(v)
            for v in so_map.values()
            if isinstance(v, dict) and str(v.get('contribution_id') or '') == cid
        ]
        if not for_cid:
            return None
        for_cid.sort(
            key=lambda s: (
                str(s.get('created_at') or ''),
                str(s.get('approval_comment_id') or ''),
                str(s.get('operation_id') or ''),
            )
        )
        return for_cid[-1]

    def list_committed_signoffs(self):
        bucket = self._bucket(write=False)
        if not isinstance(bucket, dict):
            return []
        return [dict(v) for v in (bucket.get('signoffs') or {}).values() if isinstance(v, dict)]

    def read_departed_owners(self):
        bucket = self._bucket(write=False)
        if not isinstance(bucket, dict):
            return set()
        return {str(x) for x in (bucket.get('departed_owners') or []) if isinstance(x, str)}

    def mark_owner_departed(self, owner_id, actor, reason, now=None):
        bucket = self._bucket(write=True)
        existing = sorted(self.read_departed_owners() | {owner_id})
        bucket['departed_owners'] = existing
        return {
            'schema_version': SCHEMA_VERSION,
            'departed_owners': existing,
            'updated_by': actor,
            'updated_at': utc_now_iso(now),
            'reason': reason,
        }

    def append_notification(self, entry):
        bucket = self._bucket(write=True)
        items = list(bucket.get('notifications') or [])
        items.append(parse_json(canonical_bytes(entry).decode('utf-8')))
        if len(items) > NOTIFICATIONS_MAX:
            items = items[-NOTIFICATIONS_MAX:]
        bucket['notifications'] = items

    def read_notifications(self):
        bucket = self._bucket(write=False)
        if not isinstance(bucket, dict):
            return []
        return [dict(x) for x in (bucket.get('notifications') or []) if isinstance(x, dict)]


def _extract_tracker_operation_maps(rows):
    """Extract committed ``approve`` and ``recommend`` operation_ids from tracker rows."""
    import review_recommendations

    approve_ops = {}
    rec_ops = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        for comment in row.get('comments') or ():
            if not isinstance(comment, dict):
                continue
            raw = comment.get('text')
            cid = str(comment.get('id') or '')
            if not isinstance(raw, str) or not cid:
                continue
            if raw.startswith(review_workflow.PREFIX):
                try:
                    rec = parse_json(raw[len(review_workflow.PREFIX):])
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get('operation') == 'approve':
                    op_id = rec.get('operation_id')
                    contrib = rec.get('contribution')
                    if isinstance(op_id, str) and isinstance(contrib, str):
                        approve_ops[op_id] = (contrib, cid)
            elif raw.startswith(review_recommendations.PREFIX):
                try:
                    rec = parse_json(raw[len(review_recommendations.PREFIX):])
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get('operation') == 'recommend':
                    op_id = rec.get('operation_id')
                    if isinstance(op_id, str):
                        rec_ops[op_id] = cid
    return approve_ops, rec_ops


# -- Register Schema, Inheritance, Lint & Ownership (R1, R2, R3, R3a, R5) -----

def is_person_owner_id(value, via='any'):
    if not isinstance(value, str) or not value.strip():
        return False
    if AGENT_LIKE_RE.fullmatch(value):
        return False
    if via == 'web':
        return bool(HTTP_PERSON_RE.fullmatch(value))
    if via == 'host':
        return bool(HOST_PERSON_RE.fullmatch(value) or HTTP_PERSON_RE.fullmatch(value))
    return bool(HTTP_PERSON_RE.fullmatch(value) or HOST_PERSON_RE.fullmatch(value))


def _check_clean_text(value, label, limit, allow_empty=False):
    if not isinstance(value, str):
        raise StandardsRefusal('%s must be text (at most %d characters)' % (label, limit), status=422)
    if '\x00' in value:
        raise StandardsRefusal('%s must not contain NUL bytes' % label, status=422)
    if not allow_empty and not value.strip():
        raise StandardsRefusal('%s must not be empty' % label, status=422)
    if len(value) > limit:
        raise StandardsRefusal(
            '%s exceeds %d characters (%d)' % (label, limit, len(value)), status=422
        )
    try:
        check_text(value, label, limit)
        plain_text(value, label)
    except ValueError as exc:
        raise StandardsRefusal(str(exc), status=422) from exc
    return value.strip()


def _normalize_repo_rel_pattern(pattern, label='path pattern'):
    if not isinstance(pattern, str) or not pattern.strip() or '\x00' in pattern:
        raise StandardsRefusal('Invalid %s: expected non-empty relative path pattern' % label, status=422)
    raw = pattern.strip()
    if '\\' in raw or raw.startswith('/'):
        raise StandardsRefusal(
            'Invalid %s %r: use repo-relative POSIX paths without leading slash or backslashes'
            % (label, raw),
            status=422,
        )
    parts = [p for p in raw.split('/') if p not in ('', '.')]
    if not parts or any(p == '..' for p in parts):
        raise StandardsRefusal(
            'Invalid %s %r: parent traversal (..) and empty paths are not allowed' % (label, raw),
            status=422,
        )
    if len(raw) > 300:
        raise StandardsRefusal('Invalid %s: exceeds 300 characters' % label, status=422)
    return '/'.join(parts)


def validate_rule_input(rule_raw, *, known_person_checker=None):
    """Validate and normalize one rule object (R2, R3, R4, R5)."""
    if not isinstance(rule_raw, dict):
        raise StandardsRefusal('Each rule must be a JSON object', status=422)
    allowed_keys = {
        'id',
        'statement',
        'rationale',
        'owner',
        'severity',
        'applicability',
        'check',
        'references',
        'origin',
        'owner_status',
        'effective_owner',
    }
    extra = sorted(set(rule_raw) - allowed_keys)
    if extra:
        raise StandardsRefusal('Unknown rule field(s): %s' % ', '.join(extra), status=422)

    rule_id = rule_raw.get('id')
    if not isinstance(rule_id, str) or not RULE_ID_RE.fullmatch(rule_id):
        raise StandardsRefusal(
            'Invalid rule id %r: expected format like STD-12 (^[A-Z][A-Z0-9]{1,15}-[0-9]{1,6}$)'
            % (rule_id,),
            status=422,
        )

    statement = _check_clean_text(rule_raw.get('statement'), 'rule %s statement' % rule_id, STATEMENT_MAX)
    rationale = _check_clean_text(rule_raw.get('rationale'), 'rule %s rationale' % rule_id, RATIONALE_MAX)

    owner = rule_raw.get('owner')
    if not is_person_owner_id(owner):
        raise StandardsRefusal(refusal_agent_rule_owner(rule_id, owner), status=422)
    if known_person_checker is not None and not known_person_checker(owner):
        raise StandardsRefusal(refusal_agent_rule_owner(rule_id, owner), status=422)

    severity = rule_raw.get('severity')
    if severity is None:
        severity = 'should'  # R2: new rules default to should
    if severity not in SEVERITIES:
        raise StandardsRefusal(
            "Rule %s severity must be 'must' or 'should'" % rule_id, status=422
        )

    app_raw = rule_raw.get('applicability')
    if not isinstance(app_raw, dict):
        raise StandardsRefusal('Rule %s applicability must be an object' % rule_id, status=422)
    app_extra = sorted(
        set(app_raw) - {'all_changes', 'paths', 'task_labels', 'task_kinds', 'languages'}
    )
    if app_extra:
        raise StandardsRefusal(
            'Rule %s applicability has unknown field(s): %s' % (rule_id, ', '.join(app_extra)),
            status=422,
        )
    all_changes = app_raw.get('all_changes', False)
    if not isinstance(all_changes, bool):
        raise StandardsRefusal('Rule %s applicability.all_changes must be boolean' % rule_id, status=422)

    def _norm_str_list(key, transform):
        vals = app_raw.get(key)
        if vals is None:
            return []
        if not isinstance(vals, list) or len(vals) > SELECTORS_MAX:
            raise StandardsRefusal(
                'Rule %s applicability.%s must be a list of at most %d entries'
                % (rule_id, key, SELECTORS_MAX),
                status=422,
            )
        out = []
        for item in vals:
            norm = transform(item)
            if norm not in out:
                out.append(norm)
        return out

    paths = _norm_str_list('paths', lambda p: _normalize_repo_rel_pattern(p, 'applicability path'))
    task_labels = _norm_str_list(
        'task_labels',
        lambda s: _check_clean_text(s, 'task_label', 80).lower(),
    )
    task_kinds = _norm_str_list(
        'task_kinds',
        lambda s: _check_clean_text(s, 'task_kind', 64).lower(),
    )

    def _norm_lang(val):
        lang = _check_clean_text(val, 'language', 40).lower()
        if lang not in KNOWN_LANGUAGES:
            raise StandardsRefusal(
                'Rule %s names unknown language %r; supported languages: %s'
                % (rule_id, lang, ', '.join(sorted(KNOWN_LANGUAGES))),
                status=422,
            )
        return lang

    languages = _norm_str_list('languages', _norm_lang)
    if not (all_changes or paths or task_labels or task_kinds or languages):
        raise StandardsRefusal(
            'Rule %s applicability must specify all_changes: true or at least one paths, '
            'task_labels, task_kinds, or languages selector' % rule_id,
            status=422,
        )

    chk_raw = rule_raw.get('check')
    if not isinstance(chk_raw, dict) or chk_raw.get('kind') not in CHECK_KINDS:
        raise StandardsRefusal(
            "Rule %s check must be an object with kind 'mechanical' or 'judgement'" % rule_id,
            status=422,
        )
    chk_kind = chk_raw['kind']
    if chk_kind == 'mechanical':
        if set(chk_raw) - {'kind', 'tool', 'command'}:
            raise StandardsRefusal(
                'Rule %s mechanical check accepts only kind, tool, and command' % rule_id,
                status=422,
            )
        tool = _check_clean_text(chk_raw.get('tool'), 'rule %s check.tool' % rule_id, TOOL_NAME_MAX)
        command = _check_clean_text(
            chk_raw.get('command'), 'rule %s check.command' % rule_id, COMMAND_MAX
        )
        check_obj = {'kind': 'mechanical', 'tool': tool, 'command': command}
    else:
        if set(chk_raw) != {'kind'}:
            raise StandardsRefusal(
                'Rule %s judgement check accepts only {"kind": "judgement"}' % rule_id,
                status=422,
            )
        check_obj = {'kind': 'judgement'}

    refs_raw = rule_raw.get('references')
    references = []
    if refs_raw is not None:
        if not isinstance(refs_raw, list) or len(refs_raw) > REFS_MAX:
            raise StandardsRefusal(
                'Rule %s references must be a list of at most %d reference keys'
                % (rule_id, REFS_MAX),
                status=422,
            )
        for ref in refs_raw:
            if not isinstance(ref, str) or not REF_KEY_RE.fullmatch(ref):
                raise StandardsRefusal(
                    'Rule %s reference %r is not a valid reference key' % (rule_id, ref),
                    status=422,
                )
            if ref not in references:
                references.append(ref)

    return {
        'id': rule_id,
        'statement': statement,
        'rationale': rationale,
        'owner': owner,
        'severity': severity,
        'applicability': {
            'all_changes': all_changes,
            'paths': paths,
            'task_labels': task_labels,
            'task_kinds': task_kinds,
            'languages': languages,
        },
        'check': check_obj,
        'references': references,
    }


def _validate_head_object(head):
    if not isinstance(head, dict):
        raise StandardsDamagedError('head.json is not a JSON object')
    if head.get('schema_version') != SCHEMA_VERSION:
        raise StandardsDamagedError('unsupported head.json schema_version')
    ver = head.get('current_version')
    if type(ver) is not int or ver < 1:
        raise StandardsDamagedError('invalid head.json current_version')
    sha = head.get('current_sha256')
    if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
        raise StandardsDamagedError('invalid head.json current_sha256')


def _validate_stored_version_object(vdata, expected_pid, expected_ver):
    if not isinstance(vdata, dict) or vdata.get('schema_version') != SCHEMA_VERSION:
        raise StandardsDamagedError('v%06d has invalid schema_version' % expected_ver)
    if vdata.get('project') != expected_pid or vdata.get('version') != expected_ver:
        raise StandardsDamagedError('v%06d project/version mismatch' % expected_ver)
    sha = vdata.get('sha256')
    if not isinstance(sha, str) or sha != _record_sha256(vdata):
        raise StandardsDamagedError('v%06d sha256 mismatch' % expected_ver)
    rules = vdata.get('rules')
    if not isinstance(rules, list) or len(rules) > RULES_MAX:
        raise StandardsDamagedError('v%06d rules list is invalid' % expected_ver)
    seen = set()
    for r in rules:
        try:
            validated = validate_rule_input(r)
        except StandardsRefusal as exc:
            raise StandardsDamagedError('v%06d rule invalid: %s' % (expected_ver, exc)) from exc
        if validated['id'] in seen:
            raise StandardsDamagedError('v%06d duplicate rule id %s' % (expected_ver, validated['id']))
        seen.add(validated['id'])


def _rules_equal_ignoring_meta(rule_a, rule_b):
    keys = ('id', 'statement', 'rationale', 'owner', 'severity', 'applicability', 'check', 'references')
    return {k: rule_a.get(k) for k in keys} == {k: rule_b.get(k) for k in keys}


def get_gate_view(store):
    """Return ``{'mode': 'warn'|'block', 'source': 'default'|'stored'|'damaged', ...}``.

    Damaged ``gate.json`` fails closed to ``'block'``.
    """
    state, data, detail = store.read_gate_raw()
    if state == 'default':
        return {
            'mode': 'warn',
            'source': 'default',
            'set_by': None,
            'set_at': None,
            'reason': None,
            'warning': None,
        }
    if state == 'damaged':
        return {
            'mode': 'block',
            'source': 'damaged',
            'set_by': None,
            'set_at': None,
            'reason': None,
            'warning': 'gate.json is damaged (%s); failing closed to block mode.' % detail,
        }
    return {
        'mode': data['mode'],
        'source': 'stored',
        'set_by': data.get('set_by'),
        'set_at': data.get('set_at'),
        'reason': data.get('reason'),
        'warning': None,
    }


def resolve_effective_register(
    store,
    *,
    version=None,
    shared_version_override=None,
    active_person_checker=None,
    known_ref_keys=None,
):
    """Resolve the project's register (and single-hop shared parent if ``inherits`` is set).

    Returns a dict with ``status`` in ``('none', 'damaged', 'active')``.
    Never mutates ``store``.
    """
    gate = get_gate_view(store)
    status, head, versions_map = store.inspect_register_raw()
    if status == 'none':
        return {
            'schema_version': SCHEMA_VERSION,
            'project': store.project_id,
            'status': 'none',
            'has_register': False,
            'gate': gate,
            'version': None,
            'latest_version': None,
            'rules': [],
            'effective_rules': [],
            'lint_warnings': [],
        }
    if status == 'damaged':
        return {
            'schema_version': SCHEMA_VERSION,
            'project': store.project_id,
            'status': 'damaged',
            'has_register': True,
            'gate': gate,
            'damage_detail': versions_map,
            'warning': 'Standards register in project %s is damaged (%s).'
            % (store.project_id, versions_map),
            'version': None,
            'latest_version': None,
            'rules': [],
            'effective_rules': [],
            'lint_warnings': [],
        }

    latest_version = head['current_version']
    target_ver = latest_version if version is None else int(version)
    if target_ver not in versions_map:
        raise StandardsRefusal(
            'Standards register version %d does not exist on project %s (latest is v%d).'
            % (target_ver, store.project_id, latest_version),
            status=404,
        )
    ver_obj = versions_map[target_ver]
    departed = store.read_departed_owners()

    def _is_departed(owner_id):
        if owner_id in departed:
            return True
        if active_person_checker is not None and not active_person_checker(owner_id):
            return True
        return False

    lint_warnings = []
    inherited_rules_by_id = {}
    shared_pid = ver_obj.get('inherits')
    shared_resolved_ver = None

    if shared_pid is not None:
        if store.shared_loader is None:
            return {
                'schema_version': SCHEMA_VERSION,
                'project': store.project_id,
                'status': 'damaged',
                'has_register': True,
                'gate': gate,
                'damage_detail': 'inherited project %s cannot be loaded' % shared_pid,
                'warning': 'Inherited standards register %s cannot be loaded.' % shared_pid,
                'version': target_ver,
                'latest_version': latest_version,
                'rules': [],
                'effective_rules': [],
                'lint_warnings': [],
            }
        parent_store = store.shared_loader(shared_pid)
        if parent_store is None:
            return {
                'schema_version': SCHEMA_VERSION,
                'project': store.project_id,
                'status': 'damaged',
                'has_register': True,
                'gate': gate,
                'damage_detail': 'inherited project %s does not exist' % shared_pid,
                'warning': 'Inherited standards register %s does not exist.' % shared_pid,
                'version': target_ver,
                'latest_version': latest_version,
                'rules': [],
                'effective_rules': [],
                'lint_warnings': [],
            }
        p_status, p_head, p_versions = parent_store.inspect_register_raw()
        if p_status != 'active':
            detail = p_versions if p_status == 'damaged' else ('project %s has no register' % shared_pid)
            return {
                'schema_version': SCHEMA_VERSION,
                'project': store.project_id,
                'status': 'damaged',
                'has_register': True,
                'gate': gate,
                'damage_detail': 'inherited register %s unusable (%s)' % (shared_pid, detail),
                'warning': 'Inherited standards register %s is unusable (%s).' % (shared_pid, detail),
                'version': target_ver,
                'latest_version': latest_version,
                'rules': [],
                'effective_rules': [],
                'lint_warnings': [],
            }
        p_ver_num = (
            p_head['current_version']
            if shared_version_override is None or int(shared_version_override) not in p_versions
            else int(shared_version_override)
        )
        p_ver = p_versions[p_ver_num]
        if not p_ver.get('shared'):
            return {
                'schema_version': SCHEMA_VERSION,
                'project': store.project_id,
                'status': 'damaged',
                'has_register': True,
                'gate': gate,
                'damage_detail': 'inherited register %s is no longer marked shared' % shared_pid,
                'warning': 'Inherited standards register %s is no longer marked shared.' % shared_pid,
                'version': target_ver,
                'latest_version': latest_version,
                'rules': [],
                'effective_rules': [],
                'lint_warnings': [],
            }
        if p_ver.get('inherits'):
            return {
                'schema_version': SCHEMA_VERSION,
                'project': store.project_id,
                'status': 'damaged',
                'has_register': True,
                'gate': gate,
                'damage_detail': 'transitive inheritance through %s' % shared_pid,
                'warning': 'Transitive inheritance through %s is invalid.' % shared_pid,
                'version': target_ver,
                'latest_version': latest_version,
                'rules': [],
                'effective_rules': [],
                'lint_warnings': [],
            }
        shared_resolved_ver = p_ver_num
        for prule in p_ver['rules']:
            copy_r = parse_json(canonical_bytes(prule).decode('utf-8'))
            copy_r['origin'] = {
                'kind': 'inherited',
                'project': shared_pid,
                'version': p_ver_num,
            }
            inherited_rules_by_id[copy_r['id']] = copy_r

    local_by_id = {
        r['id']: parse_json(canonical_bytes(r).decode('utf-8')) for r in ver_obj['rules']
    }
    effective_rules = []
    seen_ids = set()

    # Preserve inherited order first, then project-added rules in local order.
    for rid, irule in inherited_rules_by_id.items():
        seen_ids.add(rid)
        if rid in local_by_id:
            lrule = local_by_id[rid]
            if irule['severity'] == 'must':
                if not _rules_equal_ignoring_meta(irule, lrule):
                    lint_warnings.append({
                        'code': 'shadowed_local_override_of_inherited_must',
                        'rule_id': rid,
                        'message': (
                            "Shared project %s defines rule %s as 'must' (read-only in child); "
                            "ignoring stale local override in %s."
                            % (shared_pid, rid, store.project_id)
                        ),
                    })
                chosen = dict(irule)
            else:
                chosen = dict(lrule)
                chosen['origin'] = {
                    'kind': 'project',
                    'project': store.project_id,
                    'version': target_ver,
                    'overridden_from': {
                        'project': shared_pid,
                        'version': shared_resolved_ver,
                    },
                }
        else:
            chosen = dict(irule)
        effective_rules.append(chosen)

    for lrule in ver_obj['rules']:
        rid = lrule['id']
        if rid in seen_ids:
            continue
        chosen = parse_json(canonical_bytes(lrule).decode('utf-8'))
        chosen['origin'] = {
            'kind': 'project',
            'project': store.project_id,
            'version': target_ver,
        }
        effective_rules.append(chosen)

    for rule in effective_rules:
        if _is_departed(rule['owner']):
            rule['owner_status'] = 'departed_fallback_coordinator'
            rule['effective_owner'] = 'coordinator'
        else:
            rule['owner_status'] = 'active'
            rule['effective_owner'] = rule['owner']
        if known_ref_keys is not None:
            for ref_key in rule.get('references') or ():
                if ref_key not in known_ref_keys:
                    lint_warnings.append({
                        'code': 'missing_reference',
                        'rule_id': rule['id'],
                        'reference': ref_key,
                        'message': (
                            'Rule %s links reference %s, which is not an accepted reference '
                            'record in project %s.' % (rule['id'], ref_key, store.project_id)
                        ),
                    })

    limit = int(ver_obj.get('all_changes_must_limit') or ALL_CHANGES_MUST_DEFAULT)
    all_changes_must = [
        r['id']
        for r in effective_rules
        if r['severity'] == 'must' and r['applicability'].get('all_changes') is True
    ]
    if len(all_changes_must) > limit:
        lint_warnings.append({
            'code': 'all_changes_must_exceeds_limit',
            'count': len(all_changes_must),
            'limit': limit,
            'rule_ids': all_changes_must,
            'message': (
                "Register has %d 'must' rules matching all changes (%s), exceeding "
                "all_changes_must_limit (%d)."
                % (len(all_changes_must), ', '.join(all_changes_must), limit)
            ),
        })

    local_rules_out = []
    for lrule in ver_obj['rules']:
        rcopy = parse_json(canonical_bytes(lrule).decode('utf-8'))
        rcopy['origin'] = {'kind': 'project', 'project': store.project_id, 'version': target_ver}
        if _is_departed(rcopy['owner']):
            rcopy['owner_status'] = 'departed_fallback_coordinator'
            rcopy['effective_owner'] = 'coordinator'
        else:
            rcopy['owner_status'] = 'active'
            rcopy['effective_owner'] = rcopy['owner']
        local_rules_out.append(rcopy)

    return {
        'schema_version': SCHEMA_VERSION,
        'project': store.project_id,
        'status': 'active',
        'has_register': True,
        'gate': gate,
        'version': target_ver,
        'latest_version': latest_version,
        'newer_version_exists': target_ver < latest_version,
        'sha256': ver_obj['sha256'],
        'created_at': ver_obj['created_at'],
        'author': ver_obj['author'],
        'author_person': ver_obj.get('author_person'),
        'reason': ver_obj['reason'],
        'shared': bool(ver_obj.get('shared')),
        'inherits': shared_pid,
        'shared_version': shared_resolved_ver,
        'all_changes_must_limit': limit,
        'named_editors': list(ver_obj.get('named_editors') or []),
        'accepted_draft_id': ver_obj.get('accepted_draft_id'),
        'rules': local_rules_out,
        'effective_rules': effective_rules,
        'lint_warnings': lint_warnings,
    }


def _validate_inheritance_target(
    store,
    shared_pid,
    *,
    can_read_project=None,
):
    """Validate single-hop shared register target and return its head version dict."""
    if shared_pid is None:
        return None
    if not isinstance(shared_pid, str) or not PROJECT_ID_RE.fullmatch(shared_pid):
        raise StandardsRefusal('Invalid inherits project id %r' % (shared_pid,), status=422)
    if shared_pid == store.project_id:
        raise StandardsRefusal(refusal_inheritance_transitive(shared_pid), status=422)
    if can_read_project is not None and not can_read_project(shared_pid):
        raise StandardsRefusal(refusal_inheritance_no_read(shared_pid), status=403)
    if store.shared_loader is None:
        raise StandardsRefusal(refusal_inheritance_not_shared(shared_pid), status=422)
    parent_store = store.shared_loader(shared_pid)
    if parent_store is None:
        raise StandardsRefusal(refusal_inheritance_not_shared(shared_pid), status=422)
    p_status, p_head, p_versions = parent_store.inspect_register_raw()
    if p_status != 'active':
        raise StandardsRefusal(refusal_inheritance_not_shared(shared_pid), status=422)
    p_ver = p_versions[p_head['current_version']]
    if not p_ver.get('shared'):
        raise StandardsRefusal(refusal_inheritance_not_shared(shared_pid), status=422)
    if p_ver.get('inherits'):
        raise StandardsRefusal(refusal_inheritance_transitive(shared_pid), status=422)
    return p_ver


def _enforce_inherited_must_readonly(pid, shared_pid, parent_ver_obj, proposed_local_rules, *, dropped_ids=()):
    """Plan 2 Item 7: inherited ``must`` rules are completely read-only in the child."""
    if parent_ver_obj is None:
        return
    local_map = {r['id']: r for r in proposed_local_rules}
    for prule in parent_ver_obj['rules']:
        if prule['severity'] != 'must':
            continue
        rid = prule['id']
        if rid in dropped_ids:
            raise StandardsRefusal(refusal_inherited_must_readonly(pid, rid, shared_pid), status=422)
        if rid in local_map:
            # Child may not re-declare or alter any field of an inherited must rule.
            raise StandardsRefusal(refusal_inherited_must_readonly(pid, rid, shared_pid), status=422)


def _check_expected_version(pid, expected_version, current_version):
    if type(expected_version) is not int:
        raise StandardsRefusal(
            'expected_version is required and must be an integer (0 before version 1, else current register version).',
            status=422,
        )
    if expected_version != current_version:
        raise StandardsRefusal(
            refusal_version_conflict(pid, expected_version, current_version), status=409
        )


def put_register(
    store,
    payload,
    *,
    actor,
    author_person,
    is_person_session,
    is_coordinator_or_owner,
    via='web',
    can_read_project=None,
    known_person_checker=None,
    active_person_checker=None,
    known_ref_keys=None,
    now=None,
):
    """Create or replace a project's standards register version (R1, R2, R3, R3a, R5)."""
    if not is_person_session:
        raise StandardsRefusal(refusal_agent_register_edit(), status=403)
    if not isinstance(payload, dict):
        raise StandardsRefusal('Standards register payload must be a JSON object', status=422)

    allowed = {
        'expected_version',
        'reason',
        'rules',
        'inherits',
        'shared',
        'all_changes_must_limit',
        'named_editors',
        'dropped_rule_ids',
    }
    extra = sorted(set(payload) - allowed)
    if extra:
        raise StandardsRefusal('Unknown standards register field(s): %s' % ', '.join(extra), status=422)

    status, head, versions_map = store.inspect_register_raw()
    prev_ver_obj = versions_map[head['current_version']] if status == 'active' else None
    current_named_editors = set(prev_ver_obj.get('named_editors') or []) if prev_ver_obj else set()
    is_named_editor = bool(author_person and author_person in current_named_editors)
    if not (is_coordinator_or_owner or is_named_editor):
        raise StandardsRefusal(refusal_put_register_authority(), status=403)

    if status == 'damaged':
        raise StandardsRefusal(
            'Standards register in project %s is damaged (%s); repair or restore .standards/ first.'
            % (store.project_id, versions_map),
            status=409,
        )
    current_version = 0 if status == 'none' else head['current_version']

    _check_expected_version(store.project_id, payload.get('expected_version'), current_version)

    reason = _check_clean_text(payload.get('reason'), 'reason', SUMMARY_REASON_MAX)

    rules_raw = payload.get('rules')
    if not isinstance(rules_raw, list) or len(rules_raw) > RULES_MAX:
        raise StandardsRefusal(
            'rules must be a list of at most %d rule definitions' % RULES_MAX, status=422
        )
    normalized_rules = []
    seen_ids = set()
    for r in rules_raw:
        nr = validate_rule_input(r, known_person_checker=known_person_checker)
        if nr['id'] in seen_ids:
            raise StandardsRefusal('Duplicate rule id %s in register' % nr['id'], status=422)
        seen_ids.add(nr['id'])
        normalized_rules.append(nr)

    inherits = payload.get('inherits', prev_ver_obj.get('inherits') if prev_ver_obj else None)
    shared = payload.get('shared', prev_ver_obj.get('shared', False) if prev_ver_obj else False)
    if not isinstance(shared, bool):
        raise StandardsRefusal('shared must be boolean', status=422)
    if shared and inherits is not None:
        raise StandardsRefusal(refusal_inheritance_transitive(inherits), status=422)

    limit = payload.get(
        'all_changes_must_limit',
        prev_ver_obj.get('all_changes_must_limit', ALL_CHANGES_MUST_DEFAULT)
        if prev_ver_obj
        else ALL_CHANGES_MUST_DEFAULT,
    )
    if type(limit) is not int or limit < 1 or limit > RULES_MAX:
        raise StandardsRefusal('all_changes_must_limit must be an integer 1..%d' % RULES_MAX, status=422)

    named_editors_raw = payload.get(
        'named_editors',
        prev_ver_obj.get('named_editors', []) if prev_ver_obj else [],
    )
    if not isinstance(named_editors_raw, list) or len(named_editors_raw) > 64:
        raise StandardsRefusal('named_editors must be a list of person ids', status=422)
    named_editors = []
    for ed in named_editors_raw:
        if not is_person_owner_id(ed):
            raise StandardsRefusal(
                'named_editors entry %r is not a person account; editors must be people, never agents.'
                % (ed,),
                status=422,
            )
        if known_person_checker is not None and not known_person_checker(ed):
            raise StandardsRefusal(
                'named_editors entry %r is not a known person account.' % (ed,), status=422
            )
        if ed not in named_editors:
            named_editors.append(ed)
    named_editors.sort()

    prev_inherits = prev_ver_obj.get('inherits') if prev_ver_obj else None
    if prev_inherits is not None and inherits != prev_inherits:
        if store.shared_loader is None:
            raise StandardsRefusal(
                'Cannot remove or change inherits from shared project %s while shared register loader is unavailable.'
                % prev_inherits,
                status=409,
            )
        prev_parent_store = store.shared_loader(prev_inherits)
        if prev_parent_store is not None:
            pp_status, pp_head, pp_versions = prev_parent_store.inspect_register_raw()
            if pp_status == 'damaged':
                raise StandardsRefusal(
                    'Cannot remove or change inherits from shared project %s while its standards register is damaged (%s).'
                    % (prev_inherits, pp_versions),
                    status=409,
                )
            if pp_status == 'active':
                pp_ver = pp_versions[pp_head['current_version']]
                inherited_must_ids = [
                    r['id'] for r in pp_ver['rules'] if r.get('severity') == 'must'
                ]
                if inherited_must_ids:
                    raise StandardsRefusal(
                        refusal_detach_inherited_must(
                            store.project_id, prev_inherits, inherited_must_ids
                        ),
                        status=422,
                    )

    parent_ver_obj = None
    if inherits is not None:
        parent_ver_obj = _validate_inheritance_target(
            store,
            inherits,
            can_read_project=can_read_project if inherits != prev_inherits else can_read_project,
        )
        dropped_ids = tuple(payload.get('dropped_rule_ids') or ())
        _enforce_inherited_must_readonly(
            store.project_id, inherits, parent_ver_obj, normalized_rules, dropped_ids=dropped_ids
        )

    return _commit_new_register_version(
        store,
        prev_ver_obj=prev_ver_obj,
        rules=normalized_rules,
        inherits=inherits,
        shared=shared,
        all_changes_must_limit=limit,
        named_editors=named_editors,
        reason=reason,
        actor=actor,
        author_person=author_person,
        via=via,
        accepted_draft_id=None,
        active_person_checker=active_person_checker,
        known_ref_keys=known_ref_keys,
        now=now,
    )


def edit_rule(
    store,
    rule_id,
    payload,
    *,
    actor,
    author_person,
    is_person_session,
    is_coordinator_or_owner,
    via='web',
    known_person_checker=None,
    active_person_checker=None,
    known_ref_keys=None,
    notify_coordinator_cb=None,
    now=None,
):
    """Direct rule-owner (or coordinator/owner) edit of one existing rule (R5)."""
    if not is_person_session:
        raise StandardsRefusal(refusal_agent_register_edit(), status=403)
    if not isinstance(payload, dict):
        raise StandardsRefusal('Rule edit payload must be a JSON object', status=422)
    if not isinstance(rule_id, str) or not RULE_ID_RE.fullmatch(rule_id):
        raise StandardsRefusal('Invalid rule id %r' % (rule_id,), status=422)

    status, head, versions_map = store.inspect_register_raw()
    if status == 'none':
        raise StandardsRefusal(
            'Project %s has no standards register yet; create version 1 first.' % store.project_id,
            status=404,
        )
    if status == 'damaged':
        raise StandardsRefusal(
            'Standards register in project %s is damaged (%s).' % (store.project_id, versions_map),
            status=409,
        )
    current_version = head['current_version']
    prev_ver_obj = versions_map[current_version]
    _check_expected_version(store.project_id, payload.get('expected_version'), current_version)

    reason = _check_clean_text(payload.get('reason'), 'reason', SUMMARY_REASON_MAX)
    rule_patch = payload.get('rule')
    if not isinstance(rule_patch, dict):
        # Also allow flat fields alongside expected_version/reason for convenience
        rule_patch = {
            k: v
            for k, v in payload.items()
            if k not in ('expected_version', 'reason')
        }

    shared_pid = prev_ver_obj.get('inherits')
    parent_ver_obj = None
    if shared_pid is not None and store.shared_loader is not None:
        p_store = store.shared_loader(shared_pid)
        if p_store is not None:
            p_st, p_hd, p_vers = p_store.inspect_register_raw()
            if p_st == 'damaged':
                raise StandardsRefusal(
                    'Shared standards register in project %s is damaged (%s).' % (shared_pid, p_vers),
                    status=409,
                )
            if p_st == 'active':
                parent_ver_obj = p_vers[p_hd['current_version']]

    if parent_ver_obj is not None:
        for prule in parent_ver_obj['rules']:
            if prule['id'] == rule_id and prule['severity'] == 'must':
                raise StandardsRefusal(
                    refusal_inherited_must_readonly(store.project_id, rule_id, shared_pid),
                    status=422,
                )

    local_idx = None
    existing_rule = None
    for idx, r in enumerate(prev_ver_obj['rules']):
        if r['id'] == rule_id:
            local_idx = idx
            existing_rule = r
            break
    if existing_rule is None and parent_ver_obj is not None:
        for prule in parent_ver_obj['rules']:
            if prule['id'] == rule_id:
                existing_rule = prule
                break
    if existing_rule is None:
        raise StandardsRefusal(
            'Rule %s does not exist in project %s standards register.' % (rule_id, store.project_id),
            status=404,
        )

    departed = store.read_departed_owners()
    orig_owner = existing_rule['owner']
    owner_departed = orig_owner in departed or (
        active_person_checker is not None and not active_person_checker(orig_owner)
    )
    effective_owner = 'coordinator' if owner_departed else orig_owner
    named_editors = set(prev_ver_obj.get('named_editors') or [])
    is_named_editor = bool(author_person and author_person in named_editors)

    merged = dict(existing_rule)
    for k, v in rule_patch.items():
        merged[k] = v
    merged['id'] = rule_id
    validated_rule = validate_rule_input(merged, known_person_checker=known_person_checker)

    owner_changed = validated_rule['owner'] != orig_owner
    if owner_changed and not (is_coordinator_or_owner or is_named_editor):
        raise StandardsRefusal(refusal_unauthorized_structural_edit(rule_id), status=403)

    is_active_rule_owner = (not owner_departed) and bool(author_person and author_person == orig_owner)
    if not (is_coordinator_or_owner or is_active_rule_owner or (owner_changed and is_named_editor)):
        raise StandardsRefusal(refusal_unauthorized_rule_edit(rule_id, effective_owner), status=403)

    new_rules = [parse_json(canonical_bytes(r).decode('utf-8')) for r in prev_ver_obj['rules']]
    if local_idx is not None:
        new_rules[local_idx] = validated_rule
    else:
        new_rules.append(validated_rule)

    if parent_ver_obj is not None:
        _enforce_inherited_must_readonly(store.project_id, shared_pid, parent_ver_obj, new_rules)

    result = _commit_new_register_version(
        store,
        prev_ver_obj=prev_ver_obj,
        rules=new_rules,
        inherits=shared_pid,
        shared=bool(prev_ver_obj.get('shared')),
        all_changes_must_limit=int(
            prev_ver_obj.get('all_changes_must_limit') or ALL_CHANGES_MUST_DEFAULT
        ),
        named_editors=list(prev_ver_obj.get('named_editors') or []),
        reason=reason,
        actor=actor,
        author_person=author_person,
        via=via,
        accepted_draft_id=None,
        active_person_checker=active_person_checker,
        known_ref_keys=known_ref_keys,
        now=now,
    )

    # R5: Rule owner changing their rule notifies the project coordinator.
    notif_text = (
        'Standards rule %s updated in project %s (register v%d) by %s: %s'
        % (rule_id, store.project_id, result['version'], author_person or actor, reason)
    )
    notif_entry = {
        'schema_version': SCHEMA_VERSION,
        'kind': 'rule_edit',
        'project': store.project_id,
        'rule_id': rule_id,
        'version': result['version'],
        'actor': actor,
        'author_person': author_person,
        'reason': reason,
        'created_at': utc_now_iso(now),
        'text': notif_text,
    }
    store.append_notification(notif_entry)
    note_id = None
    if notify_coordinator_cb is not None and not is_coordinator_or_owner:
        note_id = notify_coordinator_cb(notif_text)
    if not is_coordinator_or_owner:
        result['coordinator_notification_text'] = notif_text
        result['notified_coordinator'] = bool(note_id) if via == 'web' else True
    else:
        result['notified_coordinator'] = False
    if note_id is not None:
        result['coordinator_note_id'] = note_id
    return result


def _commit_new_register_version(
    store,
    *,
    prev_ver_obj,
    rules,
    inherits,
    shared,
    all_changes_must_limit,
    named_editors,
    reason,
    actor,
    author_person,
    via,
    accepted_draft_id,
    active_person_checker=None,
    known_ref_keys=None,
    now=None,
):
    next_ver = 1 if prev_ver_obj is None else (prev_ver_obj['version'] + 1)
    ts = utc_now_iso(now)
    ver_obj = {
        'schema_version': SCHEMA_VERSION,
        'project': store.project_id,
        'version': next_ver,
        'previous_version': None if prev_ver_obj is None else prev_ver_obj['version'],
        'previous_sha256': None if prev_ver_obj is None else prev_ver_obj['sha256'],
        'created_at': ts,
        'author': actor,
        'author_person': author_person,
        'via': via,
        'reason': reason,
        'shared': bool(shared),
        'inherits': inherits,
        'all_changes_must_limit': int(all_changes_must_limit),
        'named_editors': sorted(named_editors),
        'accepted_draft_id': accepted_draft_id,
        'rules': rules,
    }
    ver_obj['sha256'] = _record_sha256(ver_obj)
    head_obj = {
        'schema_version': SCHEMA_VERSION,
        'current_version': next_ver,
        'current_sha256': ver_obj['sha256'],
        'updated_at': ts,
    }
    store.write_version_and_head(ver_obj, head_obj)
    return resolve_effective_register(
        store,
        version=next_ver,
        active_person_checker=active_person_checker,
        known_ref_keys=known_ref_keys,
    )


def list_versions(store):
    status, head, versions_map = store.inspect_register_raw()
    if status == 'none':
        return {
            'schema_version': SCHEMA_VERSION,
            'project': store.project_id,
            'status': 'none',
            'current_version': None,
            'items': [],
        }
    if status == 'damaged':
        return {
            'schema_version': SCHEMA_VERSION,
            'project': store.project_id,
            'status': 'damaged',
            'damage_detail': versions_map,
            'current_version': None,
            'items': [],
        }
    items = []
    for ver_num in sorted(versions_map, reverse=True):
        v = versions_map[ver_num]
        items.append({
            'version': v['version'],
            'sha256': v['sha256'],
            'created_at': v['created_at'],
            'author': v['author'],
            'author_person': v.get('author_person'),
            'reason': v['reason'],
            'rule_count': len(v['rules']),
            'inherits': v.get('inherits'),
            'shared': bool(v.get('shared')),
            'accepted_draft_id': v.get('accepted_draft_id'),
        })
    return {
        'schema_version': SCHEMA_VERSION,
        'project': store.project_id,
        'status': 'active',
        'current_version': head['current_version'],
        'items': items,
    }


def propose_draft(store, payload, *, actor, author_person=None, known_person_checker=None, now=None):
    """Propose a rule change draft (open to contributors and agents — never creates head.json)."""
    if not isinstance(payload, dict):
        raise StandardsRefusal('Draft proposal payload must be a JSON object', status=422)
    allowed = {'action', 'rule', 'rule_id', 'reason'}
    extra = sorted(set(payload) - allowed)
    if extra:
        raise StandardsRefusal('Unknown draft field(s): %s' % ', '.join(extra), status=422)

    reason = _check_clean_text(payload.get('reason'), 'draft reason', SUMMARY_REASON_MAX)
    action = payload.get('action') or ('remove' if 'rule' not in payload and 'rule_id' in payload else 'upsert')
    if action not in ('upsert', 'remove'):
        raise StandardsRefusal("Draft action must be 'upsert' or 'remove'", status=422)

    if action == 'upsert':
        rule_obj = validate_rule_input(
            payload.get('rule'), known_person_checker=known_person_checker
        )
        target_rule_id = rule_obj['id']
    else:
        target_rule_id = payload.get('rule_id')
        if not isinstance(target_rule_id, str) or not RULE_ID_RE.fullmatch(target_rule_id):
            raise StandardsRefusal('Draft remove requires a valid rule_id', status=422)
        rule_obj = None

    draft_id = 'draft-' + _random_hex(8)
    ts = utc_now_iso(now)
    draft = {
        'schema_version': SCHEMA_VERSION,
        'id': draft_id,
        'project': store.project_id,
        'status': 'pending',
        'action': action,
        'rule_id': target_rule_id,
        'rule': rule_obj,
        'reason': reason,
        'proposed_by': actor,
        'proposed_by_person': author_person,
        'created_at': ts,
        'decided_by': None,
        'decided_by_person': None,
        'decided_at': None,
        'decision_reason': None,
        'resulting_version': None,
    }
    store.write_draft(draft)
    return draft


def list_drafts_view(store, status_filter=None):
    if status_filter is not None and status_filter not in DRAFT_STATUSES:
        raise StandardsRefusal(
            'Draft status filter must be pending, accepted, or rejected', status=422
        )
    items = store.list_drafts()
    if status_filter is not None:
        items = [d for d in items if d.get('status') == status_filter]
    return {
        'schema_version': SCHEMA_VERSION,
        'project': store.project_id,
        'items': items,
    }


def decide_draft(
    store,
    draft_id,
    payload,
    *,
    actor,
    author_person,
    is_person_session,
    is_coordinator_or_owner,
    via='web',
    known_person_checker=None,
    active_person_checker=None,
    known_ref_keys=None,
    notify_coordinator_cb=None,
    now=None,
):
    """Accept or reject a pending draft (person-only: rule owner or coordinator/owner/named_editor)."""
    if not is_person_session:
        raise StandardsRefusal(refusal_agent_register_edit(), status=403)
    if not isinstance(payload, dict):
        raise StandardsRefusal('Draft decision payload must be a JSON object', status=422)
    draft = store.read_draft(draft_id)
    if draft is None:
        raise StandardsRefusal('Draft %s not found in project %s.' % (draft_id, store.project_id), status=404)
    if draft.get('status') != 'pending':
        raise StandardsRefusal(refusal_draft_already_decided(draft_id, draft.get('status')), status=409)

    decision = payload.get('decision')
    if decision not in ('accept', 'reject'):
        raise StandardsRefusal("Draft decision must be 'accept' or 'reject'", status=422)

    raw_reason = payload.get('reason')
    if decision == 'reject':
        if not isinstance(raw_reason, str) or not raw_reason.strip():
            raise StandardsRefusal(refusal_draft_reject_reason(draft_id), status=422)
        decision_reason = _check_clean_text(raw_reason, 'decision reason', SUMMARY_REASON_MAX)
    else:
        decision_reason = (
            _check_clean_text(raw_reason, 'decision reason', SUMMARY_REASON_MAX)
            if isinstance(raw_reason, str) and raw_reason.strip()
            else draft['reason']
        )

    status, head, versions_map = store.inspect_register_raw()
    if status == 'damaged':
        raise StandardsRefusal(
            'Standards register in project %s is damaged (%s).' % (store.project_id, versions_map),
            status=409,
        )
    current_version = 0 if status == 'none' else head['current_version']
    prev_ver_obj = None if status == 'none' else versions_map[current_version]

    if decision == 'accept' or payload.get('expected_version') is not None:
        _check_expected_version(store.project_id, payload.get('expected_version'), current_version)

    named_editors = set(prev_ver_obj.get('named_editors') or []) if prev_ver_obj else set()
    is_named_editor = bool(author_person and author_person in named_editors)
    departed = store.read_departed_owners()

    existing_map = {r['id']: r for r in (prev_ver_obj['rules'] if prev_ver_obj else [])}
    shared_pid = prev_ver_obj.get('inherits') if prev_ver_obj else None
    parent_ver_obj = None
    if shared_pid is not None and store.shared_loader is not None:
        p_store = store.shared_loader(shared_pid)
        if p_store is not None:
            p_st, p_hd, p_vers = p_store.inspect_register_raw()
            if p_st == 'damaged':
                raise StandardsRefusal(
                    'Shared standards register in project %s is damaged (%s).' % (shared_pid, p_vers),
                    status=409,
                )
            if p_st == 'active':
                parent_ver_obj = p_vers[p_hd['current_version']]

    rid = draft['rule_id']
    existing_rule = existing_map.get(rid)
    if existing_rule is None and parent_ver_obj is not None:
        for prule in parent_ver_obj['rules']:
            if prule['id'] == rid:
                existing_rule = prule
                break

    is_structural = (
        draft['action'] == 'remove'
        or existing_rule is None
        or (draft.get('rule') and draft['rule']['owner'] != existing_rule['owner'])
    )

    if is_structural:
        if not (is_coordinator_or_owner or is_named_editor):
            raise StandardsRefusal(refusal_draft_decide_structural(draft_id), status=403)
    else:
        orig_owner = existing_rule['owner']
        owner_dep = orig_owner in departed or (
            active_person_checker is not None and not active_person_checker(orig_owner)
        )
        eff_owner = 'coordinator' if owner_dep else orig_owner
        is_active_owner = (not owner_dep) and bool(author_person and author_person == orig_owner)
        if not (is_coordinator_or_owner or is_active_owner):
            raise StandardsRefusal(refusal_draft_decide_rule_owner(rid, eff_owner, draft_id), status=403)

    ts = utc_now_iso(now)
    if decision == 'reject':
        updated = dict(
            draft,
            status='rejected',
            decided_by=actor,
            decided_by_person=author_person,
            decided_at=ts,
            decision_reason=decision_reason,
        )
        store.write_draft(updated)
        return {'draft': updated, 'register': None}

    # Accept: apply to register rules and write a new version!
    if parent_ver_obj is not None:
        for prule in parent_ver_obj['rules']:
            if prule['id'] == rid and prule['severity'] == 'must':
                raise StandardsRefusal(
                    refusal_inherited_must_readonly(store.project_id, rid, shared_pid),
                    status=422,
                )

    new_rules = [
        parse_json(canonical_bytes(r).decode('utf-8'))
        for r in (prev_ver_obj['rules'] if prev_ver_obj else [])
    ]
    if draft['action'] == 'remove':
        new_rules = [r for r in new_rules if r['id'] != rid]
        if parent_ver_obj is not None:
            _enforce_inherited_must_readonly(
                store.project_id, shared_pid, parent_ver_obj, new_rules, dropped_ids=(rid,)
            )
    else:
        validated_rule = validate_rule_input(draft['rule'], known_person_checker=known_person_checker)
        replaced = False
        for idx, r in enumerate(new_rules):
            if r['id'] == rid:
                new_rules[idx] = validated_rule
                replaced = True
                break
        if not replaced:
            new_rules.append(validated_rule)
        if parent_ver_obj is not None:
            _enforce_inherited_must_readonly(store.project_id, shared_pid, parent_ver_obj, new_rules)

    reg_view = _commit_new_register_version(
        store,
        prev_ver_obj=prev_ver_obj,
        rules=new_rules,
        inherits=shared_pid,
        shared=bool(prev_ver_obj.get('shared')) if prev_ver_obj else False,
        all_changes_must_limit=int(
            prev_ver_obj.get('all_changes_must_limit') or ALL_CHANGES_MUST_DEFAULT
        )
        if prev_ver_obj
        else ALL_CHANGES_MUST_DEFAULT,
        named_editors=list(prev_ver_obj.get('named_editors') or []) if prev_ver_obj else [],
        reason=decision_reason,
        actor=actor,
        author_person=author_person,
        via=via,
        accepted_draft_id=draft_id,
        active_person_checker=active_person_checker,
        known_ref_keys=known_ref_keys,
        now=now,
    )

    updated = dict(
        draft,
        status='accepted',
        decided_by=actor,
        decided_by_person=author_person,
        decided_at=ts,
        decision_reason=decision_reason,
        resulting_version=reg_view['version'],
    )
    store.write_draft(updated)

    out = {'draft': updated, 'register': reg_view}
    if not is_coordinator_or_owner:
        notif_text = (
            'Standards draft %s on rule %s accepted in project %s (register v%d) by %s: %s'
            % (draft_id, rid, store.project_id, reg_view['version'], author_person or actor, decision_reason)
        )
        store.append_notification({
            'schema_version': SCHEMA_VERSION,
            'kind': 'draft_accepted',
            'project': store.project_id,
            'draft_id': draft_id,
            'rule_id': rid,
            'version': reg_view['version'],
            'actor': actor,
            'author_person': author_person,
            'reason': decision_reason,
            'created_at': ts,
            'text': notif_text,
        })
        note_id = None
        if notify_coordinator_cb is not None:
            note_id = notify_coordinator_cb(notif_text)
        out['notified_coordinator'] = bool(note_id) if via == 'web' else True
        out['coordinator_notification_text'] = notif_text
        if note_id is not None:
            out['coordinator_note_id'] = note_id

    return out


def set_gate_mode(
    store,
    mode,
    reason,
    *,
    actor,
    author_person,
    is_person_session,
    is_coordinator_or_owner,
    via='web',
    now=None,
):
    """Switch per-project warn/block gate (person coordinator/owner only — never creates head.json)."""
    if not is_person_session or not is_coordinator_or_owner:
        raise StandardsRefusal(refusal_gate_switch_authority(), status=403)
    if mode not in GATE_MODES:
        raise StandardsRefusal("Standards gate mode must be 'warn' or 'block'", status=422)
    clean_reason = _check_clean_text(reason, 'gate switch reason', SUMMARY_REASON_MAX)

    raw_state, prev_data, _ = store.read_gate_raw()
    prev_mode = prev_data['mode'] if raw_state == 'stored' else 'warn'
    history = list(prev_data.get('history') or []) if raw_state == 'stored' else []
    ts = utc_now_iso(now)
    entry = {
        'mode': mode,
        'previous': prev_mode,
        'set_by': actor,
        'set_by_person': author_person,
        'set_via': via,
        'set_at': ts,
        'reason': clean_reason,
    }
    history.append(entry)
    if len(history) > GATE_HISTORY_MAX:
        history = history[-GATE_HISTORY_MAX:]
    gate_obj = {
        'schema_version': SCHEMA_VERSION,
        'mode': mode,
        'previous': prev_mode,
        'set_by': actor,
        'set_by_person': author_person,
        'set_via': via,
        'set_at': ts,
        'reason': clean_reason,
        'history': history,
    }
    store.write_gate(gate_obj)
    return get_gate_view(store)


# -- Waivers (R10) ------------------------------------------------------------

def grant_waiver(
    store,
    payload,
    *,
    task_id,
    contribution,
    task_assignee,
    actor,
    author_person,
    is_person_session,
    is_coordinator_or_owner,
    same_party_checker,
    active_person_checker=None,
    pinned_version=None,
    now=None,
):
    """Grant a single-delivery waiver (optionally bounded by YYYY-MM-DD date)."""
    if not is_person_session:
        raise StandardsRefusal(refusal_waiver_agent(), status=403)
    if not isinstance(payload, dict):
        raise StandardsRefusal('Waiver payload must be a JSON object', status=422)
    allowed = {'rule_id', 'contribution_id', 'commit', 'reason', 'expiry', 'until'}
    extra = sorted(set(payload) - allowed)
    if extra:
        raise StandardsRefusal('Unknown waiver field(s): %s' % ', '.join(extra), status=422)

    rule_id = payload.get('rule_id')
    if not isinstance(rule_id, str) or not RULE_ID_RE.fullmatch(rule_id):
        raise StandardsRefusal('Invalid waiver rule_id %r' % (rule_id,), status=422)

    if not isinstance(contribution, dict) or not contribution.get('comment_id'):
        raise StandardsRefusal('Task %s has no contribution to waive against.' % task_id, status=409)
    expected_cid = str(contribution['comment_id'])
    expected_commit = str(contribution['commit']).lower()

    req_cid = payload.get('contribution_id')
    if req_cid is not None and str(req_cid) != expected_cid:
        raise StandardsRefusal(
            'Waiver contribution_id %s does not match current contribution %s on task %s.'
            % (req_cid, expected_cid, task_id),
            status=409,
        )
    req_commit = payload.get('commit')
    if req_commit is not None and str(req_commit).lower() != expected_commit:
        raise StandardsRefusal(
            'Waiver commit %s does not match current contribution commit %s.'
            % (req_commit, expected_commit),
            status=409,
        )

    clean_reason = _check_clean_text(payload.get('reason'), 'waiver reason', CHECK_REASON_MAX)

    # Parse expiry: always scoped to this single delivery; optional YYYY-MM-DD bounds it in time.
    expiry_raw = payload.get('expiry')
    until_raw = payload.get('until')
    if expiry_raw is not None:
        if not isinstance(expiry_raw, dict) or expiry_raw.get('kind') not in ('delivery', 'date'):
            raise StandardsRefusal(
                "Waiver expiry must be {'kind': 'delivery'} or {'kind': 'date', 'date': 'YYYY-MM-DD'}",
                status=422,
            )
        if expiry_raw['kind'] == 'date':
            until_raw = expiry_raw.get('date')
    if until_raw is not None:
        if not isinstance(until_raw, str) or not DATE_RE.fullmatch(until_raw):
            raise StandardsRefusal('Waiver expiry date must be YYYY-MM-DD', status=422)
        try:
            datetime.strptime(until_raw, '%Y-%m-%d')
        except ValueError as exc:
            raise StandardsRefusal('Invalid calendar date %s' % until_raw, status=422) from exc
        expiry_obj = {'kind': 'date', 'date': until_raw}
    else:
        expiry_obj = {'kind': 'delivery'}

    reg = resolve_effective_register(
        store,
        version=pinned_version,
        active_person_checker=active_person_checker,
    )
    if reg['status'] == 'damaged':
        raise StandardsRefusal(
            'Standards register in project %s is damaged (%s).'
            % (store.project_id, reg.get('damage_detail')),
            status=409,
        )
    if reg['status'] == 'active':
        matched_rule = next((r for r in reg['effective_rules'] if r['id'] == rule_id), None)
        if matched_rule is None:
            raise StandardsRefusal(
                'Rule %s does not exist in project %s standards register.'
                % (rule_id, store.project_id),
                status=404,
            )
        eff_owner = matched_rule['effective_owner']
        is_rule_owner = (
            matched_rule['owner_status'] == 'active'
            and bool(author_person and author_person == matched_rule['owner'])
        )
        if not (is_coordinator_or_owner or is_rule_owner):
            raise StandardsRefusal(refusal_waiver_unauthorized(rule_id, eff_owner), status=403)
    else:
        # Before version 1 (no register): only coordinator/owner can record a pre-v1 waiver.
        if not is_coordinator_or_owner:
            raise StandardsRefusal(refusal_waiver_unauthorized(rule_id, 'coordinator'), status=403)

    # R10: Strict same_party check — delivery author or task assignee's party can never waive.
    contrib_author = contribution.get('author')
    if same_party_checker(contrib_author) or (
        isinstance(task_assignee, str) and task_assignee and same_party_checker(task_assignee)
    ):
        raise StandardsRefusal(refusal_waiver_same_party(rule_id, actor), status=403)

    waiver_id = 'waiver-' + _random_hex(8)
    ts = utc_now_iso(now)
    waiver = {
        'schema_version': SCHEMA_VERSION,
        'id': waiver_id,
        'project': store.project_id,
        'task_id': task_id,
        'contribution_id': expected_cid,
        'commit': expected_commit,
        'rule_id': rule_id,
        'reason': clean_reason,
        'expiry': expiry_obj,
        'granted_by': actor,
        'granted_by_person': author_person,
        'granted_at': ts,
    }
    store.write_waiver(waiver)
    return waiver


def evaluate_waiver_status(waiver, contribution_id, commit, today_str):
    """Return ``('active', None)`` | ``('superseded_delivery', detail)`` | ``('expired_date', sentence)``."""
    if (
        str(waiver.get('contribution_id')) != str(contribution_id)
        or str(waiver.get('commit') or '').lower() != str(commit or '').lower()
    ):
        return ('superseded_delivery', 'Waiver applies to an earlier delivery or commit.')
    exp = waiver.get('expiry') or {'kind': 'delivery'}
    if exp.get('kind') == 'date':
        d_str = str(exp.get('date') or '')
        if d_str and today_str > d_str:
            return (
                'expired_date',
                refusal_expired_waiver(
                    waiver['id'], waiver['rule_id'], contribution_id, d_str
                ),
            )
    return ('active', None)


def list_waivers_view(store, task_id, current_contribution=None, now=None):
    today_str = utc_today_str(now)
    cid = (current_contribution or {}).get('comment_id')
    commit = (current_contribution or {}).get('commit')
    items = []
    for w in store.list_waivers(task_id=task_id):
        wcopy = dict(w)
        if cid and commit:
            st, detail = evaluate_waiver_status(w, cid, commit, today_str)
            wcopy['status'] = st
            if detail:
                wcopy['status_detail'] = detail
        else:
            wcopy['status'] = 'inactive'
        items.append(wcopy)
    return {
        'schema_version': SCHEMA_VERSION,
        'project': store.project_id,
        'task_id': task_id,
        'items': items,
    }


# -- Git Bundle & Branch Inspection + Citation Validator (R4, R6) -------------

def run_confined_git(args, *, cwd=None, timeout=BUNDLE_GIT_TIMEOUT, raw=False):
    """Run git with all remote transports disabled and only local file transport allowed."""
    cmd = [
        'git',
        '-c',
        'protocol.allow=never',
        '-c',
        'protocol.file.allow=always',
        '-c',
        'core.hooksPath=/dev/null',
    ] + [str(a) for a in args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('git failed: %s' % exc) from exc
    if proc.returncode != 0:
        err = proc.stderr.decode('utf-8', errors='replace').strip()
        out = proc.stdout.decode('utf-8', errors='replace').strip()
        raise ValueError(err or out or ('git exited %d' % proc.returncode))
    return proc.stdout if raw else proc.stdout.decode('utf-8', errors='replace')


def _count_blob_lines(raw_bytes):
    if not raw_bytes:
        return 0
    return raw_bytes.count(b'\n') + (0 if raw_bytes.endswith(b'\n') else 1)


def _inspect_repo_tree_paths(repo_dir, commit, paths):
    """Inspect specific repo-relative paths in ``commit``'s tree."""
    result = {}
    for rel_path in paths:
        out = run_confined_git(
            ['ls-tree', '-z', '--full-tree', commit, '--', rel_path],
            cwd=repo_dir,
            raw=True,
        )
        entries = [chunk for chunk in out.split(b'\x00') if chunk]
        matched = None
        for entry in entries:
            meta, sep, name_bytes = entry.partition(b'\t')
            if not sep:
                continue
            name = name_bytes.decode('utf-8', errors='surrogateescape')
            if name == rel_path:
                parts = meta.decode('ascii', errors='replace').split()
                if len(parts) >= 3:
                    matched = (parts[0], parts[1], parts[2])
                break
        if matched is None:
            result[rel_path] = {
                'exists': False,
                'regular_text': False,
                'line_count': None,
                'detail': 'file not found in commit tree',
            }
            continue
        mode, obj_type, blob_sha = matched
        if mode not in ('100644', '100755') or obj_type != 'blob':
            result[rel_path] = {
                'exists': True,
                'regular_text': False,
                'line_count': None,
                'detail': 'path is not a regular text file',
            }
            continue
        size_str = run_confined_git(['cat-file', '-s', blob_sha], cwd=repo_dir).strip()
        size = int(size_str)
        if size > MAX_BLOB_BYTES:
            result[rel_path] = {
                'exists': True,
                'regular_text': False,
                'line_count': None,
                'detail': 'path is not a regular text file',
            }
            continue
        raw_blob = run_confined_git(['cat-file', 'blob', blob_sha], cwd=repo_dir, raw=True)
        if b'\x00' in raw_blob[:BINARY_SNIFF_BYTES]:
            result[rel_path] = {
                'exists': True,
                'regular_text': False,
                'line_count': None,
                'detail': 'path is not a regular text file',
            }
            continue
        result[rel_path] = {
            'exists': True,
            'regular_text': True,
            'line_count': _count_blob_lines(raw_blob),
            'detail': None,
        }
    return result


def _extract_merge_base_diff(repo_dir, target_repo_dir, target_ref, commit):
    """Compute ``changed_files`` from ``merge_base(target_ref, commit)..commit`` with ``--no-renames``."""
    target_sha = run_confined_git(
        ['rev-parse', '--verify', '%s^{commit}' % target_ref],
        cwd=target_repo_dir,
    ).strip()
    merge_base = run_confined_git(['merge-base', target_sha, commit], cwd=repo_dir).strip()
    raw = run_confined_git(
        ['diff', '--name-only', '-z', '--no-renames', merge_base, commit],
        cwd=repo_dir,
        raw=True,
    )
    files = []
    for item in raw.split(b'\x00'):
        if not item:
            continue
        decoded = item.decode('utf-8', errors='replace')
        if decoded not in files:
            files.append(decoded)
    files.sort()
    return files, merge_base


def _resolve_git_objects_dir(repo_path):
    p = Path(repo_path)
    st = _lstat_or_none(p)
    if st is None or stat.S_ISLNK(st.st_mode) or _is_reparse_point(st) or not stat.S_ISDIR(st.st_mode):
        return None
    direct = p / 'objects'
    dst = _lstat_or_none(direct)
    if dst is not None and not stat.S_ISLNK(dst.st_mode) and stat.S_ISDIR(dst.st_mode):
        return (p, direct)
    dotgit = p / '.git'
    gst = _lstat_or_none(dotgit)
    if gst is not None and not stat.S_ISLNK(gst.st_mode) and stat.S_ISDIR(gst.st_mode):
        objs = dotgit / 'objects'
        ost = _lstat_or_none(objs)
        if ost is not None and not stat.S_ISLNK(ost.st_mode) and stat.S_ISDIR(ost.st_mode):
            return (dotgit, objs)
    return None


def inspect_delivery_git(
    contribution,
    *,
    delivery_roots=(),
    serving_repo=None,
    target_ref='refs/heads/main',
    max_bundle_bytes=DELIVERY_SERVE_MAX_BYTES,
    lock_dir=None,
    needed_paths=(),
    lock_wait_seconds=BUNDLE_LOCK_WAIT_SECONDS,
):
    """Inspect a bundle or branch delivery outside ``.coordination.lock``.

    NEVER uses ``contribution.base_commit`` for ``changed_files``: without ``serving_repo``
    (or a later verifier commit observation), ``diff_status`` is ``'diff_unverified'``.
    """
    unavailable = {
        'tree_available': False,
        'tree_source': None,
        'tree_files': {},
        'diff_status': 'diff_unverified',
        'diff_source': None,
        'changed_files': None,
        'merge_base': None,
        'lock_timed_out': False,
    }
    if not isinstance(contribution, dict):
        return unavailable
    commit = str(contribution.get('commit') or '').strip()
    if not COMMIT_RE.fullmatch(commit):
        return unavailable
    delivery = contribution.get('delivery')
    if not isinstance(delivery, dict):
        return unavailable
    kind = delivery.get('kind')
    dedup_paths = sorted(set(needed_paths or ()))

    serving_info = _resolve_git_objects_dir(serving_repo) if serving_repo else None

    if kind == 'bundle':
        recorded_path = delivery.get('path')
        expected_sha256 = delivery.get('sha256')
        if not isinstance(recorded_path, str) or not isinstance(expected_sha256, str):
            return unavailable
        try:
            with held_bundle_fetch_lock(lock_dir, wait_seconds=lock_wait_seconds):
                resolved_path, handle_fd, size = resolve_delivery_bundle_path(
                    recorded_path, delivery_roots
                )
                try:
                    verify_served_bundle(
                        resolved_path,
                        handle_fd,
                        size,
                        expected_sha256,
                        commit,
                        max_bytes=max_bundle_bytes,
                    )
                finally:
                    try:
                        os.close(handle_fd)
                    except OSError:
                        pass
                with tempfile.TemporaryDirectory(prefix='std-bundle-') as tmpdir:
                    run_confined_git(['init', '--bare', '--quiet', tmpdir])
                    if serving_info is not None:
                        alt_file = Path(tmpdir) / 'objects' / 'info' / 'alternates'
                        alt_file.parent.mkdir(parents=True, exist_ok=True)
                        alt_file.write_text(str(serving_info[1]) + '\n', encoding='utf-8')
                    run_confined_git(
                        ['fetch', '--no-tags', '--', resolved_path, commit],
                        cwd=tmpdir,
                    )
                    tree_files = (
                        _inspect_repo_tree_paths(tmpdir, commit, dedup_paths)
                        if dedup_paths
                        else {}
                    )
                    changed_files = None
                    diff_status = 'diff_unverified'
                    diff_source = None
                    merge_base = None
                    if serving_info is not None:
                        try:
                            changed_files, merge_base = _extract_merge_base_diff(
                                tmpdir, serving_info[0], target_ref, commit
                            )
                            diff_status = 'verified'
                            diff_source = 'serving_repo'
                        except ValueError:
                            changed_files = None
                            diff_status = 'diff_unverified'
                    return {
                        'tree_available': True,
                        'tree_source': 'bundle',
                        'tree_files': tree_files,
                        'diff_status': diff_status,
                        'diff_source': diff_source,
                        'changed_files': changed_files,
                        'merge_base': merge_base,
                        'lock_timed_out': False,
                    }
        except BundleLockTimeoutError:
            out = dict(unavailable)
            out['lock_timed_out'] = True
            return out
        except (
            DeliveryUnavailableError,
            DeliveryTooLargeError,
            DeliveryCorruptError,
            ValueError,
            OSError,
        ):
            return unavailable

    if kind == 'remote' and serving_info is not None:
        repo_git_dir = serving_info[0]
        try:
            run_confined_git(['cat-file', '-e', '%s^{commit}' % commit], cwd=repo_git_dir)
            tree_files = (
                _inspect_repo_tree_paths(repo_git_dir, commit, dedup_paths)
                if dedup_paths
                else {}
            )
            changed_files, merge_base = _extract_merge_base_diff(
                repo_git_dir, repo_git_dir, target_ref, commit
            )
            return {
                'tree_available': True,
                'tree_source': 'serving_repo',
                'tree_files': tree_files,
                'diff_status': 'verified',
                'diff_source': 'serving_repo',
                'changed_files': changed_files,
                'merge_base': merge_base,
                'lock_timed_out': False,
            }
        except ValueError:
            return unavailable

    return unavailable


def inspect_local_repo_commit(repo_path, commit, *, target_ref='refs/heads/main'):
    """Local CLI helper (``standards inspect-commit``) for CI jobs or verifiers."""
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise ValueError('commit must be a 40- or 64-character hex SHA')
    info = _resolve_git_objects_dir(repo_path)
    if info is None:
        raise ValueError('Repository %s is not a valid git directory' % repo_path)
    git_dir = info[0]
    resolved_commit = run_confined_git(
        ['rev-parse', '--verify', '%s^{commit}' % commit], cwd=git_dir
    ).strip()
    changed_files, merge_base = _extract_merge_base_diff(
        git_dir, git_dir, target_ref, resolved_commit
    )
    # Build tree_files summary for changed regular text files so citations to touched files resolve.
    tree_map = _inspect_repo_tree_paths(git_dir, resolved_commit, changed_files)
    compact_tree = {}
    for path, meta in tree_map.items():
        if meta.get('exists'):
            compact_tree[path] = {
                'regular_text': bool(meta.get('regular_text')),
                'line_count': meta.get('line_count'),
            }
    return {
        'schema_version': SCHEMA_VERSION,
        'commit': resolved_commit.lower(),
        'target_ref': target_ref,
        'merge_base': merge_base.lower(),
        'changed_files': changed_files,
        'tree_files': compact_tree,
        'ci_checks': [],
    }


def parse_citation(citation, rule_id, commit):
    """Parse ``path:line`` or ``path:start-end``; raise StandardsRefusal (#10) if malformed."""
    if not isinstance(citation, str) or not citation.strip() or '\x00' in citation:
        raise StandardsRefusal(
            refusal_citation_unresolved(str(citation), rule_id, commit, 'invalid file:line citation syntax'),
            status=422,
        )
    raw = citation.strip()
    m = CITATION_RE.fullmatch(raw)
    if not m:
        raise StandardsRefusal(
            refusal_citation_unresolved(raw, rule_id, commit, 'invalid file:line citation syntax'),
            status=422,
        )
    raw_path = m.group('path')
    if '\\' in raw_path or raw_path.startswith('/'):
        raise StandardsRefusal(
            refusal_citation_unresolved(raw, rule_id, commit, 'invalid file:line citation syntax'),
            status=422,
        )
    segments = raw_path.split('/')
    if any(seg in ('', '.', '..') for seg in segments):
        raise StandardsRefusal(
            refusal_citation_unresolved(raw, rule_id, commit, 'invalid file:line citation syntax'),
            status=422,
        )
    norm_path = '/'.join(segments)
    start_line = int(m.group('start'))
    end_line = int(m.group('end')) if m.group('end') is not None else start_line
    return raw, norm_path, start_line, end_line


def collect_citation_paths_from_checks(checks):
    paths = []
    seen = set()
    if not isinstance(checks, list):
        return paths
    for chk in checks[:CHECKS_MAX]:
        if not isinstance(chk, dict):
            continue
        ev = chk.get('evidence')
        if not isinstance(ev, dict):
            continue
        cites = ev.get('citations')
        if not isinstance(cites, list):
            continue
        for c in cites[:CITATIONS_MAX]:
            if isinstance(c, str) and ':' in c:
                m = CITATION_RE.fullmatch(c.strip())
                if m:
                    p = m.group('path')
                    if '\\' not in p and not p.startswith('/'):
                        segs = p.split('/')
                        if not any(s in ('', '.', '..') for s in segs):
                            norm = '/'.join(segs)
                            if norm not in seen:
                                seen.add(norm)
                                paths.append(norm)
    return paths


def validate_citation_against_tree(citation, rule_id, commit, tree_available, tree_files):
    """Validate one ``file:line`` citation against ``commit``'s resolved tree."""
    raw, norm_path, start_line, end_line = parse_citation(citation, rule_id, commit)
    if not tree_available:
        raise StandardsRefusal(
            refusal_citation_unresolved(
                raw,
                rule_id,
                commit,
                'delivered commit tree is not available to validate file:line citations',
            ),
            status=422,
        )
    meta = (tree_files or {}).get(norm_path)
    if meta is None or meta.get('exists') is False:
        raise StandardsRefusal(
            refusal_citation_unresolved(raw, rule_id, commit, 'file not found in commit tree'),
            status=422,
        )
    if not meta.get('regular_text'):
        raise StandardsRefusal(
            refusal_citation_unresolved(raw, rule_id, commit, 'path is not a regular text file'),
            status=422,
        )
    line_count = meta.get('line_count')
    if type(line_count) is not int or line_count < 0:
        raise StandardsRefusal(
            refusal_citation_unresolved(raw, rule_id, commit, 'path is not a regular text file'),
            status=422,
        )
    if start_line < 1 or start_line > line_count:
        raise StandardsRefusal(
            refusal_citation_unresolved(
                raw,
                rule_id,
                commit,
                'line %d is out of range (file has %d lines)' % (start_line, line_count),
            ),
            status=422,
        )
    if end_line < start_line or end_line > line_count:
        bad_line = end_line
        raise StandardsRefusal(
            refusal_citation_unresolved(
                raw,
                rule_id,
                commit,
                'line %d is out of range (file has %d lines)' % (bad_line, line_count),
            ),
            status=422,
        )
    return {
        'citation': raw,
        'path': norm_path,
        'start_line': start_line,
        'end_line': end_line,
    }


# -- Verifier Commit & CI Observations (R3, R4, R6) ---------------------------

def _collect_commit_participants(rows_or_tasks, commit_lower, operators=None, journal=None, *, task_id=None):
    """Collect ``(authors, assignees, reviewers, approvers)`` across tasks referencing ``commit_lower`` or ``task_id``."""
    import review_recommendations

    checkpoint_prefix = 'Kind: task-checkpoint-v1\n'
    authors, assignees, reviewers, approvers = set(), set(), set(), set()
    for issue in rows_or_tasks or ():
        if not isinstance(issue, dict) or issue.get('issue_type') == 'event':
            continue
        if task_id is not None and str(issue.get('id') or '') == str(task_id):
            if issue.get('assignee'):
                assignees.add(str(issue['assignee']))
        for cp in issue.get('checkpoints') or ():
            if isinstance(cp, dict) and str(cp.get('source_commit') or '').lower() == commit_lower:
                if issue.get('assignee'):
                    assignees.add(str(issue['assignee']))
                cp_actor = cp.get('actor') or cp.get('author')
                if cp_actor:
                    authors.add(str(cp_actor))
        for cmt in issue.get('comments') or ():
            if not isinstance(cmt, dict):
                continue
            ctxt = cmt.get('text')
            if isinstance(ctxt, str) and ctxt.startswith(checkpoint_prefix):
                try:
                    cp_doc = parse_json(ctxt[len(checkpoint_prefix):])
                except ValueError:
                    cp_doc = None
                if isinstance(cp_doc, dict) and str(cp_doc.get('source_commit') or '').lower() == commit_lower:
                    if issue.get('assignee'):
                        assignees.add(str(issue['assignee']))
                    if cmt.get('author'):
                        authors.add(str(cmt['author']))
        # Handle both canonical bd rows and InProcessBackend task dicts
        if 'review_records' in issue and isinstance(issue.get('review_records'), list):
            recs_list = issue['review_records']
            for pos_idx, rec in enumerate(recs_list):
                if not isinstance(rec, dict) or rec.get('kind') != 'contribution':
                    continue
                if str(rec.get('commit') or '').lower() == commit_lower:
                    actor_val = rec.get('actor') or rec.get('author')
                    if actor_val:
                        authors.add(str(actor_val))
                    if issue.get('assignee'):
                        assignees.add(str(issue['assignee']))
                    cid = rec.get('id')
                    decided_pos = -1
                    for other_idx, other in enumerate(recs_list):
                        if not isinstance(other, dict) or other.get('contribution_id') != cid:
                            continue
                        kind = other.get('kind') or other.get('operation')
                        oactor = other.get('actor') or other.get('author')
                        if kind == 'request-changes':
                            decided_pos = max(decided_pos, other_idx)
                            if oactor:
                                reviewers.add(str(oactor))
                        elif kind == 'approve':
                            decided_pos = max(decided_pos, other_idx)
                            if oactor:
                                approvers.add(str(oactor))
                    for rrec in issue.get('recommendations') or ():
                        if not isinstance(rrec, dict):
                            continue
                        if (
                            rrec.get('contribution_id') == cid
                            and str(rrec.get('commit') or '').lower() == commit_lower
                            and int(rrec.get('after', 0)) > decided_pos
                            and rrec.get('actor')
                        ):
                            reviewers.add(str(rrec['actor']))
            continue
        if 'contributions' in issue and isinstance(issue.get('contributions'), list):
            for rec in issue['contributions']:
                if not isinstance(rec, dict):
                    continue
                if str(rec.get('commit') or '').lower() == commit_lower:
                    if rec.get('author'):
                        authors.add(str(rec['author']))
                    if issue.get('assignee'):
                        assignees.add(str(issue['assignee']))
                    cid = rec.get('id')
                    for other in issue['contributions']:
                        if not isinstance(other, dict) or other.get('contribution_id') != cid:
                            continue
                        op = other.get('operation')
                        if op == 'request-changes' and other.get('actor'):
                            reviewers.add(str(other['actor']))
                        elif op == 'approve' and other.get('actor'):
                            approvers.add(str(other['actor']))
            for rrec in issue.get('recommendations') or ():
                if (
                    isinstance(rrec, dict)
                    and str(rrec.get('commit') or '').lower() == commit_lower
                    and rrec.get('actor')
                ):
                    reviewers.add(str(rrec['actor']))
            continue

        try:
            ordered, voids, invalid, refused, positions, reverts, invalid_reverts = (
                review_workflow.history(issue, operators, journal)
            )
            state = review_workflow.projection(
                ordered,
                voids,
                invalid,
                refused,
                positions,
                reverts,
                invalid_reverts,
                closed=issue.get('status') == 'closed',
            )
        except Exception:
            continue
        matching_cids = set()
        for payload, comment in ordered:
            if payload.get('operation') == 'contribute':
                if str(payload.get('commit') or '').lower() == commit_lower:
                    matching_cids.add(str(comment['id']))
                    if comment.get('author'):
                        authors.add(str(comment['author']))
                    if issue.get('assignee'):
                        assignees.add(str(issue['assignee']))
        if matching_cids:
            for payload, comment in ordered:
                if payload.get('contribution') in matching_cids:
                    if payload.get('operation') == 'request-changes' and comment.get('author'):
                        reviewers.add(str(comment['author']))
                    elif payload.get('operation') == 'approve' and comment.get('author'):
                        approvers.add(str(comment['author']))
            for _, rpayload, rcomment in review_recommendations._standing(issue, state):
                if (
                    rpayload.get('contribution') in matching_cids
                    and str(rpayload.get('commit') or '').lower() == commit_lower
                    and rcomment.get('author')
                ):
                    reviewers.add(str(rcomment['author']))
    return authors, assignees, reviewers, approvers


def _normalize_commit_observations_list(obs_doc):
    """Return an append-only list of per-recorder observation entries from ``obs_doc``."""
    if not isinstance(obs_doc, dict):
        return []
    raw_list = obs_doc.get('observations')
    if isinstance(raw_list, list):
        out = []
        for entry in raw_list:
            if isinstance(entry, dict):
                out.append(dict(entry))
        return out
    # Legacy single-object observation fallback
    has_any = (
        obs_doc.get('changed_files') is not None
        or obs_doc.get('tree_files') is not None
        or obs_doc.get('ci_checks') is not None
    )
    if not has_any:
        return []
    rec_by = obs_doc.get('recorded_by')
    rec_uid = obs_doc.get('recorded_by_user_id')
    rec_prin = obs_doc.get('recorded_by_principal')
    rec_at = obs_doc.get('recorded_at')
    ci_items = None
    if isinstance(obs_doc.get('ci_checks'), list):
        ci_items = []
        for c in obs_doc['ci_checks']:
            if isinstance(c, dict):
                stamped = dict(c)
                stamped.setdefault('recorded_by', rec_by)
                stamped.setdefault('recorded_by_user_id', rec_uid)
                stamped.setdefault('recorded_by_principal', rec_prin)
                stamped.setdefault('recorded_at', rec_at)
                ci_items.append(stamped)
    return [{
        'changed_files': list(obs_doc['changed_files']) if isinstance(obs_doc.get('changed_files'), list) else None,
        'tree_files': dict(obs_doc['tree_files']) if isinstance(obs_doc.get('tree_files'), dict) else None,
        'ci_checks': ci_items,
        'recorded_by': rec_by,
        'recorded_by_user_id': rec_uid,
        'recorded_by_principal': rec_prin,
        'recorded_at': rec_at,
    }]


def _entry_matches_party(entry_or_item, exclude_party):
    if exclude_party is None or not isinstance(entry_or_item, dict):
        return False
    for k in ('recorded_by', 'recorded_by_user_id', 'recorded_by_principal'):
        cand = entry_or_item.get(k)
        if isinstance(cand, str) and cand and exclude_party(cand):
            return True
    return False


def _fold_commit_observations(commit_lower, observations, *, exclude_party=None):
    """Fold ``observations`` preserving per-part provenance.

    When ``exclude_party`` is provided, exclude only observation parts that could help an
    excluded party pass (``changed_files``, ``tree_files``, and passing CI items); recorded
    CI failures always count with their recorder regardless of party.
    """
    kept_entries = []
    changed_files = None
    changed_files_recorded_by = None
    changed_files_recorded_at = None
    merged_tree_files = None
    tree_files_recorded_by = None
    tree_files_recorders = {}
    ci_order = []
    ci_by_key = {}
    latest_entry = None

    for entry in observations or ():
        if not isinstance(entry, dict):
            continue
        entry_excluded = _entry_matches_party(entry, exclude_party)
        rec_by = entry.get('recorded_by')
        rec_uid = entry.get('recorded_by_user_id')
        rec_prin = entry.get('recorded_by_principal')
        rec_at = entry.get('recorded_at')

        if not entry_excluded and isinstance(entry.get('changed_files'), list):
            changed_files = list(entry['changed_files'])
            changed_files_recorded_by = rec_by
            changed_files_recorded_at = rec_at

        if not entry_excluded and isinstance(entry.get('tree_files'), dict):
            if merged_tree_files is None:
                merged_tree_files = {}
            for p, meta in entry['tree_files'].items():
                if isinstance(meta, dict):
                    merged_tree_files[p] = dict(meta)
                    tree_files_recorders[p] = rec_by
            tree_files_recorded_by = rec_by

        retained_ci = []
        if isinstance(entry.get('ci_checks'), list):
            for raw_item in entry['ci_checks']:
                if not isinstance(raw_item, dict):
                    continue
                item = dict(raw_item)
                item.setdefault('recorded_by', rec_by)
                item.setdefault('recorded_by_user_id', rec_uid)
                item.setdefault('recorded_by_principal', rec_prin)
                item.setdefault('recorded_at', rec_at)
                is_fail = (item.get('exit_code') != 0) or (item.get('status') == 'fail')
                item_excluded = entry_excluded or _entry_matches_party(item, exclude_party)
                if item_excluded and not is_fail:
                    continue
                key = (
                    ('rule', item['rule_id'])
                    if item.get('rule_id')
                    else ('cmd', item.get('tool'), item.get('command'))
                )
                if key in ci_by_key:
                    ci_order.remove(key)
                ci_order.append(key)
                ci_by_key[key] = item
                retained_ci.append(item)

        if entry_excluded:
            if retained_ci:
                sanitized = {
                    'changed_files': None,
                    'tree_files': None,
                    'ci_checks': retained_ci,
                    'recorded_by': rec_by,
                    'recorded_by_user_id': rec_uid,
                    'recorded_by_principal': rec_prin,
                    'recorded_at': rec_at,
                }
                if entry.get('reserved_fail'):
                    sanitized['reserved_fail'] = True
                kept_entries.append(sanitized)
                latest_entry = sanitized
        else:
            kept_entries.append(entry)
            latest_entry = entry

    if not kept_entries:
        return None

    return {
        'schema_version': SCHEMA_VERSION,
        'commit': commit_lower,
        'changed_files': changed_files,
        'changed_files_recorded_by': changed_files_recorded_by,
        'changed_files_recorded_at': changed_files_recorded_at,
        'tree_files': merged_tree_files,
        'tree_files_recorded_by': tree_files_recorded_by,
        'tree_files_recorders': tree_files_recorders,
        'ci_checks': [ci_by_key[k] for k in ci_order],
        'recorded_by': latest_entry.get('recorded_by'),
        'recorded_by_user_id': latest_entry.get('recorded_by_user_id'),
        'recorded_by_principal': latest_entry.get('recorded_by_principal'),
        'recorded_at': latest_entry.get('recorded_at'),
        'observations': kept_entries,
    }


def _collect_task_contribution_participants(issue, contribution):
    """Collect author, assignee, reviewer, and approver actors for one task/contribution."""
    participants = set()
    cid = None
    commit_lower = None
    if isinstance(contribution, dict):
        for k in ('author', 'actor'):
            if isinstance(contribution.get(k), str) and contribution[k]:
                participants.add(contribution[k])
        cid_raw = contribution.get('comment_id') or contribution.get('id')
        if cid_raw:
            cid = str(cid_raw)
        if contribution.get('commit'):
            commit_lower = str(contribution['commit']).lower()
    if isinstance(issue, dict):
        if isinstance(issue.get('assignee'), str) and issue['assignee']:
            participants.add(issue['assignee'])
        import review_recommendations as _rec_mod
        import review_workflow as _rw_mod

        for comment in issue.get('comments') or ():
            if not isinstance(comment, dict):
                continue
            c_author = comment.get('author')
            raw = comment.get('text')
            if not isinstance(raw, str) or not isinstance(c_author, str) or not c_author:
                continue
            if raw.startswith(_rw_mod.PREFIX):
                try:
                    rec = parse_json(raw[len(_rw_mod.PREFIX):])
                except ValueError:
                    rec = None
                if isinstance(rec, dict):
                    op = rec.get('operation')
                    if op == 'contribute' and (
                        (cid and str(comment.get('id') or '') == cid)
                        or (commit_lower and str(rec.get('commit') or '').lower() == commit_lower)
                    ):
                        participants.add(c_author)
                    elif op in ('request-changes', 'approve') and cid and str(rec.get('contribution') or '') == cid:
                        participants.add(c_author)
            elif raw.startswith(_rec_mod.PREFIX):
                try:
                    rec = parse_json(raw[len(_rec_mod.PREFIX):])
                except ValueError:
                    rec = None
                if isinstance(rec, dict) and cid and str(rec.get('contribution') or '') == cid:
                    participants.add(c_author)
        for rec in (issue.get('review_records') or issue.get('contributions') or ()):
            if not isinstance(rec, dict):
                continue
            r_actor = rec.get('actor') or rec.get('author')
            if not isinstance(r_actor, str) or not r_actor:
                continue
            r_kind = rec.get('kind') or rec.get('operation')
            if r_kind in ('contribution', 'contribute') and (
                (cid and str(rec.get('id') or '') == cid)
                or (commit_lower and str(rec.get('commit') or '').lower() == commit_lower)
            ):
                participants.add(r_actor)
            elif r_kind in ('request-changes', 'approve') and cid and str(rec.get('contribution_id') or rec.get('contribution') or '') == cid:
                participants.add(r_actor)
        for rec in issue.get('recommendations') or ():
            if not isinstance(rec, dict):
                continue
            r_actor = rec.get('actor') or rec.get('author')
            if isinstance(r_actor, str) and r_actor and cid and str(rec.get('contribution_id') or rec.get('contribution') or '') == cid:
                participants.add(r_actor)
    return participants


def filter_commit_observation_for_evaluation(
    obs,
    *,
    issue=None,
    contribution=None,
    extra_actors=(),
    same_party_with_actor=None,
    same_party_pair=None,
):
    """Fold ``obs`` excluding any observation part recorded by author, assignee, reviewer, or approver party."""
    if not isinstance(obs, dict):
        return None
    commit_lower = str(obs.get('commit') or '').lower()
    participants = _collect_task_contribution_participants(issue, contribution)
    for act_val in extra_actors or ():
        if isinstance(act_val, str) and act_val:
            participants.add(act_val)

    def _is_excluded(cand):
        if not isinstance(cand, str) or not cand:
            return False
        if callable(same_party_with_actor) and same_party_with_actor(cand):
            return True
        cand_key = author_key(cand)
        for part in participants:
            if cand_key and cand_key == author_key(part):
                return True
            if callable(same_party_pair) and same_party_pair(cand, part):
                return True
        return False

    return _fold_commit_observations(
        commit_lower,
        _normalize_commit_observations_list(obs),
        exclude_party=_is_excluded,
    )


def _is_compact_fail_only_entry(entry):
    """True when ``entry`` carries only failing CI items (no file lists) and fits in 16 KiB."""
    if not isinstance(entry, dict):
        return False
    if entry.get('changed_files') is not None or entry.get('tree_files') is not None:
        return False
    ci_list = entry.get('ci_checks')
    if not isinstance(ci_list, list) or not ci_list:
        return False
    for item in ci_list:
        if not isinstance(item, dict):
            return False
        if not ((item.get('exit_code') != 0) or (item.get('status') == 'fail')):
            return False
    return len(canonical_bytes(entry)) <= COMMIT_FAIL_ONLY_ENTRY_MAX_BYTES


def record_commit_observation(
    store,
    commit,
    payload,
    *,
    actor,
    recorded_by_user_id=None,
    recorded_by_principal=None,
    is_authorized_recorder,
    same_party_checker,
    rows_or_tasks=(),
    operators=None,
    journal=None,
    now=None,
):
    """Record verifier/CI commit diff, tree metadata, and mechanical CI results (R3, R4, R6)."""
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise StandardsRefusal('Invalid commit SHA %r' % (commit,), status=422)
    commit_lower = commit.lower()
    if not is_authorized_recorder:
        raise StandardsRefusal(refusal_commit_observation_authority(commit_lower), status=403)
    if not isinstance(payload, dict):
        raise StandardsRefusal('Commit observation payload must be a JSON object', status=422)
    if payload.get('commit') is not None and str(payload['commit']).lower() != commit_lower:
        raise StandardsRefusal('Payload commit does not match URL/CLI commit %s' % commit_lower, status=422)

    allowed = {
        'schema_version',
        'commit',
        'task_id',
        'target_ref',
        'merge_base',
        'changed_files',
        'tree_files',
        'ci_checks',
    }
    extra = sorted(set(payload) - allowed)
    if extra:
        raise StandardsRefusal('Unknown commit observation field(s): %s' % ', '.join(extra), status=422)

    task_id_val = payload.get('task_id')
    if task_id_val is not None:
        if not isinstance(task_id_val, str) or not task_id_val.strip():
            raise StandardsRefusal('task_id must be a non-empty task id string', status=422)
        task_id_val = task_id_val.strip()
        try:
            identity(task_id_val)
        except ValueError as exc:
            raise StandardsRefusal('Invalid task_id: %s' % exc, status=422) from None
        if rows_or_tasks:
            matched_tasks = [
                r for r in rows_or_tasks
                if isinstance(r, dict) and r.get('issue_type') != 'event' and str(r.get('id') or '') == task_id_val
            ]
            if not matched_tasks:
                raise StandardsRefusal('Task %s not found.' % task_id_val, status=404)

    authors, assignees, reviewers, approvers = _collect_commit_participants(
        rows_or_tasks, commit_lower, operators=operators, journal=journal, task_id=task_id_val
    )
    for other_actor in (authors | assignees | reviewers | approvers):
        if same_party_checker(other_actor):
            raise StandardsRefusal(refusal_commit_observation_authority(commit_lower), status=403)

    changed_files = None
    if payload.get('changed_files') is not None:
        cf_raw = payload['changed_files']
        if not isinstance(cf_raw, list) or len(cf_raw) > 10000:
            raise StandardsRefusal('changed_files must be a list of repo-relative paths', status=422)
        changed_files = []
        for p in cf_raw:
            norm = _normalize_repo_rel_pattern(p, 'changed_files path')
            if norm not in changed_files:
                changed_files.append(norm)
        changed_files.sort()

    tree_files = None
    if payload.get('tree_files') is not None:
        tf_raw = payload['tree_files']
        if not isinstance(tf_raw, dict) or len(tf_raw) > 10000:
            raise StandardsRefusal('tree_files must be a mapping of path -> metadata', status=422)
        tree_files = {}
        for k, meta in tf_raw.items():
            norm = _normalize_repo_rel_pattern(k, 'tree_files path')
            if not isinstance(meta, dict):
                raise StandardsRefusal('tree_files[%s] must be an object' % norm, status=422)
            reg_txt = bool(meta.get('regular_text', True))
            lc = meta.get('line_count')
            if reg_txt and (type(lc) is not int or lc < 0):
                raise StandardsRefusal(
                    'tree_files[%s] regular_text requires non-negative integer line_count' % norm,
                    status=422,
                )
            tree_files[norm] = {
                'exists': bool(meta.get('exists', True)),
                'regular_text': reg_txt,
                'line_count': lc if reg_txt else None,
            }

    ts = utc_now_iso(now)
    ci_checks = None
    if payload.get('ci_checks') is not None:
        ci_raw = payload['ci_checks']
        if not isinstance(ci_raw, list) or len(ci_raw) > CHECKS_MAX:
            raise StandardsRefusal('ci_checks must be a list of CI check observations', status=422)
        ci_checks = []
        for item in ci_raw:
            if not isinstance(item, dict):
                raise StandardsRefusal('Each ci_checks entry must be an object', status=422)
            rid = item.get('rule_id')
            if rid is not None and (not isinstance(rid, str) or not RULE_ID_RE.fullmatch(rid)):
                raise StandardsRefusal('Invalid ci_checks rule_id %r' % (rid,), status=422)
            tool = _check_clean_text(item.get('tool'), 'ci_checks.tool', TOOL_NAME_MAX)
            cmd = _check_clean_text(item.get('command'), 'ci_checks.command', COMMAND_MAX)
            exit_code = item.get('exit_code')
            if type(exit_code) is not int:
                raise StandardsRefusal('ci_checks.exit_code must be an integer', status=422)
            run_id = (
                _check_clean_text(item['run_id'], 'ci_checks.run_id', 160)
                if item.get('run_id') is not None
                else None
            )
            summary = (
                _check_clean_text(item['summary'], 'ci_checks.summary', 400)
                if item.get('summary') is not None
                else None
            )
            ci_checks.append({
                'rule_id': rid,
                'tool': tool,
                'command': cmd,
                'exit_code': exit_code,
                'status': 'pass' if exit_code == 0 else 'fail',
                'run_id': run_id,
                'summary': summary,
                'recorded_by': actor,
                'recorded_by_user_id': recorded_by_user_id,
                'recorded_by_principal': recorded_by_principal,
                'recorded_at': ts,
            })

    try:
        existing = store.read_commit_observation(commit_lower)
    except StandardsDamagedError as exc:
        raise StandardsRefusal(
            'Commit standards observation in project %s is damaged (%s); repair or restore .standards/ before recording.'
            % (store.project_id, exc),
            status=409,
        ) from None
    prev_observations = _normalize_commit_observations_list(existing)
    cur_keys = {
        author_key(x)
        for x in (actor, recorded_by_user_id, recorded_by_principal)
        if isinstance(x, str) and x
    }
    cur_keys.discard('')
    ordinary_prev = []
    reserved_fail_prev = []
    party_ordinary_count = 0
    party_ordinary_bytes = 0
    party_reserved_fail_count = 0
    for prev_entry in prev_observations:
        if not isinstance(prev_entry, dict):
            continue
        same_p = _entry_matches_party(prev_entry, same_party_checker) or any(
            isinstance(prev_entry.get(f), str)
            and prev_entry.get(f)
            and author_key(prev_entry[f]) in cur_keys
            for f in ('recorded_by', 'recorded_by_user_id', 'recorded_by_principal')
        )
        if prev_entry.get('reserved_fail'):
            reserved_fail_prev.append(prev_entry)
            if same_p:
                party_reserved_fail_count += 1
        else:
            ordinary_prev.append(prev_entry)
            if same_p:
                party_ordinary_count += 1
                party_ordinary_bytes += len(canonical_bytes(prev_entry))

    new_entry = {
        'changed_files': changed_files,
        'tree_files': tree_files,
        'ci_checks': ci_checks,
        'recorded_by': actor,
        'recorded_by_user_id': recorded_by_user_id,
        'recorded_by_principal': recorded_by_principal,
        'recorded_at': ts,
    }

    if (
        _is_compact_fail_only_entry(new_entry)
        and party_reserved_fail_count < COMMIT_FAIL_ALLOWANCE_PER_PARTY_MAX
        and len(reserved_fail_prev) < COMMIT_FAIL_ALLOWANCE_TOTAL_MAX
    ):
        reserved_entry = dict(new_entry, reserved_fail=True)
        obs = _fold_commit_observations(commit_lower, prev_observations + [reserved_entry])
        if len(canonical_bytes(obs)) + 1 > MAX_COMMIT_OBSERVATION_FILE_BYTES:
            raise StandardsRefusal(
                refusal_commit_observation_size_cap(commit_lower, MAX_COMMIT_OBSERVATION_FILE_BYTES),
                status=409,
            )
        store.write_commit_observation(commit_lower, obs)
        return obs

    ordinary_refusal = None
    if len(ordinary_prev) >= COMMIT_OBSERVATIONS_MAX:
        ordinary_refusal = refusal_commit_observation_cap(commit_lower, COMMIT_OBSERVATIONS_MAX)
    elif party_ordinary_count >= COMMIT_OBSERVATIONS_PER_RECORDER_MAX:
        ordinary_refusal = refusal_commit_observation_recorder_cap(
            commit_lower, COMMIT_OBSERVATIONS_PER_RECORDER_MAX
        )
    elif (
        party_ordinary_bytes + len(canonical_bytes(new_entry))
        > COMMIT_OBSERVATIONS_PER_PARTY_MAX_BYTES
    ):
        ordinary_refusal = refusal_commit_observation_party_bytes_cap(
            commit_lower, COMMIT_OBSERVATIONS_PER_PARTY_MAX_BYTES
        )
    else:
        ordinary_obs = _fold_commit_observations(commit_lower, ordinary_prev + [new_entry])
        if len(canonical_bytes(ordinary_obs)) + 1 > MAX_COMMIT_OBSERVATION_BYTES:
            ordinary_refusal = refusal_commit_observation_size_cap(
                commit_lower, MAX_COMMIT_OBSERVATION_BYTES
            )

    if ordinary_refusal is not None:
        if (
            _is_compact_fail_only_entry(new_entry)
            and party_reserved_fail_count >= COMMIT_FAIL_ALLOWANCE_PER_PARTY_MAX
        ):
            raise StandardsRefusal(
                refusal_commit_fail_allowance_cap(
                    commit_lower, COMMIT_FAIL_ALLOWANCE_PER_PARTY_MAX
                ),
                status=409,
            )
        raise StandardsRefusal(ordinary_refusal, status=409)

    obs = _fold_commit_observations(commit_lower, prev_observations + [new_entry])
    if len(canonical_bytes(obs)) + 1 > MAX_COMMIT_OBSERVATION_FILE_BYTES:
        raise StandardsRefusal(
            refusal_commit_observation_size_cap(commit_lower, MAX_COMMIT_OBSERVATION_FILE_BYTES),
            status=409,
        )
    store.write_commit_observation(commit_lower, obs)
    return obs


# -- R4 Applicability Matcher -------------------------------------------------

def language_for_path(rel_path):
    if not isinstance(rel_path, str) or not rel_path:
        return None
    base = posixpath.basename(rel_path).lower()
    if base in BASENAME_LANGUAGES:
        return BASENAME_LANGUAGES[base]
    for ext, lang in EXTENSION_LANGUAGES.items():
        if base.endswith(ext) and len(base) > len(ext):
            return lang
    return None


def _path_matches_pattern(rel_path, pattern):
    """Iterative segment-DP glob matcher supporting ``*``, ``?``, ``[...]``, and ``**`` in O(P*S)."""
    path_segs = [s for s in rel_path.split('/') if s and s != '.']
    pat_segs = [s for s in pattern.split('/') if s and s != '.']
    m, n = len(pat_segs), len(path_segs)
    dp = [[False] * (n + 1) for _ in range(m + 1)]
    dp[0][0] = True
    for i in range(1, m + 1):
        if pat_segs[i - 1] == '**':
            dp[i][0] = dp[i - 1][0]
    for i in range(1, m + 1):
        pseg = pat_segs[i - 1]
        for j in range(1, n + 1):
            if pseg == '**':
                dp[i][j] = dp[i - 1][j] or dp[i][j - 1]
            else:
                dp[i][j] = dp[i - 1][j - 1] and fnmatch.fnmatchcase(path_segs[j - 1], pseg)
    return dp[m][n]


def evaluate_applicability(effective_rules, issue, *, changed_files=None, diff_status='diff_unverified'):
    """Evaluate R4 applicability for every effective rule.

    Each entry gets ``match_status`` in ``('applicable', 'possibly_applicable', 'not_matched')``
    and ``matched_by`` listing the reasons.
    """
    raw_labels = issue.get('labels') or []
    task_labels = {str(lbl).strip().lower() for lbl in raw_labels if isinstance(lbl, str)}
    raw_kind = issue.get('issue_type') or issue.get('type') or 'task'
    task_kind = str(raw_kind).strip().lower()

    touched_files = list(changed_files or []) if diff_status == 'verified' else []
    touched_langs = set()
    for fpath in touched_files:
        lang = language_for_path(fpath)
        if lang:
            touched_langs.add(lang)

    evaluated = []
    for rule in effective_rules:
        app = rule['applicability']
        matched_by = []
        if app.get('all_changes'):
            matched_by.append('all_changes')
        if app.get('task_kinds') and task_kind in set(app['task_kinds']):
            matched_by.append('task_kind:%s' % task_kind)
        if app.get('task_labels'):
            hit_labels = sorted(task_labels & set(app['task_labels']))
            for lbl in hit_labels:
                matched_by.append('task_label:%s' % lbl)

        has_file_selectors = bool(app.get('paths') or app.get('languages'))
        if diff_status == 'verified' and has_file_selectors:
            for pat in app.get('paths') or ():
                if any(_path_matches_pattern(fp, pat) for fp in touched_files):
                    matched_by.append('path:%s' % pat)
                    break
            if app.get('languages'):
                hit_langs = sorted(touched_langs & set(app['languages']))
                for lg in hit_langs:
                    matched_by.append('language:%s' % lg)

        if matched_by:
            match_status = 'applicable'
        elif diff_status != 'verified' and has_file_selectors:
            match_status = 'possibly_applicable'
        else:
            match_status = 'not_matched'

        evaluated.append({
            'rule': rule,
            'match_status': match_status,
            'matched_by': matched_by,
        })
    return evaluated


# -- Pinning, Check Records, Gate, Sign-Offs & Renderers (R6, R7, R8, R8a, R9, R15) --

def validate_declared_tool_model(declared_tool, declared_model):
    tool = (
        _check_clean_text(declared_tool, 'declared_tool', TOOL_NAME_MAX)
        if declared_tool is not None
        else None
    )
    model = (
        _check_clean_text(declared_model, 'declared_model', DECLARED_MODEL_MAX)
        if declared_model is not None
        else None
    )
    return tool, model


def resolve_pinned_version(store, contribution):
    """Resolve the pinned ``(register_version, shared_version, author_meta)`` for ``contribution``.

    Strictly read-only: if ``pins/<cid>.json`` is absent (e.g. register was created after
    ``c1``), derives the version in memory from ``versions/v*.json`` timestamps vs
    ``contribution['timestamp']`` without writing to disk.
    """
    status, head, versions_map = store.inspect_register_raw()
    if status != 'active' or not isinstance(contribution, dict) or not contribution.get('comment_id'):
        return (None, None, {})
    cid = str(contribution['comment_id'])
    try:
        pin = store.read_pin(cid)
    except (ValueError, StandardsDamagedError):
        pin = None
    if isinstance(pin, dict) and pin.get('register_version') in versions_map:
        return (
            int(pin['register_version']),
            pin.get('shared_version'),
            {
                'declared_tool': pin.get('declared_tool'),
                'declared_model': pin.get('declared_model'),
            },
        )
    c_epoch = _parse_iso_epoch(contribution.get('timestamp') or contribution.get('created_at'))
    if c_epoch is not None:
        candidates = []
        for vnum, vobj in versions_map.items():
            v_epoch = _parse_iso_epoch(vobj.get('created_at'))
            if v_epoch is not None and v_epoch <= c_epoch:
                candidates.append(vnum)
        if candidates:
            chosen = max(candidates)
            return (chosen, None, {})
        return (min(versions_map), None, {})
    return (head['current_version'], None, {})


def record_contribution_pin(store, contribution_id, commit, *, declared_tool=None, declared_model=None, now=None):
    """Write ``pins/<contribution_id>.json`` at ``contribute`` time ONLY when register is active."""
    reg = resolve_effective_register(store)
    if reg['status'] != 'active':
        return None
    tool, model = validate_declared_tool_model(declared_tool, declared_model)
    pin = {
        'schema_version': SCHEMA_VERSION,
        'contribution_id': str(contribution_id),
        'commit': str(commit).lower(),
        'register_version': reg['version'],
        'shared_project': reg.get('inherits'),
        'shared_version': reg.get('shared_version'),
        'declared_tool': tool,
        'declared_model': model,
        'pinned_at': utc_now_iso(now),
    }
    store.write_pin(contribution_id, pin)
    return pin


def strip_standards_fields(payload):
    """Strip optional Phase 1 standards fields before native ``review_workflow`` / ``recommend`` validation."""
    if not isinstance(payload, dict):
        return payload, {}
    extracted = {}
    cleaned = {}
    for k, v in payload.items():
        if k in REVIEW_STANDARDS_KEYS:
            if v is not None:
                extracted[k] = v
        else:
            cleaned[k] = v
    return cleaned, extracted


def _normalize_result_token(val):
    if val == 'not-applicable':
        return 'not_applicable'
    return val


def validate_reviewer_checks(
    checks_raw,
    *,
    effective_rules,
    commit,
    contribution_id,
    valid_item_ids,
    tree_available,
    tree_files,
    commit_obs=None,
    tree_recorders=None,
):
    """Validate reviewer-supplied check records against R6 rules and ``commit``'s tree."""
    if checks_raw is None:
        return {}
    if not isinstance(checks_raw, list) or len(checks_raw) > CHECKS_MAX:
        raise StandardsRefusal('checks must be a list of check record objects', status=422)
    rules_by_id = {r['id']: r for r in effective_rules}
    validated = {}
    for entry in checks_raw:
        if not isinstance(entry, dict):
            raise StandardsRefusal('Each check record must be a JSON object', status=422)
        allowed = {'rule_id', 'result', 'evidence', 'reason'}
        extra = sorted(set(entry) - allowed)
        if extra:
            raise StandardsRefusal('Unknown check record field(s): %s' % ', '.join(extra), status=422)
        rid = entry.get('rule_id')
        if not isinstance(rid, str) or rid not in rules_by_id:
            raise StandardsRefusal(
                'Check record names unknown rule %r (not in pinned standards register)' % (rid,),
                status=422,
            )
        if rid in validated:
            raise StandardsRefusal('Duplicate check record for rule %s' % rid, status=422)
        rule = rules_by_id[rid]
        res = _normalize_result_token(entry.get('result'))
        if res not in CHECK_RESULTS:
            raise StandardsRefusal(
                "Check record for rule %s must have result 'pass', 'fail', or 'not_applicable'"
                % rid,
                status=422,
            )

        ev_raw = entry.get('evidence')
        if res == 'pass':
            if rule['check']['kind'] == 'mechanical':
                raise StandardsRefusal(
                    refusal_mechanical_reviewer_pass(
                        rid, rule['check']['tool'], rule['check']['command'], commit
                    ),
                    status=422,
                )
            # Judgement pass: >= 1 valid file:line citation in commit tree, OR command + integer exit_code.
            if not isinstance(ev_raw, dict):
                raise StandardsRefusal(refusal_judgement_pass_evidence(rid, commit), status=422)
            cites_raw = ev_raw.get('citations')
            cmd_raw = ev_raw.get('command')
            has_cites = isinstance(cites_raw, list) and len(cites_raw) > 0
            has_cmd = cmd_raw is not None or 'exit_code' in ev_raw
            if not has_cites and not has_cmd:
                raise StandardsRefusal(refusal_judgement_pass_evidence(rid, commit), status=422)
            if has_cites:
                if len(cites_raw) > CITATIONS_MAX:
                    raise StandardsRefusal(
                        'At most %d citations per check record' % CITATIONS_MAX, status=422
                    )
                resolved_cites = []
                cited_paths = []
                for c in cites_raw:
                    rc = validate_citation_against_tree(
                        c, rid, commit, tree_available, tree_files
                    )
                    resolved_cites.append(rc['citation'])
                    cited_paths.append(rc['path'])
                evidence_out = {'kind': 'citations', 'citations': resolved_cites}
                if isinstance(tree_recorders, dict):
                    recorders = sorted({
                        tree_recorders[p] for p in cited_paths if tree_recorders.get(p)
                    })
                    if recorders:
                        evidence_out['tree_recorded_by'] = (
                            recorders[0] if len(recorders) == 1 else recorders
                        )
                if ev_raw.get('note') is not None:
                    evidence_out['note'] = _check_clean_text(
                        ev_raw['note'], 'evidence note', CHECK_REASON_MAX
                    )
            else:
                if not isinstance(cmd_raw, str) or not cmd_raw.strip():
                    raise StandardsRefusal(refusal_judgement_pass_evidence(rid, commit), status=422)
                exit_code = ev_raw.get('exit_code')
                if type(exit_code) is not int:
                    raise StandardsRefusal(refusal_judgement_pass_evidence(rid, commit), status=422)
                clean_cmd = _check_clean_text(cmd_raw, 'evidence command', COMMAND_MAX)
                evidence_out = {
                    'kind': 'command',
                    'command': clean_cmd,
                    'exit_code': exit_code,
                }
            validated[rid] = {
                'rule_id': rid,
                'result': 'pass',
                'source': 'reviewer',
                'evidence': evidence_out,
            }

        elif res == 'not_applicable':
            reason_raw = entry.get('reason')
            if reason_raw is None and isinstance(ev_raw, dict):
                reason_raw = ev_raw.get('reason')
            if not isinstance(reason_raw, str) or not reason_raw.strip():
                raise StandardsRefusal(refusal_not_applicable_reason(rid), status=422)
            clean_reason = _check_clean_text(
                reason_raw, 'not_applicable reason', CHECK_REASON_MAX
            )
            if rule['check']['kind'] == 'mechanical' and rule['severity'] == 'must':
                ci_chk = _resolve_mechanical_rule_from_ci(rule, commit, commit_obs)
                if ci_chk['result'] == 'fail':
                    raise StandardsRefusal(
                        refusal_na_over_mechanical_fail(rid, commit), status=422
                    )
            validated[rid] = {
                'rule_id': rid,
                'result': 'not_applicable',
                'source': 'reviewer',
                'evidence': {'kind': 'not_applicable', 'reason': clean_reason},
            }

        elif res == 'fail':
            item_id = None
            if isinstance(ev_raw, dict) and ev_raw.get('kind') in (None, 'review_item'):
                item_id = ev_raw.get('item_id')
            if not isinstance(item_id, str) or item_id not in valid_item_ids:
                raise StandardsRefusal(refusal_fail_item_id(rid, contribution_id), status=422)
            validated[rid] = {
                'rule_id': rid,
                'result': 'fail',
                'source': 'reviewer',
                'evidence': {'kind': 'review_item', 'item_id': item_id},
            }
    return validated


def _resolve_mechanical_rule_from_ci(rule, commit, commit_obs):
    ci_list = (commit_obs or {}).get('ci_checks') or []
    matched = None
    for entry in reversed(ci_list):
        if entry.get('rule_id') == rule['id']:
            matched = entry
            break
    if matched is None:
        for entry in reversed(ci_list):
            if (
                entry.get('rule_id') is None
                and entry.get('tool') == rule['check']['tool']
                and entry.get('command') == rule['check']['command']
            ):
                matched = entry
                break
    if matched is None:
        return {
            'rule_id': rule['id'],
            'result': 'unverified',
            'source': 'verifier-recorded',
            'evidence': {
                'kind': 'ci',
                'status': 'unverified',
                'tool': rule['check']['tool'],
                'command': rule['check']['command'],
                'commit': commit,
                'exit_code': None,
                'run_id': None,
                'recorded_by': None,
                'recorded_at': None,
                'detail': 'No CI result recorded for this commit.',
            },
        }
    res = 'pass' if matched['exit_code'] == 0 else 'fail'
    return {
        'rule_id': rule['id'],
        'result': res,
        'source': 'verifier-recorded',
        'evidence': {
            'kind': 'ci',
            'status': res,
            'tool': matched['tool'],
            'command': matched['command'],
            'commit': commit,
            'exit_code': matched['exit_code'],
            'run_id': matched.get('run_id'),
            'recorded_by': matched.get('recorded_by') or (commit_obs or {}).get('recorded_by'),
            'recorded_at': matched.get('recorded_at') or (commit_obs or {}).get('recorded_at'),
        },
    }


def derive_reviewer_must_na_counts(store, reviewer_actor, *, extra_signoff=None):
    """Derive per-reviewer ``must`` ``not_applicable`` tally from committed sign-offs (no separate counter file)."""
    target_key = author_key(reviewer_actor)
    if not target_key:
        return 0
    seen_pairs = set()
    all_signoffs = list(store.list_committed_signoffs())
    if isinstance(extra_signoff, dict):
        all_signoffs.append(extra_signoff)
    for so in all_signoffs:
        if not isinstance(so, dict):
            continue
        rev = so.get('reviewer') or {}
        rev_actor = rev.get('recommended_by') or rev.get('actor')
        if author_key(rev_actor) != target_key:
            continue
        cid = str(so.get('contribution_id') or '')
        for chk in so.get('checks') or ():
            if (
                isinstance(chk, dict)
                and chk.get('severity') == 'must'
                and chk.get('result') == 'not_applicable'
            ):
                seen_pairs.add((cid, str(chk.get('rule_id'))))
    return len(seen_pairs)


def compute_cross_model_status(author_model, reviewer_model):
    if not author_model or not reviewer_model:
        return ('unknown', None)
    if author_model.strip().lower() == reviewer_model.strip().lower():
        return (
            'same',
            'Reviewer declared the same model (%s) as the delivery author; cross-model review is recommended where practical.'
            % reviewer_model,
        )
    return ('different', None)


def evaluate_approval_signoff(
    store,
    *,
    task_id,
    issue,
    contribution,
    operation_id,
    actor,
    actor_user_id=None,
    actor_owner_person=None,
    actor_via='host',
    extracted_fields=None,
    valid_item_ids=frozenset(),
    standing_recommendations=(),
    same_party_with_actor=None,
    same_party_with_recommender=None,
    same_party_pair=None,
    git_observation=None,
    active_person_checker=None,
    known_ref_keys=None,
    now=None,
):
    """Validate and evaluate ``approve`` against the standards register, gate, CI, and waivers.

    Returns ``None`` when the project has no register (binding guarantee: zero effect!).
    Otherwise returns a staged sign-off dict ready for ``store.stage_signoff(...)``, or raises
    ``StandardsRefusal`` pre-effect when refused.
    """
    extracted = extracted_fields or {}
    status, _, _ = store.inspect_register_raw()
    if status == 'none':
        return None

    identity(str(operation_id))
    cid = str(contribution['comment_id'])
    identity(cid)
    commit = str(contribution['commit']).lower()
    gate = get_gate_view(store)

    def _damaged_warn_signoff(detail, *, latest_ver=None):
        ts = utc_now_iso(now)
        tool, model = validate_declared_tool_model(
            extracted.get('declared_tool'), extracted.get('declared_model')
        )
        return {
            'schema_version': SCHEMA_VERSION,
            'status': 'staged',
            'register_status': 'damaged',
            'project': store.project_id,
            'task_id': task_id,
            'contribution_id': cid,
            'commit': commit,
            'operation_id': str(operation_id),
            'approval_comment_id': None,
            'headline': 'Standards register damaged (approved in warn mode)',
            'register_version': None,
            'latest_register_version': latest_ver,
            'newer_register_version_exists': False,
            'shared_project': None,
            'shared_version': None,
            'gate_mode': 'warn',
            'gate_source': gate['source'],
            'would_have_refused': ['damaged_register: %s' % detail],
            'diff_status': 'diff_unverified',
            'diff_recorded_by': None,
            'reviewer': {
                'actor': actor,
                'user_id': actor_user_id,
                'owner_person': actor_owner_person,
                'via': actor_via,
                'recommended_by': None,
                'declared_tool': tool,
                'declared_model': model,
            },
            'author': {
                'actor': contribution.get('author'),
                'declared_tool': None,
                'declared_model': None,
            },
            'cross_model': 'unknown',
            'cross_model_advisory': None,
            'checks': [],
            'waivers': [],
            'expired_waivers': [],
            'must_not_applicable_rule_ids': [],
            'must_not_applicable_count': 0,
            'reviewer_must_not_applicable_total': 0,
            'created_at': ts,
        }

    reg_latest = resolve_effective_register(
        store,
        active_person_checker=active_person_checker,
        known_ref_keys=known_ref_keys,
    )
    if reg_latest['status'] == 'damaged':
        detail = reg_latest.get('damage_detail') or 'unreadable standards register'
        if gate['mode'] == 'block':
            raise StandardsRefusal(
                refusal_damaged_register_block(store.project_id, detail),
                status=409,
            )
        return _damaged_warn_signoff(detail)

    pinned_ver, pinned_shared_ver, author_meta = resolve_pinned_version(store, contribution)
    reg = resolve_effective_register(
        store,
        version=pinned_ver,
        shared_version_override=pinned_shared_ver,
        active_person_checker=active_person_checker,
        known_ref_keys=known_ref_keys,
    )
    if reg['status'] == 'damaged':
        detail = reg.get('damage_detail') or 'unreadable pinned standards version'
        if gate['mode'] == 'block':
            raise StandardsRefusal(
                refusal_damaged_register_block(store.project_id, detail),
                status=409,
            )
        return _damaged_warn_signoff(detail, latest_ver=reg_latest.get('version'))

    # Resolve adopt_recommendation first so its recommender's party is also excluded
    # when filtering commit observations for evaluation.
    adopt_id = extracted.get('adopt_recommendation')
    adopted_checks_list = None
    recommended_by_actor = None
    rec_tool = None
    rec_model = None
    if adopt_id is not None:
        if not isinstance(adopt_id, str) or not adopt_id.strip():
            raise StandardsRefusal(
                refusal_invalid_adopt_recommendation(str(adopt_id), cid, commit), status=422
            )
        matched_rec = None
        for srec in standing_recommendations or ():
            rec_key = srec.get('comment_id') or srec.get('id')
            if str(rec_key) == adopt_id:
                matched_rec = srec
                break
        if matched_rec is None:
            raise StandardsRefusal(
                refusal_invalid_adopt_recommendation(adopt_id, cid, commit), status=422
            )
        if (
            str(matched_rec.get('contribution') or matched_rec.get('contribution_id') or '') != cid
            or str(matched_rec.get('commit') or '').lower() != commit
        ):
            raise StandardsRefusal(
                refusal_invalid_adopt_recommendation(adopt_id, cid, commit), status=422
            )
        rec_op_id = matched_rec.get('operation_id')
        chk_state, rec_bundle = store.read_recommendation_checks(adopt_id, rec_op_id)
        if chk_state != 'ok' or not isinstance(rec_bundle, dict) or rec_bundle.get('checks') is None:
            raise StandardsRefusal(
                refusal_invalid_adopt_recommendation(adopt_id, cid, commit), status=422
            )
        recommended_by_actor = matched_rec.get('author') or matched_rec.get('actor')
        adopted_checks_list = list(rec_bundle.get('checks') or [])
        rec_tool = rec_bundle.get('declared_tool')
        rec_model = rec_bundle.get('declared_model')

    raw_commit_obs = store.read_commit_observation(commit)
    eff_pair = same_party_pair if same_party_pair is not None else same_party_with_recommender
    commit_obs = filter_commit_observation_for_evaluation(
        raw_commit_obs,
        issue=issue,
        contribution=contribution,
        extra_actors=(actor, actor_user_id, actor_owner_person, recommended_by_actor),
        same_party_with_actor=same_party_with_actor,
        same_party_pair=eff_pair,
    )

    # Merge git observation and verifier commit observation.
    git_obs = git_observation or {}
    tree_available = bool(git_obs.get('tree_available'))
    tree_files = dict(git_obs.get('tree_files') or {})
    tree_recorders = None
    if not tree_available and commit_obs and isinstance(commit_obs.get('tree_files'), dict):
        tree_available = True
        tree_files = dict(commit_obs['tree_files'])
        tree_recorders = dict(commit_obs.get('tree_files_recorders') or {})

    if git_obs.get('diff_status') == 'verified' and git_obs.get('changed_files') is not None:
        diff_status = 'verified'
        changed_files = list(git_obs['changed_files'])
        diff_recorded_by = 'git'
    elif commit_obs and commit_obs.get('changed_files') is not None:
        diff_status = 'verified'
        changed_files = list(commit_obs['changed_files'])
        diff_recorded_by = commit_obs.get('changed_files_recorded_by')
    else:
        diff_status = 'diff_unverified'
        changed_files = None
        diff_recorded_by = None

    # Validate adopted checks + direct approver checks.
    combined_checks = {}
    if adopted_checks_list is not None:
        adopted_map = validate_reviewer_checks(
            adopted_checks_list,
            effective_rules=reg['effective_rules'],
            commit=commit,
            contribution_id=cid,
            valid_item_ids=valid_item_ids,
            tree_available=tree_available,
            tree_files=tree_files,
            commit_obs=commit_obs,
            tree_recorders=tree_recorders,
        )
        for rid, val in adopted_map.items():
            val_copy = dict(val, source='adopted-recommendation', recommendation_id=adopt_id)
            combined_checks[rid] = val_copy

    direct_checks = validate_reviewer_checks(
        extracted.get('checks'),
        effective_rules=reg['effective_rules'],
        commit=commit,
        contribution_id=cid,
        valid_item_ids=valid_item_ids,
        tree_available=tree_available,
        tree_files=tree_files,
        commit_obs=commit_obs,
        tree_recorders=tree_recorders,
    )
    combined_checks.update(direct_checks)

    # Evaluate R4 applicability and waivers.
    app_entries = evaluate_applicability(
        reg['effective_rules'], issue, changed_files=changed_files, diff_status=diff_status
    )

    today_str = utc_today_str(now)
    active_waivers_by_rule = {}
    expired_waivers = []
    for w in store.list_waivers(task_id=task_id):
        wst, wdetail = evaluate_waiver_status(w, cid, commit, today_str)
        if wst == 'active':
            active_waivers_by_rule[w['rule_id']] = w
        elif wst == 'expired_date':
            expired_waivers.append({'waiver': w, 'message': wdetail})

    expired_msg_by_rule = {e['waiver']['rule_id']: e['message'] for e in expired_waivers}

    signoff_checks = []
    unresolved_must = []

    for entry in app_entries:
        rule = entry['rule']
        rid = rule['id']
        mstatus = entry['match_status']
        if mstatus == 'not_matched':
            continue

        waiver = active_waivers_by_rule.get(rid)
        reviewer_chk = combined_checks.get(rid)
        ci_chk = (
            _resolve_mechanical_rule_from_ci(rule, commit, commit_obs)
            if rule['check']['kind'] == 'mechanical'
            else None
        )

        if reviewer_chk is not None:
            resolved_chk = dict(reviewer_chk)
            if ci_chk is not None and isinstance(resolved_chk.get('evidence'), dict):
                resolved_chk['evidence'] = dict(resolved_chk['evidence'], ci=ci_chk['evidence'])
        elif ci_chk is not None:
            resolved_chk = ci_chk
        else:
            resolved_chk = {
                'rule_id': rid,
                'result': 'missing',
                'source': 'none',
                'evidence': None,
            }

        check_row = {
            'rule_id': rid,
            'statement': rule['statement'],
            'severity': rule['severity'],
            'check_kind': rule['check']['kind'],
            'origin': rule['origin'],
            'owner': rule['owner'],
            'effective_owner': rule['effective_owner'],
            'match_status': mstatus,
            'matched_by': entry['matched_by'],
            'result': resolved_chk['result'],
            'source': resolved_chk['source'],
            'evidence': resolved_chk['evidence'],
            'ci_evidence': ci_chk['evidence'] if ci_chk is not None else None,
            'waiver': (
                {
                    'id': waiver['id'],
                    'reason': waiver['reason'],
                    'granted_by': waiver['granted_by'],
                    'granted_at': waiver['granted_at'],
                    'expiry': waiver['expiry'],
                }
                if waiver is not None
                else None
            ),
        }
        signoff_checks.append(check_row)

        if rule['severity'] == 'must' and waiver is None:
            res = resolved_chk['result']
            if res in ('pass', 'not_applicable'):
                continue
            suffix = ''
            if rid in expired_msg_by_rule:
                suffix = '; ' + expired_msg_by_rule[rid]
            if mstatus == 'possibly_applicable':
                if res == 'unverified':
                    unresolved_must.append(
                        '%s (diff_unverified and unverified: no CI result recorded%s)' % (rid, suffix)
                    )
                elif res == 'fail':
                    unresolved_must.append(
                        '%s (diff_unverified and fail without waiver%s)' % (rid, suffix)
                    )
                else:
                    unresolved_must.append(
                        '%s (diff_unverified: missing check record or commit diff%s)' % (rid, suffix)
                    )
            else:
                if res == 'missing':
                    unresolved_must.append('%s (missing check record%s)' % (rid, suffix))
                elif res == 'fail':
                    unresolved_must.append('%s (fail without waiver%s)' % (rid, suffix))
                elif res == 'unverified':
                    unresolved_must.append('%s (unverified: no CI result recorded%s)' % (rid, suffix))

    if gate['mode'] == 'block' and unresolved_must:
        raise StandardsRefusal(refusal_block_gate(reg['version'], unresolved_must), status=422)

    direct_tool, direct_model = validate_declared_tool_model(
        extracted.get('declared_tool'), extracted.get('declared_model')
    )
    eff_tool = direct_tool or rec_tool
    eff_model = direct_model or rec_model
    cross_model, cross_advisory = compute_cross_model_status(
        author_meta.get('declared_model'), eff_model
    )

    must_na_ids = [
        c['rule_id']
        for c in signoff_checks
        if c['severity'] == 'must' and c['result'] == 'not_applicable'
    ]
    tally_actor = recommended_by_actor or actor

    ts = utc_now_iso(now)
    staged_signoff = {
        'schema_version': SCHEMA_VERSION,
        'status': 'staged',
        'register_status': 'active',
        'project': store.project_id,
        'task_id': task_id,
        'contribution_id': cid,
        'commit': commit,
        'operation_id': str(operation_id),
        'approval_comment_id': None,
        'headline': 'Checked against standards register v%d' % reg['version'],
        'register_version': reg['version'],
        'latest_register_version': reg['latest_version'],
        'newer_register_version_exists': bool(reg['newer_version_exists']),
        'shared_project': reg.get('inherits'),
        'shared_version': reg.get('shared_version'),
        'gate_mode': gate['mode'],
        'gate_source': gate['source'],
        'would_have_refused': unresolved_must if gate['mode'] == 'warn' else [],
        'diff_status': diff_status,
        'diff_recorded_by': diff_recorded_by,
        'reviewer': {
            'actor': actor,
            'user_id': actor_user_id,
            'owner_person': actor_owner_person,
            'via': actor_via,
            'recommended_by': recommended_by_actor,
            'adopted_recommendation_id': adopt_id,
            'declared_tool': eff_tool,
            'declared_model': eff_model,
        },
        'author': {
            'actor': contribution.get('author'),
            'declared_tool': author_meta.get('declared_tool'),
            'declared_model': author_meta.get('declared_model'),
        },
        'cross_model': cross_model,
        'cross_model_advisory': cross_advisory,
        'checks': signoff_checks,
        'waivers': list(active_waivers_by_rule.values()),
        'expired_waivers': [e['message'] for e in expired_waivers],
        'must_not_applicable_rule_ids': must_na_ids,
        'must_not_applicable_count': len(must_na_ids),
        'reviewer_must_not_applicable_total': 0,
        'created_at': ts,
    }
    staged_signoff['reviewer_must_not_applicable_total'] = derive_reviewer_must_na_counts(
        store, tally_actor, extra_signoff=staged_signoff
    )
    return staged_signoff


def validate_and_stage_recommendation_checks(
    store,
    *,
    contribution,
    operation_id,
    actor,
    actor_user_id=None,
    issue=None,
    extracted_fields,
    valid_item_ids,
    same_party_with_actor=None,
    same_party_pair=None,
    git_observation=None,
    active_person_checker=None,
    now=None,
):
    """Validate and stage recommendation check records before native ``recommend`` writes."""
    status, _, _ = store.inspect_register_raw()
    if status != 'active':
        return None
    extracted = extracted_fields or {}
    if (
        extracted.get('checks') is None
        and extracted.get('declared_tool') is None
        and extracted.get('declared_model') is None
    ):
        return None
    identity(str(operation_id))
    cid = str(contribution['comment_id'])
    commit = str(contribution['commit']).lower()

    raw_commit_obs = store.read_commit_observation(commit)
    commit_obs = filter_commit_observation_for_evaluation(
        raw_commit_obs,
        issue=issue,
        contribution=contribution,
        extra_actors=(actor, actor_user_id),
        same_party_with_actor=same_party_with_actor,
        same_party_pair=same_party_pair,
    )

    pinned_ver, pinned_shared_ver, _ = resolve_pinned_version(store, contribution)
    reg = resolve_effective_register(
        store,
        version=pinned_ver,
        shared_version_override=pinned_shared_ver,
        active_person_checker=active_person_checker,
    )
    if reg['status'] != 'active':
        return None

    git_obs = git_observation or {}
    tree_available = bool(git_obs.get('tree_available'))
    tree_files = dict(git_obs.get('tree_files') or {})
    tree_recorders = None
    if not tree_available and commit_obs and isinstance(commit_obs.get('tree_files'), dict):
        tree_available = True
        tree_files = dict(commit_obs['tree_files'])
        tree_recorders = dict(commit_obs.get('tree_files_recorders') or {})

    checks_raw = extracted.get('checks')
    validated_list = None
    if checks_raw is not None:
        validated_map = validate_reviewer_checks(
            checks_raw,
            effective_rules=reg['effective_rules'],
            commit=commit,
            contribution_id=cid,
            valid_item_ids=valid_item_ids,
            tree_available=tree_available,
            tree_files=tree_files,
            commit_obs=commit_obs,
            tree_recorders=tree_recorders,
        )
        # Preserve canonical raw-compatible check shape for adopt_recommendation re-validation
        validated_list = []
        for chk in checks_raw:
            rid = chk['rule_id']
            v = validated_map[rid]
            out_chk = {'rule_id': rid, 'result': v['result']}
            if v['result'] == 'not_applicable':
                out_chk['reason'] = v['evidence']['reason']
            else:
                ev_clean = {
                    k: val
                    for k, val in v['evidence'].items()
                    if k not in ('tree_recorded_by', 'ci')
                }
                out_chk['evidence'] = ev_clean
            validated_list.append(out_chk)

    tool, model = validate_declared_tool_model(
        extracted.get('declared_tool'), extracted.get('declared_model')
    )
    staged = {
        'schema_version': SCHEMA_VERSION,
        'status': 'staged',
        'operation_id': str(operation_id),
        'recommendation_id': None,
        'contribution_id': cid,
        'commit': commit,
        'register_version': reg['version'],
        'author': actor,
        'declared_tool': tool,
        'declared_model': model,
        'checks': validated_list,
        'created_at': utc_now_iso(now),
    }
    store.stage_recommendation(operation_id, staged)
    return staged


def _is_valid_identity(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        identity(value)
        return True
    except ValueError:
        return False


def has_approve_operation_id(issue, contribution_id, operation_id):
    """Return True if ``operation_id`` already landed as an ``approve`` on ``contribution_id``."""
    if not isinstance(issue, dict) or not contribution_id or not _is_valid_identity(operation_id):
        return False
    cid = str(contribution_id)
    for comment in issue.get('comments') or ():
        if not isinstance(comment, dict):
            continue
        raw = comment.get('text')
        if not isinstance(raw, str) or not raw.startswith(REVIEW_PREFIX):
            continue
        try:
            rec = parse_json(raw[len(REVIEW_PREFIX):])
        except ValueError:
            continue
        if (
            isinstance(rec, dict)
            and rec.get('operation') == 'approve'
            and str(rec.get('contribution') or '') == cid
            and rec.get('operation_id') == operation_id
        ):
            return True
    for rec in issue.get('review_records') or ():
        if (
            isinstance(rec, dict)
            and rec.get('kind') == 'approve'
            and str(rec.get('contribution_id') or '') == cid
            and rec.get('operation_id') == operation_id
        ):
            return True
    return False


def find_approve_operations(issue, contribution_id):
    """Return ordered unique ``approve`` ``operation_id``s for ``contribution_id`` in ``issue``."""
    if not isinstance(issue, dict) or not contribution_id:
        return []
    cid = str(contribution_id)
    found = []
    seen = set()
    for comment in issue.get('comments') or ():
        if not isinstance(comment, dict):
            continue
        raw = comment.get('text')
        if not isinstance(raw, str) or not raw.startswith(REVIEW_PREFIX):
            continue
        try:
            rec = parse_json(raw[len(REVIEW_PREFIX):])
        except ValueError:
            continue
        op_id = rec.get('operation_id') if isinstance(rec, dict) else None
        if (
            isinstance(rec, dict)
            and rec.get('operation') == 'approve'
            and str(rec.get('contribution') or '') == cid
            and isinstance(op_id, str)
            and op_id
            and op_id not in seen
        ):
            seen.add(op_id)
            found.append(op_id)
    for rec in issue.get('review_records') or ():
        op_id = rec.get('operation_id') if isinstance(rec, dict) else None
        if (
            isinstance(rec, dict)
            and rec.get('kind') == 'approve'
            and str(rec.get('contribution_id') or '') == cid
            and isinstance(op_id, str)
            and op_id
            and op_id not in seen
        ):
            seen.add(op_id)
            found.append(op_id)
    return found


def find_approve_operation_id(issue, contribution_id):
    """Return the latest ``approve`` operation_id for ``contribution_id`` in ``issue``, or None."""
    ops = find_approve_operations(issue, contribution_id)
    return ops[-1] if ops else None


def _refresh_signoff_view(store, so, status_and_head=None):
    out = dict(so)
    out.pop('earlier_sign_offs', None)
    status, head = (
        status_and_head
        if status_and_head is not None
        else store.inspect_register_raw()[:2]
    )
    if status == 'active' and out.get('register_version') is not None:
        out['latest_register_version'] = head['current_version']
        out['newer_register_version_exists'] = int(out['register_version']) < head['current_version']
    rev = out.get('reviewer') or {}
    tally_actor = rev.get('recommended_by') or rev.get('actor')
    if tally_actor:
        out['reviewer_must_not_applicable_total'] = derive_reviewer_must_na_counts(store, tally_actor)
    return out


def read_signoffs_for_contribution(
    store,
    contribution_id,
    expected_commit=None,
    *,
    issue=None,
    approve_operation_id=None,
):
    """Return chronological ``[earliest, ..., latest]`` sign-offs for ``contribution_id`` (R8)."""
    cid = str(contribution_id)
    exp_lower = str(expected_commit).lower() if expected_commit is not None else None
    issue_ops = list(find_approve_operations(issue, cid))
    raw_items = []
    seen_ops = set()
    for op_id in issue_ops:
        so = store.read_signoff(cid, approve_operation_id=op_id)
        if not isinstance(so, dict):
            continue
        if exp_lower is not None and str(so.get('commit') or '').lower() != exp_lower:
            continue
        seen_ops.add(str(so.get('operation_id') or op_id))
        raw_items.append(so)

    extra_committed = []
    for so in store.list_committed_signoffs():
        if not isinstance(so, dict) or str(so.get('contribution_id') or '') != cid:
            continue
        if exp_lower is not None and str(so.get('commit') or '').lower() != exp_lower:
            continue
        op_id = str(so.get('operation_id') or '')
        if op_id and op_id in seen_ops:
            continue
        if op_id:
            seen_ops.add(op_id)
        extra_committed.append(so)
    if extra_committed:
        extra_committed.sort(
            key=lambda s: (
                str(s.get('created_at') or ''),
                str(s.get('approval_comment_id') or ''),
                str(s.get('operation_id') or ''),
            )
        )
        raw_items = extra_committed + raw_items

    if isinstance(approve_operation_id, str) and approve_operation_id and approve_operation_id not in seen_ops:
        so = store.read_signoff(cid, approve_operation_id=approve_operation_id)
        if isinstance(so, dict) and (
            exp_lower is None or str(so.get('commit') or '').lower() == exp_lower
        ):
            raw_items.append(so)

    if not raw_items:
        return []
    status_and_head = store.inspect_register_raw()[:2]
    return [_refresh_signoff_view(store, item, status_and_head) for item in raw_items]


def read_signoff_for_contribution(
    store,
    contribution_id,
    expected_commit=None,
    approve_operation_id=None,
    *,
    issue=None,
    strict_operation_id=False,
):
    """Read a delivery's sign-off, refusing carryover if ``expected_commit`` differs (R8).

    When ``strict_operation_id=True``, return only the sign-off for ``approve_operation_id``
    (so an exact retry of an earlier approval returns its own sign-off). Otherwise return the
    latest sign-off for ``contribution_id`` with ``earlier_sign_offs`` attached oldest-first.
    """
    if strict_operation_id:
        if approve_operation_id is None:
            return None
        so = store.read_signoff(contribution_id, approve_operation_id=approve_operation_id)
        if not isinstance(so, dict):
            return None
        if expected_commit is not None and str(so.get('commit') or '').lower() != str(expected_commit).lower():
            return None
        return _refresh_signoff_view(store, so)

    all_signoffs = read_signoffs_for_contribution(
        store,
        contribution_id,
        expected_commit=expected_commit,
        issue=issue,
        approve_operation_id=approve_operation_id,
    )
    if not all_signoffs:
        return None
    latest = dict(all_signoffs[-1])
    latest['earlier_sign_offs'] = list(all_signoffs[:-1])
    return latest


# -- Readable Sign-Off Renderers (R8a: "Checked against", never "certified") ---

def _format_ci_summary(ci_ev):
    if not isinstance(ci_ev, dict):
        return ''
    if ci_ev.get('status') == 'unverified':
        return 'unverified (no CI result recorded for `%s: %s`)' % (
            ci_ev.get('tool'),
            ci_ev.get('command'),
        )
    return 'CI `%s: %s` exit %s (recorded by %s)' % (
        ci_ev.get('tool'),
        ci_ev.get('command'),
        ci_ev.get('exit_code'),
        ci_ev.get('recorded_by') or 'verifier',
    )


def _format_evidence_summary(chk):
    ev = chk.get('evidence')
    if not isinstance(ev, dict):
        summary = 'none'
        kind = None
    else:
        kind = ev.get('kind')
        if kind == 'citations':
            summary = 'citations: ' + ', '.join(ev.get('citations') or [])
            tree_rec = ev.get('tree_recorded_by')
            if tree_rec:
                rec_txt = ', '.join(tree_rec) if isinstance(tree_rec, list) else str(tree_rec)
                summary += ' (tree recorded by %s)' % rec_txt
        elif kind == 'command':
            summary = 'command `%s` (exit %s)' % (ev.get('command'), ev.get('exit_code'))
        elif kind == 'not_applicable':
            summary = 'reason: %s' % ev.get('reason')
        elif kind == 'review_item':
            summary = 'review item `%s`' % ev.get('item_id')
        elif kind == 'ci':
            summary = _format_ci_summary(ev)
        else:
            summary = str(ev)

    if chk.get('check_kind') == 'mechanical' and kind != 'ci':
        ci_ev = chk.get('ci_evidence') or (ev.get('ci') if isinstance(ev, dict) else None)
        if isinstance(ci_ev, dict) and ci_ev.get('status') not in (None, 'unverified'):
            summary += '; recorded ' + _format_ci_summary(ci_ev)
    return summary


def render_signoff_markdown(signoff):
    """Render a delivery sign-off as a standalone Markdown document (R8a)."""
    ver = signoff.get('register_version')
    headline = (
        'Checked against standards register v%s' % ver
        if ver is not None
        else signoff.get('headline') or 'Checked against standards register'
    )
    diff_line = '- **Diff verification:** `%s`' % signoff.get('diff_status')
    if signoff.get('diff_recorded_by'):
        diff_line += ' (recorded by `%s`)' % signoff['diff_recorded_by']
    lines = [
        '# %s' % headline,
        '',
        '- **Project:** `%s`' % signoff.get('project'),
        '- **Task:** `%s`' % signoff.get('task_id'),
        '- **Delivery:** `%s` (`%s`)' % (signoff.get('contribution_id'), signoff.get('commit')),
        '- **Gate mode:** `%s`' % signoff.get('gate_mode'),
        diff_line,
        '- **Recorded at:** `%s`' % signoff.get('created_at'),
    ]
    if signoff.get('newer_register_version_exists'):
        lines.append(
            '- **Register notice:** Checked against pinned register v%s; newer version v%s now exists.'
            % (ver, signoff.get('latest_register_version'))
        )
    rev = signoff.get('reviewer') or {}
    rev_desc = '`%s`' % (rev.get('actor') or 'unknown')
    if rev.get('owner_person'):
        rev_desc += ' (owner `%s`)' % rev['owner_person']
    if rev.get('recommended_by'):
        rev_desc += ', adopting recommendation `%s` by `%s`' % (
            rev.get('adopted_recommendation_id'),
            rev.get('recommended_by'),
        )
    lines.append('- **Reviewer / Approver:** %s' % rev_desc)
    if rev.get('declared_tool') or rev.get('declared_model'):
        lines.append(
            '- **Declared reviewer tool / model:** `%s` / `%s` (cross-model: `%s`)'
            % (
                rev.get('declared_tool') or 'unspecified',
                rev.get('declared_model') or 'unspecified',
                signoff.get('cross_model') or 'unknown',
            )
        )
    if signoff.get('cross_model_advisory'):
        lines.append('- **Model advisory:** %s' % signoff['cross_model_advisory'])
    lines.append(
        '- **Reviewer `must` not-applicable tally:** %d on this delivery (%d total across project)'
        % (
            int(signoff.get('must_not_applicable_count') or 0),
            int(signoff.get('reviewer_must_not_applicable_total') or 0),
        )
    )

    would = signoff.get('would_have_refused') or []
    if would:
        lines.extend([
            '',
            '## Warn-Mode Gate Notice (Would Have Refused in `block` Mode)',
            '',
        ])
        for item in would:
            lines.append('- %s' % item)

    lines.extend([
        '',
        '## Rule-by-Rule Check Results',
        '',
        '| Rule | Origin | Severity | Kind | Result | Evidence | Waiver |',
        '| --- | --- | --- | --- | --- | --- | --- |',
    ])
    for chk in signoff.get('checks') or ():
        origin = chk.get('origin') or {}
        origin_txt = (
            'inherited:%s@v%s' % (origin.get('project'), origin.get('version'))
            if origin.get('kind') == 'inherited'
            else 'project@v%s' % origin.get('version')
        )
        w = chk.get('waiver')
        waiver_txt = (
            'waived by `%s`: %s' % (w.get('granted_by'), w.get('reason')) if w else '-'
        )
        ev_txt = _format_evidence_summary(chk).replace('|', '\\|').replace('\n', ' ')
        waiver_txt = waiver_txt.replace('|', '\\|').replace('\n', ' ')
        lines.append(
            '| `%s` | `%s` | `%s` | `%s` | `%s` | %s | %s |'
            % (
                chk.get('rule_id'),
                origin_txt,
                chk.get('severity'),
                chk.get('check_kind'),
                chk.get('result'),
                ev_txt,
                waiver_txt,
            )
        )
    earlier = signoff.get('earlier_sign_offs') or []
    if earlier:
        lines.extend([
            '',
            '## Earlier Approvals on This Delivery',
            '',
        ])
        for prev_so in earlier:
            if not isinstance(prev_so, dict):
                continue
            prev_rev = (prev_so.get('reviewer') or {}).get('actor') or 'unknown'
            lines.append(
                '- `%s` at `%s` (gate `%s`, `must` not-applicable: %d on delivery, %d total across project)'
                % (
                    prev_rev,
                    prev_so.get('created_at'),
                    prev_so.get('gate_mode'),
                    int(prev_so.get('must_not_applicable_count') or 0),
                    int(prev_so.get('reviewer_must_not_applicable_total') or 0),
                )
            )
    return '\n'.join(lines) + '\n'


def render_signoff_text(signoff):
    """Render a concise plain-text sign-off for CLI output (R8a)."""
    ver = signoff.get('register_version')
    headline = (
        'Checked against standards register v%s' % ver
        if ver is not None
        else signoff.get('headline') or 'Checked against standards register'
    )
    diff_desc = str(signoff.get('diff_status'))
    if signoff.get('diff_recorded_by'):
        diff_desc += ' (by %s)' % signoff['diff_recorded_by']
    lines = [
        headline,
        '  task: %s  delivery: %s  commit: %s'
        % (signoff.get('task_id'), signoff.get('contribution_id'), signoff.get('commit')),
        '  gate: %s  diff: %s  recorded_at: %s'
        % (signoff.get('gate_mode'), diff_desc, signoff.get('created_at')),
    ]
    if signoff.get('newer_register_version_exists'):
        lines.append(
            '  notice: pinned to v%s; newer register v%s exists'
            % (ver, signoff.get('latest_register_version'))
        )
    rev = signoff.get('reviewer') or {}
    lines.append(
        '  reviewer: %s (must not_applicable: %d here, %d total)'
        % (
            rev.get('recommended_by') or rev.get('actor'),
            int(signoff.get('must_not_applicable_count') or 0),
            int(signoff.get('reviewer_must_not_applicable_total') or 0),
        )
    )
    for item in signoff.get('would_have_refused') or ():
        lines.append('  would_have_refused: %s' % item)
    for chk in signoff.get('checks') or ():
        w = chk.get('waiver')
        w_suffix = ' [WAIVED by %s: %s]' % (w.get('granted_by'), w.get('reason')) if w else ''
        lines.append(
            '  - %s [%s/%s] -> %s (%s)%s'
            % (
                chk.get('rule_id'),
                chk.get('severity'),
                chk.get('check_kind'),
                chk.get('result'),
                _format_evidence_summary(chk),
                w_suffix,
            )
        )
    for prev_so in signoff.get('earlier_sign_offs') or ():
        if not isinstance(prev_so, dict):
            continue
        prev_rev = (prev_so.get('reviewer') or {}).get('actor') or 'unknown'
        lines.append(
            '  earlier_sign_off: %s at %s (gate: %s, must not_applicable: %d here, %d total)'
            % (
                prev_rev,
                prev_so.get('created_at'),
                prev_so.get('gate_mode'),
                int(prev_so.get('must_not_applicable_count') or 0),
                int(prev_so.get('reviewer_must_not_applicable_total') or 0),
            )
        )
    return '\n'.join(lines)


# -- R15 Worker & Reviewer Brief Projector ------------------------------------

def brief_standards_block(
    store,
    issue,
    review_projection,
    *,
    full=False,
    git_observation=None,
    active_person_checker=None,
    same_party_pair=None,
    now=None,
):
    """Build the additive ``standards`` block for ``brief``, or ``None`` when no register exists.

    Strictly read-only; bounded below ~1,400 bytes in compact mode so ``brief`` stays well
    within its 6,000-byte compact ceiling.
    """
    status, _, damage_detail = store.inspect_register_raw()
    if status == 'none':
        return None
    gate = get_gate_view(store)
    if status == 'damaged':
        return {
            'status': 'damaged',
            'gate_mode': gate['mode'],
            'warning': 'Standards register in project %s is damaged (%s).'
            % (store.project_id, damage_detail),
        }

    contribution = (review_projection or {}).get('contribution')
    review_state = (review_projection or {}).get('review_state') or (review_projection or {}).get('state')

    if isinstance(contribution, dict) and (contribution.get('comment_id') or contribution.get('id')):
        contrib_for_pin = dict(contribution, comment_id=contribution.get('comment_id') or contribution.get('id'))
        pinned_ver, pinned_shared_ver, _ = resolve_pinned_version(store, contrib_for_pin)
    else:
        pinned_ver, pinned_shared_ver = None, None

    reg = resolve_effective_register(
        store,
        version=pinned_ver,
        shared_version_override=pinned_shared_ver,
        active_person_checker=active_person_checker,
    )
    if reg['status'] == 'damaged':
        return {
            'status': 'damaged',
            'gate_mode': gate['mode'],
            'warning': reg.get('warning'),
        }

    commit = str(contribution.get('commit') or '').lower() if isinstance(contribution, dict) else None
    raw_commit_obs = store.read_commit_observation(commit) if commit and COMMIT_RE.fullmatch(commit) else None
    commit_obs = filter_commit_observation_for_evaluation(
        raw_commit_obs,
        issue=issue,
        contribution=contribution,
        same_party_pair=same_party_pair,
    )
    git_obs = git_observation or {}
    if git_obs.get('diff_status') == 'verified' and git_obs.get('changed_files') is not None:
        diff_status = 'verified'
        changed_files = list(git_obs['changed_files'])
        diff_recorded_by = 'git'
    elif commit_obs and commit_obs.get('changed_files') is not None:
        diff_status = 'verified'
        changed_files = list(commit_obs['changed_files'])
        diff_recorded_by = commit_obs.get('changed_files_recorded_by')
    else:
        diff_status = 'diff_unverified'
        changed_files = None
        diff_recorded_by = None

    app_entries = evaluate_applicability(
        reg['effective_rules'], issue, changed_files=changed_files, diff_status=diff_status
    )

    def _rule_summary(entry):
        r = entry['rule']
        stmt = r['statement'] if full else r['statement'][:96]
        item = {
            'id': r['id'],
            'severity': r['severity'],
            'check_kind': r['check']['kind'],
            'statement': stmt,
            'match_status': entry['match_status'],
        }
        if full:
            item['rationale'] = r['rationale']
            item['origin'] = r['origin']
            item['check'] = r['check']
            item['applicability'] = r['applicability']
        return item

    applicable = [_rule_summary(e) for e in app_entries if e['match_status'] == 'applicable']
    conditional = [_rule_summary(e) for e in app_entries if e['match_status'] == 'possibly_applicable']
    if not full:
        applicable = applicable[:8]
        conditional = conditional[:6]

    block = {
        'status': 'active',
        'register_version': reg['version'],
        'latest_register_version': reg['latest_version'],
        'newer_register_version_exists': bool(reg['newer_version_exists']),
        'gate_mode': gate['mode'],
        'diff_status': diff_status,
        'applicable_rules': applicable,
        'conditional_rules': conditional,
    }
    if full:
        block['diff_recorded_by'] = diff_recorded_by

    if isinstance(contribution, dict) and (contribution.get('comment_id') or contribution.get('id')):
        cid = str(contribution.get('comment_id') or contribution.get('id'))
        today_str = utc_today_str(now)
        active_waiver_ids = [
            w['rule_id']
            for w in store.list_waivers(task_id=str(issue.get('id') or ''))
            if evaluate_waiver_status(w, cid, commit, today_str)[0] == 'active'
        ]
        required_checks = []
        for e in app_entries:
            if e['match_status'] == 'not_matched':
                continue
            r = e['rule']
            required_checks.append({
                'rule_id': r['id'],
                'severity': r['severity'],
                'check_kind': r['check']['kind'],
                'match_status': e['match_status'],
                'waived': r['id'] in active_waiver_ids,
                'expectation': (
                    'verifier-recorded CI (%s: %s)'
                    % (r['check']['tool'], r['check']['command'][:60])
                    if r['check']['kind'] == 'mechanical'
                    else 'judgement check (pass with file:line or command+exit_code, fail with item_id, or not_applicable with reason)'
                ),
            })
        if not full:
            required_checks = required_checks[:10]
        block['reviewer_notice'] = REVIEWER_ANTI_PREFILL_NOTICE
        block['required_checks'] = required_checks
        block['active_waived_rules'] = sorted(set(active_waiver_ids))
        so = read_signoff_for_contribution(
            store,
            cid,
            expected_commit=commit,
            approve_operation_id=find_approve_operation_id(issue, cid),
            issue=issue,
        )
        if so is not None:
            block['sign_off'] = (
                so
                if full
                else {
                    'headline': so.get('headline'),
                    'register_version': so.get('register_version'),
                    'gate_mode': so.get('gate_mode'),
                    'would_have_refused': so.get('would_have_refused') or [],
                }
            )
    return block


# ---------------------------------------------------------------------------
# Backup & restore (.standards.json / .standards.last-complete.json)
# ---------------------------------------------------------------------------

STANDARDS_BACKUP_SCHEMA_VERSION = 1

_SAFE_BACKUP_TOP_FILES = frozenset({
    'head.json',
    'gate.json',
    'departed_owners.json',
    'notifications.jsonl',
})
_SAFE_BACKUP_SUBDIRS = frozenset({
    'versions',
    'drafts',
    'waivers',
    'commits',
    'pins',
    'recommendations',
    'signoffs',
})
_SAFE_BACKUP_FILENAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$')


def _safe_backup_relative(name):
    if not isinstance(name, str) or not name or name.startswith('/') or '\\' in name:
        raise ValueError('Invalid standards backup path')
    parts = name.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('Invalid standards backup path')
    if len(parts) == 1 and parts[0] in _SAFE_BACKUP_TOP_FILES:
        return
    if (
        len(parts) == 2
        and parts[0] in _SAFE_BACKUP_SUBDIRS
        and _SAFE_BACKUP_FILENAME_RE.fullmatch(parts[1])
    ):
        return
    raise ValueError('Invalid standards backup path: %r' % (name,))


def _collect_standards_backup_files(project_path):
    base = Path(project_path)
    if base.is_symlink():
        raise ValueError('Project path must not be a symlink')
    root = base / '.standards'
    if root.is_symlink():
        raise ValueError('Standards directory must not be a symlink')
    if not root.exists():
        return None
    if not root.is_dir():
        raise ValueError('Standards path must be a directory')

    files = {}
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_symlink():
            raise ValueError('Standards path %s must not be a symlink' % entry.name)
        if entry.is_file():
            rel = entry.name
            _safe_backup_relative(rel)
            files[rel] = read_regular_text(entry, raw=True)
        elif entry.is_dir():
            if entry.name not in _SAFE_BACKUP_SUBDIRS:
                raise ValueError('Unexpected standards subdirectory: %s' % entry.name)
            for child in sorted(entry.iterdir(), key=lambda p: p.name):
                if child.is_symlink():
                    raise ValueError(
                        'Standards path %s/%s must not be a symlink'
                        % (entry.name, child.name)
                    )
                if not child.is_file():
                    raise ValueError(
                        'Standards path %s/%s must be a regular file'
                        % (entry.name, child.name)
                    )
                rel = '%s/%s' % (entry.name, child.name)
                _safe_backup_relative(rel)
                files[rel] = read_regular_text(child, raw=True)
        else:
            raise ValueError('Unexpected non-regular file in .standards: %s' % entry.name)
    return files


def collect_valid_item_ids_for_contribution(issue, contribution_id, payload=None):
    """Collect valid review/recommendation ``items[].id`` values on ``contribution_id``."""
    cid = str(contribution_id or '')
    ids = set()
    if isinstance(payload, dict):
        for item in payload.get('items') or ():
            if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']:
                ids.add(item['id'])
    if isinstance(issue, dict):
        import review_recommendations as _rec_mod
        import review_workflow as _rw_mod

        for comment in issue.get('comments') or ():
            if not isinstance(comment, dict):
                continue
            raw = comment.get('text')
            if not isinstance(raw, str):
                continue
            for prefix in (_rw_mod.PREFIX, _rec_mod.PREFIX):
                if raw.startswith(prefix):
                    try:
                        rec = parse_json(raw[len(prefix):])
                    except ValueError:
                        rec = None
                    if isinstance(rec, dict) and str(rec.get('contribution') or '') == cid:
                        for item in rec.get('items') or ():
                            if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']:
                                ids.add(item['id'])
        # Also support HTTP in-process issue/task shape where review_records / recommendations are lists.
        for rec in issue.get('review_records') or ():
            if isinstance(rec, dict) and str(rec.get('contribution') or rec.get('contribution_id') or '') == cid:
                r_items = rec.get('items')
                if r_items:
                    for item in r_items:
                        if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']:
                            ids.add(item['id'])
                elif rec.get('kind') == 'request-changes' and rec.get('summary'):
                    ids.add('item-1')
        for rec in issue.get('recommendations') or ():
            if isinstance(rec, dict) and str(rec.get('contribution') or rec.get('contribution_id') or '') == cid:
                for item in rec.get('items') or ():
                    if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']:
                        ids.add(item['id'])
    return ids


def _write_bytes_atomic(base_root, rel_name, raw_bytes):
    base_root = Path(base_root)
    parts = rel_name.split('/')
    target_dir = _ensure_real_dir(base_root, *parts[:-1])
    path = target_dir / parts[-1]
    if path.is_symlink():
        raise ValueError('%s must not be a symlink' % path)
    if path.exists() and not path.is_file():
        raise ValueError('%s is not a regular file' % path)
    fd, tmp_name = tempfile.mkstemp(
        prefix='.' + path.name + '.tmp.',
        dir=str(target_dir),
    )
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(raw_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def backup_record(project_path):
    """Self-contained backup record for ``backups/<name>.standards.json``, or None when ``.standards/`` is absent."""
    import base64 as _b64

    files = _collect_standards_backup_files(project_path)
    if files is None:
        return None
    encoded = {}
    for name, content in sorted(files.items()):
        encoded[name] = {
            'sha256': hashlib.sha256(content).hexdigest(),
            'bytes': len(content),
            'base64': _b64.b64encode(content).decode('ascii'),
        }
    return {
        'schema_version': STANDARDS_BACKUP_SCHEMA_VERSION,
        'status': 'complete',
        'project': Path(project_path).name,
        'generated_at': utc_now_iso(),
        'files': encoded,
    }


def validate_backup(record):
    """Verify a ``.standards.json`` backup record before any restore write."""
    import base64 as _b64

    if not isinstance(record, dict) or set(record) != {
        'schema_version',
        'status',
        'project',
        'generated_at',
        'files',
    }:
        raise ValueError('Invalid standards backup')
    if (
        record['schema_version'] != STANDARDS_BACKUP_SCHEMA_VERSION
        or record['status'] != 'complete'
    ):
        raise ValueError('Invalid standards backup: not a complete standards backup')
    if not isinstance(record['project'], str) or not record['project'].strip():
        raise ValueError('Invalid standards backup project')
    if _parse_iso_epoch(record['generated_at']) is None:
        raise ValueError('Invalid standards backup timestamp')
    if not isinstance(record['files'], dict):
        raise ValueError('Invalid standards backup files')
    for name, item in record['files'].items():
        _safe_backup_relative(name)
        if not isinstance(item, dict) or set(item) != {'sha256', 'bytes', 'base64'}:
            raise ValueError('Invalid standards backup file entry')
        if not isinstance(item['sha256'], str) or not SHA256_RE.fullmatch(item['sha256']):
            raise ValueError('Invalid standards backup digest')
        if type(item['bytes']) is not int or item['bytes'] < 0:
            raise ValueError('Invalid standards backup size')
        try:
            content = _b64.b64decode(item['base64'], validate=True)
        except Exception:
            raise ValueError('Invalid standards backup content') from None
        if (
            len(content) != item['bytes']
            or hashlib.sha256(content).hexdigest() != item['sha256']
        ):
            raise ValueError(
                'Standards backup content does not match its digest (%s)' % name
            )
    return record


def restore_files(project_path, record):
    """Restore verified ``.standards/`` files into a fresh destination project directory."""
    import base64 as _b64

    validate_backup(record)
    base = Path(project_path)
    if base.is_symlink():
        raise ValueError('Restore destination project path must not be a symlink')
    root = base / '.standards'
    if root.is_symlink():
        raise ValueError('Restore destination has a symlink standards path')
    if root.exists():
        if not root.is_dir():
            raise ValueError('Restore destination has a non-folder standards path')
        if any(root.iterdir()):
            raise ValueError(
                'Restore destination already has standards files; refusing to overwrite them'
            )
    _ensure_real_dir(base, '.standards')
    # Write versions/ and other files before head.json so head.json appears last.
    items = sorted(
        record['files'].items(),
        key=lambda pair: (pair[0] == 'head.json', pair[0]),
    )
    for name, item in items:
        content = _b64.b64decode(item['base64'], validate=True)
        _write_bytes_atomic(root, name, content)
    return len(items)


# ---------------------------------------------------------------------------
# Endpoint, Web-Service, and Admin Host Dispatchers
# ---------------------------------------------------------------------------

def _collect_known_ref_keys(rows):
    keys = set()
    try:
        import reference_records
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            for comment in row.get('comments') or ():
                if not isinstance(comment, dict):
                    continue
                txt = comment.get('text')
                if isinstance(txt, str) and txt.startswith(reference_records.PREFIX):
                    try:
                        rec = parse_json(txt[len(reference_records.PREFIX):])
                        if isinstance(rec, dict) and isinstance(rec.get('key'), str):
                            keys.add(rec['key'])
                    except ValueError:
                        pass
    except Exception:
        pass
    return keys or None


def inspect_review_git_prelock(root, path, name, args, attachments, run_single_show_fn):
    """Inspect verified bundle / serving_repo OUTSIDE ``.coordination.lock`` before ``review`` write."""
    clean_args = [tok for tok in (args or []) if tok != '--json']
    if len(clean_args) != 2 or not clean_args[1].startswith('@attachment:'):
        return None
    att_key = clean_args[1].partition(':')[2]
    att = (attachments or {}).get(att_key)
    if not isinstance(att, dict) or not isinstance(att.get('text'), str):
        return None
    try:
        payload = parse_json(att['text'])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    op = payload.get('operation')
    if op not in ('approve', 'request-changes', 'recommend'):
        return None

    store = HostStandardsStore(path, name, shared_loader=host_shared_loader_for(path))
    if store.inspect_register_raw()[0] != 'active':
        return None

    task_id = clean_args[0]
    cid = payload.get('contribution')
    if not (isinstance(cid, str) and cid):
        return None

    needed_checks = []
    if isinstance(payload.get('checks'), list):
        needed_checks.extend(payload['checks'])
    adopt_id = payload.get('adopt_recommendation')
    if isinstance(adopt_id, str) and adopt_id:
        chk_st, rec_bundle = store.read_recommendation_checks(adopt_id)
        if chk_st == 'ok' and isinstance(rec_bundle, dict) and isinstance(rec_bundle.get('checks'), list):
            needed_checks.extend(rec_bundle['checks'])
    needed_paths = collect_citation_paths_from_checks(needed_checks)

    contrib_doc = None
    try:
        shown_raw = run_single_show_fn(task_id)
        shown = parse_json(shown_raw) if isinstance(shown_raw, str) and shown_raw.strip() else None
        issue_obj = shown[0] if isinstance(shown, list) and shown else (shown if isinstance(shown, dict) else None)
        if isinstance(issue_obj, dict):
            for cmt in issue_obj.get('comments') or ():
                if not isinstance(cmt, dict) or str(cmt.get('id')) != cid:
                    continue
                raw_txt = cmt.get('text')
                if isinstance(raw_txt, str) and raw_txt.startswith(REVIEW_PREFIX):
                    crec = parse_json(raw_txt[len(REVIEW_PREFIX):])
                    if isinstance(crec, dict) and crec.get('operation') == 'contribute':
                        c_commit = str(crec.get('commit') or '').strip().lower()
                        c_deliv = crec.get('delivery')
                        if not isinstance(c_deliv, dict) and crec.get('bundle_path'):
                            c_deliv = {
                                'kind': 'bundle',
                                'path': crec.get('bundle_path'),
                                'sha256': crec.get('bundle_sha256') or '',
                            }
                        if COMMIT_RE.fullmatch(c_commit) and isinstance(c_deliv, dict):
                            contrib_doc = {
                                'comment_id': cid,
                                'commit': c_commit,
                                'delivery': c_deliv,
                            }
                        break
    except Exception:
        pass

    if contrib_doc is None:
        return None

    from admin import delivery_roots, delivery_serve_max_bytes, serving_repos, serving_target_ref
    d_roots = delivery_roots(root)
    s_repo = serving_repos(root).get(name)
    t_ref = serving_target_ref(root).get(name, 'refs/heads/main')
    max_bytes = delivery_serve_max_bytes(root)

    if contrib_doc['delivery'].get('kind') == 'bundle' and not d_roots and not s_repo:
        return None

    obs = inspect_delivery_git(
        contrib_doc,
        delivery_roots=d_roots,
        max_bundle_bytes=max_bytes,
        serving_repo=s_repo,
        target_ref=t_ref,
        needed_paths=needed_paths,
        lock_dir=root,
    )
    obs = dict(obs, contribution_id=cid, commit=contrib_doc['commit'])
    return obs


def inspect_brief_git_prelock(root, path, name, args, run_single_show_fn):
    """Read-only git diff inspection from ``serving_repos`` for ``brief`` (writes ZERO files)."""
    from admin import delivery_roots, delivery_serve_max_bytes, serving_repos, serving_target_ref
    s_repo = serving_repos(root).get(name)
    if not s_repo:
        return None
    store = HostStandardsStore(path, name, shared_loader=host_shared_loader_for(path))
    if store.inspect_register_raw()[0] != 'active':
        return None
    task_id = None
    i = 0
    raw_args = list(args or [])
    while i < len(raw_args):
        tok = raw_args[i]
        if tok in ('--items-offset', '--items-limit'):
            i += 2
            continue
        if tok.startswith('--items-offset=') or tok.startswith('--items-limit='):
            i += 1
            continue
        if tok.startswith('-'):
            i += 1
            continue
        task_id = tok
        break
    if not task_id:
        return None
    contrib_doc = None
    try:
        shown_raw = run_single_show_fn(task_id)
        shown = parse_json(shown_raw) if isinstance(shown_raw, str) and shown_raw.strip() else None
        issue_obj = shown[0] if isinstance(shown, list) and shown else (shown if isinstance(shown, dict) else None)
        if isinstance(issue_obj, dict):
            for cmt in issue_obj.get('comments') or ():
                if not isinstance(cmt, dict):
                    continue
                raw_txt = cmt.get('text')
                if isinstance(raw_txt, str) and raw_txt.startswith(REVIEW_PREFIX):
                    crec = parse_json(raw_txt[len(REVIEW_PREFIX):])
                    if isinstance(crec, dict) and crec.get('operation') == 'contribute':
                        c_commit = str(crec.get('commit') or '').strip().lower()
                        c_deliv = crec.get('delivery')
                        if COMMIT_RE.fullmatch(c_commit) and isinstance(c_deliv, dict):
                            contrib_doc = {
                                'comment_id': str(cmt.get('id') or ''),
                                'commit': c_commit,
                                'delivery': c_deliv,
                            }
    except Exception:
        return None
    if contrib_doc is None:
        return None
    d_roots = delivery_roots(root)
    t_ref = serving_target_ref(root).get(name, 'refs/heads/main')
    max_bytes = delivery_serve_max_bytes(root)
    obs = inspect_delivery_git(
        contrib_doc,
        delivery_roots=d_roots,
        max_bundle_bytes=max_bytes,
        serving_repo=s_repo,
        target_ref=t_ref,
        needed_paths=(),
        lock_dir=None,
    )
    return dict(obs, contribution_id=contrib_doc['comment_id'], commit=contrib_doc['commit'])


def endpoint_standards_read_only(args, operation=None):
    tokens = [t for t in (args or []) if t != '--json']
    sub = tokens[0] if tokens else 'show'
    return sub in ('show', 'history', 'lint', 'draft-list', 'waivers', 'sign-off', 'notifications')


def _load_single_issue_from_run(run_fn, task_id, path):
    from briefing import task_row
    from lifecycle import host_snapshot, read_event_rows
    rows = read_event_rows(run_fn)
    rows = host_snapshot(rows, run=run_fn, project=path)
    return rows, task_row(rows, task_id)


def endpoint_standards_action(root, path, name, actor, args, attachments, run_fn, operators):
    """Contributor SSH ``standards`` action — strictly read + ``draft-propose`` only!"""
    if not isinstance(args, list) or any(not isinstance(a, str) or '\0' in a for a in args):
        raise ValueError('Expected argument list')
    tokens = [t for t in args if t != '--json']
    sub = tokens[0] if tokens else 'show'

    # Unconditional SSH refusal on ALL person-only subcommands, regardless of actor/operator/principal!
    if sub in PERSON_ONLY_SUBCOMMANDS:
        raise StandardsRefusal(refusal_ssh_person_only(sub), status=403)
    if sub not in CONTRIBUTOR_SUBCOMMANDS:
        raise ValueError(
            'Unknown standards subcommand %r; contributor SSH accepts: %s'
            % (sub, ', '.join(sorted(CONTRIBUTOR_SUBCOMMANDS)))
        )

    store = HostStandardsStore(path, name, shared_loader=host_shared_loader_for(path))

    if sub == 'show':
        ver = None
        rest = tokens[1:]
        i = 0
        while i < len(rest):
            if rest[i] == '--version' and i + 1 < len(rest):
                try:
                    ver = int(rest[i + 1])
                except ValueError as exc:
                    raise ValueError('--version requires a positive integer') from exc
                i += 2
            elif rest[i] == '--effective':
                i += 1
            else:
                raise ValueError('Usage: standards show [--version N] [--effective] [--json]')
        reg = resolve_effective_register(store, version=ver)
        reg['gate'] = get_gate_view(store)
        return reg, False

    if sub == 'history':
        if len(tokens) != 1:
            raise ValueError('Usage: standards history [--json]')
        return list_versions(store), False

    if sub == 'lint':
        if len(tokens) != 1:
            raise ValueError('Usage: standards lint [--json]')
        from lifecycle import read_event_rows
        rows = read_event_rows(run_fn)
        ref_keys = _collect_known_ref_keys(rows)
        reg = resolve_effective_register(store, known_ref_keys=ref_keys)
        return {
            'schema_version': SCHEMA_VERSION,
            'project': name,
            'status': reg['status'],
            'version': reg.get('version'),
            'warnings': reg.get('lint_warnings', []),
            'warning': reg.get('warning'),
        }, False

    if sub == 'draft-list':
        status_filter = None
        rest = tokens[1:]
        i = 0
        while i < len(rest):
            if rest[i] == '--status' and i + 1 < len(rest):
                status_filter = rest[i + 1]
                i += 2
            else:
                raise ValueError('Usage: standards draft-list [--status pending|accepted|rejected] [--json]')
        return list_drafts_view(store, status_filter=status_filter), False

    if sub == 'draft-propose':
        rest = tokens[1:]
        att_token = None
        reason_override = None
        i = 0
        while i < len(rest):
            if rest[i] in ('--file', '-f') and i + 1 < len(rest) and rest[i + 1].startswith('@attachment:'):
                att_token = rest[i + 1]
                i += 2
            elif rest[i].startswith('@attachment:'):
                att_token = rest[i]
                i += 1
            elif rest[i] == '--reason' and i + 1 < len(rest):
                reason_override = rest[i + 1]
                i += 2
            else:
                raise ValueError('Usage: standards draft-propose --file payload.json [--reason TEXT]')
        if not att_token:
            raise ValueError('Usage: standards draft-propose --file payload.json [--reason TEXT]')
        att_key = att_token.partition(':')[2]
        att = (attachments or {}).get(att_key)
        if not isinstance(att, dict) or not isinstance(att.get('text'), str):
            raise ValueError('Invalid draft attachment')
        payload = parse_json(att['text'])
        if not isinstance(payload, dict):
            raise ValueError('Draft payload must be a JSON object')
        payload = dict(payload)
        if reason_override is not None:
            payload['reason'] = reason_override
        # Accept either {'rule': {...}, 'reason': ...} or a bare rule object when --reason is supplied
        if 'rule' not in payload and 'rule_id' not in payload and 'id' in payload:
            reason_val = payload.pop('reason', None)
            action_val = payload.pop('action', 'upsert')
            payload = {'action': action_val, 'rule': payload, 'reason': reason_val}
        draft = propose_draft(store, payload, actor=actor)
        return draft, True

    if sub == 'waivers':
        rest = tokens[1:]
        if len(rest) != 1:
            raise ValueError('Usage: standards waivers <TASK> [--json]')
        task_id = rest[0]
        _, issue = _load_single_issue_from_run(run_fn, task_id, path)
        rstate = review_workflow.project(issue, operators, path)
        return list_waivers_view(store, task_id, current_contribution=rstate.get('contribution')), False

    if sub == 'sign-off':
        rest = tokens[1:]
        if not rest:
            raise ValueError('Usage: standards sign-off <TASK> [--contribution ID] [--format json|markdown|text]')
        task_id = rest[0]
        fmt = 'json'
        wanted_cid = None
        i = 1
        while i < len(rest):
            if rest[i] == '--format' and i + 1 < len(rest):
                fmt = rest[i + 1]
                if fmt not in ('json', 'markdown', 'text'):
                    raise ValueError('--format must be json, markdown, or text')
                i += 2
            elif rest[i] == '--contribution' and i + 1 < len(rest):
                wanted_cid = rest[i + 1]
                i += 2
            else:
                raise ValueError('Usage: standards sign-off <TASK> [--contribution ID] [--format json|markdown|text]')
        _, issue = _load_single_issue_from_run(run_fn, task_id, path)
        rstate = review_workflow.project(issue, operators, path)
        contrib = rstate.get('contribution')
        if wanted_cid is not None:
            cid = str(wanted_cid)
            expected_commit = contrib.get('commit') if isinstance(contrib, dict) and str(contrib.get('comment_id')) == cid else None
        else:
            if not isinstance(contrib, dict) or not contrib.get('comment_id'):
                raise StandardsRefusal('Task %s has no delivery sign-off.' % task_id, status=404)
            cid = str(contrib['comment_id'])
            expected_commit = contrib.get('commit')
        so = read_signoff_for_contribution(
            store,
            cid,
            expected_commit=expected_commit,
            approve_operation_id=find_approve_operation_id(issue, cid),
            issue=issue,
        )
        if so is None:
            raise StandardsRefusal('Task %s has no delivery sign-off for its current contribution.' % task_id, status=404)
        if fmt == 'markdown':
            if '--json' in args:
                return {'format': 'markdown', 'markdown': render_signoff_markdown(so), 'sign_off': so}, False
            return render_signoff_markdown(so), False
        if fmt == 'text':
            if '--json' in args:
                return {'format': 'text', 'text': render_signoff_text(so), 'sign_off': so}, False
            return render_signoff_text(so), False
        return so, False

    if sub == 'notifications':
        if len(tokens) != 1:
            raise ValueError('Usage: standards notifications [--json]')
        return {
            'schema_version': SCHEMA_VERSION,
            'project': name,
            'items': store.read_notifications(),
        }, False

    raise ValueError('Unsupported standards subcommand %r' % (sub,))


WEB_STANDARDS_READ_OPS = frozenset({
    'read',
    'versions',
    'draft-list',
    'commit-read',
    'waivers-read',
    'sign-off',
})
WEB_STANDARDS_PERSON_OPS = frozenset({
    'put',
    'rule-edit',
    'draft-decide',
    'waive',
    'set-gate',
    'record-commit-coordinator',
})


def _web_standards_capabilities():
    import http_authority as authority
    return {
        'read': authority.CAP_READ,
        'versions': authority.CAP_READ,
        'draft-list': authority.CAP_READ,
        'commit-read': authority.CAP_READ,
        'waivers-read': authority.CAP_READ,
        'sign-off': authority.CAP_READ,
        'draft-propose': authority.CAP_REVIEWS,
        'draft-propose-tasks': authority.CAP_TASKS,
        'put': authority.CAP_READ,
        'rule-edit': authority.CAP_READ,
        'draft-decide': authority.CAP_READ,
        'waive': authority.CAP_READ,
        'set-gate': authority.CAP_COORDINATE,
        'record-commit-coordinator': authority.CAP_COORDINATE,
        'record-commit-verifier': authority.CAP_STANDARDS_VERIFY,
    }


def web_standards_request_operation(request):
    args = request.get('args')
    if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], str):
        raise ValueError('Supply one canonical web-standards JSON body')
    body = parse_json(args[0])
    if not isinstance(body, dict) or not isinstance(body.get('operation'), str):
        raise ValueError('Supply one web-standards operation')
    op = body['operation']
    if op not in _web_standards_capabilities():
        raise ValueError('Unknown web-standards operation %r' % (op,))
    return op


def web_standards_read_only(args, operation=None):
    if operation is None:
        try:
            body = parse_json(args[0]) if isinstance(args, list) and len(args) == 1 else {}
            operation = body.get('operation') if isinstance(body, dict) else None
        except Exception:
            return False
    return operation in WEB_STANDARDS_READ_OPS


class _StandardsFileEffect:
    attempted_write = False
    wrote = False

    def mark_write(self):
        self.attempted_write = True
        self.wrote = True


def _build_state_person_helpers(state, project_id):
    import http_authority as authority

    def _known_person(uid):
        if not isinstance(uid, str) or not uid:
            return False
        user = (state.get('users') or {}).get(uid)
        return isinstance(user, dict) and not user.get('disabled')

    def _active_person(uid):
        if not _known_person(uid):
            return False
        user = state['users'][uid]
        if user.get('superuser'):
            return True
        role = (state.get('memberships') or {}).get(project_id, {}).get(uid)
        return role is not None

    def _same_party_pair(left, right):
        p_left = authority.actor_parties(state, left, project_id)
        p_right = authority.actor_parties(state, right, project_id)
        return bool(p_left & p_right)

    return _known_person, _active_person, _same_party_pair


def web_standards_action(root, path, project, request, authority_config, operators, run_fn):
    """Service-only ``web-standards`` action with live HTTP authority verification."""
    import http_authority as authority

    try:
        return _web_standards_action_inner(
            root, path, project, request, authority_config, operators, run_fn
        )
    except authority.AuthorityDenied as denied:
        return {
            'returncode': 126,
            'stdout': '',
            'stderr': denied.message + '\n',
            'authority_status': denied.status,
        }
    except StandardsRefusal as err:
        return {
            'returncode': 2,
            'stdout': '',
            'stderr': str(err) + '\n',
            'refused': 'coordinator-rule',
            'standards_status': err.status,
        }


def _web_standards_action_inner(root, path, project, request, authority_config, operators, run_fn):
    import http_authority as authority

    args = request.get('args')
    if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], str):
        raise ValueError('Supply one canonical web-standards JSON body')
    body = parse_json(args[0])
    if not isinstance(body, dict) or not isinstance(body.get('operation'), str):
        raise ValueError('Supply one web-standards operation')
    operation = body['operation']
    caps_map = _web_standards_capabilities()
    if operation not in caps_map:
        raise ValueError('Unknown web-standards operation %r' % (operation,))
    capability = caps_map[operation]

    def authorize():
        descriptor = request.get('authority')
        if authority_config is None or not isinstance(descriptor, dict):
            raise authority.deny(
                401, 'unauthenticated', 'Standards web operations are available only to the authenticated web service'
            )
        if (
            request.get('project') != project
            or descriptor.get('project') != project
            or descriptor.get('capability') != capability
        ):
            raise authority.deny(
                403, 'forbidden', 'Standards authority must name this project and operation capability'
            )
        clean_desc = {k: v for k, v in descriptor.items() if k not in ('store', 'lock')}
        state = authority.read_state(authority_config.store)
        decision = authority.decide(state, clean_desc)
        if clean_desc.get('via') == 'session':
            record = (state.get('sessions') or {}).get(clean_desc.get('session_hash')) or {}
            expected = record.get('user_id')
        else:
            record = (state.get('credentials') or {}).get(clean_desc.get('credential_id')) or {}
            expected = record.get('agent_id') or record.get('actor') or record.get('user_id')
        if record.get('user_id') != clean_desc.get('user_id') or not expected or request.get('actor') != expected:
            raise authority.deny(
                403, 'forbidden', 'Standards uses only the authenticated account, agent, or credential identity'
            )
        project_rec = (state.get('projects') or {}).get(project) or {}
        if operation not in WEB_STANDARDS_READ_OPS and project_rec.get('archived'):
            raise authority.deny(409, 'conflict', 'Project is archived and read-only')

        if operation in WEB_STANDARDS_PERSON_OPS:
            if (
                clean_desc.get('via') != 'session'
                or clean_desc.get('credential_id') is not None
                or not isinstance(clean_desc.get('session_hash'), str)
                or not clean_desc.get('session_hash')
            ):
                if operation == 'set-gate':
                    raise StandardsRefusal(refusal_gate_switch_authority(), status=403)
                if operation == 'waive':
                    raise StandardsRefusal(refusal_waiver_agent(), status=403)
                if operation == 'record-commit-coordinator':
                    raise StandardsRefusal(
                        refusal_commit_observation_authority(str(body.get('commit') or '')),
                        status=403,
                    )
                raise StandardsRefusal(refusal_agent_register_edit(), status=403)

        if operation == 'record-commit-verifier':
            if clean_desc.get('via') != 'credential':
                raise StandardsRefusal(
                    refusal_commit_observation_authority(str(body.get('commit') or '')), status=403
                )
            cred = (state.get('credentials') or {}).get(clean_desc.get('credential_id')) or {}
            if list(cred.get('scopes') or []) != [authority.VERIFY_SCOPE] or cred.get('agent_id'):
                raise StandardsRefusal(
                    refusal_commit_observation_authority(str(body.get('commit') or '')), status=403
                )

        return state, clean_desc, decision

    store = HostStandardsStore(path, project, shared_loader=host_shared_loader_for(path))

    if operation in WEB_STANDARDS_READ_OPS:
        if authority_config is None:
            authorize()
        with authority.file_lock(authority_config.lock):
            state, clean_desc, _ = authorize()
            _, active_person, _ = _build_state_person_helpers(state, project)
            if operation == 'read':
                ver = body.get('version')
                reg = resolve_effective_register(
                    store, version=ver, active_person_checker=active_person
                )
                reg['gate'] = get_gate_view(store)
                result = reg
            elif operation == 'versions':
                result = list_versions(store)
            elif operation == 'draft-list':
                result = list_drafts_view(store, status_filter=body.get('status'))
            elif operation == 'commit-read':
                commit = str(body.get('commit') or '').lower()
                obs = store.read_commit_observation(commit)
                if obs is None:
                    raise StandardsRefusal(
                        'No commit standards observation recorded for %s.' % commit, status=404
                    )
                result = obs
            elif operation == 'waivers-read':
                task_id = str(body.get('task_id') or '')
                _, issue = _load_single_issue_from_run(run_fn, task_id, path)
                rstate = review_workflow.project(issue, operators, path)
                result = list_waivers_view(store, task_id, current_contribution=rstate.get('contribution'))
            else:  # sign-off
                task_id = str(body.get('task_id') or '')
                _, issue = _load_single_issue_from_run(run_fn, task_id, path)
                rstate = review_workflow.project(issue, operators, path)
                contrib = rstate.get('contribution')
                wanted_cid = body.get('contribution_id')
                if wanted_cid is not None:
                    cid = str(wanted_cid)
                    expected_commit = contrib.get('commit') if isinstance(contrib, dict) and str(contrib.get('comment_id')) == cid else None
                else:
                    if not isinstance(contrib, dict) or not contrib.get('comment_id'):
                        raise StandardsRefusal('Task %s has no delivery sign-off.' % task_id, status=404)
                    cid = str(contrib['comment_id'])
                    expected_commit = contrib.get('commit')
                so = read_signoff_for_contribution(
                    store,
                    cid,
                    expected_commit=expected_commit,
                    approve_operation_id=find_approve_operation_id(issue, cid),
                    issue=issue,
                )
                if so is None:
                    raise StandardsRefusal(
                        'Task %s has no delivery sign-off for its current contribution.' % task_id,
                        status=404,
                    )
                if body.get('format') == 'markdown':
                    result = {'sign_off': so, 'markdown': render_signoff_markdown(so)}
                else:
                    result = so
        return {'returncode': 0, 'stdout': canonical_bytes(result).decode('utf-8') + '\n', 'stderr': ''}

    op_id = request.get('operation_id')
    if not isinstance(op_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', op_id):
        raise ValueError('A stable web-standards operation identity is required')

    tracker = _StandardsFileEffect()
    captured = {}

    def _authorize_capture():
        state, clean_desc, decision = authorize()
        captured['state'] = state
        captured['desc'] = clean_desc
        captured['decision'] = decision
        return state

    def effect():
        state = captured['state']
        clean_desc = captured['desc']
        actor = request['actor']
        user_id = clean_desc.get('user_id')
        user_rec = (state.get('users') or {}).get(user_id) or {}
        role = (state.get('memberships') or {}).get(project, {}).get(user_id)
        is_coord_or_owner = bool(user_rec.get('superuser') or role in ('coordinator', 'owner'))
        is_person_sess = (
            clean_desc.get('via') == 'session'
            and clean_desc.get('credential_id') is None
            and isinstance(clean_desc.get('session_hash'), str)
            and bool(clean_desc.get('session_hash'))
        )
        known_person, active_person, same_party_pair = _build_state_person_helpers(state, project)
        payload = body.get('payload') or {}

        def _can_read_other(other_pid):
            try:
                authority.decide(
                    state,
                    dict(clean_desc, project=other_pid, capability=authority.CAP_READ),
                )
                return True
            except authority.AuthorityDenied:
                return False

        if operation in ('draft-propose', 'draft-propose-tasks'):
            result = propose_draft(
                store,
                payload,
                actor=actor,
                author_person=user_id,
                known_person_checker=known_person,
            )
        elif operation == 'put':
            from lifecycle import read_event_rows
            ref_keys = _collect_known_ref_keys(read_event_rows(run_fn))
            result = put_register(
                store,
                payload,
                actor=actor,
                author_person=user_id,
                is_person_session=is_person_sess,
                is_coordinator_or_owner=is_coord_or_owner,
                via='web',
                can_read_project=_can_read_other,
                known_person_checker=known_person,
                active_person_checker=active_person,
                known_ref_keys=ref_keys,
            )
        elif operation == 'rule-edit':
            from lifecycle import read_event_rows
            ref_keys = _collect_known_ref_keys(read_event_rows(run_fn))
            result = edit_rule(
                store,
                str(body.get('rule_id') or ''),
                payload,
                actor=actor,
                author_person=user_id,
                is_person_session=is_person_sess,
                is_coordinator_or_owner=is_coord_or_owner,
                via='web',
                known_person_checker=known_person,
                active_person_checker=active_person,
                known_ref_keys=ref_keys,
            )
        elif operation == 'draft-decide':
            from lifecycle import read_event_rows
            ref_keys = _collect_known_ref_keys(read_event_rows(run_fn))
            result = decide_draft(
                store,
                str(body.get('draft_id') or ''),
                payload,
                actor=actor,
                author_person=user_id,
                is_person_session=is_person_sess,
                is_coordinator_or_owner=is_coord_or_owner,
                via='web',
                known_person_checker=known_person,
                active_person_checker=active_person,
                known_ref_keys=ref_keys,
            )
        elif operation == 'set-gate':
            result = set_gate_mode(
                store,
                payload.get('mode'),
                payload.get('reason'),
                actor=actor,
                author_person=user_id,
                is_person_session=is_person_sess,
                is_coordinator_or_owner=is_coord_or_owner,
                via='web',
            )
        elif operation == 'waive':
            task_id = str(body.get('task_id') or '')
            _, issue = _load_single_issue_from_run(run_fn, task_id, path)
            rstate = review_workflow.project(issue, operators, path)
            contrib = rstate.get('contribution')
            pinned_ver, _, _ = resolve_pinned_version(store, contrib)
            import sessions
            try:
                host_owners = sessions.owners(path)
            except Exception:
                host_owners = {}

            def _waive_same_party(other):
                if review_workflow.same_party(actor, other, host_owners):
                    return True
                if user_id and review_workflow.same_party(user_id, other, host_owners):
                    return True
                if same_party_pair(actor, other):
                    return True
                if user_id and same_party_pair(user_id, other):
                    return True
                return False

            result = grant_waiver(
                store,
                payload,
                task_id=task_id,
                contribution=contrib,
                task_assignee=issue.get('assignee'),
                actor=actor,
                author_person=user_id,
                is_person_session=is_person_sess,
                is_coordinator_or_owner=is_coord_or_owner,
                same_party_checker=_waive_same_party,
                active_person_checker=active_person,
                pinned_version=pinned_ver,
            )
        elif operation in ('record-commit-coordinator', 'record-commit-verifier'):
            from lifecycle import host_snapshot, read_event_rows
            rows = host_snapshot(read_event_rows(run_fn), run=run_fn, project=path)
            import sessions
            try:
                host_owners = sessions.owners(path)
            except Exception:
                host_owners = {}

            def _rec_same_party(other):
                if review_workflow.same_party(actor, other, host_owners):
                    return True
                if user_id and review_workflow.same_party(user_id, other, host_owners):
                    return True
                if same_party_pair(actor, other):
                    return True
                if user_id and same_party_pair(user_id, other):
                    return True
                return False

            is_ver_or_coord = (
                True
                if operation == 'record-commit-verifier'
                else bool(is_person_sess and is_coord_or_owner)
            )
            result = record_commit_observation(
                store,
                str(body.get('commit') or ''),
                payload,
                actor=actor,
                recorded_by_user_id=user_id,
                recorded_by_principal=user_id,
                is_authorized_recorder=is_ver_or_coord,
                same_party_checker=_rec_same_party,
                rows_or_tasks=rows,
                operators=operators,
                journal=path,
            )
        else:
            raise ValueError('Unsupported write operation %r' % (operation,))
        tracker.mark_write()
        return {'returncode': 0, 'stdout': canonical_bytes(result).decode('utf-8') + '\n', 'stderr': ''}

    return authority.run_guarded(
        request,
        authority.journal_path(path),
        effect,
        authority_config=authority_config,
        require_authority=True,
        identity_check=_authorize_capture,
        runner=tracker,
    )


def admin_standards_command(root, path, project, args, run_bd_fn, operators_list, verifiers_list):
    """Host operator ``admin.py standards ...`` command implementation."""
    import fcntl
    import sessions
    from keyed_records import require_configured_operator

    op = args.standards_op
    store = HostStandardsStore(path, project, shared_loader=host_shared_loader_for(path))

    def _run(argv):
        return run_bd_fn(root, project, argv)

    if op == 'show':
        with (path / '.coordination.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            reg = resolve_effective_register(store, version=args.version)
            reg['gate'] = get_gate_view(store)
        return reg

    if op == 'history':
        with (path / '.coordination.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            return list_versions(store)

    if op == 'draft-list':
        with (path / '.coordination.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            return list_drafts_view(store, status_filter=getattr(args, 'status', None))

    if op == 'waivers':
        if not getattr(args, 'task', None):
            raise ValueError('--task is required for admin standards waivers')
        with (path / '.coordination.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            _, issue = _load_single_issue_from_run(_run, args.task, path)
            rstate = review_workflow.project(issue, operators_list, path)
            return list_waivers_view(store, args.task, current_contribution=rstate.get('contribution'))

    if op == 'sign-off':
        if not getattr(args, 'task', None):
            raise ValueError('--task is required for admin standards sign-off')
        fmt = getattr(args, 'std_format', None) or 'json'
        wanted_cid = getattr(args, 'contribution', None)
        with (path / '.coordination.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            _, issue = _load_single_issue_from_run(_run, args.task, path)
            rstate = review_workflow.project(issue, operators_list, path)
            contrib = rstate.get('contribution')
            if wanted_cid is not None:
                cid = str(wanted_cid)
                expected_commit = (
                    contrib.get('commit')
                    if isinstance(contrib, dict) and str(contrib.get('comment_id')) == cid
                    else None
                )
            else:
                if not isinstance(contrib, dict) or not contrib.get('comment_id'):
                    raise StandardsRefusal('Task %s has no delivery sign-off.' % args.task, status=404)
                cid = str(contrib['comment_id'])
                expected_commit = contrib.get('commit')
            so = read_signoff_for_contribution(
                store,
                cid,
                expected_commit=expected_commit,
                approve_operation_id=find_approve_operation_id(issue, cid),
                issue=issue,
            )
        if so is None:
            raise StandardsRefusal(
                'Task %s has no delivery sign-off for its current contribution.' % args.task,
                status=404,
            )
        if fmt == 'markdown':
            return render_signoff_markdown(so)
        if fmt == 'text':
            return render_signoff_text(so)
        return so

    if op == 'lint':
        with (path / '.coordination.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            from lifecycle import read_event_rows
            ref_keys = _collect_known_ref_keys(read_event_rows(_run))
            reg = resolve_effective_register(store, known_ref_keys=ref_keys)
        return {
            'schema_version': SCHEMA_VERSION,
            'project': project,
            'status': reg['status'],
            'version': reg.get('version'),
            'warnings': reg.get('lint_warnings', []),
            'warning': reg.get('warning'),
        }

    if op == 'notifications':
        with (path / '.coordination.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            items = store.read_notifications()
        return {'schema_version': SCHEMA_VERSION, 'project': project, 'items': items}

    if op == 'record-commit':
        actor = args.actor
        if not isinstance(actor, str) or not actor.strip():
            raise StandardsRefusal(refusal_commit_observation_authority(args.commit), status=403)
        allowed_recorders = set(operators_list or ()) | set(verifiers_list or ())
        if actor not in allowed_recorders:
            raise StandardsRefusal(refusal_commit_observation_authority(args.commit), status=403)
        raw_text = read_regular_text(args.file, limit=MAX_FILE_BYTES * 2)
        payload = parse_json(raw_text)
        if isinstance(payload, dict) and getattr(args, 'task', None) and payload.get('task_id') is None:
            payload = dict(payload, task_id=args.task)
        with (path / '.coordination.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                host_owners = sessions.owners(path)
            except Exception:
                host_owners = {}
            from lifecycle import host_snapshot, read_event_rows
            rows = host_snapshot(read_event_rows(_run), run=_run, project=path)
            return record_commit_observation(
                store,
                args.commit,
                payload,
                actor=actor,
                recorded_by_user_id=actor,
                recorded_by_principal=host_owners.get(actor, actor),
                is_authorized_recorder=True,
                same_party_checker=lambda other: review_workflow.same_party(actor, other, host_owners),
                rows_or_tasks=rows,
                operators=operators_list,
                journal=path,
            )

    # All remaining operations are person-only host operator writes!
    require_configured_operator(args.actor, operators_list, 'standards %s' % op)
    actor = args.actor

    with (path / '.coordination.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if op == 'put':
            raw_text = read_regular_text(args.file, limit=MAX_FILE_BYTES * 2)
            payload = parse_json(raw_text)
            if isinstance(payload, dict):
                if args.expected_version is not None:
                    payload = dict(payload, expected_version=args.expected_version)
                if args.reason is not None:
                    payload = dict(payload, reason=args.reason)
            from lifecycle import read_event_rows
            ref_keys = _collect_known_ref_keys(read_event_rows(_run))
            return put_register(
                store,
                payload,
                actor=actor,
                author_person=actor,
                is_person_session=True,
                is_coordinator_or_owner=True,
                via='admin',
                known_ref_keys=ref_keys,
            )

        if op == 'rule-edit':
            raw_text = read_regular_text(args.file, limit=MAX_FILE_BYTES * 2)
            payload = parse_json(raw_text)
            if isinstance(payload, dict):
                if args.expected_version is not None:
                    payload = dict(payload, expected_version=args.expected_version)
                if args.reason is not None:
                    payload = dict(payload, reason=args.reason)
            from lifecycle import read_event_rows
            ref_keys = _collect_known_ref_keys(read_event_rows(_run))
            return edit_rule(
                store,
                args.rule,
                payload,
                actor=actor,
                author_person=actor,
                is_person_session=True,
                is_coordinator_or_owner=True,
                via='admin',
                known_ref_keys=ref_keys,
            )

        if op == 'set-gate':
            return set_gate_mode(
                store,
                args.mode,
                args.reason,
                actor=actor,
                author_person=actor,
                is_person_session=True,
                is_coordinator_or_owner=True,
                via='admin',
            )

        if op == 'waive':
            try:
                host_owners = sessions.owners(path)
            except Exception:
                host_owners = {}
            _, issue = _load_single_issue_from_run(_run, args.task, path)
            rstate = review_workflow.project(issue, operators_list, path)
            contrib = rstate.get('contribution')
            pinned_ver, _, _ = resolve_pinned_version(store, contrib)
            payload = {
                'rule_id': args.rule,
                'contribution_id': args.contribution,
                'commit': args.commit,
                'reason': args.reason,
            }
            if args.until is not None:
                payload['until'] = args.until
            return grant_waiver(
                store,
                payload,
                task_id=args.task,
                contribution=contrib,
                task_assignee=issue.get('assignee'),
                actor=actor,
                author_person=actor,
                is_person_session=True,
                is_coordinator_or_owner=True,
                same_party_checker=lambda other: review_workflow.same_party(actor, other, host_owners),
                pinned_version=pinned_ver,
            )

        if op == 'draft-decide':
            payload = {'decision': args.decision}
            if args.expected_version is not None:
                payload['expected_version'] = args.expected_version
            if args.reason is not None:
                payload['reason'] = args.reason
            from lifecycle import read_event_rows
            ref_keys = _collect_known_ref_keys(read_event_rows(_run))
            return decide_draft(
                store,
                args.draft,
                payload,
                actor=actor,
                author_person=actor,
                is_person_session=True,
                is_coordinator_or_owner=True,
                via='admin',
                known_ref_keys=ref_keys,
            )

        if op == 'owner-departed':
            return store.mark_owner_departed(args.owner, actor, args.reason)

    raise ValueError('Unknown admin standards operation %r' % (op,))


