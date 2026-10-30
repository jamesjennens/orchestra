"""Decisions across levels and suggestions (kittrial-5bb.454; PROGRAM_DESIGN.md sections 6 and 9.5).

A program decision is a decision issue of the program project with a ``Binds:`` line (the areas it applies to) and
an ``Owner-only:`` line. It binds an area only when the area adopts it: a decision issue of the area with an
``Adopts: decision:<program>/<id>@<attestation id>`` line, pinning the text the area saw. An owner-only decision is adopted by a person owner of the area (or an
operator), or by an area coordinator whose adoption cites ``Per: decision:<area>/<id>``, a decision of the area
attested by a person owner (or an operator) that says ``Authorises: decision:<program>/<id>@<attestation id>`` for this
very decision. A pin that is not the program decision's attestation id now reads ``adoption-stale``.
Never an agent alone. Nothing in the program grants authority in an area.

A row's author is never its native ``created_by``: a row counts only with a live attestation of its exact text
(decision_attest.py). Authority is decided LIVE at every read from the web state's memberships, accounts, agents and
grants: an attester who has lost the role since stops counting. ``journals`` is {project: its directory}, what the
attestation journal is read from; a project without one has no honoured attestation. Nothing here reads state or a
clock; callers pass what they read.
"""
import re

from decision_attest import NOT_WORK_TYPES, OWNERS, is_decision, standing

AREA = r'[a-z][a-z0-9]{1,23}'
ISSUE = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}'
BINDS = re.compile(r'Binds: (%s(?:, %s)*)\Z' % (AREA, AREA))
OWNER_ONLY = re.compile(r'Owner-only: (yes|no)\Z')
PIN = r'([0-9a-f]{64})'
ADOPTS = re.compile(r'Adopts: decision:(%s)/(%s)@%s\Z' % (AREA, ISSUE, PIN))
PER = re.compile(r'Per: decision:(%s)/(%s)\Z' % (AREA, ISSUE))
AUTHORISES = re.compile(r'Authorises: decision:(%s)/(%s)@%s\Z' % (AREA, ISSUE, PIN))
LINES = (('Binds:', 'binds', BINDS), ('Owner-only:', 'owner_only', OWNER_ONLY), ('Adopts:', 'adopts', ADOPTS),
         ('Per:', 'per', PER), ('Authorises:', 'authorises', AUTHORISES))
FROM_LINE = re.compile(r'From: (\S{1,200})\Z')
SUGGESTION_LABEL = 'suggestion'
ROWS_MAX = 50
#: How far a supersession chain is followed for superseded-upstream.
CHAIN_MAX = 20


def lines(row):
    """The decision lines of a row's description: ``{'binds', 'owner_only', 'adopts', 'per', 'authorises',
    'problems'}``.

    Each is a whole line, anywhere in the description, at most once (Q1 at VERIFIED). A line that starts with one of
    the labels but does not read exactly, or a second line with the same label, is a problem: the row then binds and
    adopts nothing (``malformed-lines``)."""
    found = {'binds': None, 'owner_only': None, 'adopts': None, 'per': None, 'authorises': None, 'problems': []}
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
                found[name] = match.groups()
            break
    return found


def _journal(journals, project):
    return journals.get(project) if isinstance(journals, dict) else None


def program_decision(row, program, state, operators, journals=None):
    """One binding decision of the program, or None for a decision issue with no Binds: line.

    It counts only without flags: one that does not read as attested (``closed``, ``not-attested``,
    ``attestation-stale``, ``attester-not-authorised``), an owner-only decision not attested by a person owner of the
    program or an operator (``owner-only-not-by-owner``), or one whose lines do not read (``malformed-lines``) binds
    nothing. A row that counts has a coordinator's standing or better, so any other decision of that standing counts."""
    if not is_decision(row):
        return None
    found = lines(row)
    if found['binds'] is None and not any('Binds:' in problem for problem in found['problems']):
        return None
    flags, reasons = [], []
    attested = standing(row, program, state, operators, _journal(journals, program))
    if found['problems'] or found['owner_only'] is None or found['binds'] is None:
        flags.append('malformed-lines')
        reasons.extend(found['problems'] or ['a binding decision has one Binds: line and one Owner-only: line'])
    if attested['state'] != 'counts':
        flags.append(attested['state'])
        reasons.append(attested['reason'])
    elif not flags:
        if found['owner_only'] and attested['role'] not in OWNERS:
            flags.append('owner-only-not-by-owner')
            reasons.append('an owner-only decision counts only when a person owner of %s or an operator attested it'
                           % program)
    from trace_read import SUPERSEDES
    description = row.get('description') if isinstance(row.get('description'), str) else ''
    first = SUPERSEDES.fullmatch(description.split('\n')[0].rstrip('\r'))
    return {'id': str(row.get('id')), 'title': row.get('title'), 'status': row.get('status'),
            'owner_only': found['owner_only'], 'binds': list(found['binds'] or []),
            'supersedes': first.group(1) if first else None, 'attestation_id': attested['id'], 'attestation': attested['state'],
            'attested_as': attested['role'], 'flags': flags, 'reasons': reasons}


def adoption(row, decision, program, area, area_rows, state, operators, journals=None):
    """``(state, reason)`` of one adoption row in ``area`` of a counting program ``decision`` (section 6.2). A row
    that does not read as attested reads as that (closed, not-attested, attestation-stale, attester-not-authorised). One
    that pinned other text than the program decision's now reads adoption-stale, whoever attested it."""
    found = lines(row)
    if found['problems']:
        return 'adoption-invalid', 'the adoption\'s lines do not read: ' + '; '.join(found['problems'])
    attested = standing(row, area, state, operators, _journal(journals, area))
    if attested['state'] != 'counts':
        return attested['state'], attested['reason']
    if found['adopts'] is not None and found['adopts'][2] != decision['attestation_id']:
        return 'adoption-stale', ('the program decision was attested with other text since this adoption read it: the '
                                  'area adopts again to bind it')
    if not decision['owner_only'] or attested['role'] in OWNERS:
        return 'adopted', None
    if found['per'] is None:
        return 'adoption-invalid', ('an owner-only decision is adopted by a person owner of %s, or by a coordinator '
                                    'whose adoption cites the owner\'s decision with Per:' % area)
    if found['per'][0] != area:
        return 'adoption-invalid', 'Per: names a decision of another project, not of %s' % area
    cited = next((r for r in area_rows if isinstance(r, dict) and str(r.get('id')) == found['per'][1]), None)
    if cited is None or not is_decision(cited):
        return 'adoption-invalid', 'Per: names no decision of %s' % area
    choice = standing(cited, area, state, operators, _journal(journals, area))
    if choice['state'] != 'counts':
        return 'adoption-invalid', 'the Per: decision does not count: %s (%s)' % (choice['state'], choice['reason'])
    if choice['role'] not in OWNERS:
        return 'adoption-invalid', ('the Per: decision was not attested by a person owner of %s or an operator: an '
                                    'agent or a coordinator never makes an owner-only choice' % area)
    named = lines(cited)
    if named['problems']:
        return 'adoption-invalid', 'the Per: decision\'s lines do not read: ' + '; '.join(named['problems'])
    if named['authorises'] is None or named['authorises'][:2] != (program, decision['id']):
        return 'adoption-invalid', ('the Per: decision does not say Authorises: decision:%s/%s: the owner\'s decision '
                                    'must name the decision it authorises' % (program, decision['id']))
    if named['authorises'][2] != decision['attestation_id']:
        return 'adoption-stale', ('the owner authorised other text of this program decision: the owner authorises '
                                  'it again to bind it')
    return 'adopted', None


def area_adoptions(area_rows, program):
    """{decision id: [adoption rows]} of an area's decision issues that adopt a decision of ``program``."""
    found = {}
    for row in sorted((r for r in area_rows or [] if is_decision(r)), key=lambda r: str(r.get('id'))):
        adopts = lines(row)['adopts']
        if adopts is not None and adopts[0] == program:
            found.setdefault(adopts[1], []).append(row)
    return found


def area_state(decision, chain, program, area, area_rows, state, operators, journals=None):
    """``{'state', ...}`` of one bound, readable area: adopted (with the adoption's id); closed when the only
    adoptions are closed; adoption-invalid or what a row reads as when it does not count (with the reason);
    superseded-upstream (a counting adoption of a decision this one supersedes, and none of this one); or
    not-adopted."""
    adoptions = area_adoptions(area_rows, program)
    refused, closed = None, None
    for row in adoptions.get(decision['id'], []):
        verdict, reason = adoption(row, decision, program, area, area_rows, state, operators, journals)
        if verdict == 'adopted':
            return {'state': 'adopted', 'adoption': str(row.get('id'))}
        if verdict == 'closed':
            closed = closed or (str(row.get('id')), reason)
        elif refused is None:
            refused = (verdict, str(row.get('id')), reason)
    if refused is not None:
        return {'state': refused[0], 'adoption': refused[1], 'reason': refused[2]}
    if closed is not None:
        return {'state': 'closed', 'adoption': closed[0], 'reason': closed[1]}
    older = [old for old in chain
             if any(standing(row, area, state, operators, _journal(journals, area))['state'] == 'counts'
                    for row in adoptions.get(old, []))]
    if older:
        return {'state': 'superseded-upstream', 'adopts': older[0]}
    return {'state': 'not-adopted'}


def chains(decisions):
    """{decision id: [the ids it supersedes, nearest first]}, at most CHAIN_MAX deep, cycles cut. Only a row that counts
    supersedes anything: one with flags marks nothing, and the chain does not pass through it."""
    by_id = {d['id']: d for d in decisions if not d['flags']}
    out = {}
    for decision in decisions:
        if decision['flags']:
            out[decision['id']] = []
            continue
        seen, current = [], decision.get('supersedes')
        while current and current not in seen and current != decision['id'] and len(seen) < CHAIN_MAX:
            seen.append(current)
            current = (by_id.get(current) or {}).get('supersedes')
        out[decision['id']] = seen
    return out


def decisions_section(program_rows, program, states_of_areas, area_rows_by_area, state, operators, offset=0,
                      journals=None):
    """The program view's decisions section (section 5 table; 6.1, 6.2), 50 a page.

    ``states_of_areas`` is {area: 'in' or 'not-checked'} for the areas whose program side is in: any other area is
    ``not-in-program`` and is never read. 'not-checked' is an area off this page of areas or outside a bound key.
    ``area_rows_by_area`` holds the rows of the readable areas of this read only; an area that is in but not among
    them is ``not-visible`` (not readable, or no such project: the same entry, no id, no reason)."""
    decisions = [d for d in (program_decision(row, program, state, operators, journals) for row in program_rows or [])
                 if d]
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
                                                      area_rows_by_area[area], state, operators, journals)
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


def trace_adopts(row, area, area_rows, program_rows, state, operators, journals=None):
    """The trace's ``adopts`` of one decision issue of an area (Q6 at VERIFIED), or None when it adopts nothing.

    ``program_rows`` is None when the caller may not read the program (or it does not exist): ``not-visible``, with
    nothing else. Otherwise the same rules as the program view."""
    found = lines(row)
    if found['adopts'] is None:
        return None
    program, decision_id = found['adopts'][:2]
    pointer = 'decision:%s/%s' % (program, decision_id)
    if program_rows is None:
        return {'pointer': pointer, 'state': 'not-visible'}
    target = next((r for r in program_rows if isinstance(r, dict) and str(r.get('id')) == decision_id), None)
    decision = program_decision(target, program, state, operators, journals) if target is not None else None
    if decision is None:
        return {'pointer': pointer, 'state': 'unknown-decision'}
    if decision['flags']:
        return {'pointer': pointer, 'state': 'decision-does-not-count', 'reason': '; '.join(decision['reasons'])}
    if area not in decision['binds']:
        return {'pointer': pointer, 'state': 'not-bound', 'reason': 'the decision does not bind %s' % area}
    verdict, reason = adoption(row, decision, program, area, area_rows, state, operators, journals)
    return {'pointer': pointer, 'state': verdict, 'reason': reason}
