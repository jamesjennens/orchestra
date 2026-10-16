"""A per-agent limit on claimed, undelivered tasks (kittrial-5bb.276).

The owner's office incident (2026-10-14, release file 13): a new agent with no
access to the repository claimed about fifteen open tasks in a few minutes. The
bridge extension refuses a claim while an agent already holds an undelivered
task, but that is one client; any agent using curl or another client was
unlimited. This module is the service's own gate, so every claim route the
service serves applies the same limit to the same held set.

What one counted task is (the task states it in its own words): a claimed task
whose status is ``open`` or ``in_progress`` and whose review state is ``none``
or ``changes-requested``. Delivered work does not count: once a contribution is
recorded the row is ``awaiting-review`` (or beyond), and the reviewer or the
coordinator moves it, not the agent. A task whose latest checkpoint left a
blocker or a dependency is not the holder's move either, so it does not count
(the owner's decision, 2026-10-16): see :func:`blocked`.

Persons are not limited unless the owner sets a limit on their own record. The
limit lives on the agent record as ``claim_limit``; the default for an agent is
:data:`DEFAULT_LIMIT`, and an absent or malformed stored value reads as that
default rather than as "no limit".

Nothing here reads the tracker: the held set is supplied by the caller, counted
from the same bounded ``work`` snapshot the service already reads for
:meth:`GET /v1/agents/me/next`, so a claim costs no extra tracker read.
"""

#: What an agent holds when its owner has not set a limit: one undelivered claim.
DEFAULT_LIMIT = 1

#: The bounds of the owner-editable setting. A limit below 1 would mean "an agent
#: that can never claim", which is what disabling the agent is for; the ceiling
#: keeps a typo from reading as "unlimited".
LIMIT_MIN = 1
LIMIT_MAX = 50

#: How many held tasks the refusal sentence names before it says how many more.
NAMED_MAX = 5

#: The review states that still leave the work in the agent's hands. Everything
#: else (``awaiting-review``, ``awaiting-integration``, ``integrated``,
#: ``approved`` and the terminal states) has moved to another party's move.
ACTIVE_REVIEW_STATES = (None, 'none', 'changes-requested')

#: The task statuses that still count against the limit, with the active review
#: states above. ``closed`` is never held work, whatever its review state says.
ACTIVE_STATUSES = ('open', 'in_progress')

#: Who may RAISE a limit above the value its agent set for itself (kittrial-5bb.302):
#: the owner's account or a superuser, on the My agents page. Named here so the refusal
#: sentence and the read views say the same thing, and an agent can tell its owner where
#: the setting is raised.
RAISE_BY = "The owner's account or a superuser, on the My agents page"


def raise_refusal(current, requested):
    """The sentence a refused raise carries (kittrial-5bb.302).

    An agent may only LOWER its own limit, so a value above the current one is refused
    with who may raise it and where, not only that the value is too high.
    """
    return ('An agent may only lower its own claim limit: %d is above the current %d. '
            '%s raises it.' % (requested, current, RAISE_BY))


def default_limit(agent=None):
    """The effective limit for one agent record, as a nonnegative integer.

    An absent record, an absent value or a malformed one reads as
    :data:`DEFAULT_LIMIT`; a value outside the editable bounds is clamped rather
    than trusted, so a hand-edited state file cannot lift the gate.
    """
    if not isinstance(agent, dict):
        return DEFAULT_LIMIT
    value = agent.get('claim_limit')
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_LIMIT
    if value < LIMIT_MIN:
        return LIMIT_MIN
    if value > LIMIT_MAX:
        return LIMIT_MAX
    return value


def validate_limit(value):
    """The stored form of one owner-supplied limit, or ``ValueError``.

    A ``None`` clears the setting back to the default; a whole number inside the
    bounds is kept as sent. Anything else names what was wrong with it, in the
    repository's one wording for a field that breaks its limit.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('claim_limit: expected a whole number from %d to %d'
                         % (LIMIT_MIN, LIMIT_MAX))
    if not LIMIT_MIN <= value <= LIMIT_MAX:
        raise ValueError('claim_limit: %d is outside %d..%d' % (value, LIMIT_MIN, LIMIT_MAX))
    return value


def counted(task):
    """Whether one held row still counts against its holder's limit.

    A row the reader could not characterize (no status or no review state) is
    never counted as delivered: an unreadable held set must not silently lift the
    gate, so it is left to the caller's completeness check instead.
    """
    if not isinstance(task, dict):
        return False
    status = task.get('status')
    if status is not None and status not in ACTIVE_STATUSES:
        return False
    state = task.get('review_state')
    if state is not None and state not in ACTIVE_REVIEW_STATES:
        return False
    return True


def held_undelivered(tasks):
    """The subset of ``tasks`` that still counts against the limit, in order."""
    return [task for task in (tasks or []) if counted(task)]


def blocked(task):
    """Whether the task's latest checkpoint left a blocker or a dependency.

    The owner's decision (2026-10-16, the kittrial-5bb.276 revision): a BLOCKED
    task does not count against the limit. The documented order tells a lane whose
    latest checkpoint left a blocker to claim other work instead of waiting, and
    counting that task as held would make that order impossible to follow.

    An unknown blocking count (``None``, or a row that did not carry the field) is
    NOT read as blocked: the caller's own completeness check refuses an unreadable
    held set, so reading unknown as blocked here can never be the way a claim slips
    through.
    """
    if not isinstance(task, dict):
        return False
    value = task.get('blocking_items')
    return type(value) is int and value > 0


def held_against_limit(tasks):
    """The rows that count against a limit: undelivered, and not blocked."""
    return [task for task in held_undelivered(tasks) if not blocked(task)]


def names(tasks, limit=NAMED_MAX):
    """The held tasks as ``id`` strings, bounded, for a refusal sentence."""
    return [str(task.get('id')) for task in (tasks or [])[:limit] if task.get('id')]


def refusal(limit, held, more=0):
    """The sentence a refused claim carries: the limit, then what holds it.

    It names the held tasks so the caller need not read the queue to find out
    what is in the way, and says how many more there are when the list was cut.
    """
    shown = names(held)
    if shown:
        what = 'it holds %s' % ', '.join(shown)
        if more:
            what += ' and %d more' % more
    else:
        what = 'it holds %d undelivered task(s)' % (len(held) + more)
    return ('This agent already holds %d claimed undelivered task(s) and its limit is %d: '
            '%s. Deliver or release one before claiming another.' % (len(held) + more, limit, what))


def check(limit, held, more=0):
    """Refuse a NEW claim while the limit is already reached.

    ``held`` is the agent's held and undelivered set in the project; ``more``
    counts held rows that could not be listed. A limit already at its bound
    refuses; a limit of zero refuses everything. Returns the refusal sentence, or
    ``None`` when the claim may proceed.
    """
    if limit is None:
        return None
    if limit < 1 or len(held) + max(0, more) >= limit:
        return refusal(limit, held, more)
    return None
