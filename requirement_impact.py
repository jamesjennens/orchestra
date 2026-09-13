"""Compute revision-aware reassessment without changing records or historical evidence."""
import argparse
import sys

from export_requirements import check_history, exact_reference, reference
from requirements import canonical_bytes, load_json, validate_manifest


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + ' must be nonempty')


def _references(values):
    if not isinstance(values, list):
        raise ValueError('requirements must be a list')
    refs = [exact_reference(v) for v in values]
    if len(set(refs)) != len(refs) or len({r[0] for r in refs}) != len(refs):
        raise ValueError('duplicate requirement reference')
    return refs


def analyze(previous, current, graph, change=None, previous_acceptance=None, acceptance=None, history=None):
    validate_manifest(previous, previous_acceptance)
    validate_manifest(current, acceptance)
    if previous['canonical_project'] != current['canonical_project'] or previous['job'] != current['job']:
        raise ValueError('baselines must belong to the same project and job')
    check_history(previous, current)
    old = {r['id']: r for r in previous['narrative'] + previous['requirements']}
    new = {r['id']: r for r in current['narrative'] + current['requirements']}
    changed = sorted(rid for rid in old if rid not in new or reference(old[rid]) != reference(new[rid]))
    added = sorted(set(new) - set(old))
    transition = previous['sha256'] != current['sha256']
    if change is not None:
        fields = {'classification', 'state', 'decision_id', 'evidence', 'old_manifest_sha256', 'new_manifest_sha256'}
        if not isinstance(change, dict) or set(change) != fields:
            raise ValueError('change proposal has missing or unknown fields')
        if change['classification'] not in ('defect', 'ambiguity', 'scope-change') or change['state'] not in ('draft', 'accepted'):
            raise ValueError('invalid change classification/state')
        if (change['old_manifest_sha256'], change['new_manifest_sha256']) != (previous['sha256'], current['sha256']):
            raise ValueError('change proposal targets different baselines')
        if change['classification'] == 'defect' and (changed or added):
            raise ValueError('a defect fix cannot silently revise requirements')
        if change['state'] == 'accepted':
            _text(change['decision_id'], 'decision_id')
            _text(change['evidence'], 'evidence')
    if current['state'] == 'accepted' and transition and (change is None or change['state'] != 'accepted'):
        raise ValueError('accepted transition requires an accepted change decision')
    if not isinstance(graph, dict) or set(graph) != {'schema_version', 'nodes'} or type(graph['schema_version']) is not int or graph['schema_version'] != 1 or not isinstance(graph['nodes'], list):
        raise ValueError('expected version 1 work graph')
    catalogs = {previous['sha256']: previous, current['sha256']: current}
    if history is not None:
        if not isinstance(history, list):
            raise ValueError('history must be a list of manifest/acceptance objects')
        for entry in history:
            if not isinstance(entry, dict) or set(entry) != {'manifest', 'acceptance'}:
                raise ValueError('malformed history entry')
            snapshot = entry['manifest']
            validate_manifest(snapshot, entry['acceptance'])
            if (snapshot['canonical_project'], snapshot['job']) != (current['canonical_project'], current['job']):
                raise ValueError('history belongs to another project or job')
            catalogs[snapshot['sha256']] = snapshot
    available, historical_ids = set(), {}
    for snapshot in catalogs.values():
        for record in snapshot['narrative'] + snapshot['requirements']:
            ref = exact_reference(reference(record))
            if ref[:2] in historical_ids and historical_ids[ref[:2]] != ref[2]:
                raise ValueError('history has conflicting content for one revision')
            historical_ids[ref[:2]] = ref[2]
            available.add(ref)
    nodes, refs_by_node, reasons, reaffirmed = {}, {}, {}, set()
    for node in graph['nodes']:
        required = {'id', 'kind', 'requirements', 'depends_on', 'evidence'}
        if not isinstance(node, dict) or not required <= set(node) or set(node) - required - {'rationale', 'reaffirmation'}:
            raise ValueError('malformed work node')
        _text(node['id'], 'node id')
        if node['id'] in nodes or node['kind'] not in ('design', 'task', 'evidence'):
            raise ValueError('duplicate node id or unsupported kind')
        refs = _references(node['requirements'])
        if any(ref not in available for ref in refs):
            raise ValueError('work references an unknown revision: ' + node['id'])
        for field in ('depends_on', 'evidence'):
            if not isinstance(node[field], list):
                raise ValueError(field + ' must be a list')
            for value in node[field]:
                _text(value, field)
        if len(set(node['depends_on'])) != len(node['depends_on']):
            raise ValueError('duplicate dependency')
        if not refs and not node['depends_on']:
            _text(node.get('rationale'), 'unscoped work rationale')
        affirmation = node.get('reaffirmation')
        if affirmation is not None:
            fields = {'baseline_sha256', 'requirements', 'rationale', 'evidence'}
            if not isinstance(affirmation, dict) or set(affirmation) != fields:
                raise ValueError('malformed reaffirmation')
            if not isinstance(affirmation['baseline_sha256'], str) or affirmation['baseline_sha256'] not in catalogs:
                raise ValueError('reaffirmation targets an unknown baseline')
            _text(affirmation['rationale'], 'reaffirmation rationale')
            _text(affirmation['evidence'], 'reaffirmation evidence')
            if affirmation['baseline_sha256'] == current['sha256']:
                reaffirmed.add(node['id'])
        nodes[node['id']] = node
        refs_by_node[node['id']] = refs
    for node in nodes.values():
        if any(dep not in nodes for dep in node['depends_on']):
            raise ValueError('unknown work dependency: ' + node['id'])
    # Resolve inherited original scope before validating any reaffirmation.
    scopes = {nid: set(refs) for nid, refs in refs_by_node.items()}
    progress = True
    while progress:
        progress = False
        for nid, node in nodes.items():
            inherited_scope = set().union(*(scopes[d] for d in node['depends_on'])) if node['depends_on'] else set()
            if not inherited_scope <= scopes[nid]:
                scopes[nid] |= inherited_scope
                progress = True
    for nid, node in nodes.items():
        effective = scopes[nid]
        if node.get('reaffirmation') is not None:
            affirmation = node['reaffirmation']
            target = catalogs[affirmation['baseline_sha256']]
            target_records = {r['id']: r for r in target['narrative'] + target['requirements']}
            effective = _references(affirmation['requirements'])
            expected = {exact_reference(reference(target_records[rid])) for rid, _, _ in scopes[nid] if rid in target_records}
            if any(rid not in target_records for rid, _, _ in scopes[nid]) or set(effective) != expected:
                raise ValueError('reaffirmation must cite every direct and inherited replacement revision')
        reasons[nid] = {rid for rid, rev, digest in effective if rid not in new or exact_reference(reference(new[rid])) != (rid, rev, digest)}
    # Propagate before clearing current reaffirmations so every affected item
    # needs its own recorded reassessment, including within dependency cycles.
    progress = True
    while progress:
        progress = False
        for nid, node in nodes.items():
            inherited = set().union(*(reasons[d] for d in node['depends_on'])) if node['depends_on'] else set()
            if not inherited <= reasons[nid]:
                reasons[nid] |= inherited
                progress = True
    pending = {nid for nid in nodes if reasons[nid] and nid not in reaffirmed}
    while True:
        expanded = pending | {nid for nid, node in nodes.items() if any(dep in pending for dep in node['depends_on'])}
        if expanded == pending:
            break
        pending = expanded
    covered = {rid for refs in refs_by_node.values() for rid, _, _ in refs}
    return {'schema_version': 1, 'old_manifest_sha256': previous['sha256'],
            'new_manifest_sha256': current['sha256'],
            'mode': 'accepted' if current['state'] == 'accepted' else 'proposed',
            'changed': changed, 'added': added,
            'unplanned_requirements': sorted(r['id'] for r in current['requirements'] if r['id'] not in covered),
            'nodes': [{'id': nid, 'state': 'needs-reassessment' if nid in pending else ('reaffirmed' if nid in reaffirmed else 'unaffected'),
                       'affected_requirements': sorted(reasons[nid]),
                       'evidence': nodes[nid]['evidence'], 'reaffirmation': nodes[nid].get('reaffirmation')}
                      for nid in sorted(nodes)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('previous', 'current', 'graph'):
        parser.add_argument('--' + flag, required=True)
    for flag in ('change', 'previous-acceptance', 'acceptance', 'history'):
        parser.add_argument('--' + flag)
    args = parser.parse_args(argv)
    try:
        values = {k: load_json(v) if v else None for k, v in vars(args).items()}
        print(canonical_bytes(analyze(**values)).decode('utf-8'))
    except (ValueError, OSError) as exc:
        print('impact analysis failed: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
