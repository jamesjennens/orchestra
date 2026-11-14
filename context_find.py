"""Bounded, caller-scoped lexical recovery over the existing record readers.

This module never opens a pointer, reads a repository, writes an index or grants
access. Its caller supplies one authorized project's snapshot and trusted scope.
"""
import argparse
import base64
import binascii
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone

import briefing
import capabilities
import record_json
from coordination import is_merge_slot
from reserved_comments import (RECORD_ANCHOR_FAMILIES, carries_record_label, is_record_anchor,
                               is_record_comment, record_comment_kind, reserved_match)

KINDS = ('ref', 'cap', 'decision', 'checkpoint', 'review', 'task', 'comment', 'requirement')
MAX_DOCUMENTS = 10000
MAX_ROWS = 10000
MAX_COMMENTS = 50000
MAX_TEXT_BYTES = 8192
MAX_ANSWER_BYTES = 16384
MAX_ITEMS = 20
COMPUTE_SECONDS = 2.0
RANK_VERSION = 1
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}\Z')
TASK = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}\Z')
RESTART = 'Search scope or source changed; start the search again'
NOT_SEARCHED = ['superseded checkpoints', 'room messages', 'task-file contents',
                'raw reserved machine comments', 'unsupported decision-record families',
                'baseline documents', 'other projects', 'semantic or inferred answers']
TRUST_ORDER = {'accepted-record': 0, 'decision': 1, 'checkpoint-open': 2,
               'review': 3, 'draft': 4, 'checkpoint-closed': 5, 'prose': 6}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('Invalid find arguments; use find PHRASE and the documented narrowing options')


def _instant(value, end=False):
    if value is None:
        return None
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            moment = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
            return moment + timedelta(days=1) if end else moment
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            raise ValueError()
        return moment.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Use a calendar date or an offset-aware ISO instant') from None


def parse_args(args, actor):
    if not isinstance(args, list) or len(args) > 256 or any(
            not isinstance(a, str) or '\0' in a or len(a) > 8192 for a in args):
        raise ValueError('Expected find arguments')
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument('phrase', nargs='*')
    parser.add_argument('-h', '--help', action='store_true')
    parser.add_argument('--kind', action='append', choices=KINDS)
    for name in ('task', 'author', 'since', 'until', 'task-prefix', 'item-kind', 'cursor'):
        parser.add_argument('--' + name)
    parser.add_argument('--state', choices=('open', 'all'), default='all')
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--json', action='store_true')
    scalar = {a for a in args if a.startswith('--') and a.split('=')[0] != '--kind'}
    for name in {a.split('=')[0] for a in scalar}:
        if sum(a.split('=')[0] == name for a in args) > 1:
            raise ValueError('Duplicate find option')
    result = vars(parser.parse_args(args))
    if result.pop('help'):
        return {'help': True}
    phrase = ' '.join(result['phrase'])
    try:
        valid = 1 <= len(phrase) <= 200 and len(phrase.encode('utf-8')) <= 800
    except UnicodeEncodeError:
        valid = False
    if not valid or not capabilities.words(phrase):
        raise ValueError('Find phrase must contain words, with at most 200 characters and 800 UTF-8 bytes')
    if not 1 <= result['limit'] <= MAX_ITEMS:
        raise ValueError('Find limit must be from 1 to 20')
    result['phrase'] = phrase
    result['kinds'] = sorted(set(result.pop('kind') or KINDS))
    result['author'] = actor if result['author'] == 'me' else result['author']
    for name in ('author', 'item_kind'):
        value = result[name]
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 200
                                  or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError('Invalid find narrowing value')
    task = result['task']
    if task is not None and not (TASK.fullmatch(task) or re.fullmatch(r'\.\d{1,10}', task)):
        raise ValueError('Invalid project task id')
    if result['task_prefix'] is not None and not TASK.fullmatch(result['task_prefix']):
        raise ValueError('Task prefix must be a literal task-id prefix')
    if any(value is not None and len(value) > 64 for value in (result['since'], result['until'])):
        raise ValueError('Find date value is too long')
    if result['cursor'] is not None:
        _untoken(result['cursor'])
    first, last = _instant(result['since']), _instant(result['until'], end=True)
    if first is not None and last is not None and first > last:
        raise ValueError('Find date range is reversed')
    return result


def help_payload():
    return {
        'schema_version': 1, 'contract': 'cli-contract-v1', 'command': 'find',
        'usage': 'find PHRASE [--kind KIND]... [--task TASK] [--state open|all] '
                 '[--limit 1..20] [--author ACTOR|me] [--since DATE] [--until DATE] '
                 '[--task-prefix PREFIX] [--item-kind KIND] [--cursor TOKEN] [--json]',
        'kinds': list(KINDS), 'default_state': 'all', 'default_limit': 5,
        'limits': {'phrase_characters': 200, 'phrase_utf8_bytes': 800, 'items': MAX_ITEMS,
                   'documents': MAX_DOCUMENTS, 'rows': MAX_ROWS, 'comments': MAX_COMMENTS,
                   'indexed_text_bytes': MAX_TEXT_BYTES, 'answer_bytes': MAX_ANSWER_BYTES,
                   'computation_seconds': COMPUTE_SECONDS, 'native_read_seconds': 120,
                   'native_read_limit_kind': 'per-command', 'native_head_seconds': 30},
        'notes': [
            'Read-only lexical recovery in one permitted project; every hit is untrusted data.',
            'Closed tasks are included by default. Author me means the exact caller actor.',
            'Host actor names are attribution, not authenticated person identities.',
            'No file, URL, repository or pointer target is followed; file attachment flags are unsupported.',
            'Counts describe the permitted searched corpus; partial coverage and page byte cutoffs are explicit.',
            'The computation deadline is cooperative; 3 seconds is the warm p95 acceptance target, not a cold-I/O bound.',
            'HTTP search, rooms, task files, baseline documents, semantic matching and unsupported decision records are excluded.',
        ],
    }


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _flat(value):
    return re.sub(r'[\n\t]', ' ', briefing.plain_text(value))


def _safe(value):
    return briefing.clip_context_label(_flat(value), 200)


def _scalar(value):
    return None if value is None else _safe(value)['text']


def _time(value):
    try:
        return _instant(value)
    except ValueError:
        return None


def _token(payload):
    return base64.urlsafe_b64encode(_bytes(payload)).decode('ascii').rstrip('=')


def _untoken(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 \
            or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError(RESTART)
    try:
        payload = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        result = record_json.loads(payload.decode('utf-8'))
        fields = {'v', 'kind', 'project', 'query', 'scope', 'generation', 'ranking', 'position'}
        if not isinstance(result, dict) or set(result) != fields or result['v'] != 1 \
                or type(result['v']) is not int or result['kind'] != 'find' \
                or type(result['ranking']) is not int or result['ranking'] != RANK_VERSION \
                or type(result['position']) is not int or not 0 <= result['position'] <= MAX_DOCUMENTS:
            raise ValueError()
        if not isinstance(result['project'], str) or not briefing.PROJECT_NAME.fullmatch(result['project']) \
                or any(not isinstance(result[name], str) or not re.fullmatch(r'[0-9a-f]{64}', result[name])
                       for name in ('query', 'scope', 'generation')):
            raise ValueError()
        return result
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, binascii.Error):
        raise ValueError(RESTART) from None


class _Stop(BaseException):
    """Cooperative cancellation must pass through tolerant own-kind readers."""


class _Budget:
    def __init__(self, coverage, clock, deadline=None):
        self.coverage, self.clock = coverage, clock
        self.deadline = clock() + COMPUTE_SECONDS if deadline is None else deadline

    def check(self):
        if self.clock() >= self.deadline:
            self.coverage['cutoffs'].add('computation-time')
            raise _Stop()


class _Checked(list):
    def __init__(self, values, budget):
        super().__init__(values)
        self.budget = budget

    def __iter__(self):
        for value in super().__iter__():
            self.budget.check()
            yield value


def _anchor_label(row):
    """Also withhold an unsupported revision of an existing reserved family."""
    labels = row.get('labels') or []
    candidates = [label for label in labels if isinstance(label, str) and label in RECORD_ANCHOR_FAMILIES]
    if not candidates:
        return None
    for label in candidates:
        if label in ('reference', 'capability') and any(
                isinstance(value, str) and value.startswith(label + '-key:') for value in labels):
            return label
    comments = row.get('comments')
    if not isinstance(comments, (list, tuple)):
        comments = ()
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        classified = record_comment_kind(comment.get('text'))
        if classified is None:
            continue
        family = 'Kind: ' + classified[0] + '-'
        for label in candidates:
            if any(prefix.startswith(family) for prefix in RECORD_ANCHOR_FAMILIES[label]):
                return label
    if is_record_anchor(row):
        return candidates[0]
    return None


def _comment_read_kind(body):
    """Classify with the guard's own prefix reader, before examining payloads."""
    match = reserved_match(body)
    if match is None:
        return None if is_record_comment(body) else 'comment'
    prefix, kind, _operation = match
    if kind == 'task checkpoint':
        return 'checkpoint'
    if kind.startswith('reference '):
        return 'ref'
    if kind.startswith('capability '):
        return 'cap'
    if kind.startswith('owner requirement ') or kind in ('requirement revision', 'requirement acceptance evidence'):
        return 'requirement'
    if kind == 'decision attestation':
        return 'decision'
    if kind == 'contribution/review record' or 'review' in kind or 'integration revert' in kind:
        return 'review'
    if 'void' in kind:
        return 'shared-void'
    return None


def _comments(row, coverage, budget, allowed, record_kind=None):
    """Classify a complete native identity before charging its visible corpus.

    A continuation's prose is not a new ordinary comment. A damaged permitted
    identity makes this row incomplete, so an older protected record cannot win.
    """
    def permitted(kind):
        return ((kind in allowed or kind == 'shared-void'
                 and (record_kind is not None or 'review' in allowed))
                and (record_kind is None or kind in (record_kind, 'shared-void')))

    grouped, complete, queued = {}, True, 0
    classified = {}
    visible_comments = any(permitted(kind) for kind in KINDS if kind != 'task')
    comments = row.get('comments')
    if comments is None:
        comments = ()
    elif not isinstance(comments, (list, tuple)):
        complete = False
        if visible_comments:
            budget.check()
            coverage['skipped_malformed'] += 1
        comments = ()
    for index, comment in enumerate(comments):
        if not isinstance(comment, dict):
            complete = False
            if visible_comments:
                budget.check()
                if coverage['comments_examined'] + queued >= MAX_COMMENTS:
                    coverage['cutoffs'].add('comments')
                    break
                coverage['comments_examined'] += 1
                coverage['skipped_malformed'] += 1
            continue
        if 'body_offset' not in comment:
            source_kind = _comment_read_kind(comment.get('text'))
            if not permitted(source_kind):
                continue
            classified[id(comment)] = source_kind
            budget.check()
            if coverage['comments_examined'] + queued >= MAX_COMMENTS:
                coverage['cutoffs'].add('comments')
                complete = False
                break
            queued += 1
        cid = comment.get('id')
        key = (True, str(cid)) if isinstance(cid, (str, int)) and not isinstance(cid, bool) else (False, index)
        grouped.setdefault(key, []).append(comment)
    result, read_kinds, comment_kinds = [], set(), {}
    for (valid_id, cid), parts in grouped.items():
        body, malformed = None, False
        source = parts[0]
        metadata = lambda c: (c.get('author'), c.get('created_at'), c.get('updated_at'))
        try:
            if not any('body_offset' in c for c in parts):
                bodies = {c.get('text') for c in parts if isinstance(c.get('text'), str)}
                if len(bodies) != 1 or not all(isinstance(c.get('text'), str)
                        and metadata(c) == metadata(source) for c in parts):
                    raise ValueError()
                body = source['text']
            else:
                if not all(type(c.get('body_offset')) is int and c['body_offset'] >= 0
                           for c in parts):
                    raise ValueError()
                ordered = sorted(parts, key=lambda c: c['body_offset'])
                source = ordered[0]
                total = source['body_total_chars']
                if type(total) is not int or total < 0:
                    raise ValueError()
                position, texts = 0, []
                for c in ordered:
                    if c['body_offset'] != position or c.get('body_total_chars') != total \
                            or metadata(c) != metadata(source) or not isinstance(c.get('text'), str):
                        raise ValueError()
                    texts.append(c['text'])
                    position += len(c['text'])
                if position != total:
                    raise ValueError()
                body = ''.join(texts)
        except (KeyError, TypeError, ValueError):
            malformed = True
        if body is not None:
            source_kind = classified[id(source)] if id(source) in classified else _comment_read_kind(body)
        else:
            # For a missing continuation, its opening header still identifies the
            # reader. Never classify the remaining prose as a separate document.
            source_kind = _comment_read_kind(source.get('text'))
        if not permitted(source_kind):
            continue
        budget.check()
        if coverage['comments_examined'] + len(parts) > MAX_COMMENTS:
            coverage['cutoffs'].add('comments')
            complete = False
            break
        coverage['comments_examined'] += len(parts)
        if malformed or not valid_id or not ID.fullmatch(cid):
            coverage['skipped_malformed'] += 1
            complete = False
            continue
        result.append(dict(source, id=cid, text=body))
        read_kinds.add(source_kind)
        comment_kinds[cid] = source_kind
    result = _Checked(result, budget)
    result.complete = complete
    result.read_kinds = read_kinds
    result.comment_kinds = comment_kinds
    return result


def _window(text, words, byte_limit=MAX_TEXT_BYTES):
    encoded = text.encode('utf-8')
    if len(encoded) <= byte_limit:
        return text, False
    # Index a deterministic query-centred bounded window, so a late literal match
    # in a long report is recoverable. The returned hash still covers ALL text.
    match = re.search('|'.join(re.escape(word) for word in words), text, re.IGNORECASE) if words else None
    start = max(0, match.start() - 500) if match else 0
    return text[start:].encode('utf-8')[:byte_limit].decode('utf-8', 'ignore'), True


def _excerpt(text, words):
    match = re.search('|'.join(re.escape(word) for word in words), text, re.IGNORECASE) if words else None
    safe = _flat(text)
    position = len(_flat(text[:match.start()])) if match else 0
    start = max(0, position - 65)
    for escaped in re.finditer(r'\\u[0-9a-fA-F]{4}', safe):
        if escaped.start() >= start:
            break
        if escaped.start() < start < escaped.end():
            start = escaped.end()
            break
    value = briefing.clip_context_label(safe[start:], 200)
    value['omitted_chars'] += start
    return value


def _review_text(payload):
    pieces = [payload.get(name) for name in ('summary', 'reason', 'method')]
    for key in ('items', 'resolutions'):
        for item in payload.get(key) or []:
            if isinstance(item, dict):
                pieces.extend(item.get(name) for name in ('title', 'text', 'reason', 'evidence'))
    return '\n'.join(value for value in pieces if isinstance(value, str))


def _checkpoint_text(payload, item_kind):
    items = payload.get('open_items') or []
    if item_kind is not None:
        items = [item for item in items if item.get('kind') == item_kind]
        if not items:
            return None
    pieces = [payload.get(k) for k in ('intent', 'summary', 'next_action')]
    for item in items:
        pieces.extend(item.get(k) for k in ('kind', 'text', 'source'))
    if item_kind is None:
        for item in payload.get('resolved') or []:
            pieces.extend(item.get(k) for k in ('reason', 'evidence'))
    return '\n'.join(value for value in pieces if isinstance(value, str))


def search(rows, project, actor, options, scope, *, operators=(), project_path=None,
           requirement_reader=None, decision_reader=None, clock=time.monotonic, deadline=None):
    """Compute one page after the trusted caller established its read scope.

    ``scope.kinds`` is a trusted own-reader permission set, NEVER a query flag.
    The endpoint currently gives project members the existing shared read set.
    """
    if scope.get('project') != project or project not in scope.get('projects', ()):
        raise ValueError('Project search is unavailable')
    allowed = set(scope.get('kinds') or ()) & set(KINDS)
    selected = allowed & set(options['kinds'])
    if not selected:
        rows = ()
    from requirement_http import is_requirement_row
    coverage = {'kinds': sorted(selected), 'documents': 0, 'rows_examined': 0,
                'comments_examined': 0, 'skipped_malformed': 0, 'unknown_creation_excluded': 0,
                'text_cutoffs': 0, 'page_truncated': False,
                'cutoffs': set(), 'not_searched': list(NOT_SEARCHED)}
    budget = _Budget(coverage, clock, deadline)
    words = capabilities.words(options['phrase'])
    documents, identities, related, row_index, known_ids = [], set(), {}, {}, set()
    first = _instant(options.get('since'))
    last = _instant(options.get('until'), end=True)
    until_date = bool(options.get('until') and re.fullmatch(r'\d{4}-\d{2}-\d{2}', options['until']))

    def add(kind, task, title, body, native_body, *, comment=None, author=None, created=None,
            updated=None, state=None, trust='prose', exact=(), revision=None,
            record_sha256=None, successor=None, replaces=None, linked=(), hint=None,
            hash_basis='native-comment-text', contribution=None, commit=None, operation=None, owner=None):
        budget.check()
        if kind not in selected:
            return
        target = options.get('task')
        if target is not None and task != target and target not in linked:
            return
        if options.get('task_prefix') is not None and not task.startswith(options['task_prefix']):
            return
        row = row_index.get(task) or {}
        if options['state'] == 'open' and row.get('status') == 'closed':
            return
        if options.get('author') is not None and author != options['author']:
            return
        moment = _time(created)
        if first is not None or last is not None:
            if moment is None:
                coverage['unknown_creation_excluded'] += 1
                return
            if first is not None and moment < first:
                return
            if last is not None and (moment >= last if until_date else moment > last):
                return
        if options.get('item_kind') is not None and kind != 'checkpoint':
            return
        identity = (project, kind, task, str(comment or ''), str(revision or ''))
        if identity in identities:
            return
        if len(documents) >= MAX_DOCUMENTS:
            coverage['cutoffs'].add('documents')
            raise _Stop()
        try:
            full_hash = hashlib.sha256(native_body.encode('utf-8')).hexdigest()
            identity_parts = [str(x) for x in exact if x]
            title_clipped = False
            if kind in ('ref', 'cap', 'decision', 'task', 'requirement'):
                rank_title, title_clipped = _window(str(title or ''), words, byte_limit=1024)
                identity_parts.append(rank_title)
            identity_bytes = sum(len(part.encode('utf-8')) for part in identity_parts)
            indexed, clipped = _window(body, words, byte_limit=max(1, MAX_TEXT_BYTES - identity_bytes))
            clipped = clipped or title_clipped
        except (UnicodeError, TypeError):
            coverage['skipped_malformed'] += 1
            return
        budget.check()
        if clipped:
            coverage['text_cutoffs'] += 1
        pointer = ('ref:' + exact[0] if kind == 'ref' else 'cap:' + exact[0] if kind == 'cap'
                   else 'comment:' + str(comment) if comment is not None else 'task:' + task)
        item = {'kind': kind, 'project': project, 'task': task, 'comment': comment,
                'pointer': pointer, 'trust': 'data', 'trust_class': trust,
                'title': _safe(title), 'excerpt': _excerpt(body, words),
                'author': _scalar(author), 'created_at': _scalar(created) if moment else None,
                'updated_at': _scalar(updated) if _time(updated) else None,
                'state': _scalar(state), 'owner': _scalar(owner), 'closed': row.get('status') == 'closed',
                'sha256': full_hash, 'sha256_basis': hash_basis}
        if contribution is not None:
            item['contribution'] = contribution
        if commit is not None:
            item['commit'] = commit
        if operation is not None:
            item['operation'] = operation
        if revision is not None:
            item.update(revision=revision, record_sha256=record_sha256)
        if hint is not None:
            item['ranking_hint'] = hint
        if isinstance(successor, str) and briefing.CONTEXT_POINTER_RE.fullmatch(successor):
            item['superseded_by'] = successor
        if replaces:
            item['replaces'] = [p for p in replaces if isinstance(p, str)
                                and briefing.CONTEXT_POINTER_RE.fullmatch(p)][:3]
        identities.add(identity)
        doc_id = _digest(identity)
        documents.append({'id': doc_id, 'item': item,
                          'indexed': indexed, 'exact': tuple(exact),
                          'at': moment.timestamp() if moment else 0,
                          'bm25': capabilities.bm25_document(doc_id,
                                    identity_parts, [indexed])})

    try:
        # Source rows come only from the single authorized project. Permission is
        # applied before any kind reader, tokenization, hashing or corpus statistics.
        prepared = []
        for original in rows:
            if not isinstance(original, dict):
                budget.check()
                coverage['skipped_malformed'] += 1
                continue
            # Own-kind permission precedes even corpus counters and comment work.
            raw_labels = original.get('labels') or []
            valid_labels = isinstance(raw_labels, (list, tuple)) and all(
                isinstance(label, str) for label in raw_labels)
            labels = {label for label in raw_labels if isinstance(label, str)} \
                if isinstance(raw_labels, (list, tuple)) else set()
            classified = original if valid_labels else dict(original, labels=sorted(labels))
            anchor_label = _anchor_label(classified)
            anchor = anchor_label is not None
            record_kind = ('ref' if anchor_label == 'reference' else
                           'cap' if anchor_label == 'capability' else
                           'requirement' if is_requirement_row(classified) else None)
            if original.get('issue_type') in ('event', 'gate') or is_merge_slot(original):
                continue
            if record_kind is not None and record_kind not in allowed:
                continue
            if record_kind is None and not allowed.intersection(
                    ('task', 'comment', 'checkpoint', 'review', 'decision')):
                continue
            if not isinstance(original.get('id'), str) or not TASK.fullmatch(original['id']):
                budget.check()
                coverage['skipped_malformed'] += 1
                continue
            known_ids.add(original['id'])
            if record_kind is not None and record_kind not in selected:
                continue
            if anchor and record_kind is None:
                continue
            if record_kind is None and not selected.intersection(
                    ('task', 'comment', 'checkpoint', 'review', 'decision')):
                continue
            budget.check()
            if coverage['rows_examined'] >= MAX_ROWS:
                coverage['cutoffs'].add('rows')
                break
            coverage['rows_examined'] += 1
            if original.get('malformed') or not valid_labels:
                coverage['skipped_malformed'] += 1
                continue
            row = dict(original)
            row['comments'] = _comments(original, coverage, budget, allowed, record_kind)
            row['_find_comments_complete'] = row['comments'].complete
            if row['id'] in row_index:
                coverage['skipped_malformed'] += 1
                continue
            row_index[row['id']] = row
            prepared.append(row)
        target = options.get('task')
        if target is not None and target.startswith('.'):
            matches = [key for key in known_ids if key.endswith(target)]
            if len(matches) != 1:
                raise ValueError('Task is not available unambiguously in this project')
            options = dict(options, task=matches[0])
        prepared = _Checked(sorted(prepared, key=lambda row: row['id']), budget)

        for kind, module in (('ref', 'reference_records'), ('cap', 'capability_records')):
            if kind not in selected:
                continue
            reader = __import__(module)
            entries, incomplete = reader.catalog(prepared, operators)
            coverage['skipped_malformed'] += len(incomplete)
            replacements = {}
            for entry in entries:
                record = entry.get('record') or entry.get('proposed')
                if entry.get('state') == 'superseded' and record and record.get('successor'):
                    replacements.setdefault(record['successor'], []).append(kind + ':' + entry['key'])
            for entry in entries:
                budget.check()
                record = entry.get('record') or entry.get('proposed')
                if record is None or entry.get('state') in ('malformed', 'unsupported', 'conflicted'):
                    coverage['skipped_malformed'] += 1
                    continue
                cid = entry.get('record_comment_id') if entry.get('record') else entry.get('proposed_comment_id')
                row = row_index.get(entry['native_id']) or {}
                comment = next((c for c in row.get('comments') or [] if str(c['id']) == str(cid)), {})
                title = record.get('title') if kind == 'ref' else record.get('name')
                text = record.get('statement') if kind == 'ref' else record.get('summary')
                aliases = record.get('aliases') or [] if kind == 'cap' else []
                body = '\n'.join([entry['key'], title or '', text or '',
                                  ' '.join(record.get('tags') or []), ' '.join(aliases)])
                add(kind, entry['native_id'], title, body, comment.get('text') or '', comment=str(cid) if cid else None,
                    author=comment.get('author'), created=comment.get('created_at'),
                    updated=comment.get('updated_at'), owner=record.get('owner'),
                    state=entry['state'], trust='accepted-record' if entry.get('record') else 'draft',
                    exact=(entry['key'],), revision=record.get('revision'), record_sha256=record.get('sha256'),
                    successor=kind + ':' + record['successor'] if record.get('successor') else None,
                    replaces=replacements.get(entry['key']), linked=record.get('decisions') or ())

        if 'requirement' in selected and requirement_reader is None:
            coverage['skipped_malformed'] += sum(
                is_requirement_row(row) for row in prepared)
        if 'requirement' in selected and requirement_reader is not None:
            block = requirement_reader(prepared, budget.check)
            for item in block.get('items') or []:
                budget.check()
                if item.get('unreadable'):
                    coverage['skipped_malformed'] += 1
                    continue
                from reserved_comments import parse_requirement_record
                revision_sources = {}
                for comment in (row_index.get(item['id']) or {}).get('comments') or []:
                    checked = parse_requirement_record(comment.get('text'))
                    if checked is not None and checked.get('id') == item['id']:
                        revision_sources[(checked['revision'], checked['sha256'])] = comment
                seen_revisions = set()
                for record in (item.get('accepted'), item.get('current')):
                    if not record or record.get('revision') in seen_revisions:
                        continue
                    seen_revisions.add(record.get('revision'))
                    source = revision_sources.get((record.get('revision'), record.get('sha256'))) or {}
                    native_body = source.get('text') or _bytes(record).decode('utf-8').rstrip('\n')
                    add('requirement', item['id'], record.get('title'),
                        '\n'.join(str(record.get(k) or '') for k in ('key', 'title', 'description')),
                        native_body, comment=str(source['id']) if source.get('id') is not None else None,
                        author=source.get('author') or item.get('pending_author'),
                        created=source.get('created_at'), updated=source.get('updated_at'),
                        state=item.get('requirement_state') or record.get('acceptance_state'),
                        trust='accepted-record' if record == item.get('accepted') else 'draft',
                        exact=(item['id'], record.get('key') or ''), revision=record.get('revision'),
                        record_sha256=record.get('sha256'), hash_basis='native-comment-text' if source else 'canonical-requirement-revision',
                        successor='task:' + item['state']['superseded_by']['id']
                        if (item.get('state') or {}).get('superseded_by') else None,
                        replaces=['task:' + old['id'] for old in item.get('supersedes') or []])

        import decision_attest
        import review_workflow
        for row in prepared:
            task = row['id']
            anchor = _anchor_label(row) is not None
            if row.get('issue_type') in ('event', 'gate') or is_merge_slot(row) \
                    or anchor or (carries_record_label(row) and not row.get('_find_comments_complete', True)):
                continue
            # Requirements are available only through their own revision reader.
            if is_requirement_row(row):
                continue
            comments = row.get('comments') or []
            selected_pointers = []
            # Reuse only prefix classifications from this request, never reader
            # authority. Damaged identities still go through the unknown path.
            read_kinds = row['comments'].read_kinds
            incomplete = not row.get('_find_comments_complete', True)
            if ('checkpoint' in read_kinds or incomplete) and (
                    'checkpoint' in selected or ('task' in selected and 'checkpoint' in allowed)):
                try:
                    state = briefing.checkpoint_state(row, normalize=False)
                    if state['invalid'] or not row.get('_find_comments_complete', True):
                        raise ValueError('Unreadable checkpoint')
                    if state['current']:
                        payload, comment = state['current']
                        selected_pointers.append(('checkpoint', str(comment['id']), 'Latest valid checkpoint'))
                        body = _checkpoint_text(payload, options.get('item_kind'))
                        if body is not None:
                            add('checkpoint', task, 'Checkpoint on ' + task, body, comment['text'],
                                comment=str(comment['id']), author=comment.get('author'),
                                created=comment.get('created_at'), updated=comment.get('updated_at'),
                                state=row.get('status'),
                                trust='checkpoint-closed' if row.get('status') == 'closed' else 'checkpoint-open')
                except (ValueError, TypeError, KeyError):
                    coverage['skipped_malformed'] += 1
            if (read_kinds.intersection(('review', 'shared-void')) or incomplete) and (
                    'review' in selected or ('task' in selected and 'review' in allowed)):
                try:
                    if not row.get('_find_comments_complete', True):
                        raise ValueError('Incomplete review source')
                    ledger = review_workflow.history(row, operators, project_path)
                    projected = review_workflow.projection(*ledger, closed=row.get('status') == 'closed',
                                                          assignee=row.get('assignee'))
                    contribution = projected.get('contribution')
                    if contribution:
                        selected_pointers.append(('contribution', contribution['comment_id'], 'Latest contribution'))
                    latest = projected.get('latest_comment_id')
                    if latest and (not contribution or latest != contribution['comment_id']):
                        selected_pointers.append(('review', latest, 'Latest valid review'))
                    commits = {str(c['id']): p['commit'] for p, c in ledger[0]
                               if p['operation'] == 'contribute'}
                    successors = {p['supersedes']: str(c['id']) for p, c in ledger[0]
                                  if p['operation'] == 'contribute' and p.get('supersedes')}
                    current = None
                    for payload, comment in ledger[0]:
                        cid = str(comment['id'])
                        if payload['operation'] == 'contribute':
                            current = cid
                        target = payload.get('contribution') or current
                        successor = successors.get(cid)
                        add('review', task, 'Review on ' + task, _review_text(payload), comment['text'],
                            comment=cid, author=comment.get('author'), created=comment.get('created_at'),
                            updated=comment.get('updated_at'),
                            state='superseded' if successor else payload['operation'], trust='review',
                            contribution=target, commit=payload.get('commit') or commits.get(target),
                            operation=payload['operation'],
                            successor='comment:' + successor if successor else None,
                            replaces=['comment:' + payload['supersedes']] if payload.get('supersedes') else None)
                except (ValueError, TypeError, KeyError):
                    coverage['skipped_malformed'] += 1
            related[task] = [{'kind': kind, 'relation': 'related-context', 'pointer': 'comment:' + str(cid),
                              'title': _safe(title), 'trust': 'data'}
                             for kind, cid, title in selected_pointers[:3] if ID.fullmatch(str(cid))]
            decision = decision_attest.is_decision(row)
            if decision and 'decision' in selected:
                try:
                    view = decision_reader(row) if decision_reader else None
                    if view is None:
                        coverage['skipped_malformed'] += 1
                    else:
                        source = {'title': view.get('title'), 'description': view.get('description')}
                        add('decision', task, view.get('title'), view.get('description') or '',
                            _bytes(source).decode('utf-8').rstrip('\n'), author=row.get('created_by'),
                            created=row.get('created_at'), updated=row.get('updated_at'), state=view.get('attestation'),
                            trust='decision', exact=(task,), hash_basis='canonical-native-task-text-fields')
                except (ValueError, TypeError, KeyError, decision_attest.AttestRefusal):
                    coverage['skipped_malformed'] += 1
            if not decision:
                source = {key: row.get(key) for key in ('title', 'description', 'acceptance_criteria')}
                add('task', task, row.get('title'),
                    '\n'.join(str(row.get(k) or '') for k in ('description', 'acceptance_criteria')),
                    _bytes(source).decode('utf-8').rstrip('\n'), author=row.get('created_by'),
                    created=row.get('created_at'), updated=row.get('updated_at'), state=row.get('status'),
                    exact=(task,), hash_basis='canonical-native-task-text-fields')
            if 'comment' in selected:
                for comment in comments:
                    budget.check()
                    body = comment.get('text')
                    if row['comments'].comment_kinds[str(comment['id'])] != 'comment':
                        continue
                    first_line = next((line.strip() for line in body.splitlines() if line.strip()), '')
                    marker = first_line.startswith(('Decision:', 'Kind: decision'))
                    add('comment', task, 'Comment on ' + task, body, body, comment=str(comment['id']),
                        author=comment.get('author'), created=comment.get('created_at'), updated=comment.get('updated_at'),
                        state=row.get('status'), hint='decision-like' if marker else None)
    except _Stop:
        pass

    # No hidden raw row, source HEAD or native record count contributes to this watermark.
    generation = _digest([{'id': doc['id'], 'item': doc['item'],
                           'related': related.get(doc['item']['task'], [])
                           if doc['item']['kind'] == 'task' else []}
                          for doc in sorted(documents, key=lambda d: d['id'])])
    scope_hash = _digest(scope)
    query = {key: value for key, value in options.items() if key not in ('cursor', 'json', 'phrase')}
    query['phrase'] = ' '.join(words)
    query_hash = _digest(query)
    binding = {'v': 1, 'kind': 'find', 'project': project, 'query': query_hash, 'scope': scope_hash,
               'generation': generation, 'ranking': RANK_VERSION}
    position = 0
    if options.get('cursor'):
        previous = _untoken(options['cursor'])
        if any(previous[key] != value for key, value in binding.items()):
            raise ValueError(RESTART)
        position = previous['position']
    partial = bool(coverage['cutoffs'] or coverage['skipped_malformed'] or coverage['text_cutoffs'])
    try:
        scores = capabilities.bm25_scores([doc['bm25'] for doc in documents], words,
                                          check_budget=budget.check)
    except _Stop:
        scores, partial = {}, True
    # A deterministic text window can be paged while still reporting partial
    # recall. Interrupted extraction/scoring and unreadable records cannot.
    continuation_safe = not coverage['cutoffs'] and not coverage['skipped_malformed']
    exact_phrase = ' '.join(words)
    matching = []
    for doc in documents:
        exact = any(' '.join(capabilities.words(value)) == exact_phrase for value in doc['exact'] if value)
        score = scores.get(doc['id'], 0.0)
        if exact or score > 0:
            matching.append((doc, exact, score))
    matching.sort(key=lambda entry: (-int(entry[1]), -entry[2],
                                    TRUST_ORDER[entry[0]['item']['trust_class']],
                                    -int(entry[0]['item'].get('ranking_hint') == 'decision-like'),
                                    -entry[0]['at'], entry[0]['id']))
    if position > len(matching):
        raise ValueError(RESTART)
    coverage.update(documents=len(documents), truncated=partial or bool(coverage['text_cutoffs']),
                    cutoffs=sorted(coverage['cutoffs']))
    warnings = []
    if partial:
        warnings.append('Only a bounded part of the permitted corpus was searched; totals are a lower bound.')
    if coverage['text_cutoffs']:
        warnings.append('Long documents were indexed through a bounded query-centred window.')
    result = {'schema': 'find-v1', 'project': project, 'phrase': _scalar(options['phrase']),
              'total': len(matching), 'total_is_lower_bound': partial, 'cursor': None,
              'items': [], 'trust': 'data', 'coverage': coverage, 'warnings': warnings,
              'watermark': {'generation': generation, 'computed_at': datetime.now(timezone.utc).isoformat()},
              'stale': 'partial' if partial else 'current',
              'bounds': {'documents': MAX_DOCUMENTS, 'rows': MAX_ROWS, 'comments': MAX_COMMENTS,
                         'text_bytes_per_document': MAX_TEXT_BYTES, 'items': MAX_ITEMS,
                         'excerpt_characters': 200, 'answer_bytes': MAX_ANSWER_BYTES,
                         'computation_seconds': COMPUTE_SECONDS, 'budget_kind': 'cooperative',
                         'native_read_seconds': 120, 'native_read_limit_kind': 'per-command',
                         'native_head_seconds': 30, 'cursor_bytes': 4096,
                         'query_characters': 200, 'query_utf8_bytes': 800,
                         'metadata_characters': 200, 'pointer_value_characters': 200}}
    for index in range(position, min(len(matching), position + options['limit'])):
        doc, exact, score = matching[index]
        item = dict(doc['item'], rank=index + 1, exact=exact, score=round(score, 8))
        if item['kind'] == 'task' and related.get(item['task']):
            item['related_context'] = related[item['task']]
        candidate = dict(result, items=result['items'] + [item])
        candidate['cursor'] = (_token(dict(binding, position=index + 1))
                               if continuation_safe and index + 1 < len(matching) else None)
        if len(_bytes(candidate)) > MAX_ANSWER_BYTES:
            # These flags already occupy space in every candidate; changing
            # false to true cannot push the previously fitting page over its cap.
            result['coverage'] = dict(result['coverage'], truncated=True, page_truncated=True)
            break
        result = candidate
    if len(_bytes(result)) > MAX_ANSWER_BYTES:
        raise ValueError('Find answer bounds could not be met')
    return result


def format_text(answer):
    lines = ['Find %s: %d matches; stale=%s; lower_bound=%s' %
             (answer['phrase'], answer['total'], answer['stale'], str(answer['total_is_lower_bound']).lower())]
    for item in answer['items']:
        lines.append('%d. %s %s [%s] trust=data' % (item['rank'], item['kind'], item['pointer'], item['state']))
        lines.append('   ' + item['title']['text'])
        lines.append('   ' + item['excerpt']['text'])
        lines.append('   project=%s task=%s author=%s created=%s updated=%s owner=%s sha256=%s' %
                     (item['project'], item['task'], item['author'], item['created_at'],
                      item['updated_at'], item['owner'], item['sha256']))
        for related in item.get('related_context') or []:
            lines.append('   related-context %s %s' % (related['kind'], related['pointer']))
    lines.extend('Warning: ' + warning for warning in answer['warnings'])
    lines.append('Coverage: %d documents; truncated=%s; not searched: %s' %
                 (answer['coverage']['documents'], str(answer['coverage']['truncated']).lower(),
                  ', '.join(answer['coverage']['not_searched'])))
    lines.append('Bounds: ' + json.dumps(answer['bounds'], sort_keys=True))
    lines.append('Cursor: ' + (answer['cursor'] or '-'))
    return '\n'.join(lines) + '\n'
