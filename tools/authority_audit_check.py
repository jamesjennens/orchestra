#!/usr/bin/env python3
"""Read-only: authority grants in a web service state file that no committed audit event explains.

kittrial-5bb.487. Before that change a grant whose state save failed stayed in memory, in force,
and was written by the next unrelated save with no audit entry of its own. This lists, for a copy
of the state file (or the file itself; it is only read):

  * ``unexplained``: a coordinator or release grant, a project's agent-approval setting or a
    credential whose time has no audit event of its route (outcome ``committed``, or
    ``committed-unsaved`` for a change kept in memory and written by a later save) within
    ``--slack`` seconds that names the same entry: the agent or credential id when the event
    carries one, else the same account or a superuser acting for it;
  * ``unchecked``: an entry older than the oldest audit event the state still keeps (the audit is
    capped), which therefore cannot be checked this way.

An unexplained entry is a lead for an operator, not proof: compare it with the service log of that
time. It does not check memberships, project grants, agent project lists or ``enabled``, which
carry no time of their own. It refuses a state file whose ``schema_version`` it does not know.
Nothing is changed. Usage: authority_audit_check.py STATE_FILE [--slack SECONDS] [--json]
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('authority_audit_check.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import calendar
import json
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from http_authority import SCHEMA_VERSION  # noqa: E402  the state file's version, as the service writes it

OUTCOMES = ('committed', 'committed-unsaved')


def stamp(text):
    """Seconds since the epoch for the service's ``YYYY-MM-DDTHH:MM:SSZ`` stamps, else None."""
    if not isinstance(text, str):
        return None
    try:
        return calendar.timegm(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ'))
    except ValueError:
        return None


def entries(state):
    """(kind, project, subject, by, at, route actions, ids) for each authority entry with a time.

    ``ids`` are the fields an event of the route carries for exactly this entry, when it carries
    any: an event that names another entry does not explain this one, and one that names this
    entry explains it whoever sent it (review of kittrial-5bb.487 F3).
    """
    for kind, key, actions in (('coordinator grant', 'coordinator_grants', ('agents.coordinator.grant',)),
                               ('release grant', 'release_grants', ('agents.release.grant',))):
        family = state.get(key)
        for project, grants in (family.items() if isinstance(family, dict) else ()):
            for agent, grant in (grants.items() if isinstance(grants, dict) else ()):
                if isinstance(grant, dict):
                    yield (kind, project, agent, grant.get('granted_by'), grant.get('granted_at'), actions,
                           {'reason': 'agent %s' % agent})
    approval = state.get('agent_approval')
    for project, setting in (approval.items() if isinstance(approval, dict) else ()):
        if isinstance(setting, dict):
            yield ('agent-approval setting', project, setting.get('mode'), setting.get('set_by'),
                   setting.get('set_at'), ('projects.agent_approval',), {'agent_approval': setting.get('mode')})
    credentials = state.get('credentials')
    for credential_id, credential in (credentials.items() if isinstance(credentials, dict) else ()):
        if not isinstance(credential, dict):
            continue
        if credential.get('agent_id'):
            actions = ('agents.credentials.issued', 'agents.credentials.issue', 'agents.created', 'agents.create')
            ids = {'target_credential_id': credential_id, 'agent_id': credential.get('agent_id')}
        else:
            actions, ids = ('credentials.issue',), {}
        yield ('credential', credential.get('project_id'), credential_id, credential.get('user_id'),
               credential.get('created_at'), actions, ids)


def explains(event, project, by, ids, superusers):
    """Whether one in-time event of the entry's route names this entry."""
    named = [field for field in ids if event.get(field) is not None]
    if named:
        # The most exact field the event carries decides: a credential id over an agent id.
        field = 'target_credential_id' if 'target_credential_id' in named else named[0]
        if event.get(field) != ids[field]:
            return False
        if field in ('target_credential_id', 'agent_id'):
            return True
    if project is not None and event.get('project_id') not in (None, project):
        return False
    return by is None or event.get('user_id') == by or event.get('user_id') in superusers


def check(state, slack=120):
    if not isinstance(state, dict) or state.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('The state file has schema_version %r; this check knows %r only. Nothing was checked.'
                         % (state.get('schema_version') if isinstance(state, dict) else None, SCHEMA_VERSION))
    audit = [event for event in (state.get('audit') or []) if isinstance(event, dict)]
    times = [stamp(event.get('time')) for event in audit]
    oldest = min((t for t in times if t is not None), default=None)
    kept = [(stamp(e.get('time')), e) for e in audit if e.get('outcome') in OUTCOMES]
    users = state.get('users') if isinstance(state.get('users'), dict) else {}
    superusers = {uid for uid, user in users.items() if isinstance(user, dict) and user.get('superuser')}
    unexplained, unchecked = [], []
    for kind, project, subject, by, at, actions, ids in entries(state):
        when = stamp(at)
        row = {'kind': kind, 'project': project, 'subject': subject, 'by': by, 'at': at}
        if when is None or oldest is None or when < oldest:
            unchecked.append(row)
            continue
        if not any(t is not None and abs(t - when) <= slack and event.get('action') in actions
                   and explains(event, project, by, ids, superusers)
                   for t, event in kept):
            unexplained.append(row)
    return {'unexplained': unexplained, 'unchecked': unchecked,
            'audit_oldest': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(oldest)) if oldest else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('state_file')
    parser.add_argument('--slack', type=int, default=120, help='seconds between an entry and its audit event')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    with open(args.state_file, encoding='utf-8') as handle:
        state = json.load(handle)
    try:
        report = check(state, args.slack)
    except ValueError as refused:
        sys.stderr.write('%s\n' % refused)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print('Oldest audit event kept: %s' % (report['audit_oldest'] or 'none'))
        for title in ('unexplained', 'unchecked'):
            print('%s: %d' % (title, len(report[title])))
            for row in report[title]:
                print('  %(kind)s in %(project)s: %(subject)s by %(by)s at %(at)s' % row)
    return 1 if report['unexplained'] else 0


if __name__ == '__main__':
    sys.exit(main())
