"""Whether one viewer may accept one waiting record draft (kittrial-5bb.420 r2, item b1).

The accept route and the waiting list's ``viewer_may_accept`` field MUST agree:
the field is not a copy of the route's checks but a call of the SAME predicate,
so a row that offers 'Review & accept' is one the route would accept, and a row
the route would refuse offers 'View'. The predicate is PURE (plain inputs, plain
``(may_accept, reason)`` output): the endpoint passes live state it already read
under the write's lock; the web service passes what it read for the enrichment.
Neither process pickles a closure across the endpoint.py / http_service.py seam.

The rule the two sides share, in order (first refusal wins, same sentences):

1. only the project coordinator (an owner or coordinator role) may accept;
2. where approval by another party is required (the web service's
   ``approval_by_another_party`` setting, the live ``settings_seen`` flag, or
   the installation's ``independent_approval`` rule), a viewer may not accept
   a draft their own party proposed; an agent's delegation never lifts this;
3. NO waiting draft at all (nothing proposed, or a state that is not waiting)
   refuses with 'nothing waits to be accepted' (item b2), so an accepted row
   never offers 'Review & accept';
4. an agent caller needs a live, person-rooted delegation of record acceptance
   that covers the record's kind (``references`` or ``capabilities``; review
   item b1): a delegation narrowed to references never admits a capability
   draft, and no named decision is not the same as a decision that refuses;
5. an agent never accepts a draft it wrote itself, whatever a delegation says.

Authority and capability records are NOT imported here: callers pass resolved
plain values, so this module needs only the standard library and imports with
``fcntl`` unavailable (tests/test_no_fcntl_imports.py only pins http_service,
but the shared predicate must not add a POSIX-only import either).
"""

#: The planner-visible reason when the row carries no waiting draft.
NOTHING_WAITING_REASON = 'Nothing waits to be accepted.'


def _norm(names):
    """Caller/draft author names as a set of non-empty strings."""
    return {name for name in (names or ()) if isinstance(name, str) and name}


def decide(*, can_coordinate, approval_by_another_party, waiting,
           by_agent, agent_head=None, delegation_covers_kind=None,
           delegation_refused=False, delegation_unreadable=False,
           delegation_missing=False, proposer_known=True,
           caller_parties=(), draft_parties=(), own_draft=False,
           own_party=False, independent_hold=False):
    """One shared eligibility decision: ``(may_accept, reason)``.

    ``waiting`` says the row carries a waiting draft (proposed and waiting);
    anything else answers ``(False, 'Nothing waits to be accepted.')`` (item
    b2). ``caller_parties``/``draft_parties`` are the already-resolved party
    sets (accounts/agents of one person are one party) and ``own_party`` is
    whether they overlap (or the viewer wrote the draft themselves);
    ``independent_hold`` is the installation's ``independent_approval`` hold
    (session-owner sameness) where the session registry could be read.
    ``agent_head`` is the agent short name (before ``/``); ``own_draft``
    whether the agent wrote the draft itself.
    """
    if not can_coordinate:
        return False, 'Only the project coordinator may accept.'
    if not waiting:
        return False, NOTHING_WAITING_REASON
    if approval_by_another_party and (own_party or independent_hold):
        if by_agent:
            return (False, 'An agent holding a coordinator grant may not accept or verify a record '
                           'whose draft was proposed by its own party while this project requires '
                           'approval by another party; a delegation does not lift that.')
        return (False, 'A coordinator may not accept or verify a record whose draft was proposed '
                       'by the same party while approval_by_another_party is enabled.')
    if by_agent:
        if delegation_missing:
            return False, 'An agent accepts or verifies a record only within a delegation: name the delegation decision as decision_id.'
        if not isinstance(agent_head, str) or not agent_head:
            return False, 'Whether a delegation covers this agent could not be read.'
        if delegation_unreadable:
            return False, 'Whether a delegation covers this agent could not be read.'
        if delegation_refused or delegation_covers_kind is False:
            return False, 'An agent accepts only within a delegation in force.'
        if delegation_covers_kind is not True:
            return False, 'An agent accepts only within a delegation in force.'
        if not proposer_known:
            return False, 'This draft has no recorded proposer, so no delegation can admit it.'
        if own_draft:
            return False, 'An agent never accepts or verifies a draft it wrote itself, whatever its delegation says.'
        if not approval_by_another_party:
            return True, ''
        # With approval by another party on, the person-side rule below decides.
    elif not approval_by_another_party:
        return True, ''
    caller = _norm(caller_parties)
    draft = _norm(draft_parties)
    if (not draft) or bool(caller & draft):
        if by_agent:
            return (False, 'An agent holding a coordinator grant may not accept or verify a record '
                           'whose draft was proposed by its own party while this project requires '
                           'approval by another party; a delegation does not lift that.')
        return (False, 'A coordinator may not accept or verify a record whose draft was proposed '
                       'by the same party while approval_by_another_party is enabled.')
    return True, ''
