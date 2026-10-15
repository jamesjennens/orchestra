"""Deterministic Markdown and exact-reference comparisons for requirements views.

Inputs are the canonical member projection, or a validated frozen copy. This
module neither accepts records nor reads a tracker, clock or filesystem.
"""
import html
import re
import unicodedata

from coordination import identifier
from requirements import _validate_record, content_hash

_MARKDOWN = re.compile(r'([\\`*_{}\[\]()#+.!|~\-])')


def literal(value, single_line=False):
    """Render source wording as inert text, including HTML and bidi controls."""
    if not isinstance(value, str):
        raise ValueError('Requirements text must be a string')
    shown = []
    for char in value:
        if char in '\n\t':
            shown.append(' ' if single_line else char)
        elif unicodedata.category(char) in ('Cc', 'Cf', 'Cs'):
            shown.append('\\u%04x' % ord(char))
        elif char in '\u2028\u2029':
            shown.append(' ' if single_line else '\n')
        else:
            shown.append(char)
    return _MARKDOWN.sub(r'\\\1', html.escape(''.join(shown), quote=False))


def reference(record):
    if record is None:
        return None
    if not isinstance(record, dict):
        raise ValueError('Requirements revision must be an object')
    _validate_record(record, 'requirements revision', has_key='key' in record)
    identifier(record['id'])
    return {key: record[key] for key in ('id', 'revision', 'sha256')}


def _reference_line(record):
    ref = reference(record)
    return 'Record: `%s` | revision %d | SHA-256 `%s`' % (
        ref['id'], ref['revision'], ref['sha256'])


def _wording(record, label):
    return ['**%s**' % label, '',
            '#### ' + literal(record['title'], single_line=True), '',
            _reference_line(record), '',
            *('> ' + line for line in literal(record['description']).split('\n')), '']


def _items(view):
    items = view.get('items')
    if not isinstance(items, list):
        raise ValueError('Requirements document needs its complete items list')
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Invalid requirements document item')
        identity = identifier(item.get('id'))
        if identity in result:
            raise ValueError('Requirements document repeats an ID')
        if item.get('kind') not in ('requirement', 'brd-section', 'unreadable'):
            raise ValueError('Unsupported requirements document item kind')
        for key in ('current', 'accepted', 'pending_draft'):
            record = item.get(key)
            if record is not None and reference(record)['id'] != identity:
                raise ValueError('Requirements document revision belongs to another ID')
        result[identity] = item
    return result


def render(view):
    """UTF-8/LF Markdown; every pending section identifies unaccepted wording."""
    if not isinstance(view, dict):
        raise ValueError('Requirements document must be an object')
    items = _items(view)
    project = identifier(view.get('project'))
    lines = ['# Business requirements: ' + literal(project, single_line=True), '',
             'Generated from requirement records. Freezing this view does not accept a baseline or pending wording.', '',
             'Requirements mode: ' + literal(view.get('governance', {}).get('mode', 'unknown')), '']
    if view.get('governance', {}).get('sha256'):
        lines.extend(['Governance revision: ' + str(view['governance']['revision'])
                      + ' | SHA-256: ' + literal(view['governance']['sha256']), ''])
    for kind, heading in (('brd-section', 'Purpose, users and scope'),
                          ('requirement', 'Requirements'), ('unreadable', 'Unverified records')):
        selected = [item for item in items.values() if item.get('kind') == kind]
        if not selected:
            continue
        lines.extend(['## ' + heading, ''])
        for item in sorted(selected, key=lambda row: row['id']):
            lines.extend(['### ' + literal(item['id'], single_line=True), ''])
            if item.get('unreadable'):
                lines.extend(['This record cannot be verified. Ask the project owner to reconcile it.', ''])
                continue
            current, accepted, pending = (item.get(key) for key in ('current', 'accepted', 'pending_draft'))
            if current is None:
                raise ValueError('Requirements document is missing a selected revision')
            reference(current)
            state = item.get('requirement_state', 'unknown')
            lines.extend(['State: ' + literal(state, single_line=True), ''])
            if item.get('reason'):
                explanation = item['reason']
                if not isinstance(explanation, dict) or not isinstance(explanation.get('reason'), str):
                    raise ValueError('Requirement state reason must retain its evidence')
                lines.extend(['Reason: ' + literal(explanation['reason']), ''])
            successor = (item.get('state') or {}).get('superseded_by')
            if successor:
                identifier(successor['id'])
                lines.extend(['Superseded by: ' + literal(successor['id'], single_line=True)
                              + ' | revision ' + str(successor['revision'])
                              + ' | SHA-256 ' + literal(successor['sha256'], single_line=True), ''])
            if item.get('state'):
                lines.extend(['State evidence SHA-256: ' + literal(item['state']['sha256'], single_line=True), ''])
            proof = item.get('acceptance')
            if proof is not None:
                decision = proof.get('decision', {}).get('decision_id')
                if decision:
                    lines.extend(['Acceptance decision: ' + literal(decision, single_line=True), ''])
            if accepted is not None:
                if accepted.get('acceptance_state') != 'accepted':
                    raise ValueError('Accepted wording must be an accepted revision')
                lines.extend(_wording(accepted, 'Accepted wording'))
            if pending is not None:
                if pending.get('acceptance_state') != 'draft':
                    raise ValueError('Pending wording must be a draft revision')
                lines.extend(_wording(pending, 'Pending - not accepted'))
            if accepted is None and pending is None:
                raise ValueError('Requirements document is missing its accepted or pending wording')
    if not items:
        lines.extend(['No requirements yet.', ''])
    for group, heading in (('decisions', 'Decisions'), ('questions', 'Open questions')):
        lines.extend(['## ' + heading, ''])
        entries = view.get(group, [])
        if not isinstance(entries, list):
            raise ValueError('Invalid document context')
        if not entries:
            lines.extend(['None recorded in this view.', ''])
        for entry in sorted(entries, key=lambda row: (row['id'], row.get('revision', 0))):
            identifier(entry['id'])
            lines.extend(['### ' + literal(entry.get('title', entry['id']), single_line=True), '',
                          'Record: ' + literal(entry['id'], single_line=True), ''])
            if 'sha256' in entry:
                lines.extend(['Open native observation - not acceptance evidence.', '',
                              'Observation SHA-256: ' + literal(entry['sha256']), ''])
            if 'revision' in entry:
                lines.extend(['Revision: ' + str(entry['revision']), ''])
            if 'description' in entry:
                lines.extend(['> ' + line for line in literal(entry['description']).split('\n')])
                lines.append('')
    return ('\n'.join(lines).rstrip('\n') + '\n').encode('utf-8')


def compare(before, after):
    """Compare exact revisions and terminal state; timestamps never order views."""
    if before.get('project') != after.get('project'):
        raise ValueError('Requirements comparison needs views from the same project')
    old, new = _items(before), _items(after)
    groups = {key: [] for key in ('added', 'removed', 'edited', 'pending', 'accepted', 'withdrawn', 'superseded')}
    changes = []
    for identity in sorted(set(old) | set(new)):
        left, right = old.get(identity), new.get(identity)
        for item in (left, right):
            if item and (item.get('unreadable') or item.get('requirement_state') == 'unknown'):
                raise ValueError('A selected requirement cannot be verified for comparison')
        refs = []
        for item in (left, right):
            refs.append({key: reference(item.get(key)) if item else None
                         for key in ('current', 'accepted', 'pending_draft')})
        left_refs, right_refs = refs
        categories = []
        if left is None:
            categories.append('added')
        elif right is None:
            categories.append('removed')
        elif left_refs['current'] != right_refs['current']:
            categories.append('edited')
        if left_refs['pending_draft'] != right_refs['pending_draft']:
            categories.append('pending')
        if right_refs['accepted'] is not None and left_refs['accepted'] != right_refs['accepted']:
            categories.append('accepted')
        if right and right.get('requirement_state') in ('withdrawn', 'superseded'):
            if not left or (left.get('requirement_state'), left.get('state')) != (
                    right.get('requirement_state'), right.get('state')):
                categories.append(right['requirement_state'])
        if categories:
            row = dict(id=identity, categories=categories, before=left_refs, after=right_refs,
                       before_state=left.get('state') if left else None,
                       after_state=right.get('state') if right else None)
            changes.append(row)
            for key in categories:
                groups[key].append(identity)
    context = {}
    for group in ('decisions', 'questions'):
        def index(view):
            entries = view.get(group, [])
            if not isinstance(entries, list):
                raise ValueError('Invalid comparison context')
            indexed = {}
            for entry in entries:
                identity = identifier(entry.get('id'))
                if identity in indexed or content_hash(entry) != entry.get('sha256'):
                    raise ValueError('Unverified comparison context')
                indexed[identity] = entry
            return indexed
        left, right = index(before), index(after)
        rows = []
        for identity in sorted(set(left) | set(right)):
            old_entry, new_entry = left.get(identity), right.get(identity)
            if old_entry == new_entry:
                continue
            rows.append(dict(id=identity, change='added' if old_entry is None else
                'removed' if new_entry is None else 'edited',
                before=old_entry, after=new_entry))
        context[group] = rows
    governance = None
    if before.get('governance') != after.get('governance'):
        governance = dict(before=before.get('governance'), after=after.get('governance'))
    return dict(changes=changes, groups=groups, total=len(changes), context=context,
                governance=governance, context_total=sum(len(rows) for rows in context.values()))
