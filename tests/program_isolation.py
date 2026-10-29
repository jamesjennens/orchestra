"""The isolation harness of docs/design/PROGRAM_DESIGN.md sections 2.3 and 8 (kittrial-5bb.446, slice 1).

Being in a program grants nothing. For every read the program design adds, a caller who is a member of the
program project but not of an area gets the same answer about that area as a caller with no membership at all:
no rows, no counts, no names beyond the program's own link records. So does an agent whose live grant list omits
the area, and so does a host key that omits it.

Later slices add their reads to ``AREA_READS`` and their routes to ``PROGRAM_ROUTES``; a test checks that every
route whose pattern mentions "program" or "notes" is listed, so no program read can skip this harness.

kittrial-5bb.447 adds the notes between coordinators: their reads join ``AREA_READS`` and ``PERSONAL_READS``, and
``assert_text_hidden`` checks that a note's text reaches nobody but the coordinators of its two projects.
"""
import json

#: Reads of an area project, by name, as path templates. Each is asked of the area and of a project that does
#: not exist; an outsider must not be able to tell them apart.
AREA_READS = (
    ('project', '/v1/projects/{project}'),
    ('tasks', '/v1/projects/{project}/tasks'),
    ('references', '/v1/projects/{project}/references'),
    ('member-of', '/v1/projects/{project}/references/program.member-of'),
    ('program-links', '/v1/projects/{project}/program-links'),
    ('notes', '/v1/projects/{project}/notes'),
    ('notes-sent', '/v1/projects/{project}/notes?box=sent'),
)
#: Reads of the caller's own, across projects: they must show nothing of a project the caller does not coordinate.
PERSONAL_READS = (
    ('me-notes', '/v1/me/notes'),
    ('agent-next', '/v1/agents/me/next'),
    ('agent-wait', '/v1/agents/me/wait'),
    ('agent-wait-coordinator', '/v1/agents/me/wait?form=coordinator'),
)
#: Every route handler whose pattern mentions "program", with the read above that covers it.
PROGRAM_ROUTES = {'program_links_read': 'program-links', 'notes_read': 'notes', 'notes_write': 'notes',
                  'notes_noted': 'notes', 'me_notes': 'me-notes'}


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
                   if 'program' in compiled.pattern.lower() or 'notes' in compiled.pattern.lower()})


def covered_reads():
    return {name for name, _template in AREA_READS} | {name for name, _template in PERSONAL_READS}


def assert_text_hidden(test, request, text, callers, projects):
    """``text`` appears in no answer any of ``callers`` ({label: token}) gets from every area read of each of
    ``projects`` and every personal read: a note's text is for the coordinators of its two projects only."""
    for label, token in callers.items():
        reads = [template.format(project=project) for project in projects for _name, template in AREA_READS]
        reads += [template for _name, template in PERSONAL_READS]
        for path in reads:
            answer = request('GET', path, token=token)
            test.assertNotIn(text, json.dumps(answer.data), (label, path))
