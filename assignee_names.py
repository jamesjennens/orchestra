"""Resolve a display name to an actor id when an assignment is written (kittrial-5bb.333).

An assignee is an ACTOR ID: every ownership check compares it with exact equality
against the authenticated actor (``review_workflow.execute``, the claim gate, every
queue). A web agent's display name is not that id -- the agent acts as
``agent_<hex>`` while the roster shows ``jj-vscode-1`` -- so a row that stores the
display name is undeliverable by the agent that owns it and disappears from the
agent's own list.

This module is the one rule, and it is deliberately used ONLY when a NEW assignment
is written. It is never used to interpret a stored assignee:

* a value that is already an actor id (including a legacy/SSH actor that has ever
  acted in the project) is left exactly as it is -- it is an actor, not a name;
* a display name carried by exactly ONE other identity of the project resolves to
  that identity's actor id, and the note names both;
* a name carried by nobody, or by more than one identity, is refused with one
  sentence (for the ambiguous case the sentence names the candidate actor ids).

Why not resolve a stored row? A roster can be extended by any endpoint caller
(``session register --name``), so resolving at effect time lets a newcomer whose
session is named like an existing actor take over that actor's task: the stored
value ``w1`` would be read as a display name and mapped to the newcomer. The review
of this task reproduced exactly that (P1) and a redirect of an operator's raw
``--assignee`` (P5). Exact comparison of the stored value is the invariant; the one
safe mapping that remains is actor -> display name for a LABEL, which cannot move a
row to another actor.
"""
import re


class NameRefusal(ValueError):
    """One sentence saying why the name cannot be stored, and what to use instead."""


def index(entries, actors=()):
    """``{'by_actor': {actor: name}, 'by_name': {name: [actor, ...]}}``.

    ``entries`` are ``{actor, name, kind}`` records of the project's identities.
    ``actors`` are actor ids known to have acted in the project even when they carry
    no name record (legacy/SSH actors, a stored assignee or author). Those ids are
    **not** names: a display name that spells one of them is not an alias, so a
    resolution can never rename an actor that was named exactly.
    """
    by_actor = {}
    by_name = {}
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        actor = entry.get('actor')
        if not isinstance(actor, str) or not actor:
            continue
        name = entry.get('name')
        name = name if isinstance(name, str) and name else None
        by_actor.setdefault(actor, name)
        if name is not None:
            by_name.setdefault(name, set()).add(actor)
    for actor in actors or ():
        if isinstance(actor, str) and actor:
            by_actor.setdefault(actor, None)
    # A name that is any known actor id is not an alias. Two passes: the actor set is
    # complete before the names are filtered.
    return {'by_actor': by_actor,
            'by_name': {name: sorted(values) for name, values in by_name.items()
                        if name not in by_actor}}


def exact_actor(value, known):
    """Whether ``value`` is a known actor id of ``known`` (an index or an identity list)."""
    if isinstance(known, list):
        known = index(known)
    return isinstance(value, str) and value in (known.get('by_actor') or {})


def resolve(requested, known):
    """Resolve one requested assignee, or raise :class:`NameRefusal` with one sentence.

    Only a value that is NOT a known actor is treated as a display name. Returns
    ``{'requested', 'actor', 'name', 'resolved', 'sentence'}``.
    """
    if isinstance(known, list):
        known = index(known)
    if not isinstance(requested, str) or not requested:
        raise NameRefusal('An assignment needs one exact actor id; nothing was assigned.')
    if requested in known.get('by_actor', {}):
        name = known['by_actor'].get(requested) or requested
        return {'requested': requested, 'actor': requested, 'name': name,
                'resolved': False, 'sentence': None}
    actors = known.get('by_name', {}).get(requested) or []
    if len(actors) == 1:
        actor = actors[0]
        return {'requested': requested, 'actor': actor, 'name': requested, 'resolved': True,
                'sentence': 'Assigning by display name: %s is the actor %s, and the actor id '
                            'was stored.' % (requested, actor)}
    if len(actors) > 1:
        raise NameRefusal('More than one actor in this project is named %s (%s), so the name '
                          'cannot be stored: name the actor id to use, and nothing was assigned.'
                          % (requested, ', '.join(actors)))
    raise NameRefusal('No actor or agent of this project is named %s, so the name cannot be '
                      'stored: name the actor id to use (it is shown beside the name on the '
                      'project agents page), and nothing was assigned.' % requested)


def display_name(value, known):
    """The display name of an actor id; the value itself when it is not a known actor.

    Actor -> name only. A raw value that is not an actor is returned unchanged: this
    can label a row but can never decide who owns it.
    """
    if not isinstance(value, str) or not value:
        return value
    if isinstance(known, list):
        known = index(known)
    name = known.get('by_actor', {}).get(value)
    return name if isinstance(name, str) and name else value


def label(value, known):
    """``<name> (<actor id>)`` for an actor; the value unchanged for anything else."""
    if not isinstance(value, str) or not value:
        return value
    if isinstance(known, list):
        known = index(known)
    if value in known.get('by_actor', {}):
        name = display_name(value, known)
        return '%s (%s)' % (name, value) if name != value else value
    return value


def note(resolution):
    """The one sentence an assignment response carries when the name was resolved."""
    return resolution.get('sentence') if isinstance(resolution, dict) else None


#: A conservative actor-id shape, used only where a caller must decide whether a value
#: could be an actor at all without a roster. It is NOT an identity check: the roster is.
ACTOR_SHAPE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}\Z')
