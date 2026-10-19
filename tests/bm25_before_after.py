"""Generate before/after ranking tables for capability lookup and capability find (kittrial-5bb.337).

Compares top-3 results and scores on a base git revision (default: origin/main 04cd10b)
against the current working tree for all free-form questions and short alias-like phrases
used in BM25LookupTests and BM25FindTests.

Usage:
    python -X utf8 tests/bm25_before_after.py
    python -X utf8 tests/bm25_before_after.py --base 04cd10b
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
for _path in (str(KIT), str(KIT / 'tests')):
    if _path not in sys.path:
        sys.path.append(_path)

DEFAULT_BASE = '316e799c7d310bf0152116a94fd0c2b357b2642b'

KIT_LOOKUP_QUERIES = [
    ('how does the endpoint guard reserved labels on issues', 'endpoint.py::_guard_reserved_labels'),
    ('where do we validate coordination receipt files', 'admin.py::validate_coordination_files'),
    ('how do I acquire a merge slot lock', 'http_client.py::Client.acquire_merge_slot'),
    ('what checks whether a comment is a record anchor', 'reserved_comments.py::is_record_anchor'),
    ('how are markdown headings extracted for capability lookup', 'capabilities.py::markdown_headings'),
    ('where is the worker gate plan receipt verified', 'worker_gate.py::_verify'),
    ('what refuses a claim when a task has an open blocking dependency', 'endpoint.py::_guard_open_dependency_on_claim'),
    ('how do we drop a leading bom and fold crlf on a comment body', 'reserved_comments.py::_reserved_prefix_view'),
    ('where do we reject a raw comment add carrying a forged reserved body', 'reserved_comments.py::check_raw_request'),
    ('how do we bound expression tokens per file before ast parse', 'capabilities.py::_expression_tokens'),
    ('work list', 'docs/CLI_CONTRACT.md#work-listqueue-shape'),
    ('next work', 'docs/BRIEFINGS.md#the-selection-facts-next_actor-work_state-reconciliation-and-claimable'),
    ('_guard_reserved_labels', 'endpoint.py::_guard_reserved_labels'),
    ('validate coordination files', 'admin.py::validate_coordination_files'),
]

FIXTURE_LOOKUP_QUERIES = [
    ('how do I start the pipeline engine', 'pkg/core.py::Engine.start'),
    ('where do we tidy a string value', 'pkg/util.py::tidy'),
    ('how are merge slots documented in design', 'docs/DESIGN.md#merge-slots'),
    ('what is the work list queue shape', 'docs/DESIGN.md#work-listqueue-shape'),
    ('how do we verify an actor session token', 'pkg/auth.py::verify_session_token'),
    ('where can I rotate the webhook signing key', 'pkg/auth.py::rotate_signing_key'),
    ('how does a worker claim the next ready ticket', 'pkg/scheduler.py::claim_next_ticket'),
    ('what releases an expired worker heartbeat lease', 'pkg/scheduler.py::release_expired_lease'),
    ('where is the worker gate plan receipt checked', 'pkg/receipts.py::verify_plan_receipt'),
    ('how do we guard reserved labels on issues', 'pkg/labels.py::strip_reserved_labels'),
    ('where are completed batch receipts archived', 'pkg/receipts.py::archive_completed_batch'),
    ('how can I start the pipeline when a job arrives', 'pkg/core.py::Engine.start'),
    ('how can we tidy a string value before printing it', 'pkg/util.py::tidy'),
    ('how can a server validate an actor token before running a command', 'pkg/auth.py::verify_session_token'),
    ('how does a worker claim the highest priority ready job from the queue', 'pkg/scheduler.py::claim_next_ticket'),
    ('where do we check that a worker gate plan matches the registered launch', 'pkg/receipts.py::verify_plan_receipt'),
    ('where are capability and requirement labels guarded on issue mutations', 'pkg/labels.py::strip_reserved_labels'),
    ('Merge slots', 'docs/DESIGN.md#merge-slots'),
    ('start engine', 'pkg/core.py::Engine.start'),
]

FIND_CATALOG = [
    ('cap-review', 'review.structured-contribution', 'Structured contribution review',
     ['review workflow'], 'Contribute, request changes, respond and approve.', ['review'],
     ['docs/REVIEWS.md#structured-reviews'], ['review_workflow.py::execute'], ['tests/test_review_workflow.py']),
    ('cap-merge', 'merge.slot', 'Merge slot',
     ['integration lock'], 'One integrator at a time.', ['coordination'],
     ['docs/DESIGN.md#merge-slots'], ['coordination.py::merge_acquire'], ['tests/test_coordination.py']),
    ('cap-gate', 'worker.plan-gate', 'Worker plan gate',
     ['plan receipt'], 'Register a worker plan and verify the receipt before coding.', ['gate'],
     ['docs/OPERATIONS.md#worker-gate'], ['worker_gate.py::cmd_register'], ['tests/test_worker_gate.py']),
    ('cap-lookup', 'capability.lookup', 'Read-only capability lookup',
     ['symbol search'], 'Look up code symbols and markdown headings in the checkout.', ['capability'],
     ['docs/CAPABILITY_INDEX_DESIGN.md#lookup'], ['capabilities.py::lookup'], ['tests/test_capabilities.py']),
    ('cap-req', 'requirement.records', 'Requirement records',
     ['requirement catalog'], 'Propose, revise and accept requirement revisions.', ['requirement'],
     ['docs/REQUIREMENTS.md#records'], ['requirement_records.py::apply_native'], ['tests/test_requirement_records.py']),
    ('cap-miss', 'capability.misses', 'Capability miss log',
     ['lookup misses'], 'Record and aggregate capability lookup misses by phrase.', ['capability'],
     ['docs/CAPABILITY_INDEX_DESIGN.md#misses'], ['capability_misses.py::read'], ['tests/test_capability_misses.py']),
    ('cap-lane', 'coordination.lane-state', 'Worker lane state',
     ['heartbeat status'], 'Publish worker lane state and waiting target on checkpoints.', ['coordination'],
     ['docs/OPERATIONS.md#lane-state'], ['lane_state.py::record_lane_state'], ['tests/test_lane_state.py']),
    ('cap-guard', 'endpoint.reserved-labels', 'Reserved label guard',
     ['label protection'], 'Refuse caller mutations to reserved capability and requirement labels.', ['security'],
     ['docs/SECURITY.md#reserved-labels'], ['endpoint.py::_guard_reserved_labels'], ['tests/test_coordinator_endpoint.py']),
    ('cap-export', 'export.requirements', 'Export requirements snapshot',
     ['requirement export'], 'Export accepted requirements to a deterministic markdown snapshot.', ['requirement'],
     ['docs/REQUIREMENTS.md#export'], ['export_requirements.py::export'], ['tests/test_export_requirements.py']),
    ('cap-retire', 'capability.retire', 'Retire superseded capability',
     ['supersede capability'], 'Retire an obsolete capability key and link its successor.', ['capability'],
     ['docs/CAPABILITY_INDEX_DESIGN.md#retire'], ['capability_records.py::apply_native'], ['tests/test_capability_records.py']),
]

FIND_QUERIES = [
    ('how do I acquire a merge slot so only one integrator runs at a time', 'merge.slot'),
    ('what is the workflow to contribute and approve a structured review', 'review.structured-contribution'),
    ('how does a worker register a plan receipt before coding', 'worker.plan-gate'),
    ('where can I look up code symbols and markdown headings in the checkout', 'capability.lookup'),
    ('how do we propose and accept requirement revisions', 'requirement.records'),
    ('what records and aggregates capability lookup misses', 'capability.misses'),
    ('how does a worker publish lane state on checkpoints', 'coordination.lane-state'),
    ('what refuses caller mutations to reserved labels', 'endpoint.reserved-labels'),
    ('how do I export accepted requirements to a markdown snapshot', 'export.requirements'),
    ('how do we retire an obsolete capability and link its successor', 'capability.retire'),
    ('how can a contributor respond to review changes and get approval', 'review.structured-contribution'),
    ('where is the registered receipt verified prior to running tasks', 'worker.plan-gate'),
    ('how do I search for python symbols or markdown headings when inspecting files', 'capability.lookup'),
    ('how can an operator propose or revise a requirement before accepting it', 'requirement.records'),
    ('what aggregates phrases in capability_misses when a search misses', 'capability.misses'),
    ('where do we publish a waiting target when a task checkpoint is written', 'coordination.lane-state'),
    ('where are caller mutations on reserved labels refused during issue updates', 'endpoint.reserved-labels'),
    ('how can we produce a deterministic markdown file from accepted requirement records', 'export.requirements'),
    ('merge slot', 'merge.slot'),
    ('integration lock', 'merge.slot'),
    ('review workflow', 'review.structured-contribution'),
    ('plan receipt', 'worker.plan-gate'),
    ('structured review', 'review.structured-contribution'),
]


def populate_lookup_fixture(root):
    import test_capabilities as tc
    tc.fixture(root)
    tc.write(root, 'pkg/auth.py', '''def verify_session_token(token):
    """Validate an actor session token before running a command."""
    return bool(token)


def rotate_signing_key(key_id):
    """Rotate the HMAC signing key used for webhook payloads."""
    return key_id
''')
    tc.write(root, 'pkg/scheduler.py', '''def claim_next_ticket(queue):
    """Claim the highest priority ready ticket from the work queue."""
    return queue


def release_expired_lease(lease_id):
    """Release a worker lease when its heartbeat expires."""
    return lease_id
''')
    tc.write(root, 'pkg/receipts.py', '''def verify_plan_receipt(path):
    """Check that a worker gate plan receipt matches the registered launch."""
    return path


def archive_completed_batch(batch_id):
    """Archive completed batch acceptance receipts to cold storage."""
    return batch_id
''')
    tc.write(root, 'pkg/labels.py', '''def strip_reserved_labels(labels):
    """Guard reserved capability and requirement labels on issue mutations."""
    return labels
''')


def format_lookup_top3(result):
    if result.get('found'):
        return ', '.join('exact:%s' % row['id'] for row in result['matches'][:3])
    candidates = result.get('candidates') or []
    if not candidates:
        return '*(none)*'
    return ', '.join('%s (%.3f)' % (row['id'], row['score']) for row in candidates[:3])


def format_find_top3(result):
    if result.get('found'):
        return ', '.join('exact:%s' % row['key'] for row in result['records'][:3])
    candidates = result.get('candidates') or []
    if not candidates:
        return '*(none)*'
    return ', '.join('%s (%.3f)' % (row['key'], row['score']) for row in candidates[:3])


def evaluate_in_current_process():
    import capabilities as cap
    import capability_records as cr
    from test_capability_records import CapabilityNative, OPERATOR, TODAY, acceptance, entry

    with tempfile.TemporaryDirectory() as tmp:
        fixture_root = Path(tmp) / 'fixture'
        fixture_root.mkdir()
        populate_lookup_fixture(fixture_root)
        fixture_index = cap.build_index(cap.open_repo(fixture_root), 'ast', None)
        fixture_out = {
            q: format_lookup_top3(cap.lookup(fixture_index, q, limit=3))
            for q, _ in FIXTURE_LOOKUP_QUERIES
        }

    kit_index = cap.build_index(cap.open_repo(KIT), 'ast', None)
    kit_out = {
        q: format_lookup_top3(cap.lookup(kit_index, q, limit=3))
        for q, _ in KIT_LOOKUP_QUERIES
    }

    with tempfile.TemporaryDirectory() as tmp, patch('time.gmtime', return_value=TODAY):
        project = Path(tmp)
        native = CapabilityNative()
        items = []
        for op_id, key, name, aliases, summary, tags, anchors, code, tests in FIND_CATALOG:
            native.actor = 'alice'
            cr.apply_native(
                entry(operation_id=op_id, key=key, name=name, aliases=aliases,
                      summary=summary, tags=tags, anchors=anchors, code=code, tests=tests,
                      owner='person:james'),
                'alice', native, project)
            row, _ = cr.anchor_for(cr.read_rows(native), key)
            sha = cr.existing_revisions(row)[1]['sha256']
            items.append({'key': key, 'revision': 1, 'record_sha256': sha})
        native.actor = OPERATOR
        cr.apply_batch(
            {'schema_version': 1, 'operation_id': 'accept-catalog', 'items': items,
             'acceptance_state': 'accepted', 'acceptance': acceptance(decision_id='decision-7')},
            OPERATOR, native, project, operators=[OPERATOR])
        find_out = {
            q: format_find_top3(cr.read(['find', q, '--limit', '3'], native, [OPERATOR]))
            for q, _ in FIND_QUERIES
        }

    return {'fixture_lookup': fixture_out, 'kit_lookup': kit_out, 'find': find_out}


def evaluate_git_ref(ref):
    with tempfile.TemporaryDirectory() as tmp:
        overlay = Path(tmp)
        for filename in ('capabilities.py', 'capability_records.py'):
            proc = subprocess.run(
                ['git', '-C', str(KIT), 'show', '%s:%s' % (ref, filename)],
                capture_output=True, check=True)
            (overlay / filename).write_bytes(proc.stdout)
        code = (
            'import importlib.util, json, pathlib, sys; '
            'overlay = pathlib.Path(%r); '
            'sys.path.insert(0, str(overlay)); '
            'sys.path.insert(1, %r); '
            'sys.path.insert(2, %r); '
            'import capabilities, capability_records; '
            'import bm25_before_after as b; '
            'print(json.dumps(b.evaluate_in_current_process()))'
        ) % (str(overlay), str(KIT), str(KIT / 'tests'))
        proc = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', code],
            cwd=str(KIT), capture_output=True, text=True, encoding='utf-8', check=True)
        return json.loads(proc.stdout)


def render_markdown(base_ref, before, after):
    import capabilities as cap
    short_base = base_ref[:7]
    lines = [
        '# BM25 Before/After Ranking Comparison (`%s` vs working tree)' % short_base,
        '',
        'Generated by `python -X utf8 tests/bm25_before_after.py --base %s`.' % short_base,
        'Both sides index the tip\'s checkout tree and test fixtures using each ref\'s '
        '`capabilities.py` and `capability_records.py`.',
        'Active constants in `capabilities.py`: '
        '`BM25_K1=%r, BM25_B=%r, IDENTITY_WEIGHT=%r, CONTEXT_WEIGHT=%r, '
        'FIELD_TF_CAP=%r, CANDIDATE_FLOOR=%r, PENDING_ALIAS_LIFT=%r, TEST_DISCOUNT=%r`.' % (
            cap.BM25_K1, cap.BM25_B, cap.IDENTITY_WEIGHT, cap.CONTEXT_WEIGHT,
            cap.FIELD_TF_CAP, cap.CANDIDATE_FLOOR, cap.PENDING_ALIAS_LIFT, cap.TEST_DISCOUNT),
        '',
        '## 1. `capability lookup` on Kit Checkout (`--source ast`)',
        '',
        '| Query | Target | Before (`%s`) Top 3 | After (BM25) Top 3 |' % short_base,
        '| :--- | :--- | :--- | :--- |',
    ]
    for query, target in KIT_LOOKUP_QUERIES:
        lines.append('| `%s` | `%s` | %s | %s |' % (
            query, target, before['kit_lookup'][query], after['kit_lookup'][query]))
    lines.extend([
        '',
        '## 2. `capability lookup` on Multi-File Fixture (`BM25LookupTests`)',
        '',
        '| Query | Target | Before (`%s`) Top 3 | After (BM25) Top 3 |' % short_base,
        '| :--- | :--- | :--- | :--- |',
    ])
    for query, target in FIXTURE_LOOKUP_QUERIES:
        lines.append('| `%s` | `%s` | %s | %s |' % (
            query, target, before['fixture_lookup'][query], after['fixture_lookup'][query]))
    lines.extend([
        '',
        '## 3. `capability find` on 10-Capability Catalog (`BM25FindTests`)',
        '',
        '| Query | Target | Before (`%s`) Top 3 | After (BM25) Top 3 |' % short_base,
        '| :--- | :--- | :--- | :--- |',
    ])
    for query, target in FIND_QUERIES:
        lines.append('| `%s` | `%s` | %s | %s |' % (
            query, target, before['find'][query], after['find'][query]))
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default=DEFAULT_BASE, help='Base git revision to compare against.')
    parser.add_argument('--eval-current', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.eval_current:
        print(json.dumps(evaluate_in_current_process()))
        return 0
    before = evaluate_git_ref(args.base)
    after = evaluate_in_current_process()
    sys.stdout.write(render_markdown(args.base, before, after))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
