#!/usr/bin/env python3
"""Invite links (kittrial-5bb.578): a person who may administer projects makes a single-use link
that creates one account, adds it to those projects and, optionally, creates one worker agent
owned by it with its first credential.

The model is the password-reset value of ``http_auth.Service`` (``issue_reset`` /
``redeem_reset``): the token is ``secrets.token_urlsafe(32)`` (32 random bytes), the service keeps
only its SHA-256 (``http_auth.token_hash``) as the key of ``state['invites']``, it lives a fixed
time on both clocks (``Service._expired``), and it is used once. Redemption is bound to the record
the token's hash finds under the state lock: every later step reads that record, never a value
the caller sends again.

Everything here is the web service's own state (accounts, memberships, agents and credentials
all live in the one state document of ``http_auth.Store``), so the routes in ``http_service.py``
serve it for both backends; neither backend is asked to do anything. The canonical endpoint reads
the same state document for live authority, so a membership or agent made here is what it sees.

Atomicity: a redemption holds ``store.lock`` from the token lookup to the save, and writes the
account, the memberships, the agent, its credential and the invite's "used" mark inside one
``Service._widening``. Any exception before the save (a refusal, a failed credential, a failed
save) takes every one of them back out of memory; the file is replaced atomically, so a process
that dies before the replace leaves the file as it was. There is no half-made account.

The switch is the installation setting ``invites_enabled`` (``deployment.private.json`` on the
endpoint backend, read on every request; an attribute on the service-local backend). Off by
default; while off, creating, viewing and redeeming are refused with :data:`INVITES_OFF`.
Listing and revoking stay open, so an invite made while it was on can still be withdrawn.
"""
import re
import secrets

import claim_limit
from http_auth import (AGENT_DEFAULT_SCOPES, LOGIN_WINDOW_SECONDS, THROTTLE_KEY_CAP, HttpError, Principal, Service,
                       _validate_password, conflict, forbidden, hash_password, invalid, new_token, not_found,
                       now_iso, throttled, token_hash)
from http_authority import CAP_PROJECT_ADMIN, ROLE_CAPABILITIES

#: How long an invite lives: 72 hours unless the inviter asks for less or more, at most 7 days.
DEFAULT_HOURS = 72
MIN_HOURS = 1
MAX_HOURS = 7 * 24
#: The role an invite gives in every project it names: the ordinary member role. Never chosen.
MEMBER_ROLE = 'contributor'
#: The role of the one agent an invite may pre-approve. There is no coordinator grant.
AGENT_ROLE = 'worker'
DISPLAY_NAME_MAX = 100
PROJECTS_MAX = 20
#: Bounds on what the state keeps: pending invites per inviter and in all; finished ones kept.
PENDING_PER_INVITER = 50
PENDING_MAX = 500
FINISHED_KEPT = 500
#: The name the sign-in counts keep invite failures under, joined to the address group, so an
#: address that guesses tokens is held back exactly as one that guesses passwords
#: (``Service._check_throttle``), and its failures feed the same per-address count as sign-in.
#: A username can never begin with "(".
THROTTLE_PREFIX = '(invite)'
PROJECT_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
INVITE_ID = re.compile(r'inv_[0-9a-f]{16}')

CREATE_FIELDS = ('display_name', 'username', 'projects', 'expires_hours', 'agent')
AGENT_FIELDS = ('name', 'project', 'claim_limit', 'role')
VIEW_FIELDS = ('token',)
REDEEM_FIELDS = ('token', 'password')

#: The one sentence every invite route answers while the switch is off (pinned by the tests).
INVITES_OFF = ('Invite links are switched off on this installation. An operator turns them on with '
               '"invites_enabled": true in deployment.private.json.')
#: The one answer to a token that is unknown, expired, used or withdrawn: nobody can tell which.
UNUSABLE = 'This invite link cannot be used. Ask the person who sent it for a new one.'
PERSON_ONLY = 'Only a person signed in to the web page can make or withdraw an invite; an agent or worker credential cannot.'
WORKER_ONLY = ('An invite can pre-approve one worker agent only. A coordinator grant is given on the '
               "project's Members & settings page after the person has signed in.")
AGENT_PROJECT = "The pre-approved agent's project must be one of the invite's projects."
STALE = ('This invite can no longer be used as it was made: the person who sent it can no longer add '
         'people to one of its projects, or a project is archived. Ask them for a new one.')
TAKEN = ('The username of this invite has been taken since it was made. Ask the person who sent it for '
         'a new invite; nothing was created.')
SHOWN_ONCE = ('This link is a key: anyone who holds it can claim the account it names. Send it to that '
              'person only, by a private channel. It is shown only now.')


def _off():
    error = HttpError(403, 'invites_disabled', INVITES_OFF)
    error.nothing_done = True
    return error


def unusable():
    """The single refusal for a token that is unknown, expired, used or revoked."""
    return HttpError(404, 'invite_unusable', UNUSABLE)


def require_enabled(enabled):
    if enabled is not True:
        raise _off()


def _invites(service):
    invites = service.state.get('invites')
    return invites if isinstance(invites, dict) else {}


def status(service, record, moment=None):
    """``pending``, ``used``, ``revoked`` or ``expired``: used and revoked are kept as they were."""
    if record.get('used_at'):
        return 'used'
    if record.get('revoked_at'):
        return 'revoked'
    moment = service._expiry_now() if moment is None else moment
    if service._expired(moment, expires_at=record.get('expires_at'), issued_raw=record.get('issued_raw'),
                        lifetime=record.get('ttl')):
        return 'expired'
    return 'pending'


def _text(value, field, limit):
    if not isinstance(value, str) or Service._has_forbidden_text_char(value) or not value.strip() \
            or len(value.strip()) > limit:
        raise invalid('%s must be 1-%d printable characters' % (field, limit))
    return value.strip()


def _hours(value):
    if value is None:
        return DEFAULT_HOURS
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_HOURS <= value <= MAX_HOURS:
        raise invalid('expires_hours must be a whole number from %d to %d (7 days); the default is %d'
                      % (MIN_HOURS, MAX_HOURS, DEFAULT_HOURS))
    return value


def _projects(value):
    if not isinstance(value, list) or not value or len(value) > PROJECTS_MAX:
        raise invalid('projects must be a list of 1 to %d project ids' % PROJECTS_MAX)
    named = []
    for pid in value:
        if not isinstance(pid, str) or not PROJECT_ID.fullmatch(pid):
            raise invalid('Invalid project id in projects')
        if pid not in named:
            named.append(pid)
    return named


def _agent_request(value, projects):
    """The pre-approved agent as asked: a worker, in one of the invite's projects. ``None``: no agent."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise invalid('agent must be an object with name, project and an optional claim_limit')
    # Anything that would make it more than a worker is refused by name, before the field list.
    if any(key in value for key in ('coordinator', 'coordinator_grant', 'grant', 'grants', 'scopes')) or \
            ('role' in value and value.get('role') != AGENT_ROLE):
        raise forbidden(WORKER_ONLY)
    unknown = sorted(str(key) for key in set(value) - set(AGENT_FIELDS))
    if unknown:
        raise invalid('The pre-approved agent takes name, project and claim_limit; not %s' % ', '.join(unknown)[:200])
    name = Service._agent_name(value.get('name'))
    project = value.get('project')
    if not isinstance(project, str) or project not in projects:
        raise invalid(AGENT_PROJECT)
    limit = claim_limit.DEFAULT_LIMIT
    if value.get('claim_limit') is not None:
        try:
            limit = claim_limit.validate_limit(value['claim_limit'])
        except ValueError as error:
            raise invalid(str(error)) from None
    return {'name': name, 'project': project, 'claim_limit': limit, 'role': AGENT_ROLE}


def _holds(state, user_id, project_id):
    """Whether the account ``user_id`` may invite people into ``project_id`` NOW, from state alone.

    Only someone who could add the member directly today, by the rule of the Members table
    (``Service.set_member``: ``project.admin``): a superuser, or a member whose project role carries
    ``project.admin`` (an owner). A coordinator who is not an owner may not (coordinator's decision on
    kittrial-5bb.578), so an invite never widens who can add people. Used at redemption, where the
    inviter has no session in the request; creation also passes ``decide`` itself.
    An archived project takes nobody new.
    """
    user = (state.get('users') or {}).get(user_id)
    project = (state.get('projects') or {}).get(project_id)
    if not isinstance(user, dict) or user.get('disabled') or not isinstance(project, dict) or project.get('archived'):
        return False
    if user.get('superuser'):
        return True
    role = ((state.get('memberships') or {}).get(project_id) or {}).get(user_id)
    return CAP_PROJECT_ADMIN in ROLE_CAPABILITIES.get(role, frozenset())


def invitable_projects(service, principal):
    """The projects ``principal`` may name in an invite: ``[{id, name}]``. Empty for a credential."""
    if principal is None or principal.via == 'credential' or principal.agent_id:
        return []
    with service.store.lock:
        service._refresh_authority(principal)
        state = service.state
        return [{'id': pid, 'name': (project or {}).get('name') or pid}
                for pid, project in sorted(state.get('projects', {}).items())
                if _holds(state, principal.user_id, pid)]


def _person(service, principal):
    if principal is None or principal.via != 'session' or principal.agent_id or principal.credential_id:
        raise forbidden(PERSON_ONLY)


def _name_of(service, user_id):
    user = service.state['users'].get(user_id) or {}
    return user.get('display_name') or user.get('username') or user_id


def view(service, record, moment=None, *, for_inviter=True):
    """One invite as its inviter (or a superuser) lists it. Never the token or its hash."""
    state = service.state
    out = {
        'id': record['id'],
        'status': status(service, record, moment),
        'display_name': record['display_name'],
        'username': record['username'],
        'projects': [{'id': pid, 'name': (state['projects'].get(pid) or {}).get('name') or pid}
                     for pid in record['projects']],
        'role': record['role'],
        'agent': dict(record['agent']) if record.get('agent') else None,
        'invited_by': record['invited_by'],
        'invited_by_name': _name_of(service, record['invited_by']),
        'created_at': record['created_at'],
        'expires_at': now_iso(record['expires_at']),
    }
    if for_inviter:
        out.update(used_at=record.get('used_at'), used_by=record.get('used_by'),
                   used_by_name=_name_of(service, record['used_by']) if record.get('used_by') else None,
                   used_from=record.get('used_from'), agent_id=record.get('agent_id'),
                   revoked_at=record.get('revoked_at'), revoked_by=record.get('revoked_by'),
                   revoked_by_name=_name_of(service, record['revoked_by']) if record.get('revoked_by') else None)
    return out


def _prune(service):
    """Keep at most FINISHED_KEPT finished invites (the newest); pending ones are never pruned."""
    invites = _invites(service)
    moment = service._expiry_now()
    finished = [(record.get('created_raw') or 0, digest) for digest, record in invites.items()
                if status(service, record, moment) != 'pending']
    finished.sort()
    for _when, digest in finished[:max(0, len(finished) - FINISHED_KEPT)]:
        del invites[digest]


def create(service, principal, payload, *, usable=None, request_id=None):
    """Make one invite. Returns ``(public, stored)``: the token is in ``public`` only, once.

    ``usable(project_id)`` is the route's own refusal of a record the backend will not serve.
    The caller has checked the switch and refused unknown top-level fields.
    """
    _person(service, principal)
    display_name = _text(payload.get('display_name'), 'display_name', DISPLAY_NAME_MAX)
    username = service._username(payload.get('username'))
    projects = _projects(payload.get('projects'))
    hours = _hours(payload.get('expires_hours'))
    agent = _agent_request(payload.get('agent'), projects)
    with service.store.lock:
        service._refresh_authority(principal)
        state = service.state
        for pid in projects:
            # The kit's one authority rule: 404 for a project the caller cannot see, 403 for one
            # whose role may not add members (anyone but an owner), as the Members table refuses it.
            service._check_authority_locked(principal, pid, CAP_PROJECT_ADMIN)
            if not _holds(state, principal.user_id, pid):
                raise forbidden('You may invite people only to projects you own, and not '
                                'to an archived project')
            if usable is not None:
                usable(pid)
        _charge_username_check(service, principal, username, projects[0], request_id)
        holder = state['users'].get(state['usernames'].get(username.lower()) or '')
        if isinstance(holder, dict) and not holder.get('disabled'):
            raise conflict('Username already exists')
        moment = service._expiry_now()
        invites = _invites(service)
        pending = [record for record in invites.values() if status(service, record, moment) == 'pending']
        if any(record['username'].lower() == username.lower() for record in pending):
            raise conflict('A pending invite already names this username; withdraw it first')
        if len(pending) >= PENDING_MAX or \
                sum(1 for record in pending if record['invited_by'] == principal.user_id) >= PENDING_PER_INVITER:
            raise conflict('Too many pending invites: at most %d for one person and %d in all; withdraw some first'
                           % (PENDING_PER_INVITER, PENDING_MAX))
        token = new_token()
        digest = token_hash(token)
        raw = service._raw_now()
        record = {
            'id': 'inv_' + secrets.token_hex(8),
            'display_name': display_name,
            'username': username,
            'projects': projects,
            'role': MEMBER_ROLE,
            'agent': agent,
            'invited_by': principal.user_id,
            'created_at': now_iso(service._now()),
            'created_raw': raw,
            'issued_at': moment,
            'issued_raw': raw,
            'ttl': hours * 3600,
            'expires_at': moment + hours * 3600,
            'used_at': None, 'used_by': None, 'used_from': None, 'agent_id': None,
            'revoked_at': None, 'revoked_by': None,
        }
        _prune(service)
        with service._widening() as change:
            # A new invite whose save failed is taken back: its token was never handed out (kittrial-5bb.487).
            if not isinstance(state.get('invites'), dict):
                change.remember(state, 'invites')
                state['invites'] = {}
            change.created(state['invites'], digest)
            state['invites'][digest] = record
            change.save()
        listed = view(service, record, moment)
    public = {'invite': listed, 'token': token, 'token_available': True,
              'path': '/#/invite/' + token,
              'link': (service.public_url + '/#/invite/' + token) if service.public_url else None,
              'note': SHOWN_ONCE}
    stored = {'invite': listed, 'token_available': False,
              'note': 'The link was shown once, when the invite was made; it cannot be shown again. '
                      'Withdraw this invite and make a new one if it was lost.'}
    return public, stored


def list_for(service, principal):
    """The caller's own invites, newest first; a superuser lists everyone's."""
    _person(service, principal)
    with service.store.lock:
        service._refresh_authority(principal)
        moment = service._expiry_now()
        mine = [record for record in _invites(service).values()
                if principal.superuser or record.get('invited_by') == principal.user_id]
        mine.sort(key=lambda record: record.get('created_raw') or 0, reverse=True)
        return [view(service, record, moment) for record in mine]


def revoke(service, principal, invite_id, request_id=None):
    """Withdraw a pending invite: its inviter or a superuser. Anyone else gets the plain 404."""
    _person(service, principal)
    if not isinstance(invite_id, str) or not INVITE_ID.fullmatch(invite_id):
        raise not_found('Invite not found')
    with service.store.lock:
        service._refresh_authority(principal)
        found = next((record for record in _invites(service).values() if record.get('id') == invite_id), None)
        if found is None or not (principal.superuser or found.get('invited_by') == principal.user_id):
            raise not_found('Invite not found')
        state_now = status(service, found)
        if state_now != 'pending':
            raise conflict('This invite is %s already; there is nothing to withdraw' % state_now)
        found['revoked_at'] = now_iso(service._now())
        found['revoked_by'] = principal.user_id
        # A narrowing: kept in memory if the save fails, as every revocation is (kittrial-5bb.487).
        service._save_narrowing()
        return view(service, found)


def _charge_username_check(service, principal, username, project_id, request_id):
    """Count one username check against the caller's account-lookup limit (review S3 of kittrial-5bb.578).

    Making an invite answers whether a username is taken, so it is held to exactly what
    ``Service.lookup_account`` is held to: the same per-person count (``_lookups``, at most
    ``lookup_max`` in ``lookup_window``, 429 beyond it) and one audit line on the project with a keyed
    digest of the name, never the name. A disabled account is not reported as taken, as the lookup
    hides it; such an invite is refused at redemption (``username_taken``) with nothing created.
    Caller holds ``store.lock``.
    """
    digest = 'username_hmac=' + service._lookup_digest(username)
    now = service._now()
    recent = [t for t in service._lookups.get(principal.user_id, []) if t >= now - service.lookup_window]
    if len(recent) >= service.lookup_max:
        service._lookups[principal.user_id] = recent
        service.audit(request_id, principal, 'invites.username_check', 'throttled', project_id=project_id,
                      reason=digest)
        raise throttled('Too many account lookups; wait a few minutes and try again')
    recent.append(now)
    service._lookups[principal.user_id] = recent
    holder = service.state['users'].get(service.state['usernames'].get(username.lower()) or '')
    taken = isinstance(holder, dict) and not holder.get('disabled')
    service.audit(request_id, principal, 'invites.username_check', 'found' if taken else 'not_found',
                  project_id=project_id, reason=digest)


def _throttle_name(service, source, shared_source):
    return '%s%s' % (THROTTLE_PREFIX, service._source_key(source, shared_source))


def _lookup(service, token, source, shared_source):
    """The pending record ``token`` names, or the one refusal. Caller holds ``store.lock``.

    The sign-in counts are checked first and fed by every miss, so an address that guesses is
    answered 429 exactly as one that guesses passwords. The expiry clock is read once, before the
    lookup, for every token (review S1): an unknown, used or withdrawn token costs the same read as
    an expired one. Returns ``(record, moment)``.
    """
    name = _throttle_name(service, source, shared_source)
    service._check_throttle(name, source, shared_source)
    moment = service._expiry_now()
    record = _invites(service).get(token_hash(token)) if isinstance(token, str) and token else None
    if record is None or status(service, record, moment) != 'pending':
        service._record_failure(name, source, shared_source)
        raise unusable()
    return record, moment


#: How long one address's 429s on the invite routes are audited once (review S2): the sign-in window.
QUIET_WINDOW = LOGIN_WINDOW_SECONDS


def quiet_throttle(service, source, shared_source):
    """Whether a 429 on an invite route from this address was already audited in this window.

    The first 429 of an address in QUIET_WINDOW is audited (and saved); the rest are answered with
    neither an audit line nor a save, so a flood cannot roll the audit over or write the state per
    request. The table is bounded as the sign-in tables are.
    """
    group = _throttle_name(service, source, shared_source)
    now = service._now()
    with service.store.lock:
        seen = service.__dict__.setdefault('_invite_throttle_audited', {})
        last = seen.get(group)
        if last is not None and 0 <= now - last < QUIET_WINDOW:
            return True
        if group not in seen and len(seen) >= THROTTLE_KEY_CAP:
            for stale in [key for key, when in seen.items() if not 0 <= now - when < QUIET_WINDOW]:
                del seen[stale]
            if len(seen) >= THROTTLE_KEY_CAP:
                del seen[min(seen, key=seen.get)]
        seen[group] = now
        return False


def public_view(service, token, *, source='local', shared_source=False):
    """What the signed-out invite page shows: who invited whom, the projects and the agent."""
    with service.store.lock:
        record, moment = _lookup(service, token, source, shared_source)
        shown = view(service, record, moment, for_inviter=False)
        shown.pop('invited_by', None)
        shown.pop('id', None)
        shown['password_min_length'] = service.password_min_length
        return shown


def redeem(service, token, password, *, source='local', shared_source=False, usable=None, request_id=None):
    """Create the invited account, its memberships and the pre-approved agent with its credential.

    One transaction (see the module's docstring). Returns the answer the page shows once: the
    account, the projects, and, with an agent, the agent, its credential's one-time secret and
    the set-up payload New agent gives.
    """
    with service.store.lock:
        record, _moment = _lookup(service, token, source, shared_source)
        _validate_password(password, service.password_min_length)
        state = service.state
        inviter = record['invited_by']
        for pid in record['projects']:
            if not _holds(state, inviter, pid):
                raise HttpError(409, 'invite_stale', STALE)
            if usable is not None:
                try:
                    usable(pid)
                except HttpError:
                    raise HttpError(409, 'invite_stale', STALE) from None
        username = record['username']
        if username.lower() in state['usernames']:
            raise HttpError(409, 'username_taken', TAKEN)
        user_id = 'usr_' + secrets.token_hex(8)
        user = {
            'id': user_id, 'username': username, 'display_name': record['display_name'],
            'password': hash_password(password, minimum=service.password_min_length),
            'superuser': False, 'disabled': False, 'created_at': now_iso(service._now()),
            'invited_by': inviter, 'invite_id': record['id'],
        }
        # The new account as the acting principal of what follows: it owns the agent and its credential.
        acting = Principal(user_id, record['display_name'], False, 'session', user_id)
        digest = token_hash(token)
        agent = credential = secret = None
        with service._widening() as change:
            change.remember(state['invites'], digest)
            change.created(state['users'], user_id)
            change.remember(state['usernames'], username.lower())
            state['users'][user_id] = user
            state['usernames'][username.lower()] = user_id
            for pid in record['projects']:
                if not isinstance(state['memberships'].get(pid), dict):
                    change.remember(state['memberships'], pid)
                    state['memberships'][pid] = {}
                members = state['memberships'][pid]
                change.remember(members, user_id)
                members[user_id] = record['role']
            if record.get('agent'):
                asked = record['agent']
                service._check_agent_file_name(user_id, asked['name'])
                granted = service._agent_projects(acting, [asked['project']], owner_id=user_id)
                expiry = service._agent_expiry_metadata()
                agent_id = 'agent_' + secrets.token_hex(8)
                agent = {
                    'id': agent_id, 'name': asked['name'], 'owner': user_id, 'created_by': inviter,
                    'tool': None, 'working_directory': None, 'machine': None, 'notes': None,
                    'enabled': True, 'projects': granted, 'created_at': now_iso(service._now()),
                    'claim_limit': asked.get('claim_limit', claim_limit.DEFAULT_LIMIT),
                    'last_seen_at': None, 'scopes': list(AGENT_DEFAULT_SCOPES), 'scopes_source': 'set',
                    'role': AGENT_ROLE, 'invite_id': record['id'],
                }
                change.created(state['agents'], agent_id)
                state['agents'][agent_id] = agent
                service._audit_project_grants(acting, agent, list(granted), request_id)
                # Issued exactly as New agent issues the first credential (Service.create_agent).
                credential, secret = service._issue_agent_credential_locked(acting, agent, expiry=expiry)
                change.created(state['credentials'], credential['id'])
                change.created(state['credential_tokens'], credential['token_hash'])
                service._audit_agent_credential(acting, agent, credential, 'agents.credentials.issued',
                                                service.agent_scopes(agent), request_id)
                event = service.audit(request_id, acting, 'agents.created', 'committed',
                                      actor=service.agent_actor(agent))
                event.update(agent_id=agent_id, owner_id=user_id, created_by=inviter)
            record['used_at'] = now_iso(service._now())
            record['used_by'] = user_id
            record['used_from'] = str(source or 'unknown')[:64]
            record['agent_id'] = agent['id'] if agent else None
            # The inviter's notice and the trail: one event in each project's audit, which its owners read,
            # naming the invite, who sent it, the new account and where it was used from.
            for pid in record['projects']:
                event = service.audit(request_id, acting, 'invites.redeemed', 'committed', project_id=pid,
                                      reason='invite=%s by=%s' % (record['id'], inviter))
                event.update(invite_id=record['id'], invited_by=inviter, new_user=user_id,
                             used_from=record['used_from'], agent_id=record['agent_id'])
            change.save()
        answer = {
            'operation': 'invites.redeem',
            'user': {'id': user_id, 'username': username, 'display_name': record['display_name']},
            'projects': [{'id': pid, 'name': (state['projects'].get(pid) or {}).get('name') or pid,
                          'role': record['role']} for pid in record['projects']],
            'invited_by_name': _name_of(service, inviter),
            'agent': None, 'credential': None, 'setup': None, 'secret_available': False,
        }
        if agent is not None:
            answer.update(agent=service.agent_view(agent, acting),
                          credential=dict(service.credential_view(credential), secret=secret,
                                          secret_available=True),
                          setup=service.agent_setup(agent, secret), secret_available=True)
        return answer
