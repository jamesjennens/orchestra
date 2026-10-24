"""Bounded, read-only review-round projections from the existing review chain."""

import re

from briefing import clip, plain_text
from review_state import effective


ROUND_LIMIT = 20
ITEM_LIMIT = 50
ATTESTATION_LIMIT = 10
HOLD_LIMIT = 10
TEXT_LIMIT = 200
EVIDENCE_LIMIT = 240


def _text(value, limit=TEXT_LIMIT):
    if value is None:
        return None
    return clip(plain_text(value), limit)


def _pointer(value):
    if value is None:
        return None
    return plain_text(value)[:EVIDENCE_LIMIT]


def _delivery(delivery):
    if not isinstance(delivery, dict):
        return None
    answer = {'kind': delivery.get('kind')}
    for key in ('remote', 'branch', 'path'):
        if key in delivery:
            answer[key] = _text(delivery[key])
    for key in ('sha256', 'bytes'):
        if key in delivery:
            answer[key] = delivery[key]
    return answer


def _item(entry):
    answer = {
        'request': entry['request'],
        'id': entry['id'],
        'severity': entry.get('severity', 'blocking'),
        'text': _text(entry['text']),
        'state': entry.get('state', 'open'),
        'summary': _text(entry.get('summary')),
        'reason': _text(entry.get('reason')),
        'evidence': _pointer(entry.get('evidence')),
        'by': entry.get('by'),
        'record': entry.get('record'),
    }
    if entry.get('evidence') is not None:
        answer['evidence_omitted_chars'] = max(0, len(plain_text(entry['evidence'])) - EVIDENCE_LIMIT)
    if entry.get('disposition') is not None:
        answer['disposition'] = entry['disposition']
    return answer


def _carried(entry):
    answer = dict(entry, state='open', reason=None, evidence=None, by=None, record=None)
    answer.pop('disposition', None)
    return answer


def _bounded(entries, limit):
    return list(entries[:limit]), max(0, len(entries) - limit)


def _fold(ordered, voids, invalid, refused, positions, reverts, invalid_reverts,
          issue, scopes):
    from review_workflow import projection

    rounds = []
    open_items = {}
    hold_index = {}
    prefix = []
    current = None

    def finish(next_contribution=None):
        if current is None:
            return
        if next_contribution is not None:
            current['next_contribution'] = next_contribution
            if current['disposition'] is None:
                current['disposition'] = 'superseded'
        if current['disposition'] in ('withdrawn', 'superseded'):
            for entry in current['_items'].values():
                if entry.get('state', 'open') == 'open':
                    entry['state'] = current['disposition']
        raw = projection(list(prefix), voids, invalid, refused, positions, reverts,
                         invalid_reverts, closed=issue.get('status') == 'closed')
        shared = effective(raw, scopes, raw['review_state'], assignee=issue.get('assignee'),
                           closed=issue.get('status') == 'closed')
        current['review_state'] = shared['review_state']
        current['workflow_state'] = shared['workflow_state']
        current['integration'] = shared['integration']
        items, current['items_omitted'] = _bounded(list(current.pop('_items').values()), ITEM_LIMIT)
        current['items'] = [_item(entry) for entry in items]
        attestations, current['attestations_omitted'] = _bounded(current['_attestations'], ATTESTATION_LIMIT)
        current['attestations'] = current.pop('_attestations')[:ATTESTATION_LIMIT]
        holds, current['holds_omitted'] = _bounded(current['_holds'], HOLD_LIMIT)
        current['holds'] = current.pop('_holds')[:HOLD_LIMIT]
        rounds.append(current)

    for payload, comment in ordered:
        cid = str(comment['id'])
        operation = payload['operation']
        if operation == 'contribute':
            finish(cid)
            open_items = {key: _carried(entry) for key, entry in open_items.items()}
            prefix.append((payload, comment))
            current = {
                'round': len(rounds) + 1,
                'contribution': cid,
                'commit': payload['commit'],
                'base_commit': payload['base_commit'],
                'repository': _text(payload.get('repository')),
                'delivery': _delivery(payload.get('delivery')),
                'supersedes': payload.get('supersedes'),
                'follows': payload.get('follows'),
                'at': comment['created_at'],
                'summary': _text(payload.get('summary')),
                'disposition': None,
                'disposition_reason': None,
                'outcome': None,
                'review_state': 'unknown',
                'next_contribution': None,
                '_items': {key: _carried(entry) for key, entry in open_items.items()},
                '_attestations': [],
                '_holds': [],
            }
            continue
        if current is None:
            raise ValueError('Review operation has no contribution round')
        prefix.append((payload, comment))
        if operation == 'request-changes':
            current['outcome'] = {'operation': operation, 'record': cid, 'at': comment['created_at'],
                                  'summary': _text(payload.get('summary'))}
            for item in payload['items']:
                key = (cid, item['id'])
                entry = dict(request=cid, id=item['id'], text=item['text'],
                             severity=item.get('severity', 'blocking'), state='open',
                             summary=payload.get('summary'), reason=None, evidence=None,
                             by=None, record=None)
                open_items[key] = entry
                current['_items'][key] = entry
        elif operation == 'respond':
            for resolution in payload['resolutions']:
                key = (resolution['request'], resolution['item'])
                entry = open_items.pop(key)
                entry.update(state='answered', reason=resolution['reason'],
                             evidence=resolution['evidence'], by='respond', record=cid)
                if key in current['_items']:
                    current['_items'][key] = entry
        elif operation == 'resolve-item':
            key = (payload['request'], payload['item'])
            entry = open_items[key]
            disposition = payload.get('disposition') or 'resolved'
            entry.update(reason=payload['reason'], by='resolve-item', record=cid,
                         disposition=disposition)
            if disposition == 'note':
                entry['severity'] = 'note'
                entry['state'] = 'open'
            else:
                entry['state'] = 'settled'
                del open_items[key]
            if key in current['_items']:
                current['_items'][key] = entry
        elif operation == 'approve':
            current['outcome'] = {'operation': operation, 'record': cid, 'at': comment['created_at'],
                                  'summary': _text(payload.get('summary'))}
        elif operation == 'withdraw':
            if current['disposition'] is None:
                current['disposition'] = payload.get('disposition') or 'withdrawn'
                current['disposition_reason'] = _text(payload['reason'])
                current['outcome'] = {'operation': operation, 'record': cid,
                                      'at': comment['created_at'], 'summary': None}
        elif operation == 'attest':
            current['_attestations'].append({
                'record': cid, 'verdict': payload['verdict'], 'commit': payload['commit'],
                'base_commit': payload['base_commit'], 'method': _text(payload['method']),
                'reason': _text(payload['reason']), 'at': comment['created_at'],
            })
        elif operation == 'hold':
            hold = {'record': cid, 'reason': _text(payload['reason']), 'released_by': None,
                    'release_reason': None, 'at': comment['created_at']}
            current['_holds'].append(hold)
            hold_index[cid] = hold
        elif operation == 'release-hold':
            hold = hold_index.get(payload['hold'])
            if hold is not None and hold['released_by'] is None:
                hold['released_by'] = cid
                hold['release_reason'] = _text(payload['reason'])
    finish()
    return rounds


def _valid_prefix(issue, operators):
    from review_workflow import PREFIX, applied_review_voids, effective as surviving, projection

    applied, invalid, refused = applied_review_voids(issue, operators)
    applied_ids = set(applied)
    found = surviving(issue, applied_ids)
    positions = {str(comment.get('id')): index
                 for index, comment in enumerate(issue.get('comments') or [])}
    total = sum(1 for comment in issue.get('comments') or []
                if isinstance(comment.get('text'), str) and comment['text'].startswith(PREFIX)
                and str(comment.get('id')) not in applied_ids)
    ordered = []
    previous = None
    while found:
        children = [cid for cid, (payload, _comment) in found.items()
                    if payload['previous'] == previous]
        if len(children) != 1:
            break
        cid = children[0]
        candidate = found.pop(cid)
        try:
            projection(ordered + [candidate], list(applied.values()), invalid, refused,
                       positions, [], [], closed=issue.get('status') == 'closed')
        except ValueError:
            break
        ordered.append(candidate)
        previous = cid
    malformed = max(1, total - len(ordered))
    return ordered, (list(applied.values()), invalid, refused, positions, [], []), total, malformed


def read(issue, scopes=None, operators=None, journal=None):
    from review_workflow import history

    scopes = scopes or []
    malformed = 0
    try:
        ordered, voids, invalid, refused, positions, reverts, invalid_reverts = history(
            issue, operators, journal)
        total = len(ordered)
        rounds = _fold(ordered, voids, invalid, refused, positions, reverts, invalid_reverts,
                       issue, scopes)
    except (ValueError, KeyError, TypeError):
        ordered, extra, total, malformed = _valid_prefix(issue, operators)
        rounds = _fold(ordered, *extra, issue, scopes)
    proved = malformed == 0
    if not proved:
        for index, round_ in enumerate(rounds):
            for item in round_['items']:
                item['state'] = 'unknown'
            if index == len(rounds) - 1:
                round_['review_state'] = 'unknown'
                round_['outcome'] = None
    rounds_total = len(rounds)
    shown = list(reversed(rounds))[:ROUND_LIMIT]
    answer = {
        'task': issue['id'],
        'rounds_total': rounds_total,
        'rounds_omitted': max(0, rounds_total - ROUND_LIMIT),
        'rounds': shown,
        'coverage': {'records': total, 'malformed': malformed, 'proved': proved},
        'warnings': [] if proved else [
            'Malformed or unlinked review history; unresolved item states are unknown and need operator reconciliation.'
        ],
    }
    answer['text'] = format_text(answer)
    return answer


def _line_text(value):
    if value is None:
        return '-'
    text = re.sub(r'[\r\n]+', ' / ', value['text'])
    if value['omitted_chars']:
        text += ' [+%d characters]' % value['omitted_chars']
    return text


def format_text(result):
    coverage = result['coverage']
    lines = ['%s: %d rounds, %d omitted; records=%d malformed=%d proved=%s' % (
        result['task'], result['rounds_total'], result['rounds_omitted'],
        coverage['records'], coverage['malformed'], str(coverage['proved']).lower())]
    for round_ in result['rounds']:
        outcome = round_['outcome'] or {}
        lines.append('Round %d contribution=%s commit=%s state=%s outcome=%s record=%s disposition=%s next=%s' % (
            round_['round'], round_['contribution'], round_['commit'], round_['review_state'],
            outcome.get('operation') or '-', outcome.get('record') or '-',
            round_['disposition'] or '-', round_['next_contribution'] or '-'))
        if round_.get('summary') is not None:
            lines.append('  summary: ' + _line_text(round_['summary']))
        if outcome.get('summary') is not None:
            lines.append('  outcome summary: ' + _line_text(outcome['summary']))
        if round_.get('disposition_reason') is not None:
            lines.append('  disposition reason: ' + _line_text(round_['disposition_reason']))
        for item in round_['items']:
            lines.append('  item %s/%s severity=%s state=%s: %s' % (
                item['request'], item['id'], item['severity'], item['state'],
                _line_text(item['text'])))
            if item.get('reason') is not None:
                lines.append('    reason: ' + _line_text(item['reason']))
            if item.get('evidence') is not None:
                lines.append('    evidence: ' + re.sub(r'[\r\n]+', ' / ', item['evidence']))
        if round_['items_omitted']:
            lines.append('  ... %d more items' % round_['items_omitted'])
        for attestation in round_['attestations']:
            lines.append('  attestation %s verdict=%s commit=%s method=%s reason=%s' % (
                attestation['record'], attestation['verdict'], attestation['commit'],
                _line_text(attestation['method']), _line_text(attestation['reason'])))
        if round_['attestations_omitted']:
            lines.append('  ... %d more attestations' % round_['attestations_omitted'])
        for hold in round_['holds']:
            lines.append('  hold %s released_by=%s reason=%s' % (
                hold['record'], hold['released_by'] or '-', _line_text(hold['reason'])))
            if hold.get('release_reason') is not None:
                lines.append('    release reason: ' + _line_text(hold['release_reason']))
        if round_['holds_omitted']:
            lines.append('  ... %d more holds' % round_['holds_omitted'])
    for warning in result['warnings']:
        lines.append('Warning: ' + warning)
    return '\n'.join(lines) + '\n'
