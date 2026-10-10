"""Resolve explicit requirement revisions from one native Beads JSONL export."""
import argparse
import json
import record_json
import sys
from pathlib import Path

from requirements import canonical_bytes, content_hash, load_json, validate_manifest

REVISION_PREFIX = 'Kind: requirement-revision-v1\n'
# The durable F3 acceptance evidence an operator writes beside a revision. It is
# a separate reserved machine record so raw `comments add` cannot forge it, and
# so a restore keeps the acceptance decision even when the local journal is gone.
ACCEPTANCE_PREFIX = 'Kind: requirement-acceptance-v1\n'
META = ('schema_version', 'baseline', 'state', 'canonical_project', 'job',
        'authority', 'hash_convention')


def parse_json(text):
    value = record_json.loads(text, object_pairs_hook=_unique)
    canonical_bytes(value)
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field: ' + key)
        result[key] = value
    return result


def _unreadable_identity(text):
    """Prove one top-level string ID without decoding an over-limit row.

    The native marker's regex may match a nested ID or the first of two IDs.
    An offline export has no native membership query to settle that ambiguity.
    Scan strings and brackets iteratively; incomplete framing or another ID
    refuses the export rather than hiding a potentially selected record.
    """
    stack, ids = [], []
    index = 0
    if not text.strip().startswith('{'):
        raise ValueError('Unreadable export row has no provable issue ID')
    while index < len(text):
        char = text[index]
        if char == '"':
            start = index; index += 1
            while index < len(text):
                if text[index] == '\\':
                    index += 2
                elif text[index] == '"':
                    index += 1; break
                else:
                    index += 1
            else:
                raise ValueError('Unreadable export row has incomplete string framing')
            value = json.loads(text[start:index])
            after = index
            while after < len(text) and text[after].isspace():
                after += 1
            if stack == ['{'] and value == 'id' and text[after:after+1] == ':':
                after += 1
                while after < len(text) and text[after].isspace():
                    after += 1
                if text[after:after+1] != '"':
                    raise ValueError('Unreadable export row has no provable string issue ID')
                try:
                    identity, _ = json.JSONDecoder().raw_decode(text, after)
                except (ValueError, RecursionError):
                    raise ValueError('Unreadable export row has no provable issue ID') from None
                ids.append(identity)
            continue
        if char in '[{':
            stack.append(char)
        elif char in ']}':
            if not stack or stack.pop() != ( '{' if char == '}' else '[' ):
                raise ValueError('Unreadable export row has incomplete container framing')
            if not stack and text[index+1:].strip():
                raise ValueError('Unreadable export row has trailing data')
        index += 1
    if stack or len(ids) != 1 or not isinstance(ids[0], str) or not ids[0].strip():
        raise ValueError('Unreadable export row has missing, duplicate or unprovable issue ID')
    return ids[0]


def read_export(path):
    """One filesystem read; no database queries or implicit refresh."""
    text = Path(path).read_text(encoding='utf-8-sig')
    rows = []
    for line in text.split('\n'):
        if not line.strip():
            continue
        row = record_json.loads_row(line)
        if isinstance(row, dict) and row.get('malformed'):
            if record_json.nesting(line, record_json.ROW_NESTING_MAX) <= record_json.ROW_NESTING_MAX:
                # Preserve the offline export's syntax/duplicate-field refusal.
                # A parser recursion failure is the other native unreadable case.
                try:
                    json.loads(line, object_pairs_hook=_unique)
                except RecursionError:
                    pass
            row['id'] = _unreadable_identity(line)
        else:
            row = json.loads(line, object_pairs_hook=_unique)
        rows.append(row)
    return rows


def exact_reference(value):
    if not isinstance(value, dict) or set(value) != {'id', 'revision', 'sha256'}:
        raise ValueError('expected exact id/revision/sha256 reference')
    if not isinstance(value['id'], str) or not value['id'].strip():
        raise ValueError('reference id must be nonempty')
    if type(value['revision']) is not int or value['revision'] < 1:
        raise ValueError('reference revision must be a positive integer')
    digest = value['sha256']
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('reference hash must be lowercase SHA-256')
    return value['id'], value['revision'], digest


def reference(record):
    return {key: record[key] for key in ('id', 'revision', 'sha256')}


def check_history(previous, current):
    """Call only with validated manifests. A revision never acquires new content."""
    if (previous['canonical_project'], previous['job']) != (current['canonical_project'], current['job']):
        raise ValueError('history belongs to another project or job')
    old = {r['id']: r for r in previous['narrative'] + previous['requirements']}
    for record in current['narrative'] + current['requirements']:
        prior = old.get(record['id'])
        if prior is None:
            continue
        if record['revision'] < prior['revision']:
            raise ValueError('revision regression: ' + record['id'])
        if record['revision'] == prior['revision'] and record['sha256'] != prior['sha256']:
            raise ValueError('same revision has different content: ' + record['id'])


def selection_from_manifest(manifest, context_ids=(), blocking_ids=()):
    result = {key: manifest[key] for key in META}
    for group in ('narrative', 'requirements'):
        result[group] = [reference(r) for r in manifest[group]]
    result['context_ids'] = list(context_ids)
    result['blocking_ids'] = list(blocking_ids)
    return result


def revision_comment(record):
    exact_reference(reference(record))
    if content_hash(record) != record['sha256']:
        raise ValueError('revision content hash mismatch')
    return REVISION_PREFIX + canonical_bytes(record).decode('utf-8')


def adapt(rows, selection, acceptance=None, previous=None, previous_acceptance=None, governance=None):
    expected = set(META) | {'narrative', 'requirements', 'context_ids', 'blocking_ids'}
    if not isinstance(selection, dict) or set(selection) != expected:
        raise ValueError('selection has missing or unknown fields')
    issues, revisions, locations = {}, {}, {}
    terminal_states = {}
    if not isinstance(rows, list):
        raise ValueError('export must be a list of issues')
    # Malformed content costs only its explicitly selected row. Identity and
    # duplicate-id validation still covers the entire export.
    selected_rows = set()
    for group in ('narrative', 'requirements'):
        if not isinstance(selection[group], list):
            raise ValueError(group + ' selection must be a list')
        selected_rows.update(exact_reference(ref)[0] for ref in selection[group])
    if previous is not None:
        selected_rows.update(r['id'] for r in previous['narrative'] + previous['requirements'])
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
            raise ValueError('export row must have an issue id')
        if row['id'] in issues:
            raise ValueError('duplicate issue id: ' + row['id'])
        issues[row['id']] = row
        if row['id'] not in selected_rows:
            continue
        if row.get('malformed'):
            raise ValueError('selected requirement row cannot be read: ' + row['id'])
        comments = row.get('comments', [])
        if comments is None:
            comments = []
        if not isinstance(comments, list):
            raise ValueError('comments must be a list')
        seen = set()
        for comment in comments:
            if not isinstance(comment, dict) or not isinstance(comment.get('text', ''), str):
                raise ValueError('malformed comment')
            text = comment.get('text', '')
            if not text.startswith(REVISION_PREFIX):
                continue
            cid = comment.get('id')
            if type(cid) not in (str, int) or not str(cid) or str(cid) in seen:
                raise ValueError('missing/duplicate revision comment id')
            seen.add(str(cid))
            record = parse_json(text[len(REVISION_PREFIX):])
            if not isinstance(record, dict) or not {'id', 'revision', 'sha256'} <= set(record):
                raise ValueError('malformed revision record')
            identity = exact_reference(reference(record))
            if identity[0] != row['id'] or content_hash(record) != identity[2]:
                raise ValueError('revision issue identity or hash mismatch')
            key = identity[:2]
            if key in revisions and revisions[key] != record:
                raise ValueError('conflicting content for one id/revision: ' + row['id'])
            revisions[key] = record
            locations.setdefault(key, []).append(str(cid))
    selected_ids, sources = [], []
    manifest = {key: selection[key] for key in META}
    for group in ('narrative', 'requirements'):
        if not isinstance(selection[group], list):
            raise ValueError(group + ' selection must be a list')
        manifest[group] = []
        for ref in selection[group]:
            rid, rev, digest = exact_reference(ref)
            record = revisions.get((rid, rev))
            if record is None or record['sha256'] != digest:
                raise ValueError('selected revision missing or mismatched: ' + rid)
            # Owner evidence is carried in source_issues with its exact native
            # author. A claimed acceptance cannot be published on a label alone.
            from requirement_records import resolved_acceptance
            accepted = resolved_acceptance(issues[rid], record)
            if record['acceptance_state'] == 'accepted' and not accepted:
                raise ValueError('selected acceptance evidence missing or mismatched: ' + rid)
            from requirement_owner_records import existing_acceptances as owner_acceptances
            owner = owner_acceptances(issues[rid]).get(rev)
            if owner is not None:
                if governance is None:
                    raise ValueError('owner acceptance export needs its captured requirements governance snapshot')
                from requirement_governance import validate_evidence_files
                validate_evidence_files(governance, selection['canonical_project'], owner)
            manifest[group].append(record)
            selected_ids.append(rid)
            sources.append({'reference': ref, 'comment_ids': sorted(locations[(rid, rev)])})
            from requirement_owner_records import state_ledger
            terminal, explanation = state_ledger(issues[rid])
            if terminal is not None:
                if governance is None:
                    raise ValueError('Terminal requirement export needs its exact governance history')
                from requirement_governance import validate_evidence_files
                validate_evidence_files(governance, selection['canonical_project'], terminal)
                terminal_states[rid] = {'state': terminal, 'reason': explanation}
    for field in ('context_ids', 'blocking_ids'):
        values = selection[field]
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values) or len(set(values)) != len(values):
            raise ValueError(field + ' must contain unique issue IDs')
        if any(v not in issues for v in values):
            raise ValueError(field + ' contains an unknown issue')
    if selection['state'] == 'accepted' and selection['blocking_ids']:
        raise ValueError('accepted selection has unresolved explicit blockers')
    manifest['sha256'] = content_hash(manifest)
    validate_manifest(manifest, acceptance)
    if previous is not None:
        validate_manifest(previous, previous_acceptance)
        check_history(previous, manifest)
        for prior in previous['narrative'] + previous['requirements']:
            exported = revisions.get((prior['id'], prior['revision']))
            if exported is not None and exported != prior:
                raise ValueError('export rewrites a known historical revision: ' + prior['id'])
    included = sorted(set(selected_ids + selection['context_ids'] + selection['blocking_ids']))
    provenance = {'schema_version': 1, 'manifest_sha256': manifest['sha256'],
                  'export_sha256': content_hash({'issues': rows}),
                  'selection': selection, 'sources': sources,
                  'source_issues': [issues[rid] for rid in included]}
    if terminal_states:
        provenance['requirement_terminal_states'] = terminal_states
    if governance is not None:
        from requirement_governance import validate_files, FILE, SOURCE_FILE
        validate_files(governance, selection['canonical_project'])
        provenance['requirements_governance'] = {key: governance[key] for key in (FILE, SOURCE_FILE)
                                               if key in governance}
    return {'manifest': manifest, 'provenance': provenance}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', required=True)
    parser.add_argument('--selection', required=True)
    parser.add_argument('--acceptance')
    parser.add_argument('--previous')
    parser.add_argument('--previous-acceptance')
    parser.add_argument('--governance', help='captured governance sidecar files map for owner acceptance')
    parser.add_argument('--publish', help='also publish the reconstructed manifest to this directory')
    args = parser.parse_args(argv)
    try:
        result = adapt(read_export(args.export), load_json(args.selection),
                       load_json(args.acceptance) if args.acceptance else None,
                       load_json(args.previous) if args.previous else None,
                       load_json(args.previous_acceptance) if args.previous_acceptance else None,
                       load_json(args.governance) if args.governance else None)
        if args.publish:
            from publish_brd import publish
            result['publication'] = str(publish(result['manifest'], args.publish,
                                               load_json(args.acceptance) if args.acceptance else None))
        print(canonical_bytes(result).decode('utf-8'))
    except (ValueError, OSError) as exc:
        print('export adaptation failed: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
