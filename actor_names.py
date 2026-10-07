"""Names a worker credential may not write under (kittrial-5bb.184, kittrial-5bb.188).

A worker credential writes under an actor namespace its issuer chose: ``NAME`` or
``NAME/...``. The tracker's rows carry that name and nothing else, and the rules of the
review workflow ask "is the caller the assignee, is it the author". So a credential named
as somebody who already is somebody on the host IS that somebody: a project owner issued
one named as the host coordinator's session and with it claimed, contributed, answered
the changes requested of the coordinator on the coordinator's own task and wrote its
checkpoint.

One rule, used where a credential is issued (the web service) and where it is used (the
endpoint, which a caller cannot go around): the namespace's *head*, the part before the
first ``/``, compared without regard to case, must not be the head of

* a name the project's tracker already holds as an author or assignee (kittrial-5bb.188
  item 1): a plain host name is somebody on this host even when it is neither a
  registered session nor on a list, and a name that already writes rows may not be
  taken by a credential. The caller bounds this set to the rows older than the
  credential's own issuance, so a credential's own rows are not held against it;
* a session actor registered in the project;
* any name with the shape of a session actor (``session-<uuid>``), registered or not:
  the server makes those, in every project;
* a name on the operator list or on the verifier list of the installation;
* the web service's own namespace.

A head that reads as another name -- one that begins or ends with ``.`` or ``-``, or
carries an ``@`` -- is refused too (kittrial-5bb.188 item 2): the review workflow reads
``im2-coordinator.``, ``im2-coordinator-`` and ``im2-coordinator@desk`` as authors other
than ``im2-coordinator``, so the rule refuses them rather than choosing a normal form and
silently renaming somebody.

The head and the case are what the review workflow's own comparison uses
(``review_workflow.author_key``): to it ``Team``, ``team/alice`` and ``team/bob`` are one
author. A name with the shape of a web account or agent id is not this module's business:
``http_authority.http_actor_denial`` and ``Service.issue_credential`` hold those.

Pure and without imports of the kit, so the web service uses it on every platform.
"""
import datetime
import re

#: The shape of the actors ``sessions.py`` makes.
SESSION_ACTOR = re.compile(r'session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
#: The web service's own namespace, when it was started without another (``--actor-namespace``).
SERVICE_NAMESPACE = 'http'
#: The shape of a head that reads as itself: it does not begin or end with ``.`` or ``-``,
#: and carries no ``@`` (so no ``name@host``). ``verity.b`` and ``ops-lead2`` pass.
HEAD_SHAPE = re.compile(r'[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?\Z')
#: How much earlier than the credential a row may be stamped and still count as its own: the
#: credential's issuance stamp is the web service's clock and a row's is the host's, and a
#: small difference between them must not refuse an honest credential's own first write.
OWN_ROWS_SKEW_SECONDS = 300

SESSION = 'a session actor of this project'
SESSION_SHAPED = 'the shape of a session actor'
OPERATOR = 'a name on the operator list'
VERIFIER = 'a name on the verifier list'
SERVICE = "the web service's own namespace"
ROWS = "a name this project's tracker already holds"
LOOKALIKE = 'a name that reads as another'
#: What to do: about a credential that has such a name, and when one is asked for.
REVOKE = 'Revoke it and issue one under another name.'
CHOOSE = 'Choose another name.'

#: ``YYYY-MM-DDTHH:MM:SS[.ffffff][Z|+HH:MM]``, the stamps bd writes and the kit writes.
_WHEN = re.compile(r'(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6})\d*)?'
                   r'(Z|z|[+-]\d{2}:?\d{2})?\Z')


def head(name):
    """The part of an actor label that says who it is: before the first ``/``, case folded."""
    return name.split('/', 1)[0].casefold() if isinstance(name, str) else ''


def collision(namespace, sessions=(), operators=(), verifiers=(), service=SERVICE_NAMESPACE,
              authors=()):
    """Why a worker credential may not write under ``namespace``, or None.

    ``sessions``, ``operators`` and ``verifiers`` are the names the host has; ``service`` is
    the web service's namespace, or a collection of them; ``authors`` are the heads the
    project's tracker already holds as an author or assignee (bounded by the caller to the
    rows older than the credential, ``tracker_names``). A namespace that is not a string,
    or is empty, belongs to nobody and collides with nothing: a credential without one
    writes under its issuer's own account id.
    """
    if not isinstance(namespace, str) or not namespace:
        return None
    mine = head(namespace)
    if not HEAD_SHAPE.fullmatch(mine):
        return LOOKALIKE
    if SESSION_ACTOR.fullmatch(mine):
        registered = any(head(name) == mine for name in sessions or ())
        return SESSION if registered else SESSION_SHAPED
    services = (service,) if isinstance(service, str) else tuple(service or ())
    for names, reason in ((sessions, SESSION), (operators, OPERATOR), (verifiers, VERIFIER),
                          (authors, ROWS), (services, SERVICE)):
        if any(isinstance(name, str) and name and head(name) == mine for name in names or ()):
            return reason
    return None


def refusal(namespace, reason, remedy=REVOKE):
    """The sentence for a namespace that collides: which rule, what to do, and never the host's
    other names. With the longest name a credential can have it is under 200 characters, which
    is what the web service hands on of an endpoint's refusal."""
    shown = namespace if isinstance(namespace, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,63}', namespace) \
        else 'that name'
    return 'A worker credential cannot write as %s: that is %s. %s' % (shown, reason, remedy)


def inside(actor, namespace):
    """Whether ``actor`` is ``namespace`` or a label under it (the rule ``Service.bind_actor`` applies)."""
    if not isinstance(actor, str) or not isinstance(namespace, str) or not namespace:
        return False
    return actor == namespace or actor.startswith(namespace.rstrip('/') + '/')


def instant(value):
    """``value`` as seconds since the epoch, or None when it is not a time this kit writes.

    A number is a time already; a string is read as an ISO-8601 stamp, with or without
    fractional seconds and with ``Z`` or a numeric offset. No ``datetime.fromisoformat``,
    which a host's Python 3.6 does not have (kittrial-5bb.182 item 2).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    found = _WHEN.match(value.strip())
    if found is None:
        return None
    year, month, day, hour, minute, second, fraction, zone = found.groups()
    offset = 0
    if zone and zone not in ('Z', 'z'):
        sign = 1 if zone[0] == '+' else -1
        digits = zone[1:].replace(':', '')
        offset = sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    try:
        moment = datetime.datetime(int(year), int(month), int(day), int(hour), int(minute),
                                   int(second), int((fraction or '0').ljust(6, '0')),
                                   datetime.timezone.utc)
    except ValueError:
        return None
    return moment.timestamp() - offset


def tracker_marks(rows):
    """The ``(head, when)`` pairs a project's tracker rows carry as an author or assignee.

    ``rows`` is what ``bd export --all`` answers: objects with ``created_by``, ``assignee``
    and their own ``comments``. A creator is dated by the row's ``created_at``; an assignee
    and a comment's author by the later of that and ``updated_at`` (an assignment or an edit
    is an update). ``when`` is seconds since the epoch, or None when the row carries no time
    this kit can read: such a mark is held against the credential (``tracker_names``), because
    the kit cannot show it is the credential's own.
    """
    marks = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        created = instant(row.get('created_at'))
        updated = instant(row.get('updated_at'))
        if isinstance(row.get('created_by'), str) and row['created_by']:
            marks.append((head(row['created_by']), created))
        if isinstance(row.get('assignee'), str) and row['assignee']:
            marks.append((head(row['assignee']), later(created, updated)))
        for comment in row.get('comments') or ():
            if isinstance(comment, dict) and isinstance(comment.get('author'), str) and comment['author']:
                marks.append((head(comment['author']),
                              later(instant(comment.get('created_at')), instant(comment.get('updated_at')))))
    return marks


def later(one, other):
    """The later of two instants, either of which may be None."""
    if one is None:
        return other
    if other is None:
        return one
    return max(one, other)


def tracker_names(marks, before=None):
    """The heads among ``marks`` that were writing before ``before``; every one when it is None.

    ``before`` is the instant the credential was issued (an ISO-8601 stamp or seconds): a name
    whose mark is older than it is somebody else's and the credential may not take it. A mark
    with no readable time is taken as somebody else's too -- the kit cannot show it is the
    credential's own -- while one within :data:`OWN_ROWS_SKEW_SECONDS` of ``before`` counts as
    the credential's own, so the credential's issuance stamp (the web service's clock) and its
    first row (the host's clock) need not agree to the second. ``before`` that cannot be read
    at all is treated as "no boundary", which is the strict reading.
    """
    names = set()
    boundary = None if before is None else instant(before)
    for name, when in marks or ():
        if not isinstance(name, str) or not name:
            continue
        if before is None or boundary is None or when is None:
            names.add(name)
            continue
        if when < boundary - OWN_ROWS_SKEW_SECONDS:
            names.add(name)
    return names
