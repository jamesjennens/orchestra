"""Open-item records for tests and for a read-cost run on real bd (kittrial-5bb.127).

No kit writes these records yet (the writer is a later slice), so every record here is
built by hand from the field tables of docs/OPEN_ITEMS_DECISIONS_DESIGN.md (4.1
open-item-v1, 4.2 item-resolution-v1, 4.3 owner-answer-v1, 11.2.1 the `.owner-answers`
entry) and checked with the reader's own parsers before it is used.

Two sinks write the same scenario:

* `MemorySink` builds the rows `bd show --include-comments` would return; the unit
  tests and the mock read-cost measurement use it.
* `BdSink` writes them with raw `bd` calls on a scratch runtime, through
  `admin.run_bd`, so the reserved-prefix guard of the endpoint is bypassed exactly as a
  later writer slice will bypass it on the host. It refuses to run without --scratch.

Real bd, on the coordination host of a SCRATCH runtime only:

    python3 tools/open_items_fixture.py --root /srv/scratch-runtime --project trial \\
        --actor OPERATOR --items 300 --questions 100 --task trial-1 --scratch

OPERATOR should be on that runtime's operator allowlist, so host-route records read
attested. It prints one JSON summary (ids written, counts by expected state, seconds).
"""
import argparse
import json
import sys
import time
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import open_items as oi                                              # noqa: E402
from requirements import canonical_bytes, content_hash               # noqa: E402

STAMP = '2026-10-06T09:00:00Z'


def block(actor, route='host', person=None):
    if route == 'endpoint':
        return {'actor': actor, 'route': 'endpoint', 'identity': 'unverified', 'person': None}
    return {'actor': actor, 'route': route, 'identity': 'verified', 'person': person or 'operator:' + actor}


def sealed(record):
    record = dict(record)
    record['sha256'] = content_hash(record)
    return record


def body(prefix, record):
    return prefix + canonical_bytes(record).decode('utf-8')


def item_record(anchor, revision=1, kind='blocker', text='An open item', source='fixture', owner='person:james',
                task=None, for_=None, options=None, recommended=None, due_by=None, state='open', state_note=None,
                resolved_by=None, submitted_by=None, at=STAMP):
    return sealed({'schema_version': 1, 'id': anchor, 'revision': revision, 'kind': kind, 'text': text,
                   'source': source, 'owner': owner, 'task': task, 'for': for_, 'options': options,
                   'recommended': recommended, 'due_by': due_by, 'state': state, 'state_note': state_note,
                   'resolved_by': resolved_by, 'provenance': {'kind': 'new'},
                   'submitted_by': submitted_by or block('coordinator', 'endpoint'), 'at': at})


def resolution_record(anchor, revision, disposition='resolved', answer=None, evidence=None, by=None,
                      serial=0, reason='Resolved', at=STAMP):
    return sealed({'schema_version': 1, 'resolution': 'r-%012x' % serial, 'item': anchor, 'revision': revision,
                   'disposition': disposition, 'reason': reason,
                   'evidence': evidence or ('answer:%s' % answer if answer else 'task:%s' % anchor),
                   'answer': answer, 'by': by or block('coordinator', 'endpoint'), 'at': at})


def answer_record(question, comment_id_unused=None, authority='relayed', option=None, words='Keep it.', by=None,
                  owner=None, serial=0, at=STAMP):
    item = question
    by = by or block('coordinator')
    return sealed({'schema_version': 1, 'answer': 'a-%012x' % serial, 'item': item['id'],
                   'question_revision': item['revision'], 'question_sha256': item['sha256'],
                   'owner': owner or item['for'], 'option': option, 'options_offered': item['options'],
                   'words': words, 'authority': authority,
                   'relayed_by': None if authority == 'owner' else by, 'by': by, 'at': at})


def journal_entry(answer, comment_id):
    return {'schema_version': 1, 'kind': oi.ENTRY_ANSWER, 'item': answer['item'],
            'revision': answer['question_revision'], 'owner': answer['owner'], 'option': answer['option'],
            'words': answer['words'], 'comment_id': str(comment_id), 'payload': answer, 'sha256': answer['sha256']}


class MemorySink:
    """Rows as `bd show --include-comments --json` returns them, plus the journal."""

    def __init__(self, prefix='kit'):
        self.prefix, self.rows, self.journal, self.next_row, self.next_comment = prefix, {}, {}, 1, 1

    def create(self, title, labels):
        rid = '%s-%d' % (self.prefix, self.next_row)
        self.next_row += 1
        self.rows[rid] = {'id': rid, 'title': title, 'status': 'open', 'issue_type': 'task',
                          'labels': sorted(labels), 'comments': []}
        return rid

    def comment(self, rid, text, author):
        cid = str(self.next_comment)
        self.next_comment += 1
        self.rows[rid]['comments'].append({'id': cid, 'text': text, 'author': author,
                                           'created_at': '2026-10-06T10:%02d:%02dZ' % divmod(int(cid) % 3600, 60)})
        return cid

    def relabel(self, rid, labels):
        self.rows[rid]['labels'] = sorted(labels)

    def close(self, rid):
        self.rows[rid]['status'] = 'closed'

    def write_entry(self, entry):
        self.journal[entry['sha256']] = entry

    def all_rows(self):
        return [self.rows[key] for key in sorted(self.rows)]


class BdSink:
    """Raw bd writes on a scratch runtime (no endpoint, so no reserved-prefix guard)."""

    def __init__(self, root, project, actor):
        import admin
        self.admin, self.root, self.project, self.actor = admin, Path(root), project, actor
        self.journal = self.admin.project_dir(self.root, project) / oi.OWNER_ANSWERS_JOURNAL

    def run(self, argv, actor=None):
        return self.admin.run_bd(self.root, self.project, ['--actor', actor or self.actor, *argv])

    def create(self, title, labels):
        reply = json.loads(self.run(['create', '--title', title[:60], '--type', 'task', '--no-inherit-labels',
                                     '--labels', ','.join(sorted(labels)), '--json']))
        reply = reply[0] if isinstance(reply, list) else reply
        return reply['id']

    def comment(self, rid, text, author):
        reply = json.loads(self.run(['comments', 'add', rid, text, '--json'], actor=author))
        reply = reply[0] if isinstance(reply, list) else reply
        return str(reply['id'])

    def relabel(self, rid, labels):
        self.run(['update', rid, '--set-labels', ','.join(sorted(labels)), '--json'])

    def close(self, rid):
        self.run(['close', rid, '--reason', 'open item anchor (not a work item)', '--json'])

    def write_entry(self, entry):
        self.journal.mkdir(exist_ok=True)
        (self.journal / (entry['sha256'] + '.json')).write_text(json.dumps(entry), encoding='utf-8')


def build(sink, items=10, questions=5, operator='operator', task=None, owner='person:james'):
    """A deterministic mix: per 10 items 6 open, 2 blocked, 2 resolved; per 5 questions
    2 open, 1 closed by the owner, 1 closed relayed, 1 answered without a journal entry
    (which reads open). Returns the expected effective state per anchor."""
    expected = {}
    serial = 0
    for index in range(items):
        state = ('open', 'open', 'open', 'open', 'open', 'open', 'blocked', 'blocked', 'resolved', 'resolved')[index % 10]
        rid = sink.create('Item %d' % index, {oi.FAMILY_LABEL, oi.STATE_LABEL_PREFIX + 'open'})
        record = item_record(rid, text='Item %d: something to follow up' % index, task=task, owner=owner,
                             submitted_by=block(operator), due_by='2026-10-%02d' % (1 + index % 28))
        sink.comment(rid, body(oi.OPEN_ITEM_PREFIX, record), operator)
        if state == 'blocked':
            record = item_record(rid, revision=2, text=record['text'], task=task, owner=owner,
                                 submitted_by=block(operator), due_by=record['due_by'], state='blocked',
                                 state_note='Waiting on the release')
            sink.comment(rid, body(oi.OPEN_ITEM_PREFIX, record), operator)
        elif state == 'resolved':
            serial += 1
            resolution = resolution_record(rid, 1, by=block(operator), serial=serial)
            rcid = sink.comment(rid, body(oi.ITEM_RESOLUTION_PREFIX, resolution), operator)
            record = item_record(rid, revision=2, text=record['text'], task=task, owner=owner,
                                 submitted_by=block(operator), due_by=record['due_by'], state='resolved',
                                 resolved_by=rcid)
            sink.comment(rid, body(oi.OPEN_ITEM_PREFIX, record), operator)
        sink.relabel(rid, {oi.FAMILY_LABEL, oi.STATE_LABEL_PREFIX + state})
        sink.close(rid)
        expected[rid] = (state, None)
    options = [{'id': 'keep', 'text': 'Keep it'}, {'id': 'drop', 'text': 'Drop it'}]
    for index in range(questions):
        shape = ('open', 'open', 'owner', 'relayed', 'unjournaled')[index % 5]
        rid = sink.create('Question %d' % index, {oi.FAMILY_LABEL, oi.STATE_LABEL_PREFIX + 'open'})
        question = item_record(rid, kind='question', text='Question %d: keep the old path?' % index, task=task,
                               owner=owner, for_=owner, options=options, recommended='keep',
                               submitted_by=block(operator), due_by='2026-11-%02d' % (1 + index % 28))
        sink.comment(rid, body(oi.OPEN_ITEM_PREFIX, question), operator)
        state, closed_by = 'open', None
        if shape != 'open':
            serial += 1
            authority = 'owner' if shape == 'owner' else 'relayed'
            by = block(operator, person=owner if authority == 'owner' else None)
            answer = answer_record(question, authority=authority, option='keep', by=by, serial=serial,
                                   words='Keep it, for now (%d).' % index)
            acid = sink.comment(rid, body(oi.OWNER_ANSWER_PREFIX, answer), operator)
            if shape != 'unjournaled':
                sink.write_entry(journal_entry(answer, acid))
                state, closed_by = 'resolved', authority
            resolution = resolution_record(rid, 1, answer=acid, by=by, serial=serial)
            rcid = sink.comment(rid, body(oi.ITEM_RESOLUTION_PREFIX, resolution), operator)
            closed = item_record(rid, revision=2, kind='question', text=question['text'], task=task, owner=owner,
                                 for_=owner, options=options, recommended='keep', submitted_by=block(operator),
                                 due_by=question['due_by'], state='resolved', resolved_by=rcid)
            sink.comment(rid, body(oi.OPEN_ITEM_PREFIX, closed), operator)
            sink.relabel(rid, {oi.FAMILY_LABEL, oi.STATE_LABEL_PREFIX + 'resolved'})
        sink.close(rid)
        expected[rid] = (state, closed_by)
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--actor', required=True, help='an operator on the scratch runtime allowlist')
    parser.add_argument('--items', type=int, default=300)
    parser.add_argument('--questions', type=int, default=100)
    parser.add_argument('--task', default=None, help='a task the items are about, for the brief kinds')
    parser.add_argument('--owner', default='person:james')
    parser.add_argument('--scratch', action='store_true', help='required: this writes raw records')
    args = parser.parse_args(argv)
    if not args.scratch:
        parser.error('this writes raw open-item records past the endpoint guard; run it only on a scratch '
                     'runtime, and say so with --scratch')
    started = time.monotonic()
    expected = build(BdSink(args.root, args.project, args.actor), args.items, args.questions, args.actor,
                     args.task, args.owner)
    counts = {}
    for state, closed_by in expected.values():
        key = state if closed_by is None else '%s/%s' % (state, closed_by)
        counts[key] = counts.get(key, 0) + 1
    print(json.dumps({'anchors': len(expected), 'expected': counts, 'seconds': round(time.monotonic() - started, 1),
                      'read_with': ['items list --limit 100', 'questions --for %s' % args.owner,
                                    'brief %s' % args.task if args.task else 'brief TASK']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
