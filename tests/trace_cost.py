"""kittrial-5bb.425: the trace read's cost on a synthetic kittrial-sized export (TRACEABILITY_DESIGN §5.4).

Not a test (no test_ prefix): run it by hand from tests/.

    python trace_cost.py [--issues 3400] [--comments 7400] [--requirements 40] [--designs 20]

It builds the export with the kit's own writers (requirement and reference records through the test
natives, so every record is exactly what a real writer leaves), then times, in a fresh interpreter for each
view, the first answer (imports and the snapshot included) and a second answer over a new snapshot. It also
times `requirement_impact.analyze` alone and the row digest the endpoint uses when the tracker has no HEAD.
Synthetic data only.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))


def document(scope, number, keys):
    lines = ['<!-- design-v1', 'scope: %s' % scope, 'status: selected',
             'requirements: [%s]' % ', '.join('%s@2' % key for key in keys[:3]), '-->', '# Design %d' % number, '']
    for section in range(25):
        key = keys[(number + section) % len(keys)]
        lines += ['## Section %d' % section, 'Covers req:%s@2 and design:%s#section-%d here.' % (key, scope, section), '']
    lines += ['## Plan', '']
    for phase in range(1, 6):
        lines += ['## Plan phase %d' % phase, '- Contract: phase %d' % phase,
                  '- Requirements: req:%s@2' % keys[(number + phase) % len(keys)],
                  '- Epic: trial-epic-%d-%d' % (number, phase), '']
    return '\n'.join(lines)


def build(issues, comments, requirements, designs):
    import reference_records
    import requirement_records as rr
    from test_reference_records import RefNative, OPERATOR, acceptance, entry
    from test_requirement_records import Native
    from trace_read import CONTRIBUTION_PREFIX  # noqa: F401 (the module must import)
    project = Path(tempfile.mkdtemp())
    req = Native()
    req.seed('job-1', issue_type='job')
    keys = []
    for number in range(requirements):
        key = 'R%03d' % number
        keys.append(key)
        req.actor = 'alice'
        made = rr.apply_native(dict(schema_version=1, kind='requirement', title=key, key=key, description='S.',
                                    acceptance_state='draft', operation_id='d-' + key, operation='draft',
                                    parent='job-1'), 'alice', req, project)
        for revision in (2, 3) if number % 4 == 0 else (2,):
            req.actor = 'operator'
            rr.apply_native(dict(schema_version=1, kind='requirement', title=key, key=key,
                                 description='S %d.' % revision, acceptance_state='accepted',
                                 operation_id='a-%s-%d' % (key, revision), operation='revise', task=made['id'],
                                 revision=revision, acceptance=dict(owners=['owner-a'], approvers=['owner-a'],
                                                                    policy='any-owner', decision_id='dec',
                                                                    evidence='review')),
                            'operator', req, project, operator=True, operators=['operator'])
    ref = RefNative()
    ref.seed('trial-d1', issue_type='decision')
    for number in range(designs):
        scope = 'design-%d' % number
        text = document(scope, number, keys)
        authority = {'type': 'repo-document', 'path': 'docs/design/D%d.md' % number, 'commit': 'a' * 40,
                     'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        ref.actor = 'alice'
        reference_records.apply_native(entry(key='design.' + scope, authority=authority, operation_id='p-' + scope,
                                             decisions=['trial-d1']), 'alice', ref, project, document=text)
        row, _ = reference_records.anchor_for(reference_records.read_key_rows(ref, 'design.' + scope),
                                              'design.' + scope)
        digest = reference_records.existing_revisions(row)[1]['sha256']
        ref.actor = OPERATOR
        reference_records.apply_native({'schema_version': 1, 'operation_id': 'acc-' + scope, 'operation': 'accept',
                                        'key': 'design.' + scope, 'revision': 1, 'record_sha256': digest,
                                        'acceptance_state': 'accepted', 'acceptance': acceptance()},
                                       OPERATOR, ref, project, operator=True, operators=[OPERATOR])
    work = []
    per = max(1, comments // max(1, issues))
    extra = comments - per * issues
    for number in range(issues):
        key = keys[number % len(keys)]
        parent = 'trial-epic-%d-%d' % (number % designs, 1 + number % 5) if number % 3 == 0 else None
        kind = 'epic' if number < designs * 5 else ('decision' if number % 97 == 0 else 'task')
        tid = 'trial-epic-%d-%d' % (number // 5, 1 + number % 5) if kind == 'epic' else 'trial-%d' % number
        row = dict(id=tid, title='Work item %d' % number, issue_type=kind,
                   status='closed' if number % 2 else 'open', labels=[],
                   description='Implements req:%s@2 and design:design-%d#section-%d here.' % (
                       key, number % designs, number % 30) if number % 5 else 'No tokens.',
                   created_at='2026-09-01T00:00:00Z', closed_at='2026-10-%02dT00:00:00Z' % (1 + number % 28),
                   comments=[dict(id='c-%d-%d' % (number, c), author='u-owner' if c == 0 else 'lane',
                                  created_at='2026-10-%02dT00:00:00Z' % (1 + (number + c) % 28),
                                  text='We will go with option %d.' % c if c == 0 and number % 50 == 0
                                  else 'Progress note %d.' % c)
                             for c in range(per + (1 if number < extra else 0))])
        if parent and kind == 'task':
            row['dependencies'] = [dict(issue_id=tid, depends_on_id=parent, type='parent-child')]
        work.append(row)
    ref.rows.remove(next(r for r in ref.rows if r['id'] == 'trial-d1'))
    return req.rows + ref.rows + work


def measure(path, view):
    """One view in this (fresh) interpreter: first and second answer, in milliseconds."""
    started = time.perf_counter()
    import trace_read
    from datetime import datetime, timezone
    rows = json.loads(Path(path).read_text(encoding='utf-8'))
    loaded = time.perf_counter()
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    members = {'u-owner': 'owner'}
    times = []
    for _ in range(2):
        begin = time.perf_counter()
        snap = trace_read.Snapshot(rows, 'trial', now=now, operators=['operator', 'ops-james'], members=members)
        answer = trace_read.trace(snap, view)
        times.append((time.perf_counter() - begin) * 1000)
    return {'view': view, 'import_and_parse_ms': round((loaded - started) * 1000), 'first_ms': round(times[0]),
            'second_ms': round(times[1]), 'total': answer['total'], 'coverage': answer['coverage']['text']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--issues', type=int, default=3400)
    parser.add_argument('--comments', type=int, default=7400)
    parser.add_argument('--requirements', type=int, default=40)
    parser.add_argument('--designs', type=int, default=20)
    parser.add_argument('--measure', nargs=2, metavar=('EXPORT', 'VIEW'))
    args = parser.parse_args()
    if args.measure:
        print(json.dumps(measure(*args.measure)))
        return
    rows = build(args.issues, args.comments, args.requirements, args.designs)
    export = Path(tempfile.mkdtemp()) / 'export.json'
    export.write_text(json.dumps(rows), encoding='utf-8')
    comments = sum(len(row.get('comments') or []) for row in rows)
    print(json.dumps({'rows': len(rows), 'comments': comments, 'export_bytes': export.stat().st_size}))
    import trace_read
    begin = time.perf_counter()
    trace_read.rows_generation(rows)
    print(json.dumps({'rows_generation_ms': round((time.perf_counter() - begin) * 1000)}))
    from datetime import datetime, timezone
    snap = trace_read.Snapshot(rows, 'trial', now=datetime(2026, 10, 20, tzinfo=timezone.utc),
                               operators=['operator', 'ops-james'])
    snap.citations()
    begin = time.perf_counter()
    affected, note = snap.impact()
    print(json.dumps({'analyze_ms': round((time.perf_counter() - begin) * 1000), 'impact': note,
                      'tasks_affected': len(affected)}))
    for view in trace_read.VIEWS:
        done = subprocess.run([sys.executable, __file__, '--measure', str(export), view], capture_output=True,
                              text=True, encoding='utf-8', cwd=str(HERE))
        print(done.stdout.strip() or done.stderr.strip()[-500:])


if __name__ == '__main__':
    main()
