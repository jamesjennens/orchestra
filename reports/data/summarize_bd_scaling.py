"""Regenerate aggregates from the six immutable committed measurement files.

Run from any working directory. Trace names in raw rows identify archive captures;
this calculation uses only committed JSON, and needs no runtime or credentials.
"""
import collections
import hashlib
import json
import statistics
from pathlib import Path


def summarize():
    root = Path(__file__).resolve().parent
    metadata = json.loads((root / 'bd-database-scaling.json').read_text(encoding='utf-8'))
    sources = metadata['sources'] + [metadata['version_comparison']['raw']]
    groups = collections.defaultdict(list)
    count = 0
    for source in sources:
        path = root / source['path']
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != source['sha256']:
            raise ValueError('Raw sample hash mismatch: ' + source['path'])
        rows = json.loads(data)
        count += len(rows)
        for row in rows:
            if row['outcome'] != 'passed':
                raise ValueError('Unaccepted sample in ' + source['path'])
            key = (source['path'], row.get('bd_version', '1.2.2'),
                   row['operation'], row['databases'], row['logging'])
            groups[key].append(row)
    result = []
    for (source, version, operation, databases, logging), rows in sorted(groups.items()):
        item = dict(source=source, bd_version=version, operation=operation,
                    databases=databases, logging=logging, samples=len(rows))
        for field in ['seconds', 'query_count', 'catalog_query_count', 'catalog_ms',
                      'query_ms_sum', 'show_columns_count']:
            if all(field in row for row in rows):
                values = [row[field] for row in rows]
                item[field] = dict(median=statistics.median(values),
                                   min=min(values), max=max(values))
        result.append(item)
    return dict(completed_samples=count, groups=result)


if __name__ == '__main__':
    print(json.dumps(summarize(), indent=2))
