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
OPERATOR_NOTE = ('The web interface cannot do this step. It is done on the server, by an operator. If you are also '
                 'the operator, do it there with what is shown; otherwise send it to whoever is.')
#: What an entry of a step's ``commands`` is: ``shell`` can be pasted into a shell on the server as it
#: stands; ``shell-fill`` is a shell command with words to replace first, which the entry names;
#: ``unit-line`` is a line of a systemd unit file and NOT a command (kittrial-5bb.200: the page showed one
#: as "the command" and it answered Permission denied).
KINDS = ('shell', 'shell-fill', 'unit-line')


def entry(kind, label, text, note=None, replace=()):
    return {'kind': kind, 'label': label, 'text': text, 'note': note, 'replace': list(replace)}


def fill(status, label, words, note, replace):
    """A host command with words to replace: the host's own beginning of a command (interpreter,
    admin.py, --root) and then ``words``. An endpoint older than that answer, or no host at all,
    cannot say how its commands begin; the entry then names those words as words to replace too."""
    prefix = status.get('admin') if isinstance(status, dict) else None
    if isinstance(prefix, str) and prefix.strip():
        return entry('shell-fill', label, prefix + ' ' + words, note, replace)
    return entry('shell-fill', label, 'PYTHON KIT/admin.py --root RUNTIME_ROOT ' + words,
                 note + ' This server did not say how its commands begin: PYTHON is the interpreter the kit runs '
                        'under, KIT the installed kit and RUNTIME_ROOT the runtime.',
                 ('PYTHON', 'KIT', 'RUNTIME_ROOT') + tuple(replace))


def is_merge_slot(row, project_id=None):
    """The project's merge slot row: an internal record, not a task.

    One rule for the whole kit (``coordination.is_merge_slot``, kittrial-5bb.113). The
    web service's task list no longer carries the slot at all; this stays so that a
    backend that still returned it would not count it as a first task.
    """
    from coordination import is_merge_slot as shared
    return shared(row)


def step(identifier, title, state, detail, who, **where):
    result = {'id': identifier, 'title': title, 'state': state, 'detail': detail, 'who': who,
              'who_text': WHO[who], 'link': None, 'command': None, 'commands': [], 'note': None}
    result.update(where)
    # ``command`` stays for a page older than this service: the first thing that can be pasted.
    commands = result['commands'] = list(result.get('commands') or [])
    if commands and result.get('command') is None:
        result['command'] = commands[0]['text']
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


def host_step(identifier, title, status, reason, done, detail, commands, when_missing):
    """One operator step from the host status block ``status[identifier]``."""
    if status is None:
        text = {'not-applicable': 'This server has no host project behind it, so there is nothing to set.',
                'unavailable': 'Not available on this server: it runs a version that cannot report this step. '
                               'Ask an operator whether it is done.',
                'unknown': 'The server could not be asked just now. Reload the page to try again.'}[reason]
        return step(identifier, title, reason, text, 'operator', commands=commands, note=OPERATOR_NOTE)
    block = status.get(identifier) if isinstance(status.get(identifier), dict) else {}
    state, text = done(block)
    return step(identifier, title, state, text or (detail if state == 'done' else when_missing), 'operator',
                commands=[] if state == 'done' else commands, note=None if state == 'done' else OPERATOR_NOTE)


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
    # A value stored under an earlier rule that no longer passes is withheld by the
    # project view (kittrial-5bb.123): the step is to do again, and says why.
    stale = bool(project.get('repository_needs_attention'))
    result.append(step(
        'repository', 'Record where the project\'s repository is',
        'done' if repository else 'todo',
        ('Recorded: %s' % repository) if repository else
        ('A location was recorded before the rule for this field changed, and it no longer fits. It is not shown '
         'and not given to agents. Record the location again.') if stale else
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

    def merge_slot(block):
        # The host's own read of the project's merge slot (kittrial-5bb.202 item 2): a
        # project with none, or one whose slot record is damaged, cannot issue a worker
        # credential and needs an operator to run the merge-create coordination operation.
        state = block.get('state')
        if state == 'healthy':
            return 'done', 'The project has a usable merge slot.'
        if state in ('missing', 'damaged'):
            return 'todo', (block.get('detail') or
                            'Run the merge-create operation, which creates or repairs it, then try again.')
        return 'unknown', 'The server could not say whether the project has a usable merge slot.'

    result.append(host_step(
        'merge-slot', 'Make sure the project has a usable merge slot',
        # The host's key is ``merge_slot``; host_step looks a step up by its id, so hand it a
        # one-key status. A service that predates the field says unknown, not "no host".
        None if status is None else {'merge-slot': status.get('merge_slot')},
        reason, merge_slot, None, [], None))
    result.append(host_step(
        'guidance', 'Set the standing guidance every worker reads', status, reason,
        lambda block: ({'set': ('done', 'Set%s.' % (' on %s' % block['set_at'][:10] if block.get('set_at') else '')),
                        'not-set': ('todo', None),
                        'unbound': ('todo', 'A guidance file is there but its record does not match it, so workers '
                                            'are not given it. Setting it again repairs it.'),
                        'unreadable': ('todo', 'A guidance file is there but cannot be read as guidance, so '
                                               'workers are not given it. Setting clean text repairs it.')}
                       .get(block.get('state'), ('unknown', 'The server could not say whether guidance is set.'))),
        'Set.', [fill(status, 'Set the guidance (a shell command, on the server)',
                      'set-guidance %s --actor OPERATOR --file FILE' % name,
                      'Replace OPERATOR with your name on the operator list and FILE with the path of the text.',
                      ('OPERATOR', 'FILE'))],
        'Not set. Guidance is the short standing instruction every worker and agent reads at the start of a run.'))
    result.append(host_step(
        'onboarding', 'Set the project\'s onboarding entry point', status, reason,
        lambda block: ({'set': ('done', 'Set%s.' % (' (last changed %s)' % block['updated_at'][:10]
                                                    if block.get('updated_at') else '')),
                        'not-set': ('todo', None)}
                       .get(block.get('state'), ('unknown', 'The server could not say whether onboarding is set.'))),
        'Set.', [fill(status, 'Set the onboarding text (a shell command, on the server)',
                      'set-onboarding %s --file FILE' % name, 'Replace FILE with the path of the text.', ('FILE',))],
        'Not set. The onboarding entry point is what a new worker reads first about this project.'))
    # An owner may set the onboarding text on this page (kittrial-5bb.118 part 2); an
    # operator may still set it on the server. Guidance stays with the operator.
    onboarding = result[-1]
    if onboarding['state'] not in ('unavailable', 'not-applicable'):
        onboarding.update(who='owner-or-operator',
                          who_text='A project owner, on this page; or an operator, on the server.',
                          link='/v1/projects/%s/onboarding' % project_id,
                          note='Text an owner sets here is shown to workers as information written by a project '
                               'owner. It is not the standing guidance, which only an operator sets.')

    def backup(block):
        # Two different things, and the step says which is missing (kittrial-5bb.200: "todo" beside
        # "the last backup run recorded this project complete" read as a contradiction): a backup that
        # has been RUN, and a SCHEDULE that will run the next one.
        last = block.get('last_run') if isinstance(block.get('last_run'), dict) else None
        if last:
            when = ' on %s' % last['completed_at'][:10] if last.get('completed_at') else ''
            run = 'A backup of this project was run%s and recorded it %s%s' % (
                when, last.get('status'), ', degraded' if last.get('degraded') else '')
        scheduled = block.get('scheduled')
        if scheduled == 'covered':
            return 'done', ('A scheduled backup on the server covers this project. ' + (run + '.' if last else
                            'It has not run yet: no backup run has recorded this project.'))
        if scheduled == 'not-covered':
            looked = ('The step looks for a systemd unit of the service account (beads-*backup*.service) that backs '
                      'up this runtime; a schedule kept anywhere else is not seen here.')
            if last:
                return 'todo', (run + ', but no schedule on the server covers it, so it will not be backed up again '
                                'by itself. What is missing is the schedule. ' + looked)
            return 'todo', ('No backup of this project has been run, and no schedule on the server covers it: both '
                            'are missing. Run one now, then add the schedule. ' + looked)
        ran = (' ' + run + '.') if last else ' No backup run has recorded this project yet.'
        if block.get('reason') == 'no-account-home':
            return 'unknown', ('The server could not check its backup schedule: the web service was started without '
                               'the account\'s home directory, so it cannot see the installed schedule. Ask an '
                               'operator to check on the server.' + ran)
        return 'unknown', 'The server could not read its backup schedule, so ask an operator.' + ran
    block = (status or {}).get('backup') if isinstance((status or {}).get('backup'), dict) else {}
    line, run_now, check = (block.get(key) if isinstance(block.get(key), str) else None for key in ('line', 'run_now', 'check'))
    where = block.get('unit_directory') if isinstance(block.get('unit_directory'), str) else None
    commands = []
    if run_now:
        commands.append(entry('shell', 'Run a backup now (a shell command, on the server, as the service account)', run_now,
                              'It backs up every project of this server once. It does not schedule anything.'))
    commands.append(entry(
        'unit-line', 'The line for a schedule (a line of a systemd unit file, not a shell command)',
        line or 'ExecStart=PYTHON admin.py --root RUNTIME_ROOT backup --all',
        'It goes in the [Service] section of a beads-*backup*.service unit%s, which a timer runs. To check, reload this '
        'page: the step reads the installed units.%s' % (
            ' in %s' % where if where else ' in the service account\'s systemd user directory',
            ' On the server: systemctl --user list-timers, and the check below.' if check else '')))
    if check:
        commands.append(entry('shell', 'Check that every project has a complete backup (a shell command, on the server)', check))
    result.append(host_step('backup', 'Make sure a scheduled backup covers this project', status, reason, backup,
                            None, commands, None))

    if project.get('repository_warning'):
        result[1]['warning'] = project['repository_warning']
    return {'project': {'id': project_id, 'name': project.get('name'), 'repository': repository},
            'steps': result,
            'remaining': sum(1 for item in result if item['state'] in REMAINING),
            # Steps whose state the server could not read (kittrial-5bb.123): they are not
            # "left to do" and they are not done, so the page says how many there are.
            'unchecked': sum(1 for item in result if item['state'] == 'unknown'),
            'host': 'available' if status is not None else reason,
            # The installation's review rules an owner should know of (kittrial-5bb.199). A setting of
            # the web service, so it is the service that says it; the host is not asked.
            'rules': {'approval_by_another_party': bool(getattr(service, 'approval_by_another_party', False))},
            # How many project databases the server holds and its limit: for a superuser only,
            # because it counts other people's projects (kittrial-5bb.118 part 2 revision).
            'server': (status.get('project_databases') if isinstance(status, dict) and principal.superuser
                       and isinstance(status.get('project_databases'), dict) else None),
            'generated_at': None}
