"""The program view: the program manager's dashboard (kittrial-5bb.453; PROGRAM_DESIGN.md sections 5 and 9.4).

One read of a program project: its areas, the interface register (slice 3), program requirements' coverage, and
the decisions and suggestions of slice 5. The endpoint action ``program`` gathers what may be read and this module
builds the answer, so the web route (``GET /v1/projects/{program}/program``) and the client's ``trace --program``
cannot disagree.

Permission comes before counts (section 5): an area the caller may not read, and an area that does not exist,
give the same row, its link state and "not visible to you", with no number. Only an area in the caller's readable
set is read beyond the one fact the program-links read already shows every reader of the program (kittrial-5bb.446:
whether the area's own side counts). A link grants nothing (section 2.3). Nothing here reads state or a clock.
"""
import argparse

#: At most this many areas a read, as My work (section 5), and rows a page.
AREAS_MAX = 50
ROWS_MAX = 50
#: Open tasks (Q4 at VERIFIED): native issues open, in progress or blocked; not an epic, and not a row the queue
#: never lists as work (an event or gate row, the merge slot, a record anchor), nor a requirement record.
OPEN_STATUSES = ('open', 'in_progress', 'blocked')
NOT_WORK_TYPES = ('epic', 'event', 'gate')

NOT_VISIBLE = 'not visible to you'
INTERFACES_NOT_AVAILABLE = 'The interface register is not read by this build.'
INTERFACES_SCOPE = ('Providers and consumers are read from the readable areas of this read only. When any area is not '
                    'visible to you, or not on a page read, the flags that need every area are not shown.')
INTERFACES_UNSETTLED = ('Rights changed while this page was read, so the interfaces are not shown: '
                        'read the page again.')
DECISIONS_SCOPE = ('Adoptions are read from the readable areas of this read only: an area that is not visible to '
                   'you reads not-visible, and one not on this page of areas reads not-checked.')
COORDINATORS_ON_THE_WEB = 'Coordinators are shown on the web Program page.'
REQUIREMENTS_UNSETTLED = ('Rights changed while this page was read, so the program requirements are not shown: '
                          'read the page again.')
REQUIREMENTS_SCOPE = ('Program requirements are checked against the readable areas of this read only: an area '
                      'that is not visible to you, or not on this page, is not counted.')


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('Invalid program read: %s' % message)


def parse_args(args):
    """``--offset/--limit`` over the program's areas (at most 50), ``--idle-minutes`` (the lanes read's own argument,
    passed through) and ``--requirements-offset``, a page of 50 requirement rows."""
    import lanes
    parser = _Parser(prog='program', add_help=False)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int, default=AREAS_MAX)
    parser.add_argument('--idle-minutes', type=int, default=lanes.DEFAULT_IDLE_MINUTES, dest='idle_minutes')
    parser.add_argument('--requirements-offset', type=int, default=0, dest='requirements_offset')
    parser.add_argument('--interfaces-offset', type=int, default=0, dest='interfaces_offset')
    parser.add_argument('--decisions-offset', type=int, default=0, dest='decisions_offset')
    parser.add_argument('--suggestions-offset', type=int, default=0, dest='suggestions_offset')
    parser.add_argument('--json', action='store_true')       # the answer is always JSON
    parsed = parser.parse_args(list(args))
    if not 1 <= parsed.limit <= AREAS_MAX or min(parsed.offset, parsed.requirements_offset, parsed.decisions_offset,
                                                 parsed.suggestions_offset, parsed.interfaces_offset) < 0:
        raise ValueError('Invalid program read: --limit must be 1..%d, and the offsets must be >= 0' % AREAS_MAX)
    if not lanes.IDLE_MINUTES_MIN <= parsed.idle_minutes <= lanes.IDLE_MINUTES_MAX:
        raise ValueError('Invalid program read: --idle-minutes must be %d..%d'
                         % (lanes.IDLE_MINUTES_MIN, lanes.IDLE_MINUTES_MAX))
    return {'offset': parsed.offset, 'limit': parsed.limit, 'idle_minutes': parsed.idle_minutes,
            'requirements_offset': parsed.requirements_offset, 'decisions_offset': parsed.decisions_offset,
            'suggestions_offset': parsed.suggestions_offset,
            'interfaces_offset': parsed.interfaces_offset}


def area_keys(items):
    """The program's ``program.area.<area>`` entries from its ``ref links`` items, by area name."""
    import program_links
    found = {}
    for item in items or []:
        side = program_links.side_of(item.get('key')) if isinstance(item, dict) else None
        if side is not None and side[0] == 'program':
            found[side[1]] = item
    return [(area, found[area]) for area in sorted(found)]


def program_side(program, area, own_item, memberships, users):
    """The program's own side of an area's link: 'in', 'left' or None (proposed, not accepted by an owner, ...).
    Decided from the program's records alone, before anything of the area is read (review of .453, B1)."""
    import program_links
    return program_links.side_status(own_item, program, area, memberships, users)[0]


def link_state(program, area, own_item, member_item, memberships, users):
    """The link state the program-links read gives this area (kittrial-5bb.446): the program's side, then the one
    fact about the area's side, read for every reader alike, and only when the program's side is in."""
    import program_links
    status = program_side(program, area, own_item, memberships, users)
    other = (program_links.side_status(member_item, area, program, memberships, users)[0] == 'in'
             if status == 'in' else False)
    return program_links.link_state(status, other)


def unsettled():
    """The requirements section when rights kept changing while the page was read: no row at all."""
    return {'items': [], 'total': 0, 'next_offset': None, 'scope': REQUIREMENTS_SCOPE,
            'state': 'unsettled', 'note': REQUIREMENTS_UNSETTLED}


def accepted_area(items, program, memberships, users):
    """Whether the program has at least one accepted, owner-accepted ``program.area.*`` side in force: the cached
    flag that shows the Program link in the project's menu (Q1 at VERIFIED)."""
    import program_links
    return any(program_links.side_status(item, program, area, memberships, users)[0] == 'in'
               for area, item in area_keys(items))


def open_task(row):
    """Q4: a native issue that is open, in progress or blocked, and is work."""
    from coordination import is_merge_slot
    from reserved_comments import is_record_anchor
    from work import requirement_record
    if not isinstance(row, dict) or row.get('status') not in OPEN_STATUSES:
        return False
    if row.get('issue_type') in NOT_WORK_TYPES or is_merge_slot(row) or is_record_anchor(row):
        return False
    return not requirement_record(row.get('labels'))


def area_details(rows, lanes_flags):
    """A readable area's numbers: its open tasks and its lanes waiting long (lanes.read's own flags)."""
    flags = lanes_flags or {}
    return {'open_tasks': sum(1 for row in rows or [] if open_task(row)), 'rows_read': len(rows or []),
            'lanes': {'waiting_too_long': int(flags.get('waiting_too_long') or 0),
                      'expect_by_passed': int(flags.get('expect_by_passed') or 0)}}


def area_row(area, state, details=None):
    """One row of the areas section. Without details (not readable, or no such project) the row is the area's
    name, its link state and the one sentence: no number, no coordinator."""
    if details is None:
        return {'area': area, 'state': state, 'visible': False, 'note': NOT_VISIBLE}
    return {'area': area, 'state': state, 'visible': True, 'open_tasks': details['open_tasks'],
            'lanes': dict(details['lanes']), 'rows_read': details['rows_read'], 'coordinators': None}


def citations_to(snap, program, area):
    """What an area cites of the program: ``({key: [where]}, [design pins])``. Task citations and design-section
    tokens qualified to the program (``req:<program>/KEY``), from the area's own trace snapshot."""
    from briefing import pointer_target
    cited = {}
    pins = []

    def note(pointer, where):
        project, local = pointer_target(pointer, snap.project)
        if pointer['kind'] != 'req' or project != program:
            return None
        key = local.split('@', 1)[0]
        cited.setdefault(key, []).append(where)
        return local
    for task, pointers in sorted(snap.citations().items()):
        for _where, pointer in pointers:
            note(pointer, {'area': area, 'task': task})
    for design in snap.designs():
        index = design.get('index')
        if not index:
            continue
        for heading in index['headings']:
            for pointer in snap.section_tokens(design, heading):
                local = note(pointer, {'area': area, 'design': design['scope'], 'heading': heading['anchor']})
                if local is not None and '@' in local:
                    pins.append({'area': area, 'design': design['scope'], 'heading': heading['anchor'],
                                 'token': pointer['pointer'], 'local': local})
    return cited, pins


def requirements_section(program_pointers, cited, pins, offset):
    """Program requirements no readable area of this read cites (``no-area-cites``), and area design sections that
    pin a superseded program revision (``cites-superseded-revision``). Pages of 50."""
    from briefing import parse_context_pointers
    rows = []
    for key in program_pointers.requirement_keys():
        entry = program_pointers.requirement(key)
        if entry['state'] != 'live' or entry.get('flags'):
            continue            # each accepted requirement, current and in force (trace_read.requirements_view)
        if not cited.get(key):
            rows.append({'key': key, 'title': entry.get('label'), 'reference': entry.get('reference'),
                         'flags': ['no-area-cites']})
    for pin in sorted(pins, key=lambda p: (p['local'], p['area'], p['design'], p['heading'])):
        found = parse_context_pointers('req:' + pin['local'])
        if len(found) != 1:
            continue
        resolved = program_pointers.resolve(found[0])
        if 'superseded' in (resolved.get('flags') or []):
            rows.append({'key': pin['local'].split('@', 1)[0], 'token': pin['token'], 'area': pin['area'],
                         'design': pin['design'], 'heading': pin['heading'], 'flags': ['cites-superseded-revision']})
    page = rows[offset:offset + ROWS_MAX]
    following = offset + ROWS_MAX
    return {'items': page, 'total': len(rows), 'next_offset': following if following < len(rows) else None,
            'scope': REQUIREMENTS_SCOPE}


def coverage(areas, total_areas, offset, requirements):
    """What was read: the readable areas of this page, the rows read (over the visible rows only, so a row hidden
    after the read adds nothing), and what a bound cut."""
    rows_read = sum(row.get('rows_read') or 0 for row in areas if row.get('visible'))
    linked = [row for row in areas if row['state'] == 'linked']
    readable_linked = sum(1 for row in linked if row['visible'])
    shown = offset + len(areas)
    cut = {'areas': max(0, total_areas - shown) if shown < total_areas else 0,
           'requirements': max(0, requirements['total'] - len(requirements['items']))}
    text = '%d of %d linked areas readable; %s rows read' % (readable_linked, len(linked), format(rows_read, ','))
    if total_areas > len(areas):
        text += '; areas %d-%d of %d on this page' % (offset + 1 if areas else offset, shown, total_areas)
    return {'text': text, 'linked': len(linked), 'linked_readable': readable_linked,
            'areas_on_page': len(areas), 'areas_readable': sum(1 for row in areas if row['visible']),
            'areas_total': total_areas, 'rows_read': rows_read, 'cut': cut}


def interfaces_unsettled():
    """The interfaces section when rights kept changing while the page was read: no row at all."""
    return {'state': 'unsettled', 'items': [], 'total': 0, 'next_offset': None, 'complete': False,
            'scope': INTERFACES_SCOPE, 'note': INTERFACES_UNSETTLED}


def interfaces_section(views, usage, complete, flags, offset):
    """The register as the program view shows it (kittrial-5bb.467): the entries of ``interface list`` (a draft too,
    for a reader of the program, as there), by key, pages of 50. Providers and consumers come from ``usage``, which
    holds the readable areas only, so a row never names an area the caller may not read. There is no count of the
    areas left out: with one other area a count would name it. ``flags`` is interface_records.flags over the usage
    and ``complete``, which withholds the flags that need every area when any was not read."""
    rows = []
    for view in sorted(views, key=lambda v: v.get('key') or ''):
        record = view.get('record') or view.get('proposed')
        if not isinstance(record, dict):
            continue
        key = view.get('key')
        found = [u for u in usage if u.get('key') == key]
        rows.append({'key': key, 'name': record.get('name'), 'lifecycle': view.get('lifecycle'),
                     'revision': record.get('revision'), 'replacement': view.get('replacement'),
                     'flags': flags(view['record']) if view.get('record') else ['compatibility-unverified'],
                     'providers': sorted(({'area': u['project'], 'minimum': u['minimum']}
                                          for u in found if u.get('role') == 'provides'),
                                         key=lambda u: (u['area'], u['minimum'])),
                     'consumers': sorted(({'area': u['project'], 'minimum': u['minimum']}
                                          for u in found if u.get('role') == 'consumes'),
                                         key=lambda u: (u['area'], u['minimum']))})
    page = rows[offset:offset + ROWS_MAX]
    following = offset + ROWS_MAX
    return {'state': 'ok', 'items': page, 'total': len(rows), 'next_offset': following if following < len(rows) else None,
            'complete': bool(complete), 'scope': INTERFACES_SCOPE}


def empty_section():
    return {'items': [], 'total': 0, 'next_offset': None}


def answer(program, areas, total_areas, offset, limit, requirements, decisions=None, suggestions=None,
           interfaces=None):
    """The whole answer, from what the endpoint read (no I/O)."""
    following = offset + limit
    return {'schema': 'program-view-v1', 'program': program,
            'areas': {'items': areas, 'total': total_areas,
                      'next_offset': following if following < total_areas else None},
            'interfaces': interfaces or {'state': 'not-available', 'note': INTERFACES_NOT_AVAILABLE},
            'requirements': requirements,
            'decisions': dict(decisions or empty_section(), scope=DECISIONS_SCOPE),
            'suggestions': dict(suggestions or empty_section()),
            'coverage': coverage(areas, total_areas, offset, requirements)}
