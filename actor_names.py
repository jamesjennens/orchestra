"""Names a worker credential may not write under (kittrial-5bb.184).

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

* a session actor registered in the project;
* any name with the shape of a session actor (``session-<uuid>``), registered or not:
  the server makes those, in every project;
* a name on the operator list or on the verifier list of the installation;
* the web service's own namespace.

The head and the case are what the review workflow's own comparison uses
(``review_workflow.author_key``): to it ``Team``, ``team/alice`` and ``team/bob`` are one
author. A name with the shape of a web account or agent id is not this module's business:
``http_authority.http_actor_denial`` and ``Service.issue_credential`` hold those.

Pure and without imports of the kit, so the web service uses it on every platform.
"""
import re

#: The shape of the actors ``sessions.py`` makes.
SESSION_ACTOR = re.compile(r'session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
#: The web service's own namespace, when it was started without another (``--actor-namespace``).
SERVICE_NAMESPACE = 'http'

SESSION = 'a session actor of this project'
SESSION_SHAPED = 'the shape of a session actor'
OPERATOR = 'a name on the operator list'
VERIFIER = 'a name on the verifier list'
SERVICE = "the web service's own namespace"
#: What to do: about a credential that has such a name, and when one is asked for.
REVOKE = 'Revoke it and issue one under another name.'
CHOOSE = 'Choose another name.'


def head(name):
    """The part of an actor label that says who it is: before the first ``/``, case folded."""
    return name.split('/', 1)[0].casefold() if isinstance(name, str) else ''


def collision(namespace, sessions=(), operators=(), verifiers=(), service=SERVICE_NAMESPACE):
    """Why a worker credential may not write under ``namespace``, or None.

    ``sessions``, ``operators`` and ``verifiers`` are the names the host has; ``service`` is
    the web service's namespace, or a collection of them. A namespace that is not a string,
    or is empty, belongs to nobody and collides with nothing: a credential without one
    writes under its issuer's own account id.
    """
    if not isinstance(namespace, str) or not namespace:
        return None
    mine = head(namespace)
    if SESSION_ACTOR.fullmatch(mine):
        registered = any(head(name) == mine for name in sessions or ())
        return SESSION if registered else SESSION_SHAPED
    services = (service,) if isinstance(service, str) else tuple(service or ())
    for names, reason in ((sessions, SESSION), (operators, OPERATOR), (verifiers, VERIFIER), (services, SERVICE)):
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
