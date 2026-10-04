"""The setup steps of a project, as the web interface shows them (kittrial-5bb.118).

A project that an operator has created on the host and a superuser has registered still
needs several things before people and agents can work in it, and nothing told the new
owner which. ``steps`` answers, at request time, what is done and what is left: members,
where the repository is, a first task, a personal agent, guidance, onboarding and a
scheduled backup. Each step says who can do it and where, or gives the exact command
when only an operator on the host can.

Nothing here writes anywhere. The three host steps are read through the endpoint's
read-only ``setup-status`` action (states, versions and times; never guidance or
onboarding text). A server whose endpoint predates that action answers those steps as
``unavailable``; the in-process backend, which has no host, as ``not-applicable``.
"""
from http_auth import HttpError

#: Step states. ``optional`` is a step that is fine as it is (one person may be the only
#: member); ``unknown`` means the host could not say; ``unavailable`` means this server's
#: endpoint has no setup-status action; ``not-applicable`` means there is no host.
STATES = ('done', 'todo', 'optional', 'unknown', 'unavailable', 'not-applicable')
#: States that count as "left to do" on the page.
REMAINING = ('todo',)
WHO = {'owner': 'A project owner', 'member': 'Any member who may write tasks',
       'each-member': 'Each member, for their own agent',
       'operator': 'An operator, on the server that runs Orchestra'}
OPERATOR_NOTE = ('The web interface cannot do this step. It is done on the server, by an operator, with the command '
                 'shown. If you are also the operator, run it there; otherwise send the command to whoever is.')


def is_merge_slot(row, project_id):
    """The project's merge slot row: an internal record, not a task (see kittrial-5bb.113)."""
    return isinstance(row, dict) and (row.get('id') == '%s-merge-slot' % project_id
                                      or 'gt:slot' in (row.get('labels') or []))


def step(identifier, title, state, detail, who, **where):
    result = {'id': identifier, 'title': title, 'state': state, 'detail': detail, 'who': who,
              'who_text': WHO[who], 'link': None, 'command': None, 'note': None}
    result.update(where)
    return result


def host_status(backend, project_id):
    """``(status, reason)``: the host's answer, or None with why there is none."""
    read = getattr(backend, 'setup_status', None)
    if read is None:
        return None, 'not-applicable'
    try:
        status = read(project_id)
    except HttpError as error:
        if 'Unknown action' in str(error.detail or ''):
            return None, 'unavailable'
        return None, 'unknown'
    return (status, None) if isinstance(status, dict) else (None, 'unknown')


def host_step(identifier, title, status, reason, done, detail, command, when_missing):
    """One operator step from the host status block ``status[identifier]``."""
    if status is None:
        text = {'not-applicable': 'This server has no host project behind it, so there is nothing to set.',
                'unavailable': 'Not available on this server: it runs a version that cannot report this step. '
                               'Ask an operator whether it is done.',
                'unknown': 'The server could not be asked just now. Reload the page to try again.'}[reason]
        return step(identifier, title, reason, text, 'operator', command=command, note=OPERATOR_NOTE)
    block = status.get(identifier) if isinstance(status.get(identifier), dict) else {}
    state, text = done(block)
    return step(identifier, title, state, text or (detail if state == 'done' else when_missing), 'operator',
                command=None if state == 'done' else command, note=None if state == 'done' else OPERATOR_NOTE)


def steps(handler, principal, project_id):
    """The setup page's data. The caller has already checked project administration."""
    service, backend = handler.service, handler.backend
    project = service.project_view(principal, project_id)
    base = '/v1/projects/%s' % project_id
    result = []

    members = service.list_members(principal, project_id)
    result.append(step(
        'members', 'Add the people who will work in this project',
        'done' if len(members) > 1 else 'optional',
        ('%d members.' % len(members)) if len(members) > 1 else
        'You are the only member. That is fine if you work alone; otherwise add the others.',
        'owner', link=base + '/members',
        note='An owner adds contributors and viewers. Only a superuser can make someone an owner.'))

    repository = project.get('repository')
    result.append(step(
        'repository', 'Record where the project\'s repository is',
        'done' if repository else 'todo',
        ('Recorded: %s' % repository) if repository else
        'Not recorded. An agent needs a clone of the repository with a remote it can push to; this tells it '
        'which repository that is.',
        'owner', link=base,
        note='Orchestra stores this as a label and shows it to members and their agents. It does not check that '
             'the repository exists, that anyone can reach it, or that an agent\'s clone points at it.'))

    try:
        rows = [row for row in (backend.read_tasks(project_id).get('items') or [])
                if isinstance(row, dict) and not is_merge_slot(row, project_id)]
        tasks_state, tasks_detail = ('done', '%d task(s) defined.' % len(rows)) if rows else \
            ('todo', 'No task yet. Define the first piece of work so that someone, or an agent, can claim it.')
    except HttpError:
        tasks_state, tasks_detail = 'unknown', 'The task list could not be read just now.'
    result.append(step('first-task', 'Define a first task', tasks_state, tasks_detail, 'member',
                       link=base + '/tasks'))

    agents = [agent for agent in service.list_project_agents(principal, project_id) if agent.get('enabled')]
    mine = [agent for agent in agents if agent.get('owner') == principal.user_id]
    result.append(step(
        'agent', 'Grant a personal agent this project',
        'done' if agents else 'todo',
        ('%d agent(s) may work here%s.' % (len(agents), ', %d of them yours' % len(mine) if mine else
                                             ', none of them yours')) if agents else
        'No agent may work here yet. Each member creates their own agent and grants it this project; the agent '
        'page gives the setup text to copy to the machine the agent runs on.',
        'each-member', link='/v1/agents',
        note='An agent belongs to the member who created it. You can see that other members\' agents exist here, '
             'not their setup.'))

    status, reason = host_status(backend, project_id)
    name = project_id
    result.append(host_step(
        'guidance', 'Set the standing guidance every worker reads', status, reason,
        lambda block: ({'set': ('done', 'Set%s.' % (' on %s' % block['set_at'][:10] if block.get('set_at') else '')),
                        'not-set': ('todo', None),
                        'unbound': ('todo', 'A guidance file is there but its record does not match it, so workers '
                                            'are not given it. Setting it again repairs it.'),
                        'unreadable': ('todo', 'A guidance file is there but cannot be read as guidance, so '
                                               'workers are not given it. Setting clean text repairs it.')}
                       .get(block.get('state'), ('unknown', 'The server could not say whether guidance is set.'))),
        'Set.', 'admin.py set-guidance %s --actor OPERATOR --file FILE' % name,
        'Not set. Guidance is the short standing instruction every worker and agent reads at the start of a run.'))
    result.append(host_step(
        'onboarding', 'Set the project\'s onboarding entry point', status, reason,
        lambda block: ({'set': ('done', 'Set%s.' % (' (last changed %s)' % block['updated_at'][:10]
                                                    if block.get('updated_at') else '')),
                        'not-set': ('todo', None)}
                       .get(block.get('state'), ('unknown', 'The server could not say whether onboarding is set.'))),
        'Set.', 'admin.py set-onboarding %s --file FILE' % name,
        'Not set. The onboarding entry point is what a new worker reads first about this project.'))

    def backup(block):
        last = block.get('last_run') if isinstance(block.get('last_run'), dict) else None
        ran = ''
        if last:
            ran = ' The last backup run recorded this project %s%s%s.' % (
                last.get('status'), ' on %s' % last['completed_at'][:10] if last.get('completed_at') else '',
                ', degraded' if last.get('degraded') else '')
        else:
            ran = ' No backup run has recorded this project yet.'
        scheduled = block.get('scheduled')
        if scheduled == 'covered':
            return 'done', 'A scheduled backup on the server covers this project.' + ran
        if scheduled == 'not-covered':
            return 'todo', ('No scheduled backup on the server covers this project. An operator adds the line '
                            'shown to the backup schedule, or names this project in the existing one.' + ran)
        if block.get('reason') == 'no-account-home':
            return 'unknown', ('The server could not check its backup schedule: the web service was started without '
                               'the account\'s home directory, so it cannot see the installed schedule. Ask an '
                               'operator to check on the server.' + ran)
        return 'unknown', 'The server could not read its backup schedule, so ask an operator.' + ran
    line = (status or {}).get('backup', {}).get('line') if isinstance((status or {}).get('backup'), dict) else None
    result.append(host_step('backup', 'Make sure a scheduled backup covers this project', status, reason, backup,
                            None, line or 'admin.py backup --all (on a schedule)', None))

    return {'project': {'id': project_id, 'name': project.get('name'), 'repository': repository},
            'steps': result,
            'remaining': sum(1 for item in result if item['state'] in REMAINING),
            'host': 'available' if status is not None else reason,
            'generated_at': None}
