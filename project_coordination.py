"""Who coordinates a project, as the web interface shows it to any member (kittrial-5bb.198).

Item 8 of the slices of COORDINATORS_PER_PROJECT_DESIGN.md: the page shows who coordinates
a project and changes nothing; keys and the operator list stay host actions. Three parts:
the members whose role coordinates, the agents whose coordinator grant has effect, and the
rules that bind them -- the web service's own setting (approval by another party) and the
installation's three coordinator rules, which the host reports in the read-only
``setup-status`` endpoint action the setup page already uses. Of that answer only the rules
block is passed on: a member who does not administer the project is not told the rest.

The installation's operator list comes from the read-only ``host-coordination`` endpoint
action (host_reads.py), sent without an authority block: only the operators are passed on
here; the project's session names serve the names the web interface shows elsewhere.

Nothing here writes anywhere.
"""
from http_auth import HttpError
from project_setup import host_status

#: The installation's coordinator rules, said for a member (``admin.COORDINATOR_RULE_KEYS``;
#: a test holds the two lists equal). The host's own sentence is written for an operator.
RULES = {
    'lifecycle_authors': 'Reviewed, integrated, deployed, live-verified, enabled, live and a release deploy are recorded '
                         'only by a listed operator; implemented, tested and the task scope only by the assignee or a '
                         'listed operator.',
    'independent_approval': 'An approval by the contribution\'s author or the task\'s assignee is refused.',
    'coordinator_writes': 'Only a listed actor of the project approves a contribution or takes the merge slot.',
}
#: The web service's own rule (kittrial-5bb.199): the setting ``approval_by_another_party``.
OTHER_PARTY = ('Nobody approves a contribution of their own party: their account, its agents and the names its '
               'worker credentials hold.')


def host_rules(backend, project_id):
    """The installation's coordinator rules from the host, or a state saying why there are none.

    ``state`` is ``available``, ``unknown`` (the host could not read its settings, or could not
    be asked), ``unavailable`` (its endpoint predates the read or the block) or
    ``not-applicable`` (no host behind this service).
    """
    status, reason = host_status(backend, project_id)
    if status is None:
        return {'state': reason, 'rules': []}
    block = status.get('coordinator_rules')
    if not isinstance(block, dict):
        return {'state': 'unavailable', 'rules': []}
    values = block.get('rules')
    if block.get('enabled') is None or not isinstance(values, dict):
        return {'state': 'unknown', 'rules': []}
    return {'state': 'available',
            'rules': [{'rule': key, 'on': values.get(key) is True, 'text': text} for key, text in RULES.items()]}


def host_operators(backend, project_id):
    """The installation's operator list from the host, or a state saying why there is none.

    The states are those of :func:`host_rules`. The read is fresh: the panel is opened rarely, and
    an operator added just now should be seen.
    """
    read = getattr(backend, 'host_coordination', None)
    if read is None:
        return {'state': 'not-applicable', 'operators': []}
    try:
        answer = read(project_id, fresh=True)
    except HttpError as error:
        return {'state': 'unavailable' if 'Unknown action' in str(error.detail or '') else 'unknown', 'operators': []}
    operators = answer.get('operators')
    if not isinstance(operators, list):
        return {'state': 'unknown', 'operators': []}
    return {'state': 'available', 'operators': [name for name in operators if isinstance(name, str)]}


def view(handler, principal, project_id):
    """The panel's data. The caller has checked that the principal may read the project."""
    service = handler.service
    result = service.coordinators_of(principal, project_id)
    result.update(project=project_id,
                  web_rules=[{'rule': 'approval_by_another_party',
                              'on': bool(getattr(service, 'approval_by_another_party', False)),
                              'text': OTHER_PARTY}],
                  host_rules=host_rules(handler.backend, project_id),
                  host_operators=host_operators(handler.backend, project_id),
                  generated_at=None)
    return result
