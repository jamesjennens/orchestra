"""One guard for every JSON text somebody else wrote (kittrial-5bb.108).

A record comment, a `--file` attachment, a payload argument or a request body nested
thousands of levels deep used to raise `RecursionError`, which no parser catches: one
comment failed `ref get`, `work` for every actor of the project and `void-record`, and
one request was answered "outcome unknown" (exit 124) or HTTP 500. `record_json.loads`
bounds the nesting before the parse, so the failure is a `ValueError`: a comment reads
malformed and is voidable, and a request is refused with one sentence.
"""
import contextlib
import io
import json
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import briefing
import capability_records as cr
import export_requirements
import guidance
import handoff
import http_authority
import lifecycle
import proposal_records as pr
import record_json
import recovery
import reference_records as rr
import reserved_comments
import review_workflow
import work
import worker_gate
import test_reference_wiring as wiring
from test_capability_records import CapabilityCase
from test_capability_records import entry as capability_entry
from test_lifecycle import payload as lifecycle_payload
from test_proposal_records import proposal
from test_record_voids import void_payload
from test_reference_records import OPERATOR, ReferenceCase
from test_reference_records import entry as reference_entry

try:
    from test_http_proposals import ProposalHarness
except Exception:           # the HTTP harness needs a loopback socket
    ProposalHarness = None

DEPTH = 5000
MAX = record_json.NESTING_MAX
MESSAGE = 'JSON nested too deeply \\(more than 64 levels\\)'


def deep(depth=DEPTH, kind='list'):
    """Nested JSON text, built as text: a Python object this deep could not be serialised."""
    if kind == 'list':
        return '[' * depth + ']' * depth
    if kind == 'object':
        return '{"a":' * depth + '1' + '}' * depth
    return '[{"a":' * (depth // 2) + '1' + '}]' * (depth // 2)


SHAPES = ('list', 'object', 'mixed')
# Every reserved record prefix a comment can carry, in every version this kit reads.
RECORD_PREFIXES = tuple(dict.fromkeys(
    reserved_comments.PREFIXES + (reserved_comments.REFERENCE_ENTRY_V2_PREFIX,
                                  reserved_comments.REFERENCE_ENTRY_V3_PREFIX, handoff.COMPLETE_PREFIX)))


class GuardTests(unittest.TestCase):
    def test_the_bound_is_exact_and_does_not_depend_on_the_interpreter(self):
        self.assertEqual(MAX, 64)
        for kind in SHAPES:
            with self.subTest(kind=kind):
                record_json.loads(deep(MAX, kind))
                for depth in (MAX + 1 if kind != 'mixed' else MAX + 2, 400, 990, DEPTH, 200000):
                    with self.assertRaisesRegex(record_json.NestingError, MESSAGE):
                        record_json.loads(deep(depth, kind))
        self.assertTrue(issubclass(record_json.NestingError, ValueError))
        # The same answer under a recursion limit a tenth of the usual one.
        limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(200)
            record_json.loads(deep(MAX))
            with self.assertRaisesRegex(ValueError, MESSAGE):
                record_json.loads(deep(MAX + 1))
        finally:
            sys.setrecursionlimit(limit)

    def test_brackets_inside_strings_are_text(self):
        text = json.dumps({'statement': '[' * 900 + '{"a":' * 900, 'quote': 'a "quoted [[[ part" \\ and a backslash'})
        self.assertEqual(record_json.nesting(text), 1)     # 1,800 brackets, all but one inside a string
        self.assertEqual(record_json.nesting('{"a": [1, {"b": []}]}'), 0)    # few brackets: answered without a scan
        self.assertEqual(record_json.loads(text)['statement'][:2], '[[')
        many = json.dumps([{'statement': '[' * 100, 'q': 'say \\"[[[\\" twice'} for _ in range(80)])
        self.assertEqual(record_json.nesting(many), 2)
        self.assertEqual(len(record_json.loads(many)), 80)
        # After a string that is never closed the rest is text: the scan counts none of
        # it, and the parser refuses the text as malformed.
        self.assertEqual(record_json.nesting('{"a": "' + '[' * 100), 1)
        with self.assertRaises(json.JSONDecodeError):
            record_json.loads('{"a": "' + '[' * 100)
        # An escaped quote does not close a string, and an escaped backslash does not escape the quote after it.
        self.assertEqual(record_json.nesting('["a\\"' + '[' * 100 + '"]'), 1)
        self.assertEqual(record_json.nesting('["a\\\\"' + ',[' * 100), 65)
        # The scan stops at the first bracket past the bound.
        self.assertEqual(record_json.nesting('[' * 5000), MAX + 1)

    def test_the_scan_is_linear_and_never_recurses(self):
        started = time.perf_counter()
        with self.assertRaisesRegex(ValueError, MESSAGE):
            record_json.loads('[' * 2_000_000)
        with self.assertRaisesRegex(ValueError, MESSAGE):
            record_json.loads(b'{"a":' * 300000)
        self.assertLess(time.perf_counter() - started, 5)

    def test_malformed_text_of_two_megabytes_costs_a_bounded_time(self):
        # Review of 7c14f6a: with a string literal that is never closed the first scan was
        # quadratic (8 s, 30 s, 118 s, 465 s as the text doubled). One pass now: each shape
        # below took at most about half a second at 2 MB on the development machines, and
        # doubling the text doubles the time. The bound is loose so a slow machine passes;
        # the quadratic scan misses it by two orders of magnitude.
        size = 2_000_000
        shapes = {
            'an unterminated string, then brackets': '"' + '[' * (size - 1),
            'quote and bracket pairs': '"[' * (size // 2),
            'bracket and quote pairs': '["' * (size // 2),
            'quotes only': '"' * size,
            'a string of backslashes': '{"a":"' + '\\' * size,
            'escaped quotes, never closed': '["' + '\\"' * (size // 2),
            'brackets inside closed strings': '[' + ','.join(['"[[[{{{"'] * (size // 10)) + ']',
            'an open string after many brackets': '{"a":[' * 30 + '"x' + '[{' * (size // 2),
        }
        def cost(text):
            started = time.perf_counter()
            try:
                record_json.loads(text)
            except ValueError:
                pass
            return time.perf_counter() - started

        for label, text in shapes.items():
            with self.subTest(shape=label):
                whole = cost(text)
                self.assertLess(whole, 20, 'the scan is not linear on: ' + label)
                # Linear, not merely under the bound: half the text takes about half the time.
                # One pause of a shared runner (a collection, a stolen CPU) can inflate a
                # single timing, so a ratio that looks wrong is measured twice more and the
                # fastest of each is compared (kittrial-5bb.132); a quadratic scan stays
                # quadratic however often it is timed.
                half = cost(text[:len(text) // 2])
                for _ in range(2):
                    if whole < 3 * half + 0.5:
                        break
                    whole = min(whole, cost(text))
                    half = min(half, cost(text[:len(text) // 2]))
                self.assertLess(whole, 3 * half + 0.5, 'doubling the text more than tripled the time: ' + label)

    def test_it_is_json_loads_for_everything_else(self):
        self.assertEqual(record_json.loads('{"a": [1, 2.5, "x", null, true]}'), {'a': [1, 2.5, 'x', None, True]})
        self.assertEqual(record_json.loads(b'[1]'), [1])
        self.assertEqual(record_json.loads('{"a": 1}', object_pairs_hook=list), [('a', 1)])
        with self.assertRaises(json.JSONDecodeError):
            record_json.loads('{not json')
        with self.assertRaises(TypeError):
            record_json.loads(None)

    def test_a_recursion_error_from_the_parser_is_the_same_refusal(self):
        with patch.object(record_json.json, 'loads', side_effect=RecursionError('deep')):
            with self.assertRaisesRegex(record_json.NestingError, MESSAGE):
                record_json.loads('[]')

    def test_parse_json_the_record_parser_most_kinds_share(self):
        for kind in SHAPES:
            with self.assertRaisesRegex(ValueError, MESSAGE):
                export_requirements.parse_json(deep(kind=kind))
        self.assertEqual(export_requirements.parse_json('{"a": 1}'), {'a': 1})


class NoUnguardedParseTests(unittest.TestCase):
    """A new parser cannot quietly skip the guard: every `json.loads` in the kit is listed
    here with the reason it does not read text somebody else wrote."""

    ALLOWED = {
        'activity.py': (2, "the tracker export, and a cursor file on the caller's own machine"),
        'admin.py': (25, 'bd output and files on the coordination host, in operator commands; the export row reads '
                         'of credential-actors (kittrial-5bb.184) and adopt-actor\'s tracker check (kittrial-5bb.194 '
                         'revision 2) go through record_json.loads_rows (kittrial-5bb.221 revision 2): rows nested '
                         'to 750 read normally and any unreadable row fails the read as a whole; two more '
                         '(kittrial-5bb.202) read a project\'s .beads/metadata.json and one bd merge-slot check '
                         'answer in project_merge_slot_state, whose caller catches OSError/ValueError itself'),
        'artifacts.py': (1, 'the artifact index the kit writes'),
        'bootstrap.py': (2, 'bd output on the host'),
        'bd_refusals.py': (1, "bd's own answer to one command; it catches RecursionError itself"),
        'briefing.py': (3, 'bd comment-write receipt and the snapshot files the kit writes; exports use the guard'),
        'capabilities.py': (1, "a graph.json in the caller's own checkout; it catches RecursionError itself"),
        'capability_misses.py': (1, 'the telemetry file the kit writes; any failure starts a new log'),
        'client.py': (10, "the endpoint's own answer, on the caller's machine"),
        'coordination.py': (7, 'bd output'),
        'endpoint.py': (5, 'bd output, and the project\'s .beads/metadata.json whose Dolt server coordinates '
                           'guard bd (kittrial-5bb.202 rev-3 item 2; the file is the host\'s own)'),
        'export_requirements.py': (3, 'offline native export rows are first parsed through the row guard '
                                  '(750 levels), then reparsed with duplicate-field refusal; syntax checking '
                                  'of a malformed row runs only within the same bound and catches parser '
                                  'recursion; the iterative identity scanner parses single string literals '
                                  'only. Reserved revision comments still use the 64-level record guard'),
        'handoff.py': (15, 'bd output and the request, receipt and recovery files the kit writes'),
        'http_auth.py': (3, "the service's own store"),
        'http_authority.py': (3, "the service's own store and journal"),
        'http_client.py': (2, "the service's answer, and a body typed on the caller's own machine"),
        'http_service.py': (6, "the endpoint's answer to the service, and host configuration; since kittrial-5bb.169 the "
                               "answer is parsed in _parse_native only when record_json.nesting has counted it within "
                               "ROW_NESTING_MAX (one call), each row of a deeper answer in _row under the same count (one "
                               "call), and _row_id parses single string literals (one call)"),
        'keyed_records.py': (2, 'bd output'),
        'lifecycle.py': (3, "bd output, plus the endpoint's release-query and group answers on the caller's machine (kittrial-5bb.107 rev3)"),
        'native.py': (2, 'bd output'),
        'office_service.py': (1, 'host configuration'),
        'proposal_records.py': (4, 'bd output, the host session file, and a copy of a structure the kit built'),
        'record_json.py': (3, 'the guard itself, row-level parsing, and iterative array decoding'),
        'reference_records.py': (2, 'bd output'),
        'project_creation.py': (2, 'the creation record and the failure note the kit writes on the coordination host; a note that cannot be parsed, however deep, is ignored'),
        'review_recommendations.py': (1, 'bd output (the answer of comments add)'),
        'review_workflow.py': (4, 'bd output and the revert journal the kit writes'),
        'sessions.py': (1, 'the session registry the kit writes in read_registry (which turns a '
                           'damaged or nested file into one plain sentence; kittrial-5bb.194 revision 2 made '
                           'execute read through it)'),
        'version.py': (1, "the kit's own version file"),
        'work.py': (1, 'the request files the kit writes'),
        'worker.py': (2, "the endpoint's answer, on the caller's machine"),
        'worker_gate.py': (1, "the endpoint's answer, on the caller's machine"),
    }
    # Files that parse text somebody else wrote, and have no bare json.loads at all.
    GUARDED_ONLY = ('capability_records.py', 'capability_verification.py', 'feedback.py', 'guidance.py', 'keyed_entries.py', 'recovery.py', 'reserved_comments.py')

    def bare(self, path):
        return len(re.findall(r'(?<![A-Za-z_.])json\.loads\(', path.read_text(encoding='utf-8')))

    def test_every_bare_json_loads_is_accounted_for(self):
        """Each file's count of bare `json.loads` is pinned with the reason none of them
        reads a record comment or a caller's text. A new one changes the count: decide
        whether it must be `record_json.loads`, then update the table."""
        found = {path.name: self.bare(path) for path in sorted(KIT.glob('*.py')) if self.bare(path)}
        self.assertEqual(found, {name: count for name, (count, _) in self.ALLOWED.items()})
        for name in self.GUARDED_ONLY:
            self.assertEqual(self.bare(KIT / name), 0, name)

    def test_the_guard_is_used_where_other_peoples_text_is_parsed(self):
        uses = {path.name: path.read_text(encoding='utf-8').count('record_json.loads(')
                for path in KIT.glob('*.py') if path.name != 'record_json.py'}
        # lifecycle.py is down to one guarded parse (kittrial-5bb.107 rev3): its
        # reverted_integrations now delegates to review_state.reverts_by_task, so the
        # raw reject-comment parse that used record_json.loads here is gone.
        # coordination.py and one more in endpoint.py (kittrial-5bb.113 revision 2): the merge
        # slot row as bd prints it, whose metadata a contributor could once write.
        # reserved_comments.py (revision 3): the lines of a `dep add --file` list of edges.
        # endpoint.py's fourth (kittrial-5bb.188): the rows of one `bd export --all`, read to
        # judge a worker credential's name by the rows the project already holds.
        self.assertEqual({name: count for name, count in uses.items() if count}, {
            # admin.py reads the review-writes audit history through the guard
            # (kittrial-5bb.110 item 3): a deeply nested audit file used to crash
            # `review-writes status` with a RecursionError. The second (kittrial-5bb.126) reads
            # an `.owner-answers` entry for a backup: nothing in the kit writes one yet.
            # The third (kittrial-5bb.182) reads an office release's manifest.json for the
            # bundled interpreter: a manifest that is unreadable or nested too deeply must
            # fall back to the default interpreter, not traceback out of add-project.
            # The fourth (kittrial-5bb.194) reads the actor-adoptions audit, which an
            # operator command appends to and an operator reads: a nested or unreadable file
            # must be a refusal naming it, not a traceback.
            # The fifth (kittrial-5bb.192) reads the authority-changes audit, which the
            # operator/verifier list commands append to and an operator reads: a nested or
            # unreadable file must be a refusal naming it, not a traceback.
            'admin.py': 5,
            # endpoint.py's fifth (kittrial-5bb.195): the JSON payload of a coordinator
            # acceptance command, which the caller transports as a file attachment. It is
            # the caller's text, so it is parsed through the guard.
            # kittrial-5bb.221 revision 2 moved tracker_actors' export rows to
            # record_json.loads_rows (the row bound), so four remain.
            'briefing.py': 3, 'coordination.py': 3, 'endpoint.py': 4, 'export_requirements.py': 1, 'feedback.py': 3, 'guidance.py': 3,
            'handoff.py': 1, 'http_service.py': 2, 'lifecycle.py': 1, 'recovery.py': 1, 'requirements.py': 1, 'requirement_records.py': 1, 'reserved_comments.py': 1,
            # The simple owner adapter guards bd's decision-create receipt too.
            'requirement_http.py': 1,
            'review_recommendations.py': 2, 'review_workflow.py': 4, 'work.py': 1, 'worker_gate.py': 1})

    def test_the_parsers_that_do_not_call_it_directly_reach_it(self):
        # Review of 7c14f6a: the capability and proposal record parsers, and load_json.
        # Each calls export_requirements.parse_json, or is load_json itself, and those two
        # are the guarded functions. No module in the kit calls json.load on a file.
        import capability_verification
        import keyed_entries
        import requirements
        for module in (cr, capability_verification, pr, rr, keyed_entries):
            self.assertIs(module.parse_json, export_requirements.parse_json, module.__name__)
        with patch.object(record_json, 'loads', wraps=record_json.loads) as guarded:
            export_requirements.parse_json('{"a": 1}')
            self.assertEqual(guarded.call_count, 1)
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'payload.json'
                path.write_text('{"a": 1}', encoding='utf-8')
                self.assertEqual(requirements.load_json(path), {'a': 1})
                self.assertEqual(guarded.call_count, 2)
                path.write_text(deep(kind='object'), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, MESSAGE):
                    requirements.load_json(path)
        for path in KIT.glob('*.py'):
            self.assertIsNone(re.search(r'(?<![A-Za-z_.])json\.load\(', path.read_text(encoding='utf-8')), path.name)


class RecordParserTests(unittest.TestCase):
    """Each record kind's own parser answers "not a record of mine" for a deep comment."""

    def test_every_parser_returns_instead_of_raising(self):
        for kind in SHAPES:
            body = deep(kind=kind)
            with self.subTest(kind=kind):
                self.assertIsNone(rr.parse_entry(reserved_comments.REFERENCE_ENTRY_PREFIX + body))
                self.assertIsNone(rr.parse_entry(reserved_comments.REFERENCE_ENTRY_V2_PREFIX + body))
                self.assertIsNone(rr.parse_entry(reserved_comments.REFERENCE_ENTRY_V3_PREFIX + body))
                self.assertIsNone(rr.unsupported_reason(reserved_comments.REFERENCE_ENTRY_V3_PREFIX + body))
                self.assertIsNone(rr.KIND.parse_acceptance(reserved_comments.REFERENCE_ACCEPTANCE_PREFIX + body))
                self.assertIsNone(cr.parse_entry(reserved_comments.CAPABILITY_ENTRY_PREFIX + body))
                self.assertIsNone(cr.KIND.parse_acceptance(reserved_comments.CAPABILITY_ACCEPTANCE_PREFIX + body))
                self.assertIsNone(reserved_comments.parse_requirement_record(
                    export_requirements.REVISION_PREFIX + body))
                self.assertIsNone(reserved_comments.parse_acceptance_record(
                    export_requirements.ACCEPTANCE_PREFIX + body))
                self.assertIsNone(handoff.parse_identity(handoff.INTENT_PREFIX, handoff.INTENT_PREFIX + body))
                self.assertIsNone(worker_gate.parse_body(worker_gate.PREFIX + body + '\n'))
                self.assertEqual(reserved_comments.record_comment_kind(
                    reserved_comments.CAPABILITY_ENTRY_PREFIX + body)[2], 'supported')


def task(task_id, title='A task', comments=(), **extra):
    row = {'id': task_id, 'title': title, 'description': '', 'issue_type': 'task', 'status': 'in_progress',
           'labels': [], 'assignee': 'alice', 'priority': 2, 'created_by': 'alice',
           'created_at': '2026-10-01T12:00:00Z', 'updated_at': '2026-10-01T12:00:00Z', 'dependencies': [],
           'comments': [{'id': '%s-c%d' % (task_id, number), 'text': text, 'author': 'mallory',
                         'created_at': '2026-10-01T12:00:%02dZ' % number}
                        for number, text in enumerate(comments, 1)]}
    row.update(extra)
    return row


class WholeProjectTests(unittest.TestCase):
    """One bad comment never takes `work` or `brief` down for the project."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)

    def work(self, rows, actor='alice', *args):
        export = lambda argv: '\n'.join(json.dumps(row) for row in rows) + '\n'
        return work.execute(self.project, actor, 'work', list(args), {}, export, operators=[OPERATOR])

    def test_work_and_brief_answer_with_a_deep_comment_under_every_record_prefix(self):
        for prefix in RECORD_PREFIXES:
            for kind in ('list', 'object'):
                with self.subTest(prefix=prefix.strip(), kind=kind):
                    rows = [task('p-1', comments=[prefix + deep(kind=kind)]), task('p-2', 'Another task'),
                            task('p-3', 'A third', assignee='bob')]
                    for actor in ('alice', 'bob', OPERATOR):
                        result = self.work(rows, actor)
                        self.assertIn('attention', result)
                    mine = self.work(rows, 'alice', '--mine')
                    self.assertIn('p-2', [item['task'] for item in mine['items']])
                    # The task that carries the comment reads on, or reports its own error.
                    states = {item['task']: item['review_state'] for item in mine['items']}
                    self.assertIn(states.get('p-1', 'hidden'), ('none', 'error', 'hidden'))
                    self.assertEqual(briefing.brief(rows, 'p', 'p-2', operators=[OPERATOR])['task'], 'p-2')
                    try:
                        briefing.brief(rows, 'p', 'p-1', operators=[OPERATOR])
                    except ValueError:
                        pass    # its own brief may say the record is invalid; it never raises anything else

    def test_a_deep_review_record_is_that_tasks_error_and_no_other_tasks(self):
        rows = [task('p-1', comments=[review_workflow.PREFIX + deep()]), task('p-2', 'Another task')]
        with self.assertRaises(ValueError):
            work.workflow(rows[0], None, operators=[OPERATOR])
        states = {item['task']: (item['review_state'], item['error']) for item in self.work(rows, 'alice')['items']}
        self.assertEqual(states['p-1'][0], 'error')
        self.assertEqual(states['p-2'], ('none', None))

    def test_checkpoints_lifecycle_voids_and_handoffs_skip_it(self):
        row = task('p-1', comments=[briefing.PREFIX + deep(), recovery.PREFIX + deep('mixed' and DEPTH, 'object'),
                                    handoff.INTENT_PREFIX + deep()])
        current, invalid = briefing.checkpoints(row)
        self.assertIsNone(current)
        self.assertEqual(len(invalid), 1)
        voids, _, bad = recovery.records(row, [OPERATOR])
        self.assertEqual((voids, len(bad)), ([], 1))
        event = {'id': 'p-1.1', 'issue_type': 'event', 'title': 'State change: implemented → passed',
                 'close_reason': lifecycle.PREFIX + deep(), 'status': 'closed', 'labels': [], 'created_by': 'alice',
                 'comments': [], 'dependencies': [{'type': 'parent-child', 'depends_on_id': 'p-1'}]}
        lifecycle.project_facts([task('p-1'), event])          # does not raise
        self.assertEqual(lifecycle.reverted_integrations([task('p-1', comments=[lifecycle.REVERT_PREFIX + deep()])]), set())

    def test_guidance_metadata_uses_the_same_guard(self):
        (self.project / guidance.META_NAME).write_text(deep(kind='object'), encoding='utf-8')
        (self.project / guidance.GUIDANCE_NAME).write_text('Read this.\n', encoding='utf-8')
        try:
            answer = guidance.state(self.project, 'alice')
        except ValueError:
            answer = None           # it may refuse; it never raises anything else
        self.assertTrue(answer is None or isinstance(answer, dict))
        with patch.object(guidance.record_json, 'loads', wraps=record_json.loads) as guarded:
            try:
                guidance.state(self.project, 'alice')
            except ValueError:
                pass
        self.assertTrue(guarded.called)


class ReferenceRepairTests(ReferenceCase):
    def test_a_deep_comment_on_an_accepted_entry_reads_malformed_and_a_void_repairs_it(self):
        self.propose()
        self.accept(1, self.sha(1))
        for number, prefix in enumerate((reserved_comments.REFERENCE_ENTRY_PREFIX,
                                         reserved_comments.REFERENCE_ENTRY_V2_PREFIX,
                                         reserved_comments.REFERENCE_ENTRY_V3_PREFIX,
                                         reserved_comments.REFERENCE_ACCEPTANCE_PREFIX)):
            kind = 'reference-acceptance' if 'acceptance' in prefix else 'reference-entry'
            for shape in ('list', 'object'):
                with self.subTest(prefix=prefix.strip(), shape=shape):
                    bad = self.native.add_comment('ref-1', prefix + deep(kind=shape), author='mallory')
                    view = self.get()
                    self.assertEqual((view['state'], view['warnings'][-1]['code']), ('malformed', 'malformed'))
                    listing = rr.read(['list'], self.native, [OPERATOR])
                    self.assertIn('skipped as malformed (ref-1)', listing['coverage'])
                    self.assertFalse(rr.read(['find', 'trading calendar authority'], self.native, [OPERATOR])['found'])
                    self.native.actor = OPERATOR
                    rr.KIND.apply_void(void_payload('ref-1', bad['id'], bad['text'], kind=kind,
                                                    operation_id='void-%d-%s' % (number, shape)),
                                       OPERATOR, self.native, [OPERATOR])
                    self.assertEqual(self.get()['state'], 'accepted')


class CapabilityRepairTests(CapabilityCase):
    def test_a_deep_capability_comment_reads_malformed_and_a_void_repairs_it(self):
        self.propose()
        key = 'review.structured-contribution'
        for number, (prefix, kind) in enumerate(((reserved_comments.CAPABILITY_ENTRY_PREFIX, 'capability-entry'),
                                                 (reserved_comments.CAPABILITY_ACCEPTANCE_PREFIX,
                                                  'capability-acceptance'),
                                                 (reserved_comments.CAPABILITY_VERIFICATION_PREFIX,
                                                  'capability-verification'),
                                                 (reserved_comments.CAPABILITY_ALIAS_PREFIX, 'capability-alias'))):
            with self.subTest(prefix=prefix.strip()):
                row = cr.anchor_for(cr.read_rows(self.native), key)[0]
                bad = self.native.add_comment(row['id'], prefix + deep(), author='mallory')
                # A bad entry or acceptance makes the entry malformed; a bad verification or
                # alias record is skipped, as a short malformed one is. Neither fails the read.
                expected = 'malformed' if kind in ('capability-entry', 'capability-acceptance') else 'draft-only'
                self.assertEqual(self.read('get', key)['state'], expected)
                self.assertEqual(self.read('list')['total'], 0 if expected == 'malformed' else 1)
                self.read('find', 'structured contribution')
                self.native.actor = OPERATOR
                cr.KIND.apply_void(void_payload(row['id'], bad['id'], bad['text'], kind=kind,
                                                operation_id='void-%d' % number), OPERATOR, self.native, [OPERATOR])
                self.assertEqual(self.read('get', key)['state'], 'draft-only')


class CallerRouteTests(wiring.EndpointDispatchTests):
    """Text a caller sends: refused with one sentence, as a ValueError, before any write."""
    test_propose_is_a_locked_guarded_write_and_reads_are_neither = None
    test_the_payload_must_come_from_the_file_and_must_not_set_its_operation = None
    test_an_unknown_key_is_a_named_refusal = None

    def send(self, action, args, attachments=None):
        original = self.endpoint.run_guarded
        request = {'project': 'p', 'actor': 'alice', 'action': action, 'args': args,
                   'attachments': attachments or {}}
        with patch.object(self.endpoint, 'project_dir', return_value=self.project), \
                patch.object(self.endpoint, 'environment', return_value={}), \
                patch.object(self.endpoint.native, 'run', side_effect=self.fake_run), \
                patch.object(self.endpoint.fcntl, 'flock', self.locks), \
                patch.object(self.endpoint, 'run_guarded', side_effect=original):
            return self.endpoint.execute(self.root, request)

    def test_a_deep_attachment_is_refused_on_every_action_that_takes_a_file(self):
        self.native.seed('p-1')
        for action, args in (('ref', ['propose']), ('ref', ['revise']), ('capability', ['propose']),
                             ('capability', ['revise']), ('capability', ['verify']), ('proposal', ['submit']),
                             ('proposal', ['revise']), ('review', ['p-1']), ('handoff', ['p-1']),
                             ('checkpoint', ['p-1'])):
            for depth in (MAX + 1, 990, DEPTH):
                with self.subTest(action=action, args=args, depth=depth):
                    attachments = {'0': {'flag': '--file', 'text': deep(depth, 'object')}}
                    with self.assertRaisesRegex(ValueError, MESSAGE):
                        self.send(action, args + ['@attachment:0'], attachments)
        self.assertEqual(self.native.writes(), [])

    def test_sixty_four_levels_pass_the_guard_and_sixty_five_do_not_on_every_action(self):
        self.native.seed('p-1')
        actions = [(action, args + ['@attachment:0']) for action, args in (
            ('ref', ['propose']), ('ref', ['revise']), ('capability', ['propose']), ('capability', ['revise']),
            ('capability', ['verify']), ('proposal', ['submit']), ('proposal', ['revise']), ('review', ['p-1']),
            ('handoff', ['p-1']), ('checkpoint', ['p-1']))]
        for action, args in actions:
            with self.subTest(action=action, args=args[:1]):
                with self.assertRaisesRegex(ValueError, MESSAGE):
                    self.send(action, args, {'0': {'flag': '--file', 'text': deep(MAX + 1, 'object')}})
                # At the bound the text is parsed, and refused for what it says, not its depth.
                with self.assertRaises((ValueError, TypeError, KeyError)) as refused:
                    self.send(action, args, {'0': {'flag': '--file', 'text': deep(MAX, 'object')}})
                self.assertNotIn('nested too deeply', str(refused.exception))
        for action in ('lifecycle', 'coordinate', 'requirement'):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ValueError, MESSAGE):
                    self.send(action, [deep(MAX + 1)])
                with self.assertRaises((ValueError, TypeError, KeyError, AttributeError)) as refused:
                    self.send(action, [deep(MAX)])
                self.assertNotIn('nested too deeply', str(refused.exception))
        self.assertEqual(self.native.writes(), [])

    def test_a_deep_payload_argument_is_refused(self):
        for action in ('lifecycle', 'coordinate', 'requirement'):
            for depth in (MAX + 1, DEPTH):
                with self.subTest(action=action, depth=depth), self.assertRaisesRegex(ValueError, MESSAGE):
                    self.send(action, [deep(depth)])
        self.assertEqual(self.native.writes(), [])

    def test_the_request_itself_is_exit_2_with_one_sentence_never_124(self):
        for text in ('{"project": "p", "actor": "alice", "action": "work", "args": [], "junk": %s}' % deep(),
                     '{"project": "p", "actor": "alice", "action": "work", "args": %s}' % deep(MAX + 1),
                     deep(kind='object')):
            out = io.StringIO()
            with patch.object(sys, 'argv', ['endpoint.py', '--root', str(self.root)]), \
                    patch.object(self.endpoint, 'root_path', return_value=self.root), \
                    patch.object(sys, 'stdin', io.StringIO(text)), contextlib.redirect_stdout(out):
                self.endpoint.main()
            answer = json.loads(out.getvalue())
            self.assertEqual((answer['returncode'], answer['stdout'], answer['stderr']),
                             (2, '', 'NestingError: JSON nested too deeply (more than 64 levels)\n'))

    def test_under_the_operation_guard_it_is_a_refusal_and_leaves_no_reservation(self):
        # run_guarded releases a request only for a refusal raised before any write; a
        # RecursionError is not one, which is why these requests used to be "outcome unknown".
        self.assertTrue(issubclass(record_json.NestingError, http_authority.REFUSAL_EXCEPTIONS))
        self.assertFalse(issubclass(RecursionError, http_authority.REFUSAL_EXCEPTIONS))
        journal = self.root / 'journal.sqlite3'
        request = {'project': 'p', 'actor': 'alice', 'action': 'review', 'args': ['p-1', '@attachment:0'],
                   'operation_id': 'op-deep'}
        runner = self.endpoint.NativeRunner(lambda argv: '')
        with self.assertRaisesRegex(ValueError, MESSAGE):
            http_authority.run_guarded(request, journal, lambda: record_json.loads(deep()), runner=runner)
        # What the same request did before the guard: the effect raised RecursionError.
        def old_effect():
            raise RecursionError('maximum recursion depth exceeded')
        answer = http_authority.run_guarded(dict(request, operation_id='op-old'), journal, old_effect, runner=runner)
        self.assertEqual(answer['returncode'], 124)
        self.assertIn('outcome unknown', answer['stderr'])

    def test_what_a_caller_can_store_is_what_a_reader_accepts(self):
        # The same bound on both sides: a record nested at the bound parses in every reader.
        export_requirements.parse_json(deep(MAX, 'object'))
        for valid, validate in ((reference_entry(), lambda p: rr.KIND.validate_payload(p)),
                                (capability_entry(), lambda p: cr.KIND.validate_payload(p)),
                                (proposal(), pr.validate_payload), (lifecycle_payload(), lifecycle.validate_payload)):
            validate(dict(valid))
            for field in list(valid) + ['unknown_field']:
                for value in ([[[[1]]]], {'a': {'b': {'c': 1}}}):
                    with self.subTest(field=field, value=type(value).__name__), self.assertRaises(
                            (ValueError, TypeError, KeyError)):
                        validate(dict(valid, **{field: value}))


@unittest.skipIf(ProposalHarness is None, 'HTTP harness unavailable')
class HttpBodyTests(ProposalHarness if ProposalHarness else unittest.TestCase):
    """A web member's deeply nested body is bad JSON (422): it used to be HTTP 500."""

    def journal_rows(self):
        import sqlite3
        total = {}
        for path in self.tmp.rglob(http_authority.JOURNAL_FILENAME):
            connection = sqlite3.connect(str(path))
            for state, count in connection.execute('SELECT state, count(*) FROM operations GROUP BY state'):
                total[state] = total.get(state, 0) + count
            connection.close()
        return total

    def test_a_deep_body_is_refused_as_bad_json_on_every_write_route(self):
        created = self.submit()
        self.assertEqual(201, created.status, created.data)
        task_id = self.create_task(self.token('alex'), self.project, 'A task').data['id']
        before = self.journal_rows()
        base = '/v1/projects/%s' % self.project
        number = 0
        for path in (self.base(), base + '/tasks', base + '/tasks/%s/reviews' % task_id,
                     base + '/tasks/%s/checkpoints' % task_id, self.base() + '/%s/dispositions' % created.data['key']):
            for depth in (MAX + 1, 979, 980, DEPTH, 100000):
                for kind in ('list', 'object'):
                    if len(deep(depth, kind)) > 250000:
                        continue
                    number += 1
                    body = '{"text": "x", "evidence": %s}' % deep(depth, kind)
                    with self.subTest(path=path.rsplit('/', 1)[-1], depth=depth, kind=kind):
                        response = self.request('POST', path, body, token=self.token('alex'),
                                                key='deep-key-%04d' % number)
                        self.assertEqual((response.status, response.data['error']['code'],
                                          response.data['error']['message']),
                                         (422, 'invalid_payload', 'Request body is not valid JSON'), response.data)
        self.assertEqual(self.journal_rows(), before)      # no reservation, no unknown operation
        # The member, and everyone else, carries on.
        self.assertEqual(201, self.submit().status)
        self.assertEqual(201, self.submit(who='blair').status)
        self.assertEqual(200, self.request('GET', self.base(), token=self.token('casey')).status)

    def test_the_bound_itself_is_not_a_json_error(self):
        # Nesting within the bound is parsed and judged by the route, as before.
        # The body is one object, so a value nested 63 levels makes 64 in all: parsed. One more: bad JSON.
        template = '{"target": {"kind": "requirement-new"}, "text": "x", "rationale": "y", "evidence": %s, ' \
                   '"attachments": []}'
        response = self.request('POST', self.base(), template % deep(MAX - 1), token=self.token('alex'),
                                key='bound-key-1')
        self.assertEqual(422, response.status)
        self.assertNotEqual(response.data['error']['message'], 'Request body is not valid JSON')
        response = self.request('POST', self.base(), template % deep(MAX), token=self.token('alex'),
                                key='bound-key-2')
        self.assertEqual((422, 'Request body is not valid JSON'),
                         (response.status, response.data['error']['message']))

    def test_a_deep_cursor_is_an_invalid_cursor(self):
        import base64
        cursor = base64.urlsafe_b64encode(deep(2000).encode('ascii')).decode('ascii').rstrip('=')
        response = self.request('GET', self.base() + '?cursor=' + cursor, token=self.token('alex'))
        self.assertIn(response.status, (409, 422), response.data)
        self.assertNotEqual(response.status, 500)


class BdRowTests(unittest.TestCase):
    """Deeply nested bd issue rows, native output recovery and queue/render surfacing."""

    def test_loads_row_depth_threshold_and_field_recovery(self):
        # 65 levels and 500 levels parse normally under ROW_NESTING_MAX (750)
        row_65 = '{"id": "t-65", "title": "65 deep", "status": "open", "nested": ' + deep(65, 'object') + '}'
        loaded_65 = record_json.loads_row(row_65)
        self.assertEqual(loaded_65['id'], 't-65')
        self.assertFalse(loaded_65.get('malformed'))

        row_500 = '{"id": "t-500", "title": "500 deep", "status": "open", "nested": ' + deep(500, 'object') + '}'
        loaded_500 = record_json.loads_row(row_500)
        self.assertEqual(loaded_500['id'], 't-500')
        self.assertFalse(loaded_500.get('malformed'))

        # Exactly 750 levels parses normally
        row_750 = '{"id": "t-750", "title": "750 deep", "status": "open", "nested": ' + deep(749, 'object') + '}'
        loaded_750 = record_json.loads_row(row_750)
        self.assertEqual(loaded_750['id'], 't-750')
        self.assertFalse(loaded_750.get('malformed'))

        # Exceeding ROW_NESTING_MAX (751 levels) yields synthetic malformed record
        row_deep = '{"id": "t-deep", "title": "Over depth", "issue_type": "task", "labels": ["guard", "test"], "data": ' + deep(750, 'object') + '}'
        loaded_deep = record_json.loads_row(row_deep)
        self.assertTrue(loaded_deep['malformed'])
        self.assertEqual(loaded_deep['id'], 't-deep')
        self.assertEqual(loaded_deep['title'], 'Over depth')
        self.assertEqual(loaded_deep['issue_type'], 'unknown')
        self.assertEqual(loaded_deep['labels'], [])
        self.assertEqual(loaded_deep['status'], 'unknown')
        self.assertIsNone(loaded_deep['assignee'])
        self.assertIn('raw_length', loaded_deep)
        self.assertIn('raw_sha256', loaded_deep)
        self.assertIn('more than 750 levels', loaded_deep['error'])

    def test_mutations_r1_r2_r7_status_unknown_assignee_none_on_malformed_row(self):
        # Even if raw text specifies status: closed and assignee: mallory,
        # an unparseable/over-deep row must have status: unknown and assignee: None.
        line = '{"id": "t-spoofed", "title": "Spoofed", "status": "closed", "assignee": "mallory", "data": ' + deep(1500) + '}'
        loaded = record_json.loads_row(line)
        self.assertTrue(loaded['malformed'])
        self.assertEqual(loaded['status'], 'unknown')
        self.assertIsNone(loaded['assignee'])

        # In work queue: surfaced as error, status unknown, owner None, visible in general queue
        rows = [task('t-normal', 'Normal task'), loaded]
        result_all = work.queue(rows, 'alice', [])
        items = {it['task']: it for it in result_all['items']}
        self.assertIn('t-spoofed', items)
        self.assertEqual(items['t-spoofed']['status'], 'unknown')
        self.assertIsNone(items['t-spoofed']['owner'])
        self.assertEqual(items['t-spoofed']['review_state'], 'error')
        self.assertIn('Malformed issue rows: t-spoofed', result_all['coverage'])

        # In work queue with --mine: mallory does not own it (owner is None); it remains visible
        # to all workers who could own it, not claimed by mallory.
        result_mine = work.queue(rows, 'mallory', ['--mine'])
        items_mine = {it['task']: it for it in result_mine['items']}
        self.assertIn('t-spoofed', items_mine)
        self.assertIsNone(items_mine['t-spoofed']['owner'])
        self.assertEqual(items_mine['t-spoofed']['status'], 'unknown')

    def test_loads_array_rows_iterative_decoding_without_recursion(self):
        # An array with a normal item and a 3000-deep item decoded iteratively
        normal = {"id": "t-ok", "title": "Healthy"}
        arr_text = json.dumps([normal])[:-1] + ', {"id": "t-3000", "title": "Iterative", "data": ' + deep(3000) + '}]'
        rows = record_json.loads_array_rows(arr_text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['id'], 't-ok')
        self.assertFalse(rows[0].get('malformed'))
        self.assertEqual(rows[1]['id'], 't-3000')
        self.assertTrue(rows[1]['malformed'])
        self.assertEqual(rows[1]['status'], 'unknown')

        # Single object 3000 levels deep wrapped in braces
        single_obj = '{"id": "single-3000", "title": "One", "data": ' + deep(3000) + '}'
        single_res = record_json.loads_array_rows(single_obj)
        self.assertEqual(len(single_res), 1)
        self.assertEqual(single_res[0]['id'], 'single-3000')
        self.assertTrue(single_res[0]['malformed'])

    def test_loads_array_rows_damaged_output_raises_value_error(self):
        # Truncated list: raises ValueError / JSONDecodeError
        truncated = '[{"id": "t-1", "title": "One"}, {"id": "t-2", "title": "Two"'
        with self.assertRaises(ValueError):
            record_json.loads_array_rows(truncated)

        # Plain non-JSON text: raises ValueError
        with self.assertRaises(ValueError):
            record_json.loads_array_rows('plain text not json')

        # Truncated deep input: raises ValueError
        deep_truncated = '[{"id": "t-deep", "data": ' + deep(1000)
        with self.assertRaises(ValueError):
            record_json.loads_array_rows(deep_truncated)

    def test_native_split_recovers_deep_documents(self):
        import native
        doc = '{"id": "doc-deep", "deep": ' + deep(3000) + '}\n'
        val, err = native.split(types.SimpleNamespace(stdout=doc, stderr='', returncode=0))
        self.assertIn('"id": "doc-deep"', val)

    def test_render_surfaces_malformed_and_unparseable_rows(self):
        import render
        with tempfile.TemporaryDirectory() as dest:
            dest_path = Path(dest)
            malformed = record_json.loads_row('{"id": "t-bad", "title": "Malformed item", "data": ' + deep(1500) + '}')
            unparseable = record_json.loads_row('completely broken unparseable line ' + deep(1500))
            healthy = task('t-good', 'Healthy task')
            render.render([healthy, malformed, unparseable], dest_path, operators=[OPERATOR])

            current = (dest_path / 'CURRENT.md').read_text(encoding='utf-8')
            self.assertIn('## Malformed issue records', current)
            self.assertIn('[t-bad](jobs/t-bad.md)', current)
            self.assertIn('1 unparseable issue row(s) could not be read.', current)

            index = (dest_path / 'INDEX.md').read_text(encoding='utf-8')
            self.assertIn('[t-bad: Malformed item](jobs/t-bad.md)', index)
            self.assertIn('1 unparseable issue row(s)', index)

            job_path = dest_path / 'jobs' / 't-bad.md'
            self.assertTrue(job_path.exists())
            self.assertIn('## Error', job_path.read_text(encoding='utf-8'))

    def test_mutation_l7_review_write_refused_on_malformed_task_succeeds_on_healthy(self):
        healthy = task('p-good', 'Healthy task')
        malformed = record_json.loads_row('{"id": "p-bad", "title": "Bad", "data": ' + deep(1500) + '}')
        rows = [healthy, malformed]
        contribute_payload = {
            'schema_version': 1, 'operation': 'contribute', 'operation_id': 'op-c1',
            'task': 'p-good', 'previous': None, 'repository': 'ssh://git.example/p',
            'commit': 'a' * 40, 'base_commit': 'b' * 40,
            'delivery': {'kind': 'bundle', 'path': 'koopa:/b.bundle', 'sha256': 'c' * 64},
            'summary': 'Summary', 'supersedes': None
        }
        # Review write on p-good succeeds despite p-bad being present in rows
        res = review_workflow.execute(rows, 'p-good', 'alice', contribute_payload, lambda cmd: '{"id": 1}')
        self.assertIn('comment_id', res)

        # Review write on p-bad is refused naming the malformed task
        bad_payload = dict(contribute_payload, task='p-bad')
        with self.assertRaisesRegex(ValueError, r'Task p-bad is malformed:'):
            review_workflow.execute(rows, 'p-bad', 'alice', bad_payload, lambda cmd: '{"id": 1}')

    def test_mutation_ob18_lifecycle_refuses_malformed_task(self):
        healthy = task('p-good')
        bad_line = '{"id": "p-bad", "title": "Bad", "data": ' + deep(1500) + '}'
        export_text = json.dumps(healthy) + '\n' + bad_line + '\n'
        run = lambda cmd: export_text if cmd == ['export', '--all'] else ''
        p = lifecycle_payload(task='p-bad', actor='alice')
        with self.assertRaisesRegex(ValueError, r'Task p-bad is malformed:'):
            lifecycle.apply_native(p, 'alice', run, operators=[OPERATOR])

    def test_coordination_merge_acquire_refuses_malformed_task(self):
        import coordination
        bad_task = '[{"id": "t-bad", "title": "Bad", "data": ' + deep(1000) + '}]'
        run = lambda cmd: bad_task if cmd[:2] == ['show', 't-bad'] else '{"available": true}'
        p = {'operation': 'merge-acquire', 'task': 't-bad', 'target': 'origin/main'}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, r'Task t-bad is malformed:'):
                coordination.apply_native(p, 'alice', run, Path(tmp))

    def test_catalog_and_get_with_malformed_anchor(self):
        bad_ref = {'id': 's2-9ju', 'title': 'Reference anchor: x.ref', 'status': 'closed',
                   'labels': ['reference', 'reference-key:x-ref'], 'malformed': True, 'error': 'nested too deeply'}
        rows = [bad_ref]
        listed = rr.list_entries(rows, {'state': 'all'}, [OPERATOR])
        self.assertEqual(listed['total'], 1)
        self.assertIsNone(listed['items'][0]['key'])
        self.assertEqual(listed['items'][0]['state'], 'malformed')
        self.assertEqual(listed['items'][0]['native_id'], 's2-9ju')

        with self.assertRaisesRegex(ValueError, r'Reference key x\.ref exists \(anchor s2-9ju\) but cannot be read'):
            rr.get(rows, 'x.ref', [OPERATOR])

        bad_cap = {'id': 's2-e44', 'title': 'Capability anchor: x.cap', 'status': 'closed',
                   'labels': ['capability', 'capability-key:x-cap'], 'malformed': True, 'error': 'nested too deeply'}
        cap_rows = [bad_cap]
        cap_listed = cr.list_entries(cap_rows, {'state': 'all'}, [OPERATOR])
        self.assertEqual(cap_listed['total'], 1)
        self.assertIsNone(cap_listed['items'][0]['key'])
        self.assertEqual(cap_listed['items'][0]['state'], 'malformed')
        self.assertEqual(cap_listed['items'][0]['native_id'], 's2-e44')

        with self.assertRaisesRegex(ValueError, r'Capability key x\.cap exists \(anchor s2-e44\) but cannot be read'):
            cr.get(cap_rows, 'x.cap', [OPERATOR])


    def test_mutation_l2_keyed_records_uniqueness_refusal_naming_malformed_anchor(self):
        import keyed_records
        spec = rr.KIND.write_spec([OPERATOR], lambda cmd: '[]')
        spec.read_rows = lambda run, payload: [{'id': 'ref-corrupt', 'malformed': True, 'labels': ['reference-entry']}]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, r'Cannot verify reference key uniqueness: anchor ref-corrupt could not be parsed'):
                keyed_records.apply_native(reference_entry(), 'alice', lambda cmd: '[]', Path(tmp), spec)
        with self.assertRaisesRegex(ValueError, r'Cannot verify reference key uniqueness: anchor ref-corrupt could not be parsed'):
            rr.KIND.require_unique_key([{'id': 'ref-corrupt', 'malformed': True, 'labels': ['reference-entry']}], 'calendar.trading')

    def test_mutation_l3_endpoint_native_anchor_rows_catches_recursion_error(self):
        ep = wiring._endpoint_module(self)
        with patch('subprocess.run') as mock_run, patch.object(ep, 'environment', return_value={}):
            mock_run.return_value = types.SimpleNamespace(returncode=0, stdout='[{"id": "p-1"}]', stderr='')
            with patch.object(ep.json, 'loads', side_effect=RecursionError('stack overflow')):
                with self.assertRaisesRegex(ValueError, r'Could not parse the current rows of p-1 before the status write; refusing\.'):
                    ep._native_anchor_rows(Path('.'), Path('.'), 'alice', ['p-1'])

    def test_p2_4_coordination_create_child_refuses_malformed_child(self):
        import coordination
        from requirements import content_hash
        payload = {'operation': 'create-child', 'request_id': 'req-child-1', 'parent': 'p-parent',
                   'title': 'Child task', 'description': 'Description', 'type': 'task'}
        identity = content_hash({'request_id': 'req-child-1'})
        label = 'request:' + identity
        child_line = '{"id": "p-parent.1", "labels": ["' + label + '"], "data": ' + deep(1500) + '}'
        def run(argv):
            if argv[:1] == ['list']:
                return '[' + child_line + ']'
            return '[]'
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, r'Cannot verify native request: child p-parent\.1 could not be parsed'):
                coordination.apply_native(payload, 'alice', run, Path(tmp))

    def test_p2_4_catalog_reads_handle_3000_deep_anchors_without_recursion_error(self):
        import keyed_entries
        bad_anchor = '{"id": "ref-3000", "labels": ["reference-entry"], "data": ' + deep(3000) + '}'
        def run(argv):
            if argv[0] == 'list':
                return '[{"id": "ref-3000"}]'
            if argv[0] == 'show':
                return '[' + bad_anchor + ']'
            return '[]'
        shown = keyed_entries.AnchoredKind.shown(run, ['ref-3000'])
        self.assertEqual(len(shown), 1)
        self.assertTrue(shown[0]['malformed'])
        view = rr.KIND.entry_view(shown[0], [OPERATOR])
        self.assertEqual(view['state'], 'malformed')
        self.assertEqual(view['warnings'][0]['code'], 'malformed')


class ScanPathMutationsTests(unittest.TestCase):
    def test_scan_path_unclosed_array(self):
        text = '[{"a": ' + deep(752, 'object') + '}'
        with self.assertRaisesRegex(ValueError, r'Truncated or invalid JSON array'):
            record_json.loads_array_rows(text)

    def test_scan_path_trailing_data(self):
        text = '[{"a": ' + deep(752, 'object') + '}] trailing'
        with self.assertRaisesRegex(ValueError, r'Trailing data after JSON array'):
            record_json.loads_array_rows(text)

    def test_scan_path_does_not_catch_arbitrary_parse_failures(self):
        with self.assertRaises(json.JSONDecodeError):
            record_json.loads_array_rows('[{not json}]')
        with self.assertRaises(json.JSONDecodeError):
            record_json.loads_array_rows('[{"id": "t1"}')


class DupKeyRefusalTests(unittest.TestCase):


    def test_reference_write_refusal_naming_malformed_anchor(self):
        bad_ref = {'id': 'ref-bad', 'title': 'Reference anchor: x.ref', 'malformed': True, 'error': 'nested too deeply'}
        payload = {'schema_version': 1, 'operation': 'propose', 'key': 'x.ref', 'statement': 'st', 'tags': ['tag'],
                   'title': 'Title', 'authority': {'type': 'repo-path', 'path': 'docs/ref.md'}, 'owner': 'person:alice',
                   'operation_id': 'op-ref-1'}
        with tempfile.TemporaryDirectory() as tmp:
            def run(cmd):
                if cmd[:1] == ['list']:
                    return '[{"id": "ref-bad"}]'
                if cmd[:1] == ['show']:
                    return json.dumps([bad_ref])
                return '[]'
            with self.assertRaisesRegex(ValueError, r'Cannot verify reference key uniqueness: anchor ref-bad could not be parsed'):
                rr.apply_native(payload, 'alice', run, Path(tmp))

    def test_capability_write_refusal_naming_malformed_anchor(self):
        bad_cap = {'id': 'cap-bad', 'title': 'Capability anchor: x.cap', 'malformed': True, 'error': 'nested too deeply'}
        payload = {'schema_version': 1, 'operation': 'propose', 'key': 'x.cap', 'name': 'Name', 'summary': 'Summary',
                   'owner': 'person:alice', 'operation_id': 'op-cap-1'}
        with tempfile.TemporaryDirectory() as tmp:
            def run(cmd):
                if cmd[:1] == ['list']:
                    return '[{"id": "cap-bad"}]'
                if cmd[:1] == ['show']:
                    return json.dumps([bad_cap])
                return '[]'
            with self.assertRaisesRegex(ValueError, r'Cannot verify capability key uniqueness: anchor cap-bad could not be parsed'):
                cr.apply_native(payload, 'alice', run, Path(tmp))

    def test_proposal_write_refusal_naming_malformed_anchor(self):
        bad_prop = {'id': 'prop-bad', 'title': 'Requirement proposal p-01', 'malformed': True, 'error': 'nested too deeply'}
        payload = {'schema_version': 1, 'operation': 'submit', 'submitter': 'person:alice', 'text': 'text',
                   'operation_id': 'op-prop-1'}
        with tempfile.TemporaryDirectory() as tmp:
            def run(cmd):
                if cmd[:1] == ['list']:
                    return json.dumps([bad_prop])
                if cmd[:1] == ['show']:
                    return json.dumps([bad_prop])
                return '[]'
            with self.assertRaisesRegex(ValueError, r'Cannot verify proposal key uniqueness: anchor prop-bad could not be parsed'):
                pr.apply_native(payload, 'alice', run, Path(tmp))


class AnchorAsWorkTests(unittest.TestCase):

    def test_retitled_anchor_recognized_by_label(self):
        # Anchor retitled by contributor but carrying reference label in bd list
        bad_anchor = {'id': 'ref-retitled', 'issue_type': 'task', 'status': 'closed', 'title': 'Custom title',
                      'malformed': True, 'error': 'deep'}
        listed = [{'id': 'ref-retitled', 'labels': ['reference', 'reference-key:custom-ref']}]
        def run(cmd):
            if cmd[:1] == ['list']:
                return json.dumps(listed)
            if cmd[:1] == ['show']:
                return json.dumps([bad_anchor])
            return '[]'
        rows = rr.KIND.read_key_rows(run, 'custom.ref')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], 'ref-retitled')
        self.assertIn('reference-key:custom-ref', rows[0]['labels'])
        with self.assertRaisesRegex(ValueError, r'Reference key custom\.ref exists \(anchor ref-retitled\) but cannot be read'):
            rr.KIND.find_entry(rows, 'custom.ref', [OPERATOR])


class SilentSkipsTests(unittest.TestCase):

    def test_proposal_records_read_catalog_above_show_limit_refuses_unreadable_anchor(self):
        bad_prop = '{"id": "p-deep", "issue_type": "task", "title": "Requirement proposal p-deep", "labels": [], "malformed": true, "error": "deep"}'
        def run(cmd):
            if cmd[:1] == ['list']:
                return json.dumps([{'id': 'p-deep'}] + [{'id': 'p-%d' % i} for i in range(101)])
            if cmd[:1] == ['export']:
                return bad_prop + '\n' + '\n'.join(json.dumps({'id': 'p-%d' % i, 'title': 'Requirement proposal p-%d' % i}) for i in range(101))
            return '[]'
        with self.assertRaisesRegex(ValueError, r'Unreadable proposal anchor \(p-deep\)'):
            pr.read_catalog(run)



class Revision3MutationKillTests(unittest.TestCase):


    def test_mutation_k8_shown_single_id_returns_malformed_on_native_failure(self):
        def fail_run(cmd):
            raise ValueError('nested too deeply')
        res = rr.KIND.shown(fail_run, ['single-id'])
        self.assertEqual(res, [{'id': 'single-id', 'malformed': True, 'error': 'nested too deeply'}])

    def test_proposal_find_entry_refuses_unreadable_anchor(self):
        bad_prop = {'id': 'p-bad', 'issue_type': 'task', 'malformed': True,
                    'labels': ['requirement-proposal', 'proposal-key:p-c69fd651ab85']}
        with self.assertRaisesRegex(ValueError, r'Proposal key p-c69fd651ab85 exists \(anchor p-bad\) but cannot be read'):
            pr.find_entry([bad_prop], 'p-c69fd651ab85')


class AdditionalFileGuardTests(unittest.TestCase):
    def test_activity_load_export_and_cursor_guard(self):
        import activity
        with tempfile.TemporaryDirectory() as tmp:
            exp_path = Path(tmp) / 'export.jsonl'
            exp_path.write_text('{"a": ' + deep(1500) + '}\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                activity.load_export(exp_path)

            cur_path = Path(tmp) / 'cursor.json'
            cur_path.write_text(deep(MAX + 1, 'object'), encoding='utf-8')
            with self.assertRaises(ValueError):
                activity.load_cursor(cur_path, 'test-scope')

    def test_artifacts_manifest_guard(self):
        import artifacts
        with tempfile.TemporaryDirectory() as tmp:
            store = artifacts.ArtifactStore(Path(tmp))
            store.manifest_path.write_text('{"a": ' + deep(1500) + '}\n', encoding='utf-8')
            with self.assertRaises(artifacts.ArtifactError):
                store.entries()

    def test_handoff_task_guard(self):
        import handoff
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            p_exec = {
                'schema_version': 1,
                'operation_id': 'op-handoff-1',
                'task': 'p-bad',
                'from_actor': 'alice',
                'to_actor': 'bob',
                'reason': 'Handoff reason',
                'approval': 'Approval reason'
            }
            def fail_run(cmd):
                raise RecursionError('deep nesting in show')
            with self.assertRaisesRegex(ValueError, 'Task p-bad is malformed'):
                handoff.execute(path, 'alice', p_exec, fail_run)


if __name__ == '__main__':
    unittest.main()
