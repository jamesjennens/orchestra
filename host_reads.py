"""Two read-only endpoint actions for the web interface's coordination views (kittrial-5bb.198).

``host-coordination`` (one project): the installation's operator list and the project's
registered sessions, each session with its registered name and the principal it is mapped
to (``lane:...`` or ``person:...``). The web service shows the operators on the "Who
coordinates" panel and shows a session actor by its name and principal, never by its id
alone.

``host-projects`` (no project): the initialized projects of this runtime that are projects
(a creation that has not finished is left out). The web service lists, for a superuser,
those it has not registered, with or without a creation record.

Both take no lock, write nothing and take no arguments. Both are in the endpoint's
``SERVICE_ONLY_ACTIONS``: a key bound to a project or a principal is refused them before
they are looked at, so a worker's key is not told the operator list, other sessions' names
or the other projects of the runtime. The web service sends them without an authority
block: they are not in the endpoint's capability table, and a block names one.
"""
import json

#: What the answer holds when the project's session registry cannot be read: the operators
#: are still answered, and the sessions say why they are not.
REGISTRY_UNREADABLE = 'The project\'s session registry cannot be read; an operator must look at it on the server.'


def _reply(document):
    return {'returncode': 0, 'stdout': json.dumps(document) + '\n', 'stderr': ''}


def _no_arguments(request, action):
    if request.get('args', []) not in ([], None):
        raise ValueError('Use %s without arguments' % action)


def coordination(request, path, operators):
    """The ``host-coordination`` answer. ``operators`` is the installation's operator list."""
    _no_arguments(request, 'host-coordination')
    from sessions import owner_map, read_registry
    document = {'schema_version': 1, 'operators': sorted(operators), 'sessions': None, 'sessions_note': None}
    try:
        data = read_registry(path)
    except ValueError:
        document['sessions_note'] = REGISTRY_UNREADABLE
        return _reply(document)
    owners = owner_map(data)
    sessions = {}
    for record in data['records'].values():
        actor = record.get('actor')
        if isinstance(actor, str) and actor not in sessions:
            principal = owners.get(actor)
            sessions[actor] = {'actor': actor, 'name': record.get('name') if isinstance(record.get('name'), str) else None,
                               'principal': principal if isinstance(principal, str) else None}
    document['sessions'] = [sessions[actor] for actor in sorted(sessions)]
    return _reply(document)


def projects(request, root):
    """The ``host-projects`` answer: initialized projects whose creation, if any, finished."""
    _no_arguments(request, 'host-projects')
    if request.get('project') is not None:
        raise ValueError('host-projects names no project')
    from admin import initialized_projects
    import project_creation
    found = [name for name in initialized_projects(root) if not project_creation.unfinished(root, name)]
    return _reply({'schema_version': 1, 'projects': found})
