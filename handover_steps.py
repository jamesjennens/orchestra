"""The steps of handing a project to a coordinator (kittrial-5bb.442).

Slices 2 and 3 of docs/design/COORDINATOR_ONBOARDING_DESIGN.md: steps O1-O6, C1 and C3 (O6 and C3, the
hand-over note, from kittrial-5bb.443), each checked from facts the web service already holds and said in one plain sentence, with the existing page or route that fixes it.
Nothing here reads or writes state: the route gathers the facts (``Service.handover_facts`` and the
switches read) and this module only turns them into steps, masked for the viewer:

* ``owner``: an owner or superuser reading the walkthrough of another member. It sees every step, but only
  the agents that already have the project (it cannot read anyone's other agents), so a missing agent is
  said in a sentence that names none.
* ``self``: a member reading their own walkthrough. It sees its own agents, and an agent made before the
  membership is named (failure 1 of the design).
* ``coordinator``: a project coordinator reading the walkthrough of the person being handed the project. It
  cannot read other people's agents or their grants' reasons, so those steps are ``hidden``.
* ``agent``: an agent reading its own steps through its credential.
"""
STATES = ('done', 'todo', 'waiting', 'failed', 'hidden')
TIERS = ('owner', 'self', 'coordinator', 'agent')
COORDINATING_ROLES = ('coordinator', 'owner')

#: Why a grant on record has no effect now (``Service.coordinator_view``), said to a person.
NO_EFFECT = {
    'agent-disabled': '{agent} is switched off, so its grant has no effect. Its owner switches it on under My agents.',
    'account-disabled': '{name}\'s account is disabled, so the grant has no effect. A superuser enables the account.',
    'account-role': '{name} is no longer a coordinator here, so the grant has no effect. Set their role to '
                    'Coordinator again.',
    'issuer-role': 'The owner who granted it is no longer an owner here, so the grant has no effect. An owner '
                   'grants it again.',
}
APPROVAL = {
    'by-agent': 'A coordinator agent may approve the work of another agent of the same person (the default).',
    'by-person': 'Strict: a coordinator agent never approves the work of any agent of its own person.',
}


def step(identifier, title, state, detail, who=None, fix=None):
    return {'id': identifier, 'title': title, 'state': state, 'who': who, 'detail': detail, 'fix': fix}


def pick(facts, agent_id, tier):
    """The agent the walkthrough is about: the one named, else one holding a grant here, else the first with
    the project. Only agents the viewer may see are candidates (``visible_agents``)."""
    agents = visible_agents(facts, tier)
    if agent_id:
        return next((a for a in agents if a['id'] == agent_id), None)
    return (next((a for a in agents if a['has_project'] and a.get('coordinator')), None)
            or next((a for a in agents if a['has_project']), None))


def visible_agents(facts, tier):
    """The person's agents this viewer may see: all of them for the person and their own agent; only those
    that have the project for an owner (as GET /v1/projects/{pid}/agents shows); none for a coordinator."""
    agents = facts.get('agents') or []
    if tier == 'self':
        return list(agents)
    if tier == 'owner':
        return [a for a in agents if a['has_project']]
    if tier == 'agent':
        return [a for a in agents if a['id'] == facts.get('viewer_agent')]
    return []


def steps(facts, *, tier, agent_id=None, switches=None, approval=None):
    """Steps O1-O5 and C1 for ``facts`` as ``tier`` may see them.

    ``switches`` is ``{'items': [...]}`` from the switches read, or ``{'refused': True}`` when the viewer may
    not read them, or ``{'unavailable': True}`` where this server has none. ``approval`` is the project's
    ``agent_approval`` read.
    """
    if tier not in TIERS:
        raise ValueError('unknown viewer tier')
    name = facts.get('name') or facts.get('person')
    you = tier in ('self', 'agent')
    agent = pick(facts, agent_id if tier != 'agent' else facts.get('viewer_agent'), tier)
    out = []

    # O1: the person's role.
    if facts.get('role') in COORDINATING_ROLES:
        o1 = step('role', 'Coordinator role', 'done',
                  ('You are' if you else '%s is' % name) + ' a %s of this project.' % facts['role'])
    elif you:
        o1 = step('role', 'Coordinator role', 'todo',
                  'You are not a coordinator here yet: ask an owner of this project to set your role to Coordinator.',
                  who='owner')
    else:
        o1 = step('role', 'Coordinator role', 'todo',
                  '%s is not yet a coordinator here. Set their role to Coordinator.' % name, who='owner',
                  fix={'page': 'members', 'route': 'PUT /v1/projects/{pid}/members/{user}'})
    out.append(o1)

    # O2: one of their agents has the project. Only after O1, so an agent is made after the membership.
    if o1['state'] != 'done':
        o2 = step('agent', 'Their agent has this project', 'waiting', 'Waits for the coordinator role (step 1).',
                  who='person')
    elif agent_id and agent is None and tier in ('owner', 'self'):
        # An agent asked for by id that this reader cannot see (another person's, or one without the project for
        # an owner): one sentence for every such id, so the answer says nothing about which agents exist
        # (review of kittrial-5bb.442, note 3).
        o2 = step('agent', 'Their agent has this project', 'hidden',
                  'The agent asked for is not one you can see in this project.', who='person')
    elif tier == 'coordinator':
        o2 = step('agent', 'Their agent has this project', 'hidden',
                  'Only an owner of this project, or %s, sees their agents here.' % name, who='person')
    elif agent is not None and agent['has_project']:
        o2 = step('agent', 'Their agent has this project', 'done', '%s has this project.' % agent['name'])
    elif tier == 'self':
        lacking = [a for a in visible_agents(facts, tier) if not a['has_project']]
        detail = ('%s was made before you could see this project: open it under My agents and tick this project.'
                  % lacking[0]['name'] if lacking else
                  'You have no agent yet: create one under My agents now and tick this project.')
        o2 = step('agent', 'Their agent has this project', 'todo', detail, who='person',
                  fix={'page': 'my-agents', 'route': 'PATCH /v1/agents/{agent} or POST /v1/agents'})
    else:
        o2 = step('agent', 'Their agent has this project', 'todo',
                  'No agent of %s has this project yet: they open My agents and tick it, or create an agent now.'
                  % name, who='person')
    out.append(o2)

    # O3: the coordinator grant, and whether it has effect.
    grant = agent.get('coordinator') if agent else None
    if tier == 'coordinator':
        # A coordinator reads only the grants that have effect (the coordination view, Service.coordinators_of).
        o3 = (step('grant', 'Coordinator grant', 'done',
                   '%s holds an effective coordinator grant here.' % facts['effective_agent'])
              if facts.get('effective_agent') else
              step('grant', 'Coordinator grant', 'hidden', 'Only an owner of this project sees the state of a grant.',
                   who='owner'))
    elif o2['state'] != 'done':
        o3 = step('grant', 'Coordinator grant', 'waiting', 'Waits for their agent (step 2).', who='owner')
    elif grant is None:
        o3 = step('grant', 'Coordinator grant', 'todo',
                  ('%s holds no coordinator grant here: ask an owner of this project to grant it.' if you else
                   'Grant %s the coordinator role here.') % agent['name'], who='owner',
                  fix=None if you else {'page': 'settings', 'route': 'PUT /v1/projects/{pid}/agents/{agent}/coordinator'})
    elif grant.get('effective'):
        o3 = step('grant', 'Coordinator grant', 'done', '%s holds an effective coordinator grant here.' % agent['name'])
    elif tier == 'agent':
        o3 = step('grant', 'Coordinator grant', 'failed',
                  'This agent\'s coordinator grant has no effect now: ask an owner of this project.', who='owner')
    else:
        reason = NO_EFFECT.get(grant.get('no_effect_because'),
                               'The grant has no effect now; an owner grants it again.')
        o3 = step('grant', 'Coordinator grant', 'failed', reason.format(agent=agent['name'], name=name), who='owner',
                  fix={'page': 'settings', 'route': 'PUT /v1/projects/{pid}/agents/{agent}/coordinator'})
    out.append(o3)

    # O4: the installation switches, read only; the operator changes them.
    switches = switches or {}
    if switches.get('refused'):
        o4 = step('switches', 'Installation switches', 'hidden', 'Shown to coordinators and owners of this project.')
    elif switches.get('unavailable'):
        o4 = step('switches', 'Installation switches', 'hidden', 'This server cannot read installation switches.')
    else:
        items = [i for i in switches.get('items') or [] if isinstance(i, dict)]
        broken = [i for i in items if i.get('state') == 'unreadable']
        off = [i for i in items if i.get('state') == 'off']
        if broken:
            o4 = step('switches', 'Installation switches', 'failed',
                      'The setting %s cannot be read: ask your operator to repair it on the server.'
                      % ', '.join(i['name'] for i in broken), who='operator',
                      fix={'commands': [i.get('command') for i in broken]})
        elif off:
            o4 = step('switches', 'Installation switches', 'todo',
                      'Off: %s. Ask your operator to run this on the server.' % ', '.join(i['name'] for i in off),
                      who='operator', fix={'commands': [i.get('command') for i in off]})
        else:
            o4 = step('switches', 'Installation switches', 'done', 'Every switch a coordinator uses is on.')
    out.append(o4)

    # O5: how agent approvals are judged (kittrial-5bb.438). Information, always shown.
    approval = approval or {}
    mode = approval.get('mode') if approval.get('mode') in APPROVAL else 'by-person'
    setter = approval.get('set_by_display_name') or approval.get('set_by')
    detail = APPROVAL[mode] + (' Set by %s at %s.' % (setter, approval.get('set_at')) if setter else
                               ' Nobody has changed it.')
    out.append(step('agent-approval', 'Agent approvals', 'done', detail,
                    fix={'page': 'settings', 'route': 'PUT /v1/projects/{pid}/agent-approval'}))

    # O6: the hand-over note (kittrial-5bb.443). Optional: without one the step is todo, never blocking.
    note = facts.get('note')
    if note:
        out.append(step('handover-note', 'Hand-over note', 'done',
                        '%s wrote a hand-over note for %s (version %d, %s).'
                        % (note['written_by_name'], 'you' if you else name, note['version'], note['written_at'])))
    else:
        out.append(step('handover-note', 'Hand-over note', 'todo',
                        'No hand-over note was written for %s. The outgoing coordinator or an owner writes one '
                        'on this page; it is optional and blocks nothing.' % ('you' if you else name),
                        who='owner', fix={'page': 'handover-note', 'route': 'PUT /v1/projects/{pid}/handover/note'}))

    # C1: the agent's folder set up after the grant (it has reached the server since the grant).
    if tier == 'coordinator' or (o3['state'] == 'done' and agent is None):
        c1 = step('setup-after-grant', 'Set up after the grant', 'hidden',
                  'Only an owner of this project, or %s, sees when their agent last reached the server.' % name,
                  who='person')
    elif o3['state'] != 'done':
        c1 = step('setup-after-grant', 'Set up after the grant', 'waiting',
                  'Waits for an effective grant (step 3).', who='person')
    elif reached_since(agent.get('last_seen_at'), grant.get('granted_at')):
        c1 = step('setup-after-grant', 'Set up after the grant', 'done',
                  '%s has reached the server since the grant.' % agent['name'])
    else:
        c1 = step('setup-after-grant', 'Set up after the grant', 'todo',
                  'Open the set-up for %s again now and replace its AGENT.md: it was written before the grant.'
                  % agent['name'], who='person', fix={'page': 'agent-setup', 'route': None})
    out.append(c1)

    # C3: the hand-over note acknowledged by its person or their granted agent (kittrial-5bb.443).
    if not note:
        out.append(step('acknowledge-note', 'Acknowledge the hand-over note', 'done',
                        'There is no hand-over note to acknowledge.'))
    elif note['acknowledged']:
        out.append(step('acknowledge-note', 'Acknowledge the hand-over note', 'done',
                        'Version %d of the hand-over note has been acknowledged.' % note['version']))
    else:
        out.append(step('acknowledge-note', 'Acknowledge the hand-over note', 'todo',
                        ('Read the hand-over note and acknowledge version %d.' if you else
                         '%s has not yet acknowledged version %%d of the hand-over note.' % name) % note['version'],
                        who='person', fix={'page': 'handover-note',
                                           'route': 'POST /v1/projects/{pid}/handover/note/acknowledge'}))
    return out


def reached_since(last_seen, granted_at):
    """Whether ``last_seen`` is after ``granted_at``: both are the service's own ISO stamps (``now_iso``)."""
    return isinstance(last_seen, str) and isinstance(granted_at, str) and last_seen > granted_at
