"""Episode-keyed, transparent wait snoozes (kittrial-5bb.598 Slice 2, §2.3, SHOULD-1/5).

A snooze is a reserved native comment ``Kind: wait-snooze-v1\\n{canonical_json}`` paired with
a host journal entry in ``<project>/.wait-snoozes/<sha256>.json`` written under the exclusive
project lock. Plain reads are strictly read-only and honour only journal-backed snoozes.

Core guarantees:
- **Never hides a row:** Snoozes remain visible on every list, board, and count, and expire
  automatically once ``now >= until``.
- **Always bounded (422):** Requires ``reason`` (1..300 single-line printable chars) and a
  strictly future ``until`` ISO-8601 timestamp with seconds and timezone offset, at most
  ``SNOOZE_MAX_AHEAD_DAYS`` (7 days) ahead. Missing ``until`` is refused (422).
- **Anti-self-suppression (SHOULD-1):** A snooze issued by a member/party of ``waiting_on``
  (or the silent lane itself) sets ``waited_on_party=True`` and ``suppresses_owner_notice=False``,
  so the +4 h owner escalation still fires with the snoozer, reason, and ``until``.
- **Renewal cap (409):** At most ``MAX_SNOOZES_PER_EPISODE`` (2) snoozes per ``episode_id``
  (and total span <= 7 days from the first snooze); a 3rd snooze is refused (409) unless
  issued by a human project owner who is NOT the waited-on party.
- **Wake isolation (SHOULD-5):** ``Kind: wait-snooze-v1`` comments are excluded from
  ``briefing.wait_activity_marker`` and ``waiting.comment_pointers`` so snoozing never wakes
  a waiting lane.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import uuid

import lane_state
import record_json
from requirements import canonical_bytes, content_hash
from reserved_comments import WAIT_SNOOZE_PREFIX as PREFIX

KIND = 'wait-snooze-v1'
JOURNAL_KIND = 'wait-snooze'
JOURNAL_DIR = '.wait-snoozes'
REASON_LIMIT = 300
SNOOZE_MAX_AHEAD_DAYS = 7
MAX_SNOOZES_PER_EPISODE = 2
JOURNAL_FILE_LIMIT = 65536
MAX_JOURNAL_FILES = 10000

REQUEST_FIELDS = frozenset({'schema_version', 'task', 'episode_id', 'reason', 'until'})
REQUIRED_REQUEST_FIELDS = frozenset({'task', 'episode_id', 'reason', 'until'})
PAYLOAD_FIELDS = frozenset({
    'schema_version', 'task', 'episode_id', 'reason', 'until',
    'actor', 'actor_display', 'waited_on_party', 'suppresses_owner_notice', 'created_at',
})
JOURNAL_FIELDS = frozenset({
    'schema_version', 'kind', 'task', 'episode_id', 'comment_id',
    'actor', 'payload', 'sha256', 'created_at',
})

IDENTITY_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}\Z')
EPISODE_RE = re.compile(r'[A-Za-z0-9_-]{8,64}\Z')
ACTOR_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@:/-]{0,127}\Z')


class SnoozeRefusal(ValueError):
    """Structured refusal carrying an HTTP status code (422 validation, 409 conflict, 404/403)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = int(status)


def _iso_utc(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _refuse(status, message):
    return SnoozeRefusal(status, message.rstrip() + ' Nothing was written.')


def _validate_reason(value):
    if not isinstance(value, str) or not value.strip():
        raise _refuse(422, 'A snooze reason must be nonempty single-line text.')
    if len(value) > REASON_LIMIT:
        raise _refuse(422, 'A snooze reason is limited to %d characters.' % REASON_LIMIT)
    if '\n' in value or '\r' in value or not value.isprintable():
        raise _refuse(422, 'A snooze reason must be one line without control or direction characters.')
    return value.strip()


def _validate_until(value, now):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise _refuse(422, "A snooze requires an explicit future 'until' timestamp (ISO-8601 with seconds and offset).")
    if not isinstance(value, str) or not lane_state.NOT_BEFORE_SHAPE.fullmatch(value.strip()):
        raise _refuse(422, "Invalid 'until' timestamp: expected full ISO-8601 with seconds and timezone offset.")
    raw = value.strip()
    try:
        dt = lane_state.parse_moment(raw, 'until')
    except ValueError as exc:
        raise _refuse(422, str(exc)) from None
    now_utc = now.astimezone(timezone.utc)
    if dt <= now_utc:
        raise _refuse(422, "A snooze 'until' timestamp must be strictly in the future.")
    if dt > now_utc + timedelta(days=SNOOZE_MAX_AHEAD_DAYS):
        raise _refuse(422, "A snooze 'until' timestamp may be at most %d days ahead." % SNOOZE_MAX_AHEAD_DAYS)
    return _iso_utc(dt), dt


def validate_request(body, *, now=None):
    """Validate a caller snooze request before any write. Raises SnoozeRefusal(422) on error."""
    if not isinstance(body, dict):
        raise _refuse(422, 'Snooze request must be a JSON object.')
    unknown = sorted(set(body) - REQUEST_FIELDS)
    if unknown:
        raise _refuse(422, 'Snooze request carries unknown field(s): %s.'
                      % ', '.join(str(k) for k in unknown[:5]))
    if 'schema_version' in body:
        sv = body['schema_version']
        if type(sv) is not int or sv != 1:
            raise _refuse(422, 'Snooze request schema_version must be 1.')
    task = body.get('task')
    if not isinstance(task, str) or not IDENTITY_RE.fullmatch(task):
        raise _refuse(422, 'Snooze request requires a valid task id.')
    episode_id = body.get('episode_id')
    if not isinstance(episode_id, str) or not EPISODE_RE.fullmatch(episode_id):
        raise _refuse(422, 'Snooze request requires a valid episode_id.')
    reason = _validate_reason(body.get('reason'))
    if now is None:
        now = datetime.now(timezone.utc)
    until_iso, until_dt = _validate_until(body.get('until'), now)
    return {
        'task': task,
        'episode_id': episode_id,
        'reason': reason,
        'until': until_iso,
        'until_dt': until_dt,
    }


def comment_body(payload):
    """Exact native comment body for a validated snooze payload."""
    return PREFIX + canonical_bytes(payload).decode('utf-8')


def parse_text(text):
    """Parse a ``Kind: wait-snooze-v1`` comment payload, or raise ValueError."""
    if not isinstance(text, str) or not text.startswith(PREFIX):
        raise ValueError('not a wait snooze record')
    raw = text[len(PREFIX):]
    try:
        data = record_json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError('not canonical JSON') from None
    if not isinstance(data, dict) or canonical_bytes(data).decode('utf-8') != raw:
        raise ValueError('not canonical JSON')
    return data


def parse_comment_add_id(stdout):
    """Extract the created comment id from ``bd comments add --json`` stdout via ``record_json.loads``."""
    try:
        parsed = record_json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict) and parsed.get('id') is not None:
        return str(parsed['id'])
    return None


def is_waited_on_party(actor, wait_record, *, resolver=None, caller_user_id=None,
                       authority_state=None, project=None):
    """True when ``actor`` belongs to the party being waited on (SHOULD-1 anti-self-suppression).

    - For a role wait (``coordinator``, ``reviewer``, ``user``): True when ``actor`` (or
      ``caller_user_id``) holds that role in the project.
    - For a named actor wait: True when ``actor`` / ``caller_user_id`` matches ``waiting_on``
      or shares the same owning principal/party.
    - For a silent lane (``waiting_on is None``): True when ``actor`` is the silent lane itself.
    """
    from briefing import same_activity_actor
    target = wait_record.get('waiting_on') if isinstance(wait_record, dict) else None
    candidates = [c for c in (actor, caller_user_id) if isinstance(c, str) and c]
    if not target:
        lane_actor = wait_record.get('actor') if isinstance(wait_record, dict) else None
        return any(same_activity_actor(c, lane_actor) for c in candidates) if lane_actor else False
    target_l = str(target).lower()
    if target_l in ('coordinator', 'reviewer', 'user'):
        if resolver is not None:
            for cand in candidates:
                if target_l in resolver.caller_roles(cand):
                    return True
        if isinstance(authority_state, dict) and project:
            members = (authority_state.get('memberships') or {}).get(project) or {}
            for cand in candidates:
                role = members.get(cand)
                if target_l == 'coordinator' and role in ('coordinator', 'owner'):
                    return True
                if target_l == 'reviewer' and role in ('coordinator', 'owner'):
                    return True
                if target_l == 'user':
                    lane_actor = wait_record.get('actor') if isinstance(wait_record, dict) else None
                    agent = (authority_state.get('agents') or {}).get(str(lane_actor or '').split('/', 1)[0])
                    if isinstance(agent, dict) and agent.get('owner') == cand:
                        return True
        return False
    # Named actor target
    for cand in candidates:
        if same_activity_actor(cand, target):
            return True
    # Also check same owning human if authority_state maps agent -> owner
    if isinstance(authority_state, dict):
        agents = authority_state.get('agents') or {}
        target_agent = agents.get(str(target).split('/', 1)[0])
        target_owner = target_agent.get('owner') if isinstance(target_agent, dict) else None
        for cand in candidates:
            if target_owner and cand == target_owner:
                return True
            cand_agent = agents.get(str(cand).split('/', 1)[0])
            cand_owner = cand_agent.get('owner') if isinstance(cand_agent, dict) else None
            if cand_owner and (cand_owner == target or (target_owner and cand_owner == target_owner)):
                return True
    return False


def check_renewal_allowed(prior_entries, *, until_dt, now, waited_on_party,
                          is_human_project_owner=False):
    """Enforce MAX_SNOOZES_PER_EPISODE = 2 and 7-day total span (409 unless non-waited-on human owner)."""
    override = bool(is_human_project_owner and not waited_on_party)
    if len(prior_entries) >= MAX_SNOOZES_PER_EPISODE and not override:
        raise _refuse(
            409,
            'Snooze renewal limit reached (%d per episode); only a human project owner who is not '
            'the waited-on party may extend further.' % MAX_SNOOZES_PER_EPISODE,
        )
    if prior_entries and not override:
        first_times = []
        for item in prior_entries:
            ts = item.get('created_at')
            if isinstance(ts, str) and ts:
                try:
                    first_times.append(lane_state.parse_moment(ts, 'created_at'))
                except ValueError:
                    pass
        if first_times:
            earliest = min(first_times)
            if until_dt > earliest + timedelta(days=SNOOZE_MAX_AHEAD_DAYS):
                raise _refuse(
                    409,
                    'Snooze renewals on one episode cannot extend more than %d days from the first snooze.'
                    % SNOOZE_MAX_AHEAD_DAYS,
                )


def _validate_journal_entry(data):
    if not isinstance(data, dict):
        return None
    if data.get('schema_version') != 1 or data.get('kind') != JOURNAL_KIND:
        return None
    payload = data.get('payload')
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get('episode_id'), str) or not EPISODE_RE.fullmatch(payload['episode_id']):
        return None
    if not isinstance(payload.get('task'), str) or not IDENTITY_RE.fullmatch(payload['task']):
        return None
    if not isinstance(payload.get('reason'), str) or not payload['reason'].strip():
        return None
    if not isinstance(payload.get('until'), str):
        return None
    try:
        lane_state.parse_moment(payload['until'], 'until')
    except ValueError:
        return None
    if data.get('sha256') != content_hash(payload):
        return None
    return payload


def load_snoozes(journal):
    """Read-only loader of ``<project>/.wait-snoozes/*.json``. Refuses symlinks; never writes.

    Returns ``{episode_id: [payload, ...]}`` sorted oldest-first by ``(created_at, until)``.
    """
    if journal is None:
        return {}
    root = Path(journal) / JOURNAL_DIR
    if root.is_symlink() or not root.is_dir():
        return {}
    grouped = {}
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return {}
    count = 0
    for item in entries:
        if item.is_symlink() or not item.is_file() or item.suffix != '.json':
            continue
        count += 1
        if count > MAX_JOURNAL_FILES:
            break
        try:
            if item.stat().st_size > JOURNAL_FILE_LIMIT:
                continue
            data = record_json.loads(item.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        payload = _validate_journal_entry(data)
        if payload is None:
            continue
        grouped.setdefault(payload['episode_id'], []).append(payload)
    for eid in grouped:
        grouped[eid].sort(key=lambda p: (str(p.get('created_at') or ''), str(p.get('until') or '')))
    return grouped


def active_snooze(entries, *, now):
    """Return ``(active_view_or_None, total_snooze_count)`` for one episode at ``now``."""
    if not entries:
        return None, 0
    now_utc = now.astimezone(timezone.utc)
    ordered = sorted(entries, key=lambda p: (str(p.get('created_at') or ''), str(p.get('until') or '')))
    active = None
    for p in ordered:
        try:
            until_dt = lane_state.parse_moment(p['until'], 'until')
        except (ValueError, KeyError, TypeError):
            continue
        if until_dt > now_utc:
            active = {
                'reason': lane_state.display_reason(p['reason']),
                'until': _iso_utc(until_dt),
                'actor': p.get('actor'),
                'actor_display': p.get('actor_display') or p.get('actor'),
                'waited_on_party': bool(p.get('waited_on_party')),
                'suppresses_owner_notice': bool(p.get('suppresses_owner_notice')),
                'created_at': p.get('created_at'),
            }
    return active, len(ordered)


def open_journal_dir(project):
    """Return writable ``<project>/.wait-snoozes/`` for a writer; refuses symlinked dirs."""
    if project is None:
        raise _refuse(422, 'Recording a snooze requires the project directory for %s.' % JOURNAL_DIR)
    directory = Path(project) / JOURNAL_DIR
    if directory.is_symlink():
        raise _refuse(422, 'Refusing %s: the path is a symlink.' % JOURNAL_DIR)
    try:
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        raise _refuse(422, 'Cannot create %s: %s.' % (JOURNAL_DIR, exc)) from None
    if not directory.is_dir():
        raise _refuse(422, '%s is not a directory.' % JOURNAL_DIR)
    return directory


def write_journal_entry(project, payload, comment_id=None):
    """Atomically write one snooze journal entry to ``<project>/.wait-snoozes/<sha256>.json``."""
    directory = open_journal_dir(project)
    digest = content_hash(payload)
    entry = {
        'schema_version': 1,
        'kind': JOURNAL_KIND,
        'task': payload['task'],
        'episode_id': payload['episode_id'],
        'comment_id': str(comment_id) if comment_id is not None else None,
        'actor': payload['actor'],
        'payload': payload,
        'sha256': digest,
        'created_at': payload['created_at'],
    }
    target = directory / (digest + '.json')
    if target.is_symlink():
        raise _refuse(422, 'Refusing to overwrite symlinked snooze journal file.')
    tmp = directory / ('.snooze-%s.tmp' % uuid.uuid4().hex)
    raw = canonical_bytes(entry) + b'\n'
    try:
        tmp.write_bytes(raw)
        if os.name != 'nt':
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise _refuse(422, 'Cannot write snooze journal entry: %s.' % exc) from None
    return entry


def build_snooze_payload(validated, *, actor, actor_display, waited_on_party, now):
    """Build the canonical ``wait-snooze-v1`` payload dict."""
    if not isinstance(actor, str) or not ACTOR_RE.fullmatch(actor):
        raise _refuse(422, 'Invalid snooze actor identity.')
    return {
        'schema_version': 1,
        'task': validated['task'],
        'episode_id': validated['episode_id'],
        'reason': validated['reason'],
        'until': validated['until'],
        'actor': actor,
        'actor_display': str(actor_display or actor)[:128],
        'waited_on_party': bool(waited_on_party),
        'suppresses_owner_notice': not bool(waited_on_party),
        'created_at': _iso_utc(now),
    }


def apply(body, *, actor, wait_record, now=None, actor_display=None, caller_user_id=None,
          journal=None, existing_entries=None, resolver=None, authority_state=None,
          project=None, is_human_project_owner=False, write_comment=None):
    """Validate, enforce renewal/anti-self-suppression rules, and persist one wait snooze.

    ``write_comment`` (optional callable taking ``(task_id, comment_text)`` -> ``comment_id``)
    writes the reserved ``Kind: wait-snooze-v1`` comment. When ``journal`` is provided, also
    writes the host journal entry under ``<project>/.wait-snoozes/``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    validated = validate_request(body, now=now)
    if not isinstance(wait_record, dict) or wait_record.get('task') != validated['task'] \
            or wait_record.get('episode_id') != validated['episode_id']:
        raise _refuse(404, 'No matching open wait or silent lane for that task and episode_id.')
    waited_on = is_waited_on_party(
        actor, wait_record, resolver=resolver, caller_user_id=caller_user_id,
        authority_state=authority_state, project=project,
    )
    if existing_entries is not None:
        prior = list(existing_entries)
    elif journal is not None:
        prior = list(load_snoozes(journal).get(validated['episode_id'], ()))
    else:
        prior = []
    check_renewal_allowed(
        prior, until_dt=validated['until_dt'], now=now,
        waited_on_party=waited_on, is_human_project_owner=is_human_project_owner,
    )
    payload = build_snooze_payload(
        validated, actor=actor, actor_display=actor_display or actor,
        waited_on_party=waited_on, now=now,
    )
    if journal is not None:
        open_journal_dir(journal)
    comment_id = None
    if callable(write_comment):
        comment_id = write_comment(validated['task'], comment_body(payload))
    if journal is not None:
        write_journal_entry(journal, payload, comment_id=comment_id)
    active, count = active_snooze(prior + [payload], now=now)
    return {
        'payload': payload,
        'comment_id': str(comment_id) if comment_id is not None else None,
        'snooze': active,
        'snooze_count': count,
        'waited_on_party': bool(payload['waited_on_party']),
        'suppresses_owner_notice': bool(payload['suppresses_owner_notice']),
    }


def parse_cli_snooze_args(args):
    """Parse ``waiting snooze TASK --until ISO8601 --reason TEXT [--episode ID] [--json]``.

    Covers both ``--opt VALUE`` and ``--opt=VALUE`` spellings; raises ``SnoozeRefusal(422)``
    on unknown options, missing task, or duplicate options.
    """
    tokens = list(args or ())
    if tokens and tokens[0] == 'snooze':
        tokens = tokens[1:]
    task = None
    until = None
    reason = None
    episode_id = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == '--json':
            i += 1
        elif tok == '--until' or tok.startswith('--until='):
            if until is not None:
                raise _refuse(422, 'Duplicate --until option.')
            if tok == '--until':
                if i + 1 >= len(tokens):
                    raise _refuse(422, '--until requires an ISO-8601 timestamp with offset.')
                until = tokens[i + 1]
                i += 2
            else:
                until = tok.split('=', 1)[1]
                i += 1
        elif tok == '--reason' or tok.startswith('--reason='):
            if reason is not None:
                raise _refuse(422, 'Duplicate --reason option.')
            if tok == '--reason':
                if i + 1 >= len(tokens):
                    raise _refuse(422, '--reason requires text.')
                reason = tokens[i + 1]
                i += 2
            else:
                reason = tok.split('=', 1)[1]
                i += 1
        elif tok == '--episode' or tok.startswith('--episode='):
            if episode_id is not None:
                raise _refuse(422, 'Duplicate --episode option.')
            if tok == '--episode':
                if i + 1 >= len(tokens):
                    raise _refuse(422, '--episode requires an episode id.')
                episode_id = tokens[i + 1]
                i += 2
            else:
                episode_id = tok.split('=', 1)[1]
                i += 1
        elif tok.startswith('-'):
            raise _refuse(422, 'Unknown waiting snooze option: %r.' % tok)
        elif task is None:
            task = tok
            i += 1
        else:
            raise _refuse(422, 'Unexpected extra argument to waiting snooze: %r.' % tok)
    if not task or not IDENTITY_RE.fullmatch(task):
        raise _refuse(422, 'waiting snooze requires a valid TASK id.')
    out = {'task': task, 'until': until, 'reason': reason}
    if episode_id is not None:
        out['episode_id'] = episode_id
    return out

