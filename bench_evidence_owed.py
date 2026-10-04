"""Time lifecycle.evidence_owed on one synthetic export (kittrial-5bb.107 rev2 item 6.4).

Run from a kit checkout: python3 bench_evidence_owed.py NTASKS NSCOPES
Prints the row/event counts and the best-of-5 wall time in seconds.
"""
import hashlib
import json
import statistics
import sys
import time

sys.path.insert(0, '.')
import lifecycle
from requirements import canonical_bytes


def event(eid, task, dim, value, reason, actor='bench'):
    return {'_type': 'issue', 'id': eid, 'issue_type': 'event',
            'title': 'State change: %s \u2192 %s' % (dim, value),
            'description': 'Set %s to %s\n\nReason: %s' % (dim, value, reason),
            'status': 'closed', 'created_by': actor, 'created_at': '',
            'dependencies': [{'issue_id': eid, 'depends_on_id': task,
                              'type': 'parent-child'}]}


def scope(index):
    return {'source_commit': '%040d' % (index * 3 + 1),
            'integration_commit': '%040d' % (index * 3 + 2),
            'release_id': 'release-%05d' % index,
            'environment': 'production' if index % 2 else 'staging'}


def fact_payload(operation_id, task, dim, value, scope_value, actor='bench'):
    return {'schema_version': 1, 'operation_id': operation_id, 'task': task,
            'dimension': dim, 'value': value, 'scope': scope_value,
            'evidence': ['bench:' + operation_id], 'provenance': 'performed',
            'actor': actor}


def build(ntasks, nscopes):
    rows = []
    counter = 0
    for index in range(ntasks):
        task = 'bench-task-%05d' % index
        task_rows = [{'_type': 'issue', 'id': task, 'title': 'bench', 'issue_type': 'task',
                      'status': 'open', 'labels': [], 'assignee': 'bench'}]
        newest = None
        for depth in range(nscopes):
            scope_value = scope(index * nscopes + depth)
            token = hashlib.sha256(canonical_bytes(scope_value)).hexdigest()

            def add(dim, value):
                nonlocal counter
                counter += 1
                eid = '%s.%d' % (task, counter)
                payload = fact_payload('%s/%s/%d' % (task, dim, counter), task, dim,
                                       value, scope_value)
                task_rows.append(event(eid, task, dim, value,
                                       lifecycle.PREFIX + canonical_bytes(payload).decode()))

            add('lifecycle-scope', token)
            add('deployed', 'passed')
            add('live-verified', 'passed')
            add('enabled', 'enabled')
            newest = token
        task_rows[0]['labels'] = ['lifecycle-scope:' + newest, 'deployed:passed',
                                  'live-verified:passed', 'enabled:enabled', 'live:live']
        rows.extend(task_rows)
    return rows


def main():
    ntasks = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    nscopes = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    rows = build(ntasks, nscopes)
    lifecycle.evidence_owed(rows)  # warm up
    times = []
    result = []
    for _ in range(5):
        start = time.perf_counter()
        result = lifecycle.evidence_owed(rows)
        times.append(time.perf_counter() - start)
    print(json.dumps({'rows': len(rows),
                      'tasks': ntasks,
                      'scopes_per_task': nscopes,
                      'groups': len(result),
                      'best_seconds': round(min(times), 4),
                      'median_seconds': round(statistics.median(times), 4)}))


if __name__ == '__main__':
    main()
