"""The isolation harness of docs/design/PROGRAM_DESIGN.md sections 2.3 and 8 (kittrial-5bb.446, slice 1).

Being in a program grants nothing. For every read the program design adds, a caller who is a member of the
program project but not of an area gets the same answer about that area as a caller with no membership at all:
no rows, no counts, no names beyond the program's own link records. So does an agent whose live grant list omits
the area, and so does a host key that omits it.

Later slices add their reads to ``AREA_READS`` and their routes to ``PROGRAM_ROUTES``; a test checks that every
route whose pattern mentions "program" or "notes" is listed, so no program read can skip this harness.

kittrial-5bb.447 adds the notes between coordinators: their reads join ``AREA_READS`` and ``PERSONAL_READS``, and
``assert_text_hidden`` checks that a note's text reaches nobody but the coordinators of its two projects.

kittrial-5bb.453 adds the program view: as a read of any project it joins ``AREA_READS`` (an outsider's view of an
area is a missing project's 404), and as the program's own read ``assert_area_rows_hidden`` checks that a reader of
the program who may not read an area gets that area's row exactly as a missing area's: the name, the link state and
"not visible to you", no number and no coordinator.
"""
import json

#: Reads of an area project, by name, as path templates. Each is asked of the area and of a project that does
#: not exist; an outsider must not be able to tell them apart.
AREA_READS = (
    ('project', '/v1/projects/{project}'),
    ('tasks', '/v1/projects/{project}/tasks'),
    ('references', '/v1/projects/{project}/references'),
    ('interfaces', '/v1/projects/{project}/interfaces'),
    ('interface-detail', '/v1/projects/{project}/interfaces/iface.synthetic.v1'),
    ('member-of', '/v1/projects/{project}/references/program.member-of'),
    ('program-links', '/v1/projects/{project}/program-links'),
    ('notes', '/v1/projects/{project}/notes'),
    ('notes-sent', '/v1/projects/{project}/notes?box=sent'),
    ('trace', '/v1/projects/{project}/trace'),          # kittrial-5bb.449: tokens into other projects
    ('program', '/v1/projects/{project}/program'),      # kittrial-5bb.453: the program view of a project
)
#: Reads of the caller's own, across projects: they must show nothing of a project the caller does not coordinate.
PERSONAL_READS = (
    ('me-notes', '/v1/me/notes'),
    ('agent-next', '/v1/agents/me/next'),
    ('agent-wait', '/v1/agents/me/wait'),
    ('agent-wait-coordinator', '/v1/agents/me/wait?form=coordinator'),
    ('agent-wait-coordinator-comments', '/v1/agents/me/wait?form=coordinator&comments=others'),   # kittrial-5bb.457
)
#: Every route handler whose pattern mentions "program", with the read above that covers it.
PROGRAM_ROUTES = {'interfaces_list':'interfaces','interfaces_get':'interface-detail',
                  'interfaces_propose':'interfaces','interfaces_revise':'interfaces','interfaces_accept':'interfaces',
                  'program_links_read': 'program-links', 'notes_read': 'notes', 'notes_write': 'notes',
                  'notes_noted': 'notes', 'me_notes': 'me-notes', 'program_view_read': 'program'}


def without_request_id(body):
    """An answer body with only the request id left out: the one field that differs between any two answers."""
    if isinstance(body, dict):
        body = {key: value for key, value in body.items() if key != 'request_id'}
        if isinstance(body.get('error'), dict):
            body['error'] = {key: value for key, value in body['error'].items() if key != 'request_id'}
    return body


def answers(request, token, project):
    """``{read name: (status, body without its request id)}`` for one caller and one project."""
    out = {}
    for name, template in AREA_READS:
        answer = request('GET', template.format(project=project), token=token)
        out[name] = (answer.status, json.dumps(without_request_id(answer.data), sort_keys=True))
    return out


def assert_outsiders_see_nothing(test, request, area, missing, callers):
    """For each ``{label: token}``, every area read answers exactly as for the project ``missing``, which does not
    exist, and that answer is a 404 with no rows and no counts."""
    for label, token in callers.items():
        seen, absent = answers(request, token, area), answers(request, token, missing)
        test.assertEqual(seen, absent, label)
        for name, (status, body) in seen.items():
            test.assertEqual(404, status, (label, name, body))
            parsed = json.loads(body)
            test.assertEqual({'error'}, set(parsed), (label, name))
            test.assertNotIn(area, body, (label, name))


def program_routes(routes):
    """The handler names of the routes whose pattern mentions "program" or "notes", from ``http_service.ROUTES``."""
    return sorted({name for _method, compiled, name, *_rest in routes
                   if 'program' in compiled.pattern.lower() or 'notes' in compiled.pattern.lower() or 'interfaces' in compiled.pattern.lower()})


def covered_reads():
    return {name for name, _template in AREA_READS} | {name for name, _template in PERSONAL_READS}


def area_rows(request, token, program):
    """``{area: row}`` of the program view's areas section, for one caller (every page)."""
    rows, path = {}, '/v1/projects/%s/program' % program
    while path:
        answer = request('GET', path, token=token)
        if answer.status != 200:
            raise AssertionError('the program view answered %s: %s' % (answer.status, answer.data))
        for row in answer.data['areas']['items']:
            rows[row['area']] = row
        cursor = answer.data['areas'].get('next_cursor')
        path = '/v1/projects/%s/program?cursor=%s' % (program, cursor) if cursor else None
    return rows


def assert_area_rows_hidden(test, request, program, area, missing, callers):
    """For each ``{label: token}`` that may read ``program``: the rows of ``area`` (which it may not read) and of
    ``missing`` (no such project, but entered in the program) are the same hidden row apart from the name and the
    link state: "not visible to you", and no number, no coordinator, no lane."""
    import program_view
    for label, token in callers.items():
        rows = area_rows(request, token, program)
        seen, absent = rows[area], rows[missing]
        test.assertEqual(program_view.area_row(area, seen['state']), seen, label)
        test.assertEqual(program_view.area_row(missing, absent['state']), absent, label)
        test.assertEqual({key for key in seen if key not in ('area', 'state')},
                         {key for key in absent if key not in ('area', 'state')}, label)


def assert_adoptions_hidden(test, request, program, area, missing, callers):
    """kittrial-5bb.454: in every decision of the program view, the entry of ``area`` (which the caller may not read)
    equals the entry of ``missing`` (no such project, but bound): ``not-visible`` and nothing else, no adoption id and
    no reason."""
    for label, token in callers.items():
        answer = request('GET', '/v1/projects/%s/program' % program, token=token)
        test.assertEqual(200, answer.status, (label, answer.data))
        for decision in answer.data['decisions']['items']:
            areas = decision.get('areas') or {}
            if area in areas or missing in areas:
                test.assertEqual({'state': 'not-visible'}, areas.get(area), (label, decision['id']))
                test.assertEqual(areas.get(area), areas.get(missing), (label, decision['id']))


def assert_text_hidden(test, request, text, callers, projects):
    """``text`` appears in no answer any of ``callers`` ({label: token}) gets from every area read of each of
    ``projects`` and every personal read: a note's text is for the coordinators of its two projects only."""
    for label, token in callers.items():
        reads = [template.format(project=project) for project in projects for _name, template in AREA_READS]
        reads += [template for _name, template in PERSONAL_READS]
        for path in reads:
            answer = request('GET', path, token=token)
            test.assertNotIn(text, json.dumps(answer.data), (label, path))
