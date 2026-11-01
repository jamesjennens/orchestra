#!/usr/bin/env python3
"""Read-only: authority grants in a web service state file that no committed audit event explains.

kittrial-5bb.487. Before that change a grant whose state save failed stayed in memory, in force,
and was written by the next unrelated save with no audit entry of its own. This lists, for a copy
of the state file (or the file itself; it is only read):

  * ``unexplained``: a coordinator or release grant, a project's agent-approval setting or a
    credential whose time has no ``committed`` audit event of its route, by the same account,
    within ``--slack`` seconds;
  * ``unchecked``: an entry older than the oldest audit event the state still keeps (the audit is
    capped), which therefore cannot be checked this way.

An unexplained entry is a lead for an operator, not proof: compare it with the service log of that
time. Nothing is changed. Usage: authority_audit_check.py STATE_FILE [--slack SECONDS] [--json]
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
import time


def stamp(text):
    """Seconds since the epoch for the service's ``YYYY-MM-DDTHH:MM:SSZ`` stamps, else None."""
    if not isinstance(text, str):
        return None
    try:
        return calendar.timegm(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ'))
    except ValueError:
        return None


def entries(state):
    """(kind, project, subject, by, at, route actions) for each authority entry with a time."""
    for kind, key, actions in (('coordinator grant', 'coordinator_grants', ('agents.coordinator.grant',)),
                               ('release grant', 'release_grants', ('agents.release.grant',))):
        family = state.get(key)
        for project, grants in (family.items() if isinstance(family, dict) else ()):
            for agent, grant in (grants.items() if isinstance(grants, dict) else ()):
                if isinstance(grant, dict):
                    yield kind, project, agent, grant.get('granted_by'), grant.get('granted_at'), actions
    approval = state.get('agent_approval')
    for project, setting in (approval.items() if isinstance(approval, dict) else ()):
        if isinstance(setting, dict):
            yield ('agent-approval setting', project, setting.get('mode'), setting.get('set_by'),
                   setting.get('set_at'), ('projects.agent_approval',))
    credentials = state.get('credentials')
    for credential_id, credential in (credentials.items() if isinstance(credentials, dict) else ()):
        if not isinstance(credential, dict):
            continue
        actions = (('agents.credentials.issued', 'agents.credentials.issue', 'agents.created', 'agents.create')
                   if credential.get('agent_id') else ('credentials.issue',))
        yield ('credential', credential.get('project_id'), credential_id, credential.get('user_id'),
               credential.get('created_at'), actions)


def check(state, slack=120):
    audit = [event for event in (state.get('audit') or []) if isinstance(event, dict)]
    times = [stamp(event.get('time')) for event in audit]
    oldest = min((t for t in times if t is not None), default=None)
    committed = [(stamp(e.get('time')), e.get('action'), e.get('user_id'), e.get('project_id'))
                 for e in audit if e.get('outcome') == 'committed']
    unexplained, unchecked = [], []
    for kind, project, subject, by, at, actions in entries(state):
        when = stamp(at)
        row = {'kind': kind, 'project': project, 'subject': subject, 'by': by, 'at': at}
        if when is None or oldest is None or when < oldest:
            unchecked.append(row)
            continue
        if not any(t is not None and abs(t - when) <= slack and action in actions
                   and (by is None or user == by) and (project is None or event_project in (None, project))
                   for t, action, user, event_project in committed):
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
    report = check(state, args.slack)
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
