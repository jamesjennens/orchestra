"""Program links: an area joins a program through two reference entries (kittrial-5bb.446).

Slice 1 of docs/design/PROGRAM_DESIGN.md (sections 2 and 9.1). A program is an ordinary project. An area
project joins it through two reference entries with a ``decision`` authority, one on each side:

* ``program.area.<area>`` in the program project, whose statement is exactly ``Area: <area>`` and
  ``State: in`` (or ``State: left``);
* ``program.member-of`` in the area project, whose statement is exactly ``Program: <program>`` and the same
  ``State:`` line.

Joining or leaving is the project owner's decision (design section 2.2), so a side counts only when the live
acceptance of its newest accepted revision was written by a person owner of that project, by their own web
session, or by a listed operator. A native issue author is only an ``--actor`` label and is never read
(TRACEABILITY_DESIGN.md section 5, "a native author is only an --actor label").

A link grants nothing (design section 2.3): no role, no grant, no visibility. Nothing here reads state; the
callers pass what they read, and each project is still read only through its own per-project check.
"""
import re

PREFIX = 'program.'
MEMBER_OF = 'program.member-of'
AREA_KEY = re.compile(r'program\.area\.([a-z][a-z0-9]{1,23})\Z')
#: A project id that is a host project's canonical name (http_authority.CANONICAL_PROJECT_NAME).
PROJECT = r'[a-z][a-z0-9]{1,23}'
STATE_LINE = re.compile(r'State: (in|left)\Z')
#: A person's own web session writes as the bare account id (http_auth.Principal: ``actor`` is ``user['id']``).
PERSON = re.compile(r'usr_[0-9a-f]{16}\Z')
#: At most this many areas, and so at most this many area reads, on one page (design section 5).
PAGE_MAX = 50
STATES = ('linked', 'one-sided', 'left', 'proposed')

KEYS_SENTENCE = ('The program. keys are kept for program links: program.area.<area> in the program project '
                 '(<area> a host project name) and program.member-of in the area project')
AUTHORITY_SENTENCE = ("A program link needs a decision authority: the decision issue in which this project's owner "
                      'decided to join or admit (PROGRAM_DESIGN.md section 2.2)')
SELF_SENTENCE = 'A program link names another project: a project cannot link itself'
OWNER_SENTENCE = ("Only a person owner of this project accepts or retires a program link: joining or leaving a "
                  "program is the owner's decision (PROGRAM_DESIGN.md section 2.2). A coordinator or an agent "
                  'proposes it, and the owner accepts it')


def side_of(key):
    """``('program', area)`` for a program-side key, ``('area', None)`` for ``program.member-of``, else None."""
    if key == MEMBER_OF:
        return ('area', None)
    match = AREA_KEY.fullmatch(key) if isinstance(key, str) else None
    return ('program', match.group(1)) if match else None


def statement_lines(statement, label):
    """``(named project, 'in' or 'left')`` from a link statement, which is exactly its two lines: no second State
    line and nothing beyond them (review of kittrial-5bb.446, N3). Anything else is a ValueError."""
    lines = statement.split('\n') if isinstance(statement, str) else []
    named = re.fullmatch(r'%s: (%s)' % (label, PROJECT), lines[0]) if lines else None
    state = STATE_LINE.fullmatch(lines[1]) if len(lines) > 1 else None
    if not named or not state or len(lines) != 2:
        raise ValueError('A program link statement is exactly two lines: "%s: <project>" and "State: in" or '
                         '"State: left"' % label)
    return named.group(1), state.group(1)


def write_problem(payload, project_id=None):
    """The refusal sentence for a write of a ``program.`` key, or None (accept and retire carry no content).

    ``project_id`` is the project written to: a link naming it is refused (review of kittrial-5bb.446, N2); the read
    still refuses one that got in before this check (``side_status``, ``links-itself``)."""
    key = payload.get('key') if isinstance(payload, dict) else None
    if not isinstance(key, str) or not key.startswith(PREFIX) or payload.get('operation') in ('accept', 'retire'):
        return None
    side = side_of(key)
    if side is None:
        return KEYS_SENTENCE
    authority = payload.get('authority')
    if not isinstance(authority, dict) or authority.get('type') != 'decision':
        return AUTHORITY_SENTENCE
    try:
        named, _state = statement_lines(payload.get('statement'), 'Area' if side[0] == 'program' else 'Program')
    except ValueError as error:
        return str(error)
    if side[0] == 'program' and named != side[1]:
        return 'The Area line must name the area of the key, %s' % side[1]
    if project_id is not None and named == project_id:
        return SELF_SENTENCE
    return None


def owner_or_operator(accepted_by, project, memberships, users):
    """Whether a live acceptance was written by a person owner of ``project`` (live role, account enabled) or by
    a listed operator. ``accepted_by`` comes from the reader's live evidence (keyed_entries.evidence_is_live), so a
    name that is not shaped like an HTTP id is an operator on the allowlist; an agent or a worker credential never
    counts."""
    if not isinstance(accepted_by, str) or not accepted_by:
        return False
    if PERSON.fullmatch(accepted_by):
        user = (users or {}).get(accepted_by)
        return (isinstance(user, dict) and not user.get('disabled')
                and ((memberships or {}).get(project) or {}).get(accepted_by) == 'owner')
    from http_authority import http_actor_id
    return http_actor_id(accepted_by) is None


def side_status(item, project, other, memberships, users):
    """One side's standing: ``('in' | 'left' | None, reason)``. ``item`` is the side's ``ref links`` item in
    ``project``; ``other`` is the project the side must name (None: any, for an area naming its program)."""
    if not isinstance(item, dict):
        return None, 'no-record'
    record = item.get('record')
    if not isinstance(record, dict) or item.get('state') not in ('accepted', 'superseded'):
        return None, 'not-accepted'
    if not owner_or_operator(item.get('accepted_by'), project, memberships, users):
        return None, 'accepted-by-a-non-owner'
    if not isinstance(record.get('authority'), dict) or record['authority'].get('type') != 'decision':
        return None, 'no-decision-authority'
    side = side_of(item.get('key'))
    try:
        named, state = statement_lines(record.get('statement'), 'Area' if side and side[0] == 'program' else 'Program')
    except ValueError:
        return None, 'statement'
    if named == project:
        return None, 'links-itself'
    if other is not None and named != other:
        return None, 'names-another-project'
    if item.get('state') == 'superseded':
        return 'left', None
    return state, None


def link_state(own, other_counts):
    """The link's state from this side's standing and whether the other side counts (design section 2.2)."""
    if own == 'left':
        return 'left'
    if own != 'in':
        return 'proposed'
    return 'linked' if other_counts else 'one-sided'
