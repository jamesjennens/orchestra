"""Server-computed wait episodes, silent lanes, and read-time wait metrics (kittrial-5bb.598 Slice 1).

Core computation is pure (no tracker writes, no host ledger writes): plain reads never
mutate ``.wait-escalations/`` or any other file. ``lanes.py`` delegates checkpoint/wait
projection here so there is one canonical implementation.

Explicit differences between ``whose_move`` (``briefing.assignee_comment_pending``) and
``waits.py`` (Condition 2 of review-598r2b.md):
1. Authority target: ``assignee_comment_pending`` checks ``comment_author_allowed`` (any
   coordinator/owner/operator), whereas ``waits.py`` narrows authority to the episode's
   specific ``waiting_on`` target (actor id, ``coordinator``, ``reviewer``, or ``user``).
2. Verdict vs acknowledgement: ``assignee_comment_pending`` accepts any non-reserved
   comment so acknowledgements/clarifications still flag ``whose_move``; ``waits.py``
   requires a qualifying verdict (``is_verdict_body``) to close a wait episode.
3. Response consumption (``briefing.py:1158-1161``): ``assignee_comment_pending`` consumes
   entries up to the holder's ``last_response``; ``waits.py`` evaluates verdicts per
   checkpoint episode without ``last_response`` consumption.
4. Newest-own-checkpoint vs episode walk: ``assignee_comment_pending`` inspects only the
   holder's newest checkpoint ``own[-1]``; ``waits.py`` walks the full holder checkpoint
   chain ``H`` to preserve wait continuity across re-checkpoints, ``idle`` checkpoints,
   and target switches.
5. Frozen-ledger override: ``waits.py`` honours a read-only frozen ``answered`` record in
   ``.wait-escalations/`` so historical answers stay closed even after role changes.
6. Source 3 (delivery and named review-request waits): ``waits.py`` computes pending
   delivery/review-request waits directly from ``review_state`` and
   ``pending_review_requests`` independent of ``comment_pending`` and ``work.py:384-391``'s
   ``next_actor`` override.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

import record_json

DEFAULT_EXPECT_BY_HOURS = 4
MAX_EXPECT_BY_HOURS = 72
DEFAULT_SILENT_HOURS = 4
SILENT_HOURS_MIN = 1
SILENT_HOURS_MAX = 168
DEFAULT_METRICS_DAYS = 14
METRICS_DAYS_MIN = 1
METRICS_DAYS_MAX = 90
WAITING_LIMIT_DEFAULT = 50
WAITING_LIMIT_MAX = 200

PROSE_WAIT_ITEMS = ('blocker', 'question', 'dependency', 'decision')
PAGE_ELIGIBLE_OPEN_KINDS = ('question', 'decision')

VERDICT_TOKENS = (
    'VERIFIED',
    'APPROVE',
    'APPROVED',
    'REQUEST CHANGES',
    'REQUEST-CHANGES',
    'REVISE',
    'REJECT',
    'DECLINED',
    'PARKED',
    'ANSWER',
)

# Case-insensitive header anchored strictly on the header's own colon:
# either '<opt-prefix> <role>:' or '<opt-prefix> <role>[,| (]<UTC-timestamp-Z>[)]:'.
# Mid-sentence colons after prose (e.g. 'Coordinator received r2; for .511: VERIFIED')
# or comma-prose ('Coordinator, received: REVISE') never match.
_VERDICT_HEADER_RE = re.compile(
    r'^(?:[A-Za-z0-9_-]+\s+)?(?:coordinator|pm|reviewer|owner)'
    r'(?:(?:\s*,\s*|\s*\(\s*|\s+)(?:(?:\d{4}-)?\d{2}-\d{2}[T ])?\d{2}:\d{2}(?::\d{2})?Z\)?)?'
    r':\s*',
    re.IGNORECASE,
)
_VERDICT_BODY_RE = re.compile(
    r'^[*_\s]*(?:' + '|'.join(re.escape(tok) for tok in VERDICT_TOKENS) + r')(?![A-Za-z0-9_])'
)
_ROLE_RE = re.compile(r'\b(coordinator|reviewer|user)\b', re.IGNORECASE)
_REVIEW_VERDICT_OPS = frozenset(('approve', 'request-changes', 'decline'))
_HOLDER_DELIVERY_OPS = frozenset(('contribute', 'respond'))

# Reserved prefixes that are never answers to a wait (even if allow_reserved=True).
_NON_ANSWER_PREFIXES = (
    'Kind: lane-state-v1',
    'Kind: task-checkpoint-v1',
    'Kind: wait-overdue-v1',
    'Kind: wait-snooze-v1',
    'Kind: wait-escalation-v1',
)

ESCALATION_CONFIG_FILE = '.escalation-config.json'
ESCALATION_LEDGER_DIR = '.wait-escalations'
MAX_LEDGER_FILES = 10000
MAX_LEDGER_BYTES = 65536


def _iso_utc(dt):
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _parse_moment(text):
    if not isinstance(text, str) or not text.strip():
        return None
    from lane_state import parse_moment
    try:
        return parse_moment(text, 'timestamp')
    except ValueError:
        return None


def _episode_id(project, task, kind_tag, origin_id, extra=None):
    parts = [str(project or ''), str(task), str(kind_tag), str(origin_id)]
    if extra is not None:
        parts.append(str(extra))
    raw = '\x00'.join(parts).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:32]


def _parse_review_payload(text):
    """Parse ``Kind: contribution-review-v1`` JSON payload through ``record_json.loads``, or None."""
    if not isinstance(text, str) or not text.startswith('Kind: contribution-review-v1'):
        return None
    parts = text.split('\n', 1)
    if len(parts) < 2:
        return None
    try:
        payload = record_json.loads(parts[1])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def is_verdict_body(text):
    """True when ``text`` is a structured review verdict or a lead-keyword verdict comment.

    Plain acknowledgements ('received', 'under review', 'CI started') and mid-body mentions
    ('Coordinator: received r2; for .511: VERIFIED earlier' or colon-less variants) return
    False. Matches ``kittrial-waits.py`` parity: a lead verdict naming another task id
    (e.g. 'Coordinator: VERIFIED for kittrial-5bb.511') still counts as a verdict.
    """
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    if not stripped:
        return False
    if stripped.startswith('Kind: contribution-review-v1'):
        payload = _parse_review_payload(stripped)
        return payload is not None and payload.get('operation') in _REVIEW_VERDICT_OPS
    for prefix in _NON_ANSWER_PREFIXES:
        if stripped.startswith(prefix):
            return False
    from reserved_comments import reserved_match
    if reserved_match(text) is not None:
        return False
    m = _VERDICT_HEADER_RE.match(stripped)
    rest = stripped[m.end():] if m is not None else stripped
    return bool(_VERDICT_BODY_RE.match(rest))


def load_escalation_config(journal):
    """Read optional ``<project>/.escalation-config.json`` (read-only, refuses symlinks). Returns dict."""
    if journal is None:
        return {}
    path = Path(journal) / ESCALATION_CONFIG_FILE
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        if path.stat().st_size > MAX_LEDGER_BYTES:
            return {}
        data = record_json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_frozen_ledger(journal):
    """Read optional frozen answers from ``<project>/.wait-escalations/`` (read-only, bounded, refuses symlinks)."""
    if journal is None:
        return {}
    root = Path(journal) / ESCALATION_LEDGER_DIR
    if root.is_symlink() or not root.is_dir():
        return {}
    out = {}
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return {}
    count = 0
    for item in entries:
        if item.is_symlink() or not item.is_file() or item.suffix != '.json':
            continue
        count += 1
        if count > MAX_LEDGER_FILES:
            break
        try:
            if item.stat().st_size > MAX_LEDGER_BYTES:
                continue
            data = record_json.loads(item.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            eid = data.get('episode_id') or item.stem
            if isinstance(eid, str) and eid:
                out[eid] = data
    return out


def resolve_ssh_lane_owner(actor, owners=None, lane_owners=None, operators=None):
    """Resolve an SSH lane actor -> owning ``person:*`` principal(s) via ``lane_owners`` (Change 6).

    Falls back to installation operators when a ``lane:*`` principal has no explicit mapping.
    Returns ``(mapped_principals_set, fell_back_to_operators)``.
    """
    owners_map = owners if isinstance(owners, dict) else {}
    lane_map = lane_owners if isinstance(lane_owners, dict) else {}
    principal = owners_map.get(actor)
    if isinstance(principal, str) and principal:
        mapped = lane_map.get(principal)
        if isinstance(mapped, str) and mapped.strip():
            return frozenset([mapped.strip()]), False
        if isinstance(mapped, list):
            cleaned = frozenset(x.strip() for x in mapped if isinstance(x, str) and x.strip())
            if cleaned:
                return cleaned, False
        if principal.startswith('person:'):
            return frozenset([principal]), False
    ops = frozenset(x for x in (operators or ()) if isinstance(x, str) and x)
    return ops, True


def _http_agent_owner(actor, authority_state):
    if not isinstance(authority_state, dict) or not isinstance(actor, str):
        return None
    agents = authority_state.get('agents') or {}
    agent_id = actor.split('/', 1)[0]
    agent = agents.get(agent_id)
    if isinstance(agent, dict):
        owner = agent.get('owner')
        if isinstance(owner, str) and owner:
            return owner
    return None


def _http_project_role_members(authority_state, project, roles):
    """Active HTTP users (and agents with coordinator grant if coordinator in roles) holding ``roles``."""
    if not isinstance(authority_state, dict) or not project:
        return set()
    users = authority_state.get('users') or {}
    members = (authority_state.get('memberships') or {}).get(project) or {}
    out = set()
    for uid, role in members.items():
        user = users.get(uid)
        if not isinstance(user, dict) or user.get('disabled'):
            continue
        if role in roles:
            out.add(uid)
    if 'coordinator' in roles:
        from http_authority import coordinator_grant_capabilities, ROLE_CAPABILITIES, CAP_COORDINATE
        agents = authority_state.get('agents') or {}
        for aid, agent in agents.items():
            if not isinstance(agent, dict) or not agent.get('enabled'):
                continue
            if project not in (agent.get('projects') or []):
                continue
            account = agent.get('owner')
            user = users.get(account)
            if not isinstance(user, dict) or user.get('disabled'):
                continue
            held = ROLE_CAPABILITIES.get(members.get(account), frozenset())
            if CAP_COORDINATE in coordinator_grant_capabilities(authority_state, {'agent_id': aid}, project, held):
                out.add(aid)
    return out


class _AuthorityResolver:
    """Memoized per-read authority checker for ``waiting_on`` targets (SHOULD-9)."""

    def __init__(self, project=None, operators=None, owners=None, authority_state=None, lane_owners=None):
        self.project = project
        self.operators = tuple(operators) if operators is not None else ()
        self.owners = owners if isinstance(owners, dict) else {}
        self.authority_state = authority_state if isinstance(authority_state, dict) else None
        self.lane_owners = lane_owners if isinstance(lane_owners, dict) else {}
        self._cache = {}
        self._role_members_cache = {}

    def matches(self, author, target, holder=None, pending_reviewers=()):
        key = (author, target, holder if target == 'user' else None,
               tuple(sorted(pending_reviewers)) if target == 'reviewer' else ())
        found = self._cache.get(key)
        if found is not None:
            return found
        result = self._compute(author, target, holder, pending_reviewers)
        self._cache[key] = result
        return result

    def _compute(self, author, target, holder, pending_reviewers):
        from briefing import same_activity_actor, comment_author_allowed
        from lane_state import ROLES
        from review_workflow import listed_operator_actor
        if not isinstance(author, str) or not author.strip() or not isinstance(target, str) or not target.strip():
            return False
        if target not in ROLES:
            return same_activity_actor(author, target)
        if target == 'coordinator':
            return comment_author_allowed(author, self.operators, self.owners, self.authority_state, self.project)
        if target == 'reviewer':
            for reviewer in pending_reviewers:
                if same_activity_actor(author, reviewer):
                    return True
            if listed_operator_actor(author, self.operators, self.owners):
                return True
            if self.authority_state is not None and self.project:
                return self._http_has_cap(author, 'approve')
            return False
        if target == 'user':
            # HTTP: project owner(s) OR the waiting lane's owning human.
            if self.authority_state is not None and self.project:
                users = self.authority_state.get('users') or {}
                members = (self.authority_state.get('memberships') or {}).get(self.project) or {}
                user = users.get(author)
                if isinstance(user, dict) and not user.get('disabled'):
                    if members.get(author) == 'owner':
                        return True
                    agent_owner = _http_agent_owner(holder, self.authority_state)
                    if agent_owner and author == agent_owner and author in members:
                        return True
            # SSH: listed installation operators OR actor whose principal matches lane_owners[holder_principal].
            if listed_operator_actor(author, self.operators, self.owners):
                return True
            if holder:
                mapped, fell_back = resolve_ssh_lane_owner(holder, self.owners, self.lane_owners, self.operators)
                if not fell_back:
                    author_principal = self.owners.get(author)
                    if author_principal and author_principal in mapped:
                        return True
            return False
        return False

    def _http_has_cap(self, author, cap_name):
        from http_authority import ROLE_CAPABILITIES, CAP_APPROVE, CAP_COORDINATE, coordinator_grant_capabilities
        cap = CAP_APPROVE if cap_name == 'approve' else CAP_COORDINATE
        users = self.authority_state.get('users') or {}
        agents = self.authority_state.get('agents') or {}
        members = (self.authority_state.get('memberships') or {}).get(self.project) or {}
        user = users.get(author)
        if isinstance(user, dict):
            if user.get('disabled'):
                return False
            return cap in ROLE_CAPABILITIES.get(members.get(author), frozenset())
        agent_id = author.split('/', 1)[0]
        agent = agents.get(agent_id)
        if not isinstance(agent, dict) or not agent.get('enabled'):
            return False
        if self.project not in (agent.get('projects') or []):
            return False
        account = agent.get('owner')
        owner_user = users.get(account)
        if not isinstance(owner_user, dict) or owner_user.get('disabled'):
            return False
        held = ROLE_CAPABILITIES.get(members.get(account), frozenset())
        if cap == CAP_APPROVE:
            return cap in held
        return cap in coordinator_grant_capabilities(self.authority_state, {'agent_id': agent_id}, self.project, held)

    def role_has_no_members(self, target, holder=None, pending_reviewers=()):
        from lane_state import ROLES
        if target not in ROLES:
            return False
        key = (target, holder if target == 'user' else None,
               tuple(sorted(pending_reviewers)) if target == 'reviewer' else ())
        found = self._role_members_cache.get(key)
        if found is not None:
            return found
        has_any = False
        if target == 'coordinator':
            if self.operators:
                has_any = True
            elif self.authority_state is not None and self.project:
                has_any = bool(_http_project_role_members(self.authority_state, self.project, ('owner', 'coordinator')))
        elif target == 'reviewer':
            if pending_reviewers or self.operators:
                has_any = True
            elif self.authority_state is not None and self.project:
                has_any = bool(_http_project_role_members(
                    self.authority_state, self.project, ('owner', 'coordinator', 'reviewer')))
        elif target == 'user':
            if self.authority_state is not None and self.project:
                if _http_project_role_members(self.authority_state, self.project, ('owner',)):
                    has_any = True
                elif _http_agent_owner(holder, self.authority_state):
                    has_any = True
            if not has_any and holder:
                mapped, fell_back = resolve_ssh_lane_owner(holder, self.owners, self.lane_owners, self.operators)
                if mapped:
                    has_any = True
            if not has_any and self.operators:
                has_any = True
        empty = not has_any
        self._role_members_cache[key] = empty
        return empty

    def caller_roles(self, actor):
        """Roles ('coordinator', 'reviewer', 'user') currently held by ``actor`` in this project."""
        roles = set()
        if self.matches(actor, 'coordinator'):
            roles.add('coordinator')
        if self.matches(actor, 'reviewer'):
            roles.add('reviewer')
        if self.matches(actor, 'user'):
            roles.add('user')
        return frozenset(roles)

    def caller_owns_lane(self, caller, holder):
        """True if ``caller`` is the owning human of ``holder`` (HTTP agent owner or SSH lane_owners)."""
        if not caller or not holder:
            return False
        if self.authority_state is not None:
            agent_owner = _http_agent_owner(holder, self.authority_state)
            if agent_owner and caller == agent_owner:
                return True
        mapped, fell_back = resolve_ssh_lane_owner(holder, self.owners, self.lane_owners, self.operators)
        if not fell_back:
            caller_principal = self.owners.get(caller)
            if caller_principal and caller_principal in mapped:
                return True
        return False


def _extract_next_action_role(next_action):
    if not isinstance(next_action, str) or not next_action:
        return None
    m = _ROLE_RE.search(next_action)
    return m.group(1).lower() if m is not None else None


def _classify_checkpoint(payload, paired_ls):
    """Return ``(kind, info)`` for one holder checkpoint ``C_i``.

    ``kind`` is one of:
    - ``'wait'``: dict with ``source``, ``inferred``, ``page_eligible``, ``waiting_on``, ``expect_by_raw``, ``state``
    - ``'working'``: explicit end of wait (``state == 'working'``)
    - ``'idle'``: idle checkpoint (preserves any prior open wait episode, Change 3)
    - ``'clear'``: Case C3 — no paired lane-state, no wait open_items, no role in next_action (ends episode)
    """
    from lane_state import FAULT_STATE, WAITING_STATES
    if paired_ls is not None:
        st = paired_ls.get('state')
        if st in WAITING_STATES:
            return 'wait', {
                'source': 'lane-state-v1',
                'inferred': False,
                'page_eligible': True,
                'waiting_on': paired_ls.get('waiting_on'),
                'expect_by_raw': paired_ls.get('expect_by'),
                'state': st,
            }
        if st in ('idle', FAULT_STATE):
            return 'idle', None
        return 'working', None

    # Case C: no paired lane-state-v1 on this checkpoint.
    next_action = str(payload.get('next_action') or '')
    matched_role = _extract_next_action_role(next_action)
    open_items = payload.get('open_items') or []
    wait_items = [
        item for item in open_items
        if isinstance(item, dict) and item.get('kind') in PROSE_WAIT_ITEMS
    ]
    if wait_items:
        page_eligible = (
            any(item.get('kind') in PAGE_ELIGIBLE_OPEN_KINDS for item in wait_items)
            or matched_role is not None
        )
        target = matched_role or 'coordinator'
        return 'wait', {
            'source': 'checkpoint-open-items',
            'inferred': True,
            'page_eligible': page_eligible,
            'waiting_on': target,
            'expect_by_raw': None,
            'state': 'waiting-on-' + target if target in ('coordinator', 'reviewer', 'user') else 'waiting-on-coordinator',
        }
    if matched_role is not None:
        return 'wait', {
            'source': 'checkpoint-next-action',
            'inferred': True,
            'page_eligible': True,
            'waiting_on': matched_role,
            'expect_by_raw': None,
            'state': 'waiting-on-' + matched_role,
        }
    # Case C3 (Condition 6: no separate C3 idle-stop marker; lanes keep waiting-on-*).
    return 'clear', None


def _compute_expect_by(started_at, expect_by_raw):
    """Compute ``effective_expect_by`` + flags from origin checkpoint's ``started_at`` and ``expect_by_raw``."""
    flags = {
        'expect_by_defaulted': False,
        'expect_by_clamped': False,
        'expect_by_before_checkpoint': False,
        'expect_by_extension_ignored': False,
        'warnings': [],
    }
    if started_at is None:
        return None, flags
    max_eb = started_at + timedelta(hours=MAX_EXPECT_BY_HOURS)
    default_eb = started_at + timedelta(hours=DEFAULT_EXPECT_BY_HOURS)
    parsed = None
    if expect_by_raw is not None:
        parsed = _parse_moment(expect_by_raw)
        if parsed is None:
            flags['warnings'].append('expect_by_unparseable')
            flags['expect_by_defaulted'] = True
            eb = default_eb
        else:
            eb = parsed
    else:
        flags['expect_by_defaulted'] = True
        eb = default_eb
    if eb > max_eb:
        eb = max_eb
        flags['expect_by_clamped'] = True
    if eb < started_at:
        flags['expect_by_before_checkpoint'] = True
    return eb, flags


def _apply_later_expect_by(ep, expect_by_raw):
    """Later re-checkpoints may tighten ``effective_expect_by`` earlier, never push it later."""
    if ep['started_at'] is None or expect_by_raw is None:
        return
    parsed = _parse_moment(expect_by_raw)
    if parsed is None:
        return
    max_eb = ep['started_at'] + timedelta(hours=MAX_EXPECT_BY_HOURS)
    if parsed > max_eb:
        parsed = max_eb
    cur = ep['effective_expect_by']
    if cur is None:
        ep['effective_expect_by'] = parsed
        ep['expect_by_defaulted'] = False
    elif parsed < cur:
        ep['effective_expect_by'] = parsed
        ep['expect_by_defaulted'] = False
        if parsed < ep['started_at']:
            ep['expect_by_before_checkpoint'] = True
    elif parsed > cur:
        ep['expect_by_extension_ignored'] = True


def _paired_holder_lane_states(row, task, holder):
    """Map ``checkpoint_id -> newest valid holder-authored lane-state-v1 payload``."""
    from briefing import same_activity_actor
    from lane_state import parse as parse_lane_state, problems as lane_state_problems
    paired = {}
    for index, comment in enumerate(row.get('comments') or []):
        if not isinstance(comment, dict):
            continue
        if not same_activity_actor(comment.get('author'), holder):
            continue
        payload = parse_lane_state(comment.get('text'))
        if payload is None:
            continue
        cid = payload.get('checkpoint')
        if not isinstance(cid, str) or not cid:
            continue
        errs = lane_state_problems(payload, task, cid)
        if errs:
            # Allow a stored record whose ONLY problem is a garbage expect_by so the wait stays
            # visible and defaults to +4 h with expect_by_unparseable (test #10).
            if 'expect_by' in payload:
                without_eb = {k: v for k, v in payload.items() if k != 'expect_by'}
                if lane_state_problems(without_eb, task, cid):
                    continue
            else:
                continue
        stamp = str(comment.get('created_at') or '')
        prev = paired.get(cid)
        if prev is None or (stamp, index) >= (prev[0], prev[1]):
            paired[cid] = (stamp, index, comment, payload)
    return {cid: (item[2], item[3]) for cid, item in paired.items()}


def _find_checkpoint_verdict(full, state, origin_hist_idx, origin_comment, holder, target,
                             resolver, sel_cache, before_hist_idx=None, before_comment=None,
                             pending_reviewers=()):
    """Find earliest qualifying verdict after ``origin_comment`` (and before ``before_comment`` if given).

    Uses ``briefing.checkpoint_selection`` / ``newer_selection`` + ``qualifying_other_comments``
    so out-of-order native comments AND backdated late arrivals are seen identically to ``whose_move``.
    """
    from briefing import checkpoint_selection, qualifying_other_comments
    sel_origin = checkpoint_selection(full, state, origin_hist_idx, origin_comment, sel_cache)[2][1]
    if before_hist_idx is not None and before_comment is not None:
        sel_after = checkpoint_selection(full, state, before_hist_idx, before_comment, sel_cache)[2][1]
        after_ids = {e['entry_id'] for e in sel_after}
        selected = [e for e in sel_origin if e['entry_id'] not in after_ids]
    else:
        selected = sel_origin
    if not selected:
        return None
    auth_fn = lambda author: resolver.matches(author, target, holder=holder, pending_reviewers=pending_reviewers)
    matches = qualifying_other_comments(
        full['entries'],
        holder,
        selected=selected,
        acknowledged=None,
        authority=auth_fn,
        predicate=lambda e: is_verdict_body(e.get('body', '')),
        allow_reserved=True,
    )
    if not matches:
        return None
    # Earliest verdict in snapshot order.
    return matches[0]


def _holder_delivery_origin(row, holder, review):
    """Anchor for Source 3 ``delivery-awaiting-review``: newest holder contribute/respond timestamp."""
    from briefing import same_activity_actor
    contribution = review.get('contribution') if isinstance(review, dict) else None
    last_response = review.get('last_response') if isinstance(review, dict) else None
    candidates = []
    if isinstance(contribution, dict) and contribution.get('comment_id'):
        candidates.append((str(contribution['comment_id']), _parse_moment(contribution.get('timestamp'))))
    if (isinstance(last_response, dict) and last_response.get('comment_id')
            and same_activity_actor(last_response.get('author'), holder)):
        candidates.append((str(last_response['comment_id']), _parse_moment(last_response.get('timestamp'))))
    # Also scan holder's own contribution-review-v1 contribute/respond comments (kittrial-waits.py:44-46 parity)
    for comment in row.get('comments') or []:
        if not isinstance(comment, dict):
            continue
        if not same_activity_actor(comment.get('author'), holder):
            continue
        payload = _parse_review_payload(comment.get('text') or '')
        if payload is not None and payload.get('operation') in _HOLDER_DELIVERY_OPS:
            cid = str(comment.get('id') or '')
            if cid:
                candidates.append((cid, _parse_moment(comment.get('created_at'))))
    valid = [(cid, dt) for cid, dt in candidates if dt is not None]
    if valid:
        best_cid, best_dt = max(valid, key=lambda pair: (pair[1], pair[0]))
        return best_cid, best_dt
    if candidates:
        return candidates[-1][0], None
    return str(row.get('id') or ''), _parse_moment(row.get('created_at'))


def _approval_origin(row, review):
    """Anchor for Source 3 ``delivery-awaiting-integration``: approving review timestamp, else contribution."""
    contribution = review.get('contribution') if isinstance(review, dict) else None
    contrib_id = str(contribution.get('comment_id') or '') if isinstance(contribution, dict) else ''
    best = None
    for comment in row.get('comments') or []:
        if not isinstance(comment, dict):
            continue
        payload = _parse_review_payload(comment.get('text') or '')
        if payload is None or payload.get('operation') != 'approve':
            continue
        if contrib_id and str(payload.get('contribution') or '') != contrib_id:
            continue
        dt = _parse_moment(comment.get('created_at'))
        cid = str(comment.get('id') or '')
        if dt is not None and (best is None or dt > best[1]):
            best = (cid, dt)
    if best is not None:
        return best
    if isinstance(contribution, dict):
        return contrib_id or str(row.get('id') or ''), _parse_moment(contribution.get('timestamp'))
    return str(row.get('id') or ''), _parse_moment(row.get('created_at'))


def holder_activity_timestamp(row, assignee, registry=None, project=None):
    """Strict holder-only activity clock for silent lanes (MUST-4; NEVER ``row['updated_at']``).

    Max over:
    1. Holder's own comments on the task (plain comments, checkpoints, lane-state, deliveries/responses),
    2. Holder's session run activity for this task in ``registry['runs']`` (``last_heartbeat``,
       ``started_at``, ``ended_at``, matching ``sessions.py:117`` and ``lanes.py:229-233``),
    3. Fallback ONLY when neither (1) nor (2) exists: newest ``Kind: task-assignment-v1``
       notice naming ``assignee``, else ``row['created_at']``.
    """
    from briefing import same_activity_actor
    task_id = str(row.get('id') or '')
    best = None
    for comment in row.get('comments') or []:
        if not isinstance(comment, dict):
            continue
        if not same_activity_actor(comment.get('author'), assignee):
            continue
        dt = _parse_moment(comment.get('created_at'))
        if dt is not None and (best is None or dt > best):
            best = dt
    if isinstance(registry, dict):
        for run in (registry.get('runs') or {}).values():
            if not isinstance(run, dict):
                continue
            if not same_activity_actor(run.get('actor'), assignee):
                continue
            run_task = run.get('task')
            if run_task and task_id and str(run_task) != task_id:
                continue
            for field in ('last_heartbeat', 'started_at', 'ended_at'):
                dt = _parse_moment(run.get(field))
                if dt is not None and (best is None or dt > best):
                    best = dt
    if best is not None:
        return best
    # Fallback 3: newest task-assignment-v1 naming assignee, else row['created_at'] (NEVER updated_at).
    from task_assignment import parse_comment as parse_assignment
    proj = project or ''
    assign_best = None
    for comment in row.get('comments') or []:
        if not isinstance(comment, dict):
            continue
        notice = parse_assignment(comment, project=proj, task=task_id)
        if isinstance(notice, dict) and same_activity_actor(notice.get('to'), assignee):
            dt = _parse_moment(comment.get('created_at') or notice.get('timestamp'))
            if dt is not None and (assign_best is None or dt > assign_best):
                assign_best = dt
    if assign_best is not None:
        return assign_best
    return _parse_moment(row.get('created_at'))


def _finalize_wait_record(ep, row, project, holder, lane_name, now, resolver,
                          pending_reviewers=(), name_map=None):
    """Build the canonical wait dict for one open episode."""
    started_at = ep['started_at']
    eff_eb = ep['effective_expect_by']
    unparseable = started_at is None
    if unparseable:
        age_seconds = None
        overdue = True
        overdue_seconds = None
        escalation_base = None
        page_eligible = False
    else:
        age_seconds = max(0, int((now - started_at).total_seconds()))
        escalation_base = max(eff_eb, started_at) if eff_eb is not None else started_at
        overdue = (eff_eb is not None and now >= eff_eb)
        overdue_seconds = max(0, int((now - eff_eb).total_seconds())) if overdue else 0
        page_eligible = bool(ep['page_eligible'])
    target = ep['waiting_on']
    waiting_on_name = (name_map or {}).get(target, target) if target else None
    ledger_rec = ep.get('ledger_record') or {}
    stage = ledger_rec.get('stage')
    if not stage:
        stage = 'overdue' if overdue else 'none'
    return {
        'kind': 'wait',
        'episode_id': ep['episode_id'],
        'task': str(row['id']),
        'title': str(row.get('title') or '')[:200],
        'status': row.get('status'),
        'lane': lane_name,
        'actor': holder,
        'source': ep['source'],
        'state': ep.get('state'),
        'inferred': bool(ep['inferred']),
        'page_eligible': page_eligible,
        'origin_checkpoint': ep.get('origin_checkpoint'),
        'latest_checkpoint': ep.get('latest_checkpoint'),
        'origin_id': ep.get('origin_id'),
        'waiting_on': target,
        'waiting_on_name': waiting_on_name,
        'role_has_no_members': resolver.role_has_no_members(target, holder=holder, pending_reviewers=pending_reviewers),
        'target_switches': ep.get('target_switches', 0),
        'target_churn': bool(ep.get('target_churn', False)),
        'started_at': _iso_utc(started_at),
        'age_seconds': age_seconds,
        'expect_by': _iso_utc(eff_eb),
        'escalation_base': _iso_utc(escalation_base),
        'expect_by_defaulted': bool(ep.get('expect_by_defaulted', False)),
        'expect_by_clamped': bool(ep.get('expect_by_clamped', False)),
        'expect_by_before_checkpoint': bool(ep.get('expect_by_before_checkpoint', False)),
        'expect_by_extension_ignored': bool(ep.get('expect_by_extension_ignored', False)),
        'timestamp_unparseable': unparseable,
        'warnings': list(ep.get('warnings') or ()),
        'overdue': overdue,
        'overdue_seconds': overdue_seconds,
        'escalation_stage': stage,
        'escalation_notified_at': ledger_rec.get('notified_at'),
        'snoozed': False,
        'snooze_count': 0,
        'snooze': None,
        'last_verdict_wake': None,
    }


def _task_episodes_and_waits(row, project, now, resolver, review=None, ledger=None,
                             lane_name=None, name_map=None):
    """Compute ``(open_waits, all_episodes)`` for a single task row (closed tasks emit no open waits)."""
    from briefing import checkpoint_state, checkpoint_snapshot, normalize_provenance, same_activity_actor
    task = str(row['id'])
    holder = row.get('assignee')
    closed = row.get('status') == 'closed'
    if not holder:
        return [], []
    lane_label = lane_name or holder
    ledger_map = ledger if isinstance(ledger, dict) else {}

    pending_review_reqs = (review.get('pending_review_requests') or []) if isinstance(review, dict) else []
    pending_reviewers = tuple(
        str(req['reviewer']) for req in pending_review_reqs
        if isinstance(req, dict) and req.get('reviewer')
    )

    all_episodes = []
    open_checkpoint_ep = None

    # --- Sources 1 & 2: holder-anchored checkpoint chain H ---
    from lane_state import FAULT_STATE
    paired_ls_map = _paired_holder_lane_states(row, task, holder)
    fault_cids = {cid for cid, (_c, p) in paired_ls_map.items() if p.get('state') == FAULT_STATE}
    try:
        state = checkpoint_state(row, normalize=False)
    except (ValueError, TypeError, KeyError):
        state = {'history': [], 'current': None, 'invalid': True, 'provenance': {}}
    # A detached needs-investigation checkpoint (previous=None, fault_clear channel) can fork
    # the raw checkpoint chain or sit at the head; filter fault checkpoints out so the task's
    # underlying declared wait chain is preserved (mirrors lane_state.newest skipping FAULT_STATE).
    if fault_cids and (
        state.get('invalid')
        or not state.get('history')
        or (str(state['history'][-1][0]) in fault_cids and state['history'][-1][1].get('previous') is None)
    ):
        filtered_row = dict(row, comments=[
            c for c in (row.get('comments') or [])
            if not (isinstance(c, dict) and str(c.get('id')) in fault_cids)
        ])
        try:
            alt = checkpoint_state(filtered_row, normalize=False)
            if not alt.get('invalid') and alt.get('history'):
                state = alt
        except (ValueError, TypeError, KeyError):
            pass
    comments_by_id = {
        str(c['id']): c for c in row.get('comments') or []
        if isinstance(c, dict) and c.get('id') is not None
    }
    has_holder_chain = False
    if not state.get('invalid') and state.get('history'):
        if not state.get('provenance'):
            state = dict(state, provenance={cid: normalize_provenance(payload)
                                            for cid, payload in state['history']})
        holder_chain = [
            (hist_idx, str(cid), payload)
            for hist_idx, (cid, payload) in enumerate(state['history'])
            if str(cid) in comments_by_id
            and same_activity_actor(comments_by_id[str(cid)].get('author'), holder)
        ]
        if holder_chain:
            has_holder_chain = True
            full = checkpoint_snapshot([row], row, state)
            sel_cache = {}
            ep = None
            for hist_idx, cid, payload in holder_chain:
                ci_comment = comments_by_id[cid]
                ci_time = _parse_moment(ci_comment.get('created_at'))
                # Check if an open episode was answered BEFORE C_i.
                if ep is not None:
                    frozen = ledger_map.get(ep['episode_id'])
                    if isinstance(frozen, dict) and frozen.get('answered_at'):
                        ans_dt = _parse_moment(frozen.get('answered_at'))
                        if ans_dt is not None and (ci_time is None or ans_dt <= ci_time):
                            ep['outcome'] = 'answered'
                            ep['ended_at'] = ans_dt
                            ep['answered_by'] = frozen.get('answered_by')
                            all_episodes.append(ep)
                            ep = None
                    if ep is not None:
                        verdict = _find_checkpoint_verdict(
                            full, state, ep['origin_hist_idx'], ep['origin_comment'],
                            holder, ep['waiting_on'], resolver, sel_cache,
                            before_hist_idx=hist_idx, before_comment=ci_comment,
                            pending_reviewers=pending_reviewers,
                        )
                        if verdict is not None:
                            ep['outcome'] = 'answered'
                            ep['ended_at'] = _parse_moment(verdict.get('timestamp'))
                            ep['answered_by'] = verdict.get('author')
                            all_episodes.append(ep)
                            ep = None

                paired_pair = paired_ls_map.get(cid)
                paired_payload = paired_pair[1] if paired_pair is not None else None
                kind, info = _classify_checkpoint(payload, paired_payload)

                if kind == 'idle':
                    # Change 3: idle-stop never ends an open wait episode.
                    if ep is not None:
                        ep['latest_checkpoint'] = cid
                    continue
                if kind in ('working', 'clear'):
                    if ep is not None:
                        ep['outcome'] = 'superseded'
                        ep['ended_at'] = ci_time
                        all_episodes.append(ep)
                        ep = None
                    continue

                # kind == 'wait'
                if ep is None:
                    eff_eb, eb_flags = _compute_expect_by(ci_time, info['expect_by_raw'])
                    eid = _episode_id(project, task, 'checkpoint', cid)
                    ep = {
                        'episode_id': eid,
                        'task': task,
                        'actor': holder,
                        'source': info['source'],
                        'state': info['state'],
                        'inferred': info['inferred'],
                        'page_eligible': info['page_eligible'],
                        'origin_checkpoint': cid,
                        'latest_checkpoint': cid,
                        'origin_id': cid,
                        'origin_hist_idx': hist_idx,
                        'origin_comment': ci_comment,
                        'waiting_on': info['waiting_on'],
                        'started_at': ci_time,
                        'effective_expect_by': eff_eb,
                        'target_switches': 0,
                        'target_churn': False,
                        'outcome': 'open',
                        'ended_at': None,
                        'answered_by': None,
                        'ledger_record': ledger_map.get(eid),
                        **eb_flags,
                    }
                else:
                    # Continuation of unbroken wait episode (MUST-2, Change 4).
                    ep['latest_checkpoint'] = cid
                    new_target = info['waiting_on']
                    if new_target and new_target != ep['waiting_on']:
                        ep['waiting_on'] = new_target
                        ep['target_switches'] += 1
                        if ep['target_switches'] >= 2:
                            ep['target_churn'] = True
                    if info['source'] == 'lane-state-v1' and ep['inferred']:
                        ep['source'] = 'lane-state-v1'
                        ep['inferred'] = False
                        ep['page_eligible'] = True
                        ep['state'] = info['state']
                    elif not info['inferred']:
                        ep['state'] = info['state']
                    _apply_later_expect_by(ep, info['expect_by_raw'])

            # Evaluate trailing open episode after walking H.
            if ep is not None:
                frozen = ledger_map.get(ep['episode_id'])
                if isinstance(frozen, dict) and frozen.get('answered_at'):
                    ep['outcome'] = 'answered'
                    ep['ended_at'] = _parse_moment(frozen.get('answered_at'))
                    ep['answered_by'] = frozen.get('answered_by')
                    all_episodes.append(ep)
                else:
                    verdict = _find_checkpoint_verdict(
                        full, state, ep['origin_hist_idx'], ep['origin_comment'],
                        holder, ep['waiting_on'], resolver, sel_cache,
                        pending_reviewers=pending_reviewers,
                    )
                    if verdict is not None:
                        ep['outcome'] = 'answered'
                        ep['ended_at'] = _parse_moment(verdict.get('timestamp'))
                        ep['answered_by'] = verdict.get('author')
                        all_episodes.append(ep)
                    elif closed:
                        ep['outcome'] = 'closed_unanswered'
                        ep['ended_at'] = _parse_moment(row.get('closed_at') or row.get('updated_at'))
                        all_episodes.append(ep)
                    else:
                        open_checkpoint_ep = ep
                        all_episodes.append(ep)

    # Fallback when no holder task-checkpoint-v1 chain exists yet, but the holder wrote a valid
    # lane-state-v1 paired with a holder-authored comment on this task (lane_state.newest parity).
    if not has_holder_chain and paired_ls_map:
        from lane_state import WAITING_STATES
        from briefing import qualifying_other_comments, snapshot as briefing_snapshot
        usable = []
        for cid, (rec_comment, ls_payload) in paired_ls_map.items():
            if ls_payload.get('state') == FAULT_STATE:
                continue
            anchor = comments_by_id.get(cid)
            if anchor is None or not same_activity_actor(anchor.get('author'), holder):
                continue
            usable.append((str(rec_comment.get('created_at') or ''), cid, anchor, ls_payload))
        if usable:
            usable.sort(key=lambda item: (item[0], item[1]))
            _stamp, cid, anchor, ls_payload = usable[-1]
            if ls_payload.get('state') in WAITING_STATES:
                ci_time = _parse_moment(anchor.get('created_at'))
                eff_eb, eb_flags = _compute_expect_by(ci_time, ls_payload.get('expect_by'))
                eid = _episode_id(project, task, 'checkpoint', cid)
                ep = {
                    'episode_id': eid,
                    'task': task,
                    'actor': holder,
                    'source': 'lane-state-v1',
                    'state': ls_payload.get('state'),
                    'inferred': False,
                    'page_eligible': True,
                    'origin_checkpoint': cid,
                    'latest_checkpoint': cid,
                    'origin_id': cid,
                    'waiting_on': ls_payload.get('waiting_on'),
                    'started_at': ci_time,
                    'effective_expect_by': eff_eb,
                    'target_switches': 0,
                    'target_churn': False,
                    'outcome': 'open',
                    'ended_at': None,
                    'answered_by': None,
                    'ledger_record': ledger_map.get(eid),
                    **eb_flags,
                }
                frozen = ledger_map.get(eid)
                if isinstance(frozen, dict) and frozen.get('answered_at'):
                    ep['outcome'] = 'answered'
                    ep['ended_at'] = _parse_moment(frozen.get('answered_at'))
                    ep['answered_by'] = frozen.get('answered_by')
                    all_episodes.append(ep)
                else:
                    snap = briefing_snapshot([row], project or 'work-queue', task)
                    anchor_ts = str(anchor.get('created_at') or '')
                    selected = [
                        e for e in snap['entries']
                        if e.get('kind') == 'comment'
                        and e.get('entry_id') != f'{task}-c{cid}'
                        and (not anchor_ts or str(e.get('timestamp') or '') > anchor_ts)
                    ]
                    auth_fn = lambda author: resolver.matches(
                        author, ep['waiting_on'], holder=holder, pending_reviewers=pending_reviewers
                    )
                    matches = qualifying_other_comments(
                        snap['entries'], holder, selected=selected, acknowledged=None,
                        authority=auth_fn, predicate=lambda e: is_verdict_body(e.get('body', '')),
                        allow_reserved=True,
                    )
                    if matches:
                        ep['outcome'] = 'answered'
                        ep['ended_at'] = _parse_moment(matches[0].get('timestamp'))
                        ep['answered_by'] = matches[0].get('author')
                        all_episodes.append(ep)
                    elif closed:
                        ep['outcome'] = 'closed_unanswered'
                        ep['ended_at'] = _parse_moment(row.get('closed_at') or row.get('updated_at'))
                        all_episodes.append(ep)
                    else:
                        open_checkpoint_ep = ep
                        all_episodes.append(ep)

    if closed:
        return [], all_episodes

    # --- Source 3: Pending deliveries and named pending_review_requests (MUST-3, Change 5) ---
    open_eps = []
    merged_cp = False
    rstate = review.get('review_state') if isinstance(review, dict) else None
    if rstate in ('awaiting-review', 'legacy-review-ready'):
        origin_id, started_at = _holder_delivery_origin(row, holder, review)
        target = 'reviewer'
        eid = _episode_id(project, task, 'delivery', origin_id, target)
        frozen = ledger_map.get(eid)
        if not (isinstance(frozen, dict) and frozen.get('answered_at')):
            eff_eb, eb_flags = _compute_expect_by(started_at, None)
            deliv_ep = {
                'episode_id': eid,
                'task': task,
                'actor': holder,
                'source': 'delivery-awaiting-review',
                'state': 'waiting-on-review',
                'inferred': False,
                'page_eligible': True,
                'origin_checkpoint': None,
                'latest_checkpoint': None,
                'origin_id': origin_id,
                'waiting_on': target,
                'started_at': started_at,
                'effective_expect_by': eff_eb,
                'target_switches': 0,
                'target_churn': False,
                'outcome': 'open',
                'ended_at': None,
                'answered_by': None,
                'ledger_record': frozen,
                **eb_flags,
            }
            if open_checkpoint_ep is not None and open_checkpoint_ep['waiting_on'] == target:
                merged_cp = True
                cp_start = open_checkpoint_ep['started_at']
                if cp_start is not None and (started_at is None or cp_start < started_at):
                    deliv_ep['started_at'] = cp_start
                    deliv_ep['origin_checkpoint'] = open_checkpoint_ep['origin_checkpoint']
                deliv_ep['latest_checkpoint'] = open_checkpoint_ep['latest_checkpoint']
                if open_checkpoint_ep['effective_expect_by'] is not None:
                    cp_eb = open_checkpoint_ep['effective_expect_by']
                    if deliv_ep['effective_expect_by'] is None or cp_eb < deliv_ep['effective_expect_by']:
                        deliv_ep['effective_expect_by'] = cp_eb
                        deliv_ep['expect_by_defaulted'] = open_checkpoint_ep['expect_by_defaulted']
                # Replace checkpoint ep in all_episodes with merged delivery ep
                all_episodes = [e for e in all_episodes if e is not open_checkpoint_ep]
            all_episodes.append(deliv_ep)
            open_eps.append(deliv_ep)
    elif rstate in ('awaiting-integration', 'approved'):
        origin_id, started_at = _approval_origin(row, review)
        target = 'coordinator'
        eid = _episode_id(project, task, 'delivery', origin_id, target)
        frozen = ledger_map.get(eid)
        if not (isinstance(frozen, dict) and frozen.get('answered_at')):
            eff_eb, eb_flags = _compute_expect_by(started_at, None)
            deliv_ep = {
                'episode_id': eid,
                'task': task,
                'actor': holder,
                'source': 'delivery-awaiting-integration',
                'state': 'waiting-on-coordinator',
                'inferred': False,
                'page_eligible': True,
                'origin_checkpoint': None,
                'latest_checkpoint': None,
                'origin_id': origin_id,
                'waiting_on': target,
                'started_at': started_at,
                'effective_expect_by': eff_eb,
                'target_switches': 0,
                'target_churn': False,
                'outcome': 'open',
                'ended_at': None,
                'answered_by': None,
                'ledger_record': frozen,
                **eb_flags,
            }
            if open_checkpoint_ep is not None and open_checkpoint_ep['waiting_on'] == target:
                merged_cp = True
                cp_start = open_checkpoint_ep['started_at']
                if cp_start is not None and (started_at is None or cp_start < started_at):
                    deliv_ep['started_at'] = cp_start
                    deliv_ep['origin_checkpoint'] = open_checkpoint_ep['origin_checkpoint']
                deliv_ep['latest_checkpoint'] = open_checkpoint_ep['latest_checkpoint']
                if open_checkpoint_ep['effective_expect_by'] is not None:
                    cp_eb = open_checkpoint_ep['effective_expect_by']
                    if deliv_ep['effective_expect_by'] is None or cp_eb < deliv_ep['effective_expect_by']:
                        deliv_ep['effective_expect_by'] = cp_eb
                        deliv_ep['expect_by_defaulted'] = open_checkpoint_ep['expect_by_defaulted']
                all_episodes = [e for e in all_episodes if e is not open_checkpoint_ep]
            all_episodes.append(deliv_ep)
            open_eps.append(deliv_ep)

    if open_checkpoint_ep is not None and not merged_cp:
        open_eps.append(open_checkpoint_ep)

    # Named pending review requests (MUST-3)
    for req in pending_review_reqs:
        if not isinstance(req, dict):
            continue
        reviewer = req.get('reviewer')
        req_id = req.get('request') or req.get('comment_id')
        if not reviewer or not req_id:
            continue
        started_at = _parse_moment(req.get('timestamp'))
        eid = _episode_id(project, task, 'review-request', str(req_id), str(reviewer))
        frozen = ledger_map.get(eid)
        if isinstance(frozen, dict) and frozen.get('answered_at'):
            continue
        eff_eb, eb_flags = _compute_expect_by(started_at, None)
        req_ep = {
            'episode_id': eid,
            'task': task,
            'actor': holder,
            'source': 'review-request',
            'state': 'waiting-on-review',
            'inferred': False,
            'page_eligible': True,
            'origin_checkpoint': None,
            'latest_checkpoint': None,
            'origin_id': str(req_id),
            'waiting_on': str(reviewer),
            'started_at': started_at,
            'effective_expect_by': eff_eb,
            'target_switches': 0,
            'target_churn': False,
            'outcome': 'open',
            'ended_at': None,
            'answered_by': None,
            'ledger_record': frozen,
            **eb_flags,
        }
        all_episodes.append(req_ep)
        open_eps.append(req_ep)

    finalized = [
        _finalize_wait_record(
            ep, row, project, holder, lane_label, now, resolver,
            pending_reviewers=pending_reviewers, name_map=name_map,
        )
        for ep in open_eps
    ]
    return finalized, all_episodes


def _iter_task_rows(rows):
    from reserved_comments import is_record_anchor
    from coordination import is_merge_slot
    for row in rows:
        if not isinstance(row, dict) or not row.get('id') or row.get('malformed'):
            continue
        if row.get('issue_type') in ('event', 'gate') or is_merge_slot(row) or is_record_anchor(row):
            continue
        yield row


def _review_for_row(row, task, evidence, revert_map, revert_problems, operators, journal):
    from work import workflow
    scopes = evidence.get(task, {})
    try:
        return workflow(
            row, scopes, operators=operators, reverts=revert_map.get(task, []),
            journal=journal, invalid_reverts=revert_problems.get(task),
        )
    except ValueError:
        return {}


def compute_waits(rows, *, now=None, project=None, registry=None, operators=None,
                  authority_state=None, journal=None, silent_hours=DEFAULT_SILENT_HOURS,
                  lane_owners=None, ledger=None, snoozes=None, evidence=None, revert_map=None,
                  revert_problems=None, identity_names=None, include_closed_episodes=False):
    """Pure wait + silent-lane computation over one cached export.

    Returns ``{'waits': [...], 'silent': [...], 'episodes': [...], 'by_task': {task: [...]}}``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    project_id = project or (Path(journal).name if journal is not None else '')
    from lanes import registry_names
    import wait_snooze
    names = dict(registry_names(registry))
    if isinstance(identity_names, dict):
        names.update({k: v for k, v in identity_names.items() if isinstance(k, str) and isinstance(v, str)})
    owners = (registry or {}).get('owners') if isinstance(registry, dict) else {}
    if lane_owners is None:
        cfg = load_escalation_config(journal)
        lane_owners = cfg.get('lane_owners') if isinstance(cfg.get('lane_owners'), dict) else {}
    if ledger is None:
        ledger = load_frozen_ledger(journal)
    if snoozes is None:
        snoozes = wait_snooze.load_snoozes(journal)
    snooze_map = snoozes if isinstance(snoozes, dict) else {}

    if evidence is None:
        from lifecycle import integration_evidence
        evidence = {
            item['id']: item['scopes']
            for item in integration_evidence(rows)
            if isinstance(item, dict) and item.get('id')
        }
    if revert_map is None or revert_problems is None:
        if journal is not None:
            from review_state import reverts_by_task
            revert_map, revert_problems = reverts_by_task(rows, operators, journal)
        else:
            revert_map, revert_problems = {}, {}

    resolver = _AuthorityResolver(
        project=project_id,
        operators=operators,
        owners=owners,
        authority_state=authority_state,
        lane_owners=lane_owners,
    )

    waits_list = []
    silent_list = []
    all_episodes = []
    by_task = {}
    silent_threshold = timedelta(hours=silent_hours)

    for row in _iter_task_rows(rows):
        task = str(row['id'])
        holder = row.get('assignee')
        if not holder:
            continue
        closed = row.get('status') == 'closed'
        if closed and not include_closed_episodes:
            continue
        review = {} if closed else _review_for_row(
            row, task, evidence, revert_map, revert_problems, operators, journal
        )
        lane_name = names.get(holder, holder)
        open_waits, task_eps = _task_episodes_and_waits(
            row, project_id, now, resolver, review=review, ledger=ledger,
            lane_name=lane_name, name_map=names,
        )
        all_episodes.extend(task_eps)
        if open_waits:
            for item in open_waits:
                act_sn, sn_cnt = wait_snooze.active_snooze(snooze_map.get(item['episode_id'], ()), now=now)
                item['snoozed'] = act_sn is not None
                item['snooze_count'] = sn_cnt
                item['snooze'] = act_sn
            by_task[task] = open_waits
            waits_list.extend(open_waits)
        elif not closed:
            last_act = holder_activity_timestamp(row, holder, registry=registry, project=project_id)
            if last_act is not None and (now - last_act) >= silent_threshold:
                age_sec = max(0, int((now - last_act).total_seconds()))
                last_iso = _iso_utc(last_act)
                eid = _episode_id(project_id, task, 'silent', holder, last_iso)
                ledger_rec = ledger.get(eid) or {}
                stage = ledger_rec.get('stage') or 'overdue'
                act_sn, sn_cnt = wait_snooze.active_snooze(snooze_map.get(eid, ()), now=now)
                silent_list.append({
                    'kind': 'silent-lane',
                    'silent': True,
                    'episode_id': eid,
                    'task': task,
                    'title': str(row.get('title') or '')[:200],
                    'status': row.get('status'),
                    'lane': lane_name,
                    'actor': holder,
                    'source': 'silent-lane',
                    'inferred': False,
                    'page_eligible': True,
                    'waiting_on': None,
                    'waiting_on_name': None,
                    'started_at': last_iso,
                    'last_holder_activity_at': last_iso,
                    'age_seconds': age_sec,
                    'silent_hours': silent_hours,
                    'overdue': True,
                    'overdue_seconds': max(0, age_sec - int(silent_threshold.total_seconds())),
                    'escalation_stage': stage,
                    'escalation_notified_at': ledger_rec.get('notified_at'),
                    'snoozed': act_sn is not None,
                    'snooze_count': sn_cnt,
                    'snooze': act_sn,
                })

    # Oldest first by started_at ascending (unparseable/None first so fail-visible), tie-break task, episode_id.
    waits_list.sort(key=lambda w: (w['started_at'] is not None, w['started_at'] or '', w['task'], w['episode_id']))
    silent_list.sort(key=lambda s: (s['started_at'] or '', s['task'], s['episode_id']))
    return {
        'waits': waits_list,
        'silent': silent_list,
        'episodes': all_episodes,
        'by_task': by_task,
        'resolver': resolver,
    }


def task_wait_summary(row, task, actor, now, review=None, operators=None, owners=None,
                      authority_state=None, project=None, journal=None, ledger=None):
    """Single-task wait projection used by ``lanes.held_task`` so there is ONE implementation."""
    cfg = load_escalation_config(journal) if journal is not None else {}
    lane_owners = cfg.get('lane_owners') if isinstance(cfg.get('lane_owners'), dict) else {}
    resolver = _AuthorityResolver(
        project=project or (Path(journal).name if journal is not None else ''),
        operators=operators,
        owners=owners,
        authority_state=authority_state,
        lane_owners=lane_owners,
    )
    open_waits, _eps = _task_episodes_and_waits(
        row, resolver.project, now, resolver, review=review, ledger=ledger or {},
    )
    return open_waits


def _median_seconds(values):
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return s[mid]
    return int(round((s[mid - 1] + s[mid]) / 2.0))


def compute_daily_metrics(rows, *, now=None, days=DEFAULT_METRICS_DAYS, project=None,
                          registry=None, operators=None, authority_state=None,
                          journal=None, lane_owners=None, ledger=None, identity_names=None,
                          scope='all', actor=None):
    """Read-time wait metrics per UTC day (Section 6, SHOULD-6/7/9).

    Invoked ONLY by ``waiting --metrics`` and ``GET /v1/projects/{pid}/waiting/metrics``.
    Scans all tasks including closed tasks. Four outcome buckets: ``answered``,
    ``closed_unanswered``, ``superseded``, and ``open_now``; grouped by both
    ``by_target`` (``waiting_on``) and ``by_actor`` (``answered_by``).
    When ``scope == 'own'``, narrows episodes to waits authored by, addressed to, or
    answered by ``actor`` so bound contributor keys never see other lanes' metrics.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    today_str = now_utc.date().isoformat()
    day_keys = [
        (now_utc.date() - timedelta(days=offset)).isoformat()
        for offset in range(days - 1, -1, -1)
    ]
    day_set = set(day_keys)

    from lanes import registry_names
    from briefing import same_activity_actor
    names = dict(registry_names(registry))
    if isinstance(identity_names, dict):
        names.update({k: v for k, v in identity_names.items() if isinstance(k, str) and isinstance(v, str)})

    computed = compute_waits(
        rows, now=now_utc, project=project, registry=registry, operators=operators,
        authority_state=authority_state, journal=journal, lane_owners=lane_owners,
        ledger=ledger, identity_names=identity_names, include_closed_episodes=True,
    )

    by_target_raw = {}  # (day, target) -> buckets
    by_actor_raw = {}   # (day, actor) -> answered durations

    def _bucket(day, target):
        key = (day, target)
        b = by_target_raw.get(key)
        if b is None:
            b = {'answered': [], 'closed_unanswered': [], 'superseded': [], 'open_now': []}
            by_target_raw[key] = b
        return b

    episodes = computed['episodes']
    if scope == 'own':
        episodes = [
            ep for ep in episodes
            if (actor and same_activity_actor(ep.get('actor'), actor))
            or (actor and ep.get('waiting_on') and same_activity_actor(ep.get('waiting_on'), actor))
            or (actor and ep.get('answered_by') and same_activity_actor(ep.get('answered_by'), actor))
        ]

    for ep in episodes:
        started = ep.get('started_at')
        if started is None:
            continue
        target = ep.get('waiting_on') or 'unknown'
        outcome = ep.get('outcome')
        if outcome == 'open':
            dur = max(0, int((now_utc - started).total_seconds()))
            _bucket(today_str, target)['open_now'].append(dur)
        elif outcome == 'answered':
            ended = ep.get('ended_at')
            if ended is None:
                continue
            day = ended.astimezone(timezone.utc).date().isoformat()
            if day not in day_set:
                continue
            dur = max(0, int((ended - started).total_seconds()))
            _bucket(day, target)['answered'].append(dur)
            ans_actor = ep.get('answered_by')
            if isinstance(ans_actor, str) and ans_actor:
                by_actor_raw.setdefault((day, ans_actor), []).append(dur)
        elif outcome in ('closed_unanswered', 'superseded'):
            ended = ep.get('ended_at') or started
            day = ended.astimezone(timezone.utc).date().isoformat()
            if day not in day_set:
                continue
            dur = max(0, int((ended - started).total_seconds()))
            _bucket(day, target)[outcome].append(dur)

    by_target = []
    for (day, target), b in sorted(by_target_raw.items()):
        ans = b['answered']
        cu = b['closed_unanswered']
        sup = b['superseded']
        opn = b['open_now']
        by_target.append({
            'day': day,
            'waiting_on': target,
            'waiting_on_name': names.get(target, target),
            'answered': {
                'count': len(ans),
                'median_seconds': _median_seconds(ans),
                'max_seconds': max(ans) if ans else None,
            },
            'closed_unanswered': {
                'count': len(cu),
                'max_seconds': max(cu) if cu else None,
            },
            'superseded': {
                'count': len(sup),
                'max_seconds': max(sup) if sup else None,
            },
            'open_now': {
                'open_count': len(opn),
                'open_max_seconds': max(opn) if opn else None,
            },
        })

    by_actor = []
    for (day, actor_id), durs in sorted(by_actor_raw.items()):
        by_actor.append({
            'day': day,
            'actor': actor_id,
            'actor_name': names.get(actor_id, actor_id),
            'answered': {
                'count': len(durs),
                'median_seconds': _median_seconds(durs),
                'max_seconds': max(durs) if durs else None,
            },
        })

    return {
        'schema_version': 1,
        'scope': scope,
        'days': days,
        'computed_at': _iso_utc(now_utc),
        'by_target': by_target,
        'by_actor': by_actor,
    }


def _bounded_int(value, name, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError('%s must be an integer between %d and %d' % (name, low, high)) from None
    if n < low or n > high:
        raise ValueError('%s must be between %d and %d' % (name, low, high))
    return n


def parse_args(args):
    """Parse ``waiting`` CLI options (covers both ``--opt VAL`` and ``--opt=VAL`` spellings)."""
    on_me = False
    show_all = False
    role = None
    silent_hours = DEFAULT_SILENT_HOURS
    metrics = False
    days = DEFAULT_METRICS_DAYS
    limit = WAITING_LIMIT_DEFAULT
    offset = 0
    i = 0
    tokens = list(args or ())
    while i < len(tokens):
        tok = tokens[i]
        if tok == '--json':
            i += 1
        elif tok == '--on-me':
            on_me = True
            i += 1
        elif tok == '--all':
            show_all = True
            i += 1
        elif tok == '--metrics':
            metrics = True
            i += 1
        elif tok == '--role' or tok.startswith('--role='):
            if tok == '--role':
                if i + 1 >= len(tokens):
                    raise ValueError('--role requires a value')
                val = tokens[i + 1]
                i += 2
            else:
                val = tok.split('=', 1)[1]
                i += 1
            if not val.strip():
                raise ValueError('--role requires a non-empty value')
            role = val.strip()
        elif tok == '--silent-hours' or tok.startswith('--silent-hours='):
            if tok == '--silent-hours':
                if i + 1 >= len(tokens):
                    raise ValueError('--silent-hours requires an integer')
                val = tokens[i + 1]
                i += 2
            else:
                val = tok.split('=', 1)[1]
                i += 1
            silent_hours = _bounded_int(val, '--silent-hours', SILENT_HOURS_MIN, SILENT_HOURS_MAX)
        elif tok == '--days' or tok.startswith('--days='):
            if tok == '--days':
                if i + 1 >= len(tokens):
                    raise ValueError('--days requires an integer')
                val = tokens[i + 1]
                i += 2
            else:
                val = tok.split('=', 1)[1]
                i += 1
            days = _bounded_int(val, '--days', METRICS_DAYS_MIN, METRICS_DAYS_MAX)
        elif tok == '--limit' or tok.startswith('--limit='):
            if tok == '--limit':
                if i + 1 >= len(tokens):
                    raise ValueError('--limit requires an integer')
                val = tokens[i + 1]
                i += 2
            else:
                val = tok.split('=', 1)[1]
                i += 1
            limit = _bounded_int(val, '--limit', 1, WAITING_LIMIT_MAX)
        elif tok == '--offset' or tok.startswith('--offset='):
            if tok == '--offset':
                if i + 1 >= len(tokens):
                    raise ValueError('--offset requires an integer')
                val = tokens[i + 1]
                i += 2
            else:
                val = tok.split('=', 1)[1]
                i += 1
            offset = _bounded_int(val, '--offset', 0, 1000000)
        else:
            raise ValueError('Unknown waiting option: %r' % tok)
    if on_me and show_all:
        raise ValueError('Use either --on-me or --all, not both')
    if on_me and role is not None:
        raise ValueError('Use either --on-me or --role, not both')
    return SimpleNamespace(
        on_me=on_me,
        show_all=show_all,
        role=role,
        silent_hours=silent_hours,
        metrics=metrics,
        days=days,
        limit=limit,
        offset=offset,
    )


def read(rows, actor, registry, args, now=None, operators=None, journal=None,
         scope='all', project=None, authority_state=None, identity_names=None, snoozes=None):
    """Bounded ``waiting`` read view (CLI + HTTP reuse). Plain reads never write."""
    parsed = parse_args(args)
    if now is None:
        now = datetime.now(timezone.utc)
    if parsed.metrics:
        return compute_daily_metrics(
            rows, now=now, days=parsed.days, project=project, registry=registry,
            operators=operators, authority_state=authority_state, journal=journal,
            identity_names=identity_names, scope=scope, actor=actor,
        )

    computed = compute_waits(
        rows, now=now, project=project, registry=registry, operators=operators,
        authority_state=authority_state, journal=journal, silent_hours=parsed.silent_hours,
        identity_names=identity_names, snoozes=snoozes,
    )
    resolver = computed['resolver']
    waits_list = computed['waits']
    silent_list = computed['silent']
    from briefing import same_activity_actor

    roles = resolver.caller_roles(actor) if (scope == 'own' or parsed.on_me) else frozenset()

    if scope == 'own':
        # Non-coordinator bound key narrows to waits authored by or addressed to this actor
        # (or live roles held by this actor, or owned agents), and silent rows for this actor
        # (or owned agents) only.
        waits_list = [
            w for w in waits_list
            if same_activity_actor(w['actor'], actor)
            or resolver.caller_owns_lane(actor, w['actor'])
            or (w['waiting_on'] in roles)
            or (w['waiting_on'] and same_activity_actor(w['waiting_on'], actor))
        ]
        silent_list = [
            s for s in silent_list
            if same_activity_actor(s['actor'], actor)
            or resolver.caller_owns_lane(actor, s['actor'])
        ]

    if parsed.on_me:
        waits_list = [
            w for w in waits_list
            if w['waiting_on'] in roles
            or (w['waiting_on'] == 'user' and resolver.caller_owns_lane(actor, w['actor']))
            or (w['waiting_on'] and same_activity_actor(w['waiting_on'], actor))
        ]
        is_coord = 'coordinator' in roles
        silent_list = [
            s for s in silent_list
            if is_coord or resolver.caller_owns_lane(actor, s['actor'])
        ]
    elif parsed.role is not None:
        want = parsed.role.lower()
        waits_list = [
            w for w in waits_list
            if w['waiting_on'] and (w['waiting_on'].lower() == want or same_activity_actor(w['waiting_on'], parsed.role))
        ]
        silent_list = []

    total_waits = len(waits_list)
    overdue_count = sum(1 for w in waits_list if w['overdue'])
    snoozed_count = sum(1 for w in waits_list if w.get('snoozed')) + sum(1 for s in silent_list if s.get('snoozed'))
    unreached_count = (
        sum(1 for w in waits_list if w.get('escalation_stage') == 'owner-not-reached')
        + sum(1 for s in silent_list if s.get('escalation_stage') == 'owner-not-reached')
    )
    project_visible_count = (
        sum(1 for w in waits_list if w.get('escalation_stage') == 'project-visible')
        + sum(1 for s in silent_list if s.get('escalation_stage') == 'project-visible')
    )
    page_waits = waits_list[parsed.offset:parsed.offset + parsed.limit]
    next_offset = (
        parsed.offset + parsed.limit
        if parsed.offset + parsed.limit < total_waits
        else None
    )
    return {
        'schema_version': 1,
        'project': project or (Path(journal).name if journal is not None else None),
        'actor': actor,
        'scope': scope,
        'filter': 'on-me' if parsed.on_me else (f'role:{parsed.role}' if parsed.role else 'all'),
        'computed_at': _iso_utc(now),
        'silent_hours': parsed.silent_hours,
        'total': total_waits,
        'overdue_count': overdue_count,
        'unreached_count': unreached_count,
        'project_visible_count': project_visible_count,
        'snoozed_count': snoozed_count,
        'silent_count': len(silent_list),
        'items': page_waits,
        'silent': silent_list,
        'next_offset': next_offset,
    }
