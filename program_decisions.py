"""Decisions across levels and suggestions (kittrial-5bb.454; PROGRAM_DESIGN.md sections 6 and 9.5).

A program decision is a decision issue of the program project with a ``Binds:`` line (the areas it applies to) and
an ``Owner-only:`` line. It binds an area only when the area adopts it: a decision issue of the area with an
``Adopts: decision:<program>/<id>`` line. An owner-only decision is adopted by a person owner of the area (or an
operator), or by an area coordinator whose adoption cites ``Per: decision:<area>/<id>``, a decision of the area
written by a person owner (or an operator). Never an agent alone. Nothing in the program grants authority in an area.

Who wrote a row is its native ``created_by``, trusted only for what cannot be forged there:
* a bare ``usr_<16 hex>`` or ``agent_<16 hex>`` reaches the tracker only from the web service, under a live
  descriptor that names it (http_authority.http_actor_denial); every host route refuses it and its look-alikes
  (reserved_comments.refuse_http_actor);
* any other name counts only as an operator on the installation's allowlist, by its exact name.
Authority is decided LIVE at every read from the web state's memberships, accounts, agents and grants: an author
who has lost the role since stops counting. Nothing here reads state, a clock or a file; callers pass what they read.
"""
import re

AREA = r'[a-z][a-z0-9]{1,23}'
ISSUE = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}'
BINDS = re.compile(r'Binds: (%s(?:, %s)*)\Z' % (AREA, AREA))
OWNER_ONLY = re.compile(r'Owner-only: (yes|no)\Z')
ADOPTS = re.compile(r'Adopts: decision:(%s)/(%s)\Z' % (AREA, ISSUE))
PER = re.compile(r'Per: decision:(%s)/(%s)\Z' % (AREA, ISSUE))
LINES = (('Binds:', 'binds', BINDS), ('Owner-only:', 'owner_only', OWNER_ONLY), ('Adopts:', 'adopts', ADOPTS),
         ('Per:', 'per', PER))
FROM_LINE = re.compile(r'From: (\S{1,200})\Z')
SUGGESTION_LABEL = 'suggestion'
PERSON = re.compile(r'usr_[0-9a-f]{16}\Z')
AGENT = re.compile(r'agent_[0-9a-f]{16}\Z')
#: Rows that are never decisions or suggestions, whatever their labels: what the queue never lists as work.
NOT_WORK_TYPES = ('event', 'gate')
ROWS_MAX = 50
#: How far a supersession chain is followed for superseded-upstream.
CHAIN_MAX = 20

OWNERS = ('owner', 'operator')
COORDINATORS = ('owner', 'operator', 'coordinator', 'agent-coordinator')


def lines(row):
    """The decision lines of a row's description: ``{'binds', 'owner_only', 'adopts', 'per', 'problems'}``.

    Each is a whole line, anywhere in the description, at most once (Q1 at VERIFIED). A line that starts with one of
    the four labels but does not read exactly, or a second line with the same label, is a problem: the row then
    binds and adopts nothing (``malformed-lines``)."""
    found = {'binds': None, 'owner_only': None, 'adopts': None, 'per': None, 'problems': []}
    description = row.get('description') if isinstance(row, dict) else None
    if not isinstance(description, str):
        return found
    for line in description.split('\n'):
        line = line.rstrip('\r')
        for label, name, pattern in LINES:
            if not line.startswith(label):
                continue
            match = pattern.fullmatch(line)
            if match is None:
                found['problems'].append('the %s line does not read as the design writes it' % label)
            elif found[name] is not None:
                found['problems'].append('a second %s line' % label)
            elif name == 'binds':
                areas = match.group(1).split(', ')
                if len(set(areas)) != len(areas):
                    found['problems'].append('an area named twice on the Binds: line')
                found['binds'] = areas
            elif name == 'owner_only':
                found['owner_only'] = match.group(1) == 'yes'
            else:
                found[name] = (match.group(1), match.group(2))
            break
    return found


def is_decision(row):
    """A decision issue (the kit's one predicate) that is not an event or gate row."""
    from delegations import is_decision_issue
    return is_decision_issue(row) and row.get('issue_type') not in NOT_WORK_TYPES


def authority(author, project, state, operators):
    """Who ``author`` is in ``project`` now: 'owner', 'coordinator', 'agent-coordinator', 'operator' or None.

    * a bare ``usr_`` id: its account enabled, and its role in the project owner or coordinator;
    * a bare ``agent_`` id: the agent enabled and holding the project, its account enabled and itself owner or
      coordinator there (a superuser who is only a viewer does not count: .447 r2's rule), and the coordinator grant
      in force (http_authority.coordinator_grant, grant_issuer_holds);
    * any other name: an operator on the allowlist, by its exact name (review_workflow.listed_operator_actor);
    * anything else, a worker credential (``usr_.../label``) included: None."""
    if not isinstance(author, str) or not author:
        return None
    state = state if isinstance(state, dict) else {}
    users = state.get('users') or {}
    roles = ((state.get('memberships') or {}).get(project) or {})
    if PERSON.fullmatch(author):
        user = users.get(author)
        if not isinstance(user, dict) or user.get('disabled'):
            return None
        return {'owner': 'owner', 'coordinator': 'coordinator'}.get(roles.get(author))
    if AGENT.fullmatch(author):
        from http_authority import coordinator_grant, grant_issuer_holds
        agent = (state.get('agents') or {}).get(author)
        if not isinstance(agent, dict) or not agent.get('enabled') or project not in (agent.get('projects') or []):
            return None
        owner = users.get(agent.get('owner'))
        if not isinstance(owner, dict) or owner.get('disabled') or roles.get(agent.get('owner')) not in ('owner',
                                                                                                        'coordinator'):
            return None
        grant = coordinator_grant(state, project, author)
        if grant is None or not grant_issuer_holds(state, project, grant):
            return None
        return 'agent-coordinator'
    from review_workflow import listed_operator_actor
    if '/' not in author and listed_operator_actor(author, list(operators or ()), None):
        return 'operator'
    return None


def program_decision(row, program, state, operators):
    """One binding decision of the program, or None for a decision issue with no Binds: line.

    It counts only without flags: an owner-only decision whose author is not a person owner of the program or an
    operator (``owner-only-not-by-owner``), another whose author does not coordinate the program
    (``not-by-coordinator``), or one whose lines do not read (``malformed-lines``) binds nothing."""
    if not is_decision(row):
        return None
    found = lines(row)
    if found['binds'] is None and not any('Binds:' in problem for problem in found['problems']):
        return None
    flags, reasons = [], []
    if found['problems'] or found['owner_only'] is None or found['binds'] is None:
        flags.append('malformed-lines')
        reasons.extend(found['problems'] or ['a binding decision has one Binds: line and one Owner-only: line'])
    else:
        who = authority(row.get('created_by'), program, state, operators)
        if found['owner_only'] and who not in OWNERS:
            flags.append('owner-only-not-by-owner')
            reasons.append('an owner-only decision counts only when a person owner of %s or an operator wrote it'
                           % program)
        elif not found['owner_only'] and who not in COORDINATORS:
            flags.append('not-by-coordinator')
            reasons.append('a program decision counts only when a coordinator or an owner of %s, or an operator, '
                           'wrote it' % program)
    from trace_read import SUPERSEDES
    description = row.get('description') if isinstance(row.get('description'), str) else ''
    first = SUPERSEDES.fullmatch(description.split('\n')[0].rstrip('\r'))
    return {'id': str(row.get('id')), 'title': row.get('title'), 'status': row.get('status'),
            'owner_only': found['owner_only'], 'binds': list(found['binds'] or []),
            'supersedes': first.group(1) if first else None, 'flags': flags, 'reasons': reasons}


def adoption(row, decision, program, area, area_rows, state, operators):
    """``(state, reason)`` of one adoption row in ``area`` of a counting program ``decision`` (section 6.2)."""
    found = lines(row)
    if found['problems']:
        return 'adoption-invalid', 'the adoption\'s lines do not read: ' + '; '.join(found['problems'])
    who = authority(row.get('created_by'), area, state, operators)
    if not decision['owner_only']:
        if who in COORDINATORS:
            return 'adopted', None
        return 'adoption-invalid', 'the adoption was not written by a coordinator or an owner of %s' % area
    if who in OWNERS:
        return 'adopted', None
    if who not in ('coordinator', 'agent-coordinator'):
        return 'adoption-invalid', 'the adoption was not written by a coordinator or an owner of %s' % area
    if found['per'] is None:
        return 'adoption-invalid', ('an owner-only decision is adopted by a person owner of %s, or by a coordinator '
                                    'whose adoption cites the owner\'s decision with Per:' % area)
    if found['per'][0] != area:
        return 'adoption-invalid', 'Per: names a decision of another project, not of %s' % area
    cited = next((r for r in area_rows if isinstance(r, dict) and str(r.get('id')) == found['per'][1]), None)
    if cited is None or not is_decision(cited):
        return 'adoption-invalid', 'Per: names no decision of %s' % area
    if authority(cited.get('created_by'), area, state, operators) not in OWNERS:
        return 'adoption-invalid', ('the Per: decision was not written by a person owner of %s or an operator: an '
                                    'agent or a coordinator never makes an owner-only choice' % area)
    return 'adopted', None


def area_adoptions(area_rows, program):
    """{decision id: [adoption rows]} of an area's decision issues that adopt a decision of ``program``."""
    found = {}
    for row in sorted((r for r in area_rows or [] if is_decision(r)), key=lambda r: str(r.get('id'))):
        adopts = lines(row)['adopts']
        if adopts is not None and adopts[0] == program:
            found.setdefault(adopts[1], []).append(row)
    return found


def area_state(decision, chain, program, area, area_rows, state, operators):
    """``{'state', ...}`` of one bound, readable area: adopted (with the adoption's id), adoption-invalid (with the
    reason), superseded-upstream (an adoption of a decision this one supersedes, and none of this one), or
    not-adopted."""
    adoptions = area_adoptions(area_rows, program)
    first_reason = None
    for row in adoptions.get(decision['id'], []):
        verdict, reason = adoption(row, decision, program, area, area_rows, state, operators)
        if verdict == 'adopted':
            return {'state': 'adopted', 'adoption': str(row.get('id'))}
        if first_reason is None:
            first_reason = (str(row.get('id')), reason)
    if first_reason is not None:
        return {'state': 'adoption-invalid', 'adoption': first_reason[0], 'reason': first_reason[1]}
    older = [old for old in chain if adoptions.get(old)]
    if older:
        return {'state': 'superseded-upstream', 'adopts': older[0]}
    return {'state': 'not-adopted'}


def chains(decisions):
    """{decision id: [the ids it supersedes, nearest first]}, at most CHAIN_MAX deep, cycles cut."""
    by_id = {d['id']: d for d in decisions}
    out = {}
    for decision in decisions:
        seen, current = [], decision.get('supersedes')
        while current and current not in seen and current != decision['id'] and len(seen) < CHAIN_MAX:
            seen.append(current)
            current = (by_id.get(current) or {}).get('supersedes')
        out[decision['id']] = seen
    return out


def decisions_section(program_rows, program, states_of_areas, area_rows_by_area, state, operators, offset=0):
    """The program view's decisions section (section 5 table; 6.1, 6.2), 50 a page.

    ``states_of_areas`` is {area: 'in' or 'not-checked'} for the areas whose program side is in: any other area is
    ``not-in-program`` and is never read. 'not-checked' is an area off this page of areas or outside a bound key.
    ``area_rows_by_area`` holds the rows of the readable areas of this read only; an area that is in but not among
    them is ``not-visible`` (not readable, or no such project: the same entry, no id, no reason)."""
    decisions = [d for d in (program_decision(row, program, state, operators) for row in program_rows or []) if d]
    decisions.sort(key=lambda d: d['id'])
    links = chains(decisions)
    superseded = {old for chain in links.values() for old in chain[:1]}
    items = []
    for decision in decisions:
        entry = dict(decision, superseded=decision['id'] in superseded, areas={})
        if not decision['flags']:
            for area in decision['binds']:
                where = states_of_areas.get(area)
                if where is None:
                    entry['areas'][area] = {'state': 'not-in-program'}
                elif where == 'not-checked':
                    entry['areas'][area] = {'state': 'not-checked'}
                elif area in area_rows_by_area:
                    entry['areas'][area] = area_state(decision, links[decision['id']], program, area,
                                                      area_rows_by_area[area], state, operators)
                else:
                    entry['areas'][area] = {'state': 'not-visible'}
        items.append(entry)
    page = items[offset:offset + ROWS_MAX]
    following = offset + ROWS_MAX
    return {'items': page, 'total': len(items), 'next_offset': following if following < len(items) else None}


def suggestions_section(program_rows, offset=0):
    """The open tasks of the program labelled ``suggestion``, oldest first, 50 a page, with their From: line as written.
    No area is read for them (Q5 at VERIFIED)."""
    from coordination import is_merge_slot
    from reserved_comments import is_record_anchor
    found = []
    for row in program_rows or []:
        if not isinstance(row, dict) or SUGGESTION_LABEL not in (row.get('labels') or []):
            continue
        if row.get('status') == 'closed' or row.get('issue_type') in NOT_WORK_TYPES or is_merge_slot(row) \
                or is_record_anchor(row):
            continue
        description = row.get('description') if isinstance(row.get('description'), str) else ''
        source = next((m.group(1) for m in (FROM_LINE.fullmatch(line.rstrip('\r')) for line in description.split('\n'))
                       if m), None)
        found.append({'id': str(row.get('id')), 'title': row.get('title'), 'created_at': row.get('created_at'),
                      'status': row.get('status'), 'from': source})
    found.sort(key=lambda s: (str(s.get('created_at') or ''), s['id']))
    page = found[offset:offset + ROWS_MAX]
    following = offset + ROWS_MAX
    return {'items': page, 'total': len(found), 'next_offset': following if following < len(found) else None}


def trace_adopts(row, area, area_rows, program_rows, state, operators):
    """The trace's ``adopts`` of one decision issue of an area (Q6 at VERIFIED), or None when it adopts nothing.

    ``program_rows`` is None when the caller may not read the program (or it does not exist): ``not-visible``, with
    nothing else. Otherwise the same rules as the program view."""
    found = lines(row)
    if found['adopts'] is None:
        return None
    program, decision_id = found['adopts']
    pointer = 'decision:%s/%s' % (program, decision_id)
    if program_rows is None:
        return {'pointer': pointer, 'state': 'not-visible'}
    target = next((r for r in program_rows if isinstance(r, dict) and str(r.get('id')) == decision_id), None)
    decision = program_decision(target, program, state, operators) if target is not None else None
    if decision is None:
        return {'pointer': pointer, 'state': 'unknown-decision'}
    if decision['flags']:
        return {'pointer': pointer, 'state': 'decision-does-not-count', 'reason': '; '.join(decision['reasons'])}
    if area not in decision['binds']:
        return {'pointer': pointer, 'state': 'not-bound', 'reason': 'the decision does not bind %s' % area}
    verdict, reason = adoption(row, decision, program, area, area_rows, state, operators)
    return {'pointer': pointer, 'state': verdict, 'reason': reason}
