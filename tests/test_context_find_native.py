"""Native freshness and a measured local-client warm path, in an owned runtime."""
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import unittest
import uuid
from unittest import mock

import test_bd_label_aliases as fixture

KIT = Path(__file__).resolve().parents[1]


@unittest.skipIf(fixture.endpoint is None, 'canonical endpoint needs POSIX')
@unittest.skipIf(fixture.BD is None, 'configure the real pinned ORCHESTRA_BD_BIN')
class NativeFindTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = type('ContextFindRuntime', (fixture.RealBdLabelAliasTests,), {'BD_TIMEOUT': 120})
        cls.runtime.setUpClass()
        launcher = cls.runtime.root / 'bin' / 'measured-python'
        source = (KIT / 'tests' / 'context_find_trace.py').read_text()
        launcher.write_text('#!' + sys.executable + '\n' + source.split('\n', 1)[1])
        launcher.chmod(0o700)
        cls.launcher = launcher

    @classmethod
    def tearDownClass(cls):
        cls.runtime.tearDownClass()

    def setUp(self):
        self.case = self.runtime('runTest')
        self.root, self.project = self.case.root, self.case.project
        self.trace = self.root / 'find-process-trace.jsonl'
        self.config = dict(transport='local', python=str(self.launcher),
                           endpoint=str(KIT / 'endpoint.py'), root=str(self.root))

    def emit(self, record):
        line = json.dumps(record, sort_keys=True)
        print('NATIVE_FIND ' + line, flush=True)
        target = os.environ.get('ORCHESTRA_FIND_MEASUREMENTS')
        if target:
            with Path(target).open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')

    def native(self, args, actor='worker'):
        args = list(args)
        if args[:2] == ['comments', 'add'] and '--author' not in args:
            args += ['--author', actor]
        answer = self.case.bd(*args, actor=actor)
        self.assertEqual(0, answer.returncode, answer.stderr)
        return answer.stdout

    def measure(self, sample, phrase, *args):
        import client
        load_before = float(Path('/proc/loadavg').read_text().split()[0])
        outcome, body = {'sample': sample, 'load_before': load_before}, None
        began = time.monotonic()
        try:
            with mock.patch.dict(os.environ, CONTEXT_FIND_TRACE_LOG=str(self.trace), CONTEXT_FIND_SAMPLE=sample):
                result = client.request(self.config, 'pp', 'worker', [phrase, *args, '--json'], 'find')
            outcome['returncode'] = result['returncode']
            if result['returncode'] == 0:
                body = json.loads(result['stdout'])
                outcome.update(stale=body['stale'], total=body['total'],
                               documents=body['coverage']['documents'],
                               comments_examined=body['coverage']['comments_examined'],
                               truncated=body['coverage']['truncated'],
                               cutoff_reasons=body['coverage']['cutoffs'])
            else:
                outcome['error'] = result.get('stderr', '')[:500]
        except Exception as error:
            outcome.update(exception=type(error).__name__, error=str(error)[:500])
        outcome['seconds'] = time.monotonic() - began
        outcome['load_after'] = float(Path('/proc/loadavg').read_text().split()[0])
        events = [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []
        events = [event for event in events if event.get('sample') == sample]
        starts = [event for event in events if event['kind'] == 'process-start']
        commands = [event['command'] for event in starts if event['executable'] in ('bd', 'bd.exe')]
        outcome.update(exports=commands.count('export'), bd_commands=commands,
                       head_queries=sum(event['kind'] == 'head-query' for event in events),
                       trace_events=events)
        self.emit(outcome)
        return body, outcome

    def find(self, sample, phrase, *args, exports=None):
        body, measured = self.measure(sample, phrase, *args)
        self.assertEqual(0, measured.get('returncode'), measured)
        self.assertEqual('current', body['stale'], measured)
        self.assertEqual(2, measured['head_queries'], measured)
        if exports is not None:
            self.assertEqual(exports, measured['exports'], measured)
        self.assertTrue(all(command == 'export' for command in measured['bd_commands']), measured)
        return body

    def owned_records(self, prefix, parent):
        import reference_records
        import capability_records
        import requirement_records
        import test_reference_records as refs
        import test_capability_records as caps
        payload = refs.entry(operation_id=prefix + '-ref', key='search.' + prefix,
                             title='Headwater reference', statement='A headwater native rule.', owner='person:owner')
        reference_records.apply_native(payload, 'worker', self.native, self.project)
        payload = caps.entry(operation_id=prefix + '-cap', key='search.' + prefix,
                             name='Headwater capability', summary='A headwater native capability.', owner='person:owner')
        capability_records.apply_native(payload, 'worker', self.native, self.project)
        payload = dict(schema_version=1, operation_id=prefix + '-req', operation='draft',
                       kind='requirement', parent=parent, key='R01' if prefix == 'fresh' else 'R99',
                       title='Headwater requirement', description='A headwater native requirement.',
                       acceptance_state='draft')
        requirement_records.apply_native(payload, 'worker', self.native, self.project)

    def test_generation_changes_and_reads_never_mutate_native_records(self):
        import briefing
        import read_cache
        before_top = {path.name for path in self.project.iterdir()}
        self.find('fresh-cold', 'unmatchedcoldterm', exports=1)
        self.find('fresh-warm', 'unmatchedcoldterm', exports=0)
        task = json.loads(self.native(['create', 'Native topic freshness task', '--assignee', 'worker', '--json']))['id']
        made = self.find('fresh-task', task, '--kind', 'task', exports=1)
        self.assertEqual('task:' + task, made['items'][0]['pointer'])
        cid = str(json.loads(self.native(['comments', 'add', task, 'amethystcairn ordinary evidence', '--json']))['id'])
        made = self.find('fresh-comment', 'amethystcairn', '--kind', 'comment', exports=1)
        self.assertEqual('comment:' + cid, made['items'][0]['pointer'])

        def checkpoint(summary):
            rows = self.case.export_rows()
            current = briefing.brief(rows, 'pp', task)
            payload = dict(schema_version=1, task=task,
                           previous=(current['checkpoint'] or {}).get('comment_id'),
                           activity_cursor=current['activity_cursor'], source_commit='', branch='',
                           intent='Native search freshness', acceptance='Read current source',
                           summary=summary, next_action='Continue', open_items=[], resolved=[])
            return briefing.save_checkpoint(rows, 'pp', task, payload, 'worker', self.native)
        checkpoint('zorandel')
        self.assertEqual(1, self.find('fresh-checkpoint-one', 'zorandel', '--kind', 'checkpoint', exports=1)['total'])
        checkpoint('quenvora')
        self.assertEqual(1, self.find('fresh-checkpoint-two', 'quenvora', '--kind', 'checkpoint', exports=1)['total'])
        self.assertEqual(0, self.find('fresh-checkpoint-old', 'zorandel', '--kind', 'checkpoint', exports=0)['total'])
        self.owned_records('fresh', task)
        for index, kind in enumerate(('ref', 'cap', 'requirement')):
            answer = self.find('fresh-' + kind, 'headwater', '--kind', kind, exports=1 if index == 0 else 0)
            self.assertEqual(1, answer['total'], (kind, answer))

        before = self.case.export_rows()
        head = read_cache.project_generation(self.root, 'pp')
        self.find('fresh-read-only', 'headwater', exports=0)
        self.assertEqual(head, read_cache.project_generation(self.root, 'pp'))
        self.assertEqual(before, self.case.export_rows())
        self.assertLessEqual({path.name for path in self.project.iterdir()} - before_top,
                             {'.coordination.lock', '.read-cache', '.reference-requests',
                              '.capability-requests', '.requirement-requests'})

    def test_kittrial_scale_warm_find_p95_under3s(self):
        import briefing
        import review_workflow
        import test_briefing as typed
        from requirements import canonical_bytes
        rows = []
        for number in range(3400):
            task = 'pp-find%04d' % number
            def cid(label):
                return str(uuid.uuid5(uuid.NAMESPACE_URL, 'synthetic-find/' + task + '/' + label))
            def comment(label, text, stamp='2026-01-02T03:04:05Z'):
                return dict(id=cid(label), text=text, author='worker', created_at=stamp)
            description = ('Headwater recovery keeps the current task and its source pointers visible. '
                           'An ordinary read respects project access, reports incomplete evidence, and never '
                           'uses stored prose as an instruction. This is synthetic benchmark text.')
            ordinary = ('A headwater topic was discussed with its native source and exact revision. '
                        'The next reader can recover that evidence through a bounded lexical query, '
                        'without inspecting a room, a local file, another project, or a remote URL. ' * 2)
            row = dict(id=task, title='Synthetic headwater topic %04d' % number,
                       description=description, acceptance_criteria='Retain bounded source evidence.',
                       status='in_progress', issue_type='task', priority=2, assignee='worker', created_by='worker',
                       created_at='2026-01-02T03:04:05Z', updated_at='2026-01-02T03:05:05Z', labels=[],
                       comments=[comment('ordinary', ordinary),
                                 comment('lane', 'Kind: lane-state-v1\n' + json.dumps(dict(
                                     schema_version=1, task=task, state='working', checkpoint=cid('checkpoint'))))])
            if number < 400:
                snapshot = briefing.snapshot([row], 'pp', task)
                payload = dict(schema_version=1, task=task, previous=None,
                               activity_cursor=briefing.activity_cursor(snapshot), source_commit='a' * 40,
                               branch='work/topic', intent='Recover headwater source evidence',
                               acceptance='Keep exact current source links', summary='A headwater rule is current.',
                               next_action='Read the cited evidence', open_items=[dict(
                                   id='q1', kind='question', text='Which headwater rule applies?', source='task:' + task)],
                               resolved=[])
                row['comments'].append(comment('checkpoint', briefing.PREFIX + canonical_bytes(payload).decode(),
                                               '2026-01-02T03:04:06Z'))
            if 400 <= number < 600:
                payload = typed.contribution('a' * 40)
                payload.update(task=task, operation_id='benchmark-%04d' % number,
                               summary='The headwater topic has exact contribution evidence.')
                row['comments'].append(comment('contribution', review_workflow.PREFIX + canonical_bytes(payload).decode(),
                                               '2026-01-02T03:04:06Z'))
            rows.append(row)
        self.assertEqual(7400, sum(len(row['comments']) for row in rows))
        raw = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows).encode()
        imported = self.root / 'synthetic-find-input.jsonl'
        imported.write_bytes(raw)
        # Setup uses bounded native imports. The full fixture and the measured
        # calls are unchanged; no one import must seed every comment at once.
        for start in range(0, len(rows), 200):
            batch = rows[start:start + 200]
            part = self.root / 'synthetic-find-batch.jsonl'
            part.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in batch))
            began = time.monotonic()
            self.native(['import', str(part), '--json'])
            self.emit(dict(sample='fixture-import-%02d' % (start // 200), rows=len(batch),
                           imported_so_far=start + len(batch), seconds=time.monotonic() - began))
        self.owned_records('perf', rows[0]['id'])
        native_rows = self.case.export_rows()
        for row in [row for row in native_rows if row['id'] in {r['id'] for r in rows[400:405]}]:
            source = next(comment for comment in row['comments']
                          if comment['text'].startswith(review_workflow.PREFIX))
            payload = typed.approval(str(source['id']))
            payload.update(task=row['id'], operation_id='benchmark-approval-' + row['id'],
                           summary='The headwater contribution was reviewed.')
            review_workflow.execute(native_rows, row['id'], 'reviewer', payload,
                                    lambda args: self.native(args, actor='reviewer'), operators=[], journal=self.project)
        exported = self.case.export_rows()
        self.assertGreaterEqual(len(exported), 3400)
        self.assertGreaterEqual(sum(len(row.get('comments') or []) for row in exported), 7400)
        self.emit(dict(sample='fixture', imported_rows=len(rows), imported_comments=7400,
                       imported_bytes=len(raw), imported_sha256=hashlib.sha256(raw).hexdigest(),
                       ordinary_comments=3400, excluded_lane_envelopes=3400,
                       current_checkpoints=400, contribution_records=200, actual_approvals=5,
                       actual_rows=len(exported), actual_comments=sum(len(row.get('comments') or []) for row in exported)))
        cold, cold_measure = self.measure('scale-cold', 'headwater')
        self.assertEqual(1, cold_measure.get('exports'), cold_measure)
        samples = [self.measure('scale-warm-%02d' % number, 'headwater')[1] for number in range(20)]
        values = sorted(sample['seconds'] for sample in samples)
        p95 = values[math.ceil(len(values) * 0.95) - 1]
        completed = sum(sample.get('returncode') == 0 and sample.get('stale') == 'current' for sample in samples)
        report = dict(sample='scale-summary', samples=samples, nearest_rank_p95_seconds=p95,
                      completed=completed, target_seconds=3.0, target_met=completed == 20 and p95 < 3.0,
                      method='Real local client wire and endpoint process, native HEAD, JSON encoding/decoding; '
                             'process tracing included, no HTTP or SSH network claim.')
        self.emit(report)
        self.assertEqual(20, completed, report)
        self.assertTrue(all(sample.get('documents', 0) >= 7400 for sample in samples), report)
        self.assertTrue(all(sample['exports'] == 0 and sample['head_queries'] == 2
                            and not sample['bd_commands'] for sample in samples), report)
        self.assertLess(p95, 3.0, report)


if __name__ == '__main__':
    unittest.main()
