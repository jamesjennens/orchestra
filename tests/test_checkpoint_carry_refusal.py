"""The carry-forward refusal names the ids it compared (kittrial-5bb.287).

Two jjbp lanes could not write ANY checkpoint after the 2026-10-13 kit update:
six payload shapes each were refused with one sentence that named nothing. The
rule (`old-now != resolved`) is correct and unchanged; the refusal was not
actionable. These tests pin the three distinct mistakes that reach it - an item
dropped without a resolution, an item resolved and still carried, and an id that
was never open - the stored-pair wording the chain walk uses instead, and the
harness mistake that makes every later write read as a stale previous checkpoint.

The real-`bd` class proves the same mechanism against real native comments in a
disposable runtime, following the fixture in `tests/test_bd_label_aliases.py`
the way `tests/test_checkpoint_real_bd.py` does.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import briefing as b
from requirements import canonical_bytes
import test_bd_label_aliases as rb

TASK = 'trial-task'
PROJECT = 'trial'
STAMP = '2026-10-15T10:00:00Z'


def item(name, text='Unresolved work'):
    return dict(id=name, kind='dependency', text=text, source='task build list')


def resolution(name, reason='Superseded by the delivered revision', evidence='contribution 01a13314'):
    return dict(id=name, reason=reason, evidence=evidence)


class Store:
    """A native comment store whose `run` really persists every accepted checkpoint.

    A harness that only RETURNS an id never forms a chain: the next write reads no
    previous checkpoint, so it is refused as `Stale previous checkpoint` and the
    carry rule is never reached. Persisting the `comments add` as the server does
    is what makes this fixture test the mechanism the lanes hit.
    """

    def __init__(self, persist=True):
        self.comments = [dict(id='first', text='A finding', author='carol/session',
                              created_at=STAMP)]
        self.persist = persist
        self.writes = []
        self.counter = 0
        # A harness that does not persist still HANDS BACK the id of the record it
        # claims to have written, so this is the previous checkpoint its next call
        # believes in; the server, which reads its own comments, has none.
        self.claimed = None

    def rows(self):
        return [dict(id=TASK, title='A task', issue_type='task', status='in_progress',
                     assignee='alice/session', description='Intent',
                     acceptance_criteria='Acceptance', labels=[], comments=list(self.comments))]

    def run(self, argv):
        assert argv[:2] == ['comments', 'add'], argv
        self.counter += 1
        cid = 'cp%d' % self.counter
        self.writes.append(argv)
        if self.persist:
            self.comments.append(dict(id=cid, text=argv[3], author='alice/session',
                                      created_at=STAMP))
        else:
            self.claimed = cid
        return json.dumps(dict(id=cid))

    def current(self):
        current, _invalid = b.checkpoints(b.task_row(self.rows(), TASK))
        return current

    def payload(self, **changes):
        current = self.current()
        snap = b.snapshot(self.rows(), PROJECT, TASK)
        payload = dict(schema_version=1, task=TASK,
                       previous=str(current[1]['id']) if current else self.claimed,
                       activity_cursor=b.activity_cursor(snap),
                       source_commit='a159cfe8', branch='work/feature',
                       intent='Deliver the feature', acceptance='Pass the agreed checks',
                       summary='Part 4 delivered', next_action='Run the focused tests',
                       open_items=[], resolved=[])
        payload.update(changes)
        return payload

    def save(self, **changes):
        return b.save_checkpoint(self.rows(), PROJECT, TASK, self.payload(**changes),
                                 'alice/session', self.run, provenance_writes=False)


class CarryRefusalNamesIdsTests(unittest.TestCase):
    """The sentence names old-now, resolved and the parent's own resolutions."""

    def seeded(self):
        store = Store()
        store.save(open_items=[item('parts-4-5-not-built'), item('blocking-review-items-answered-rev5'),
                               item('integration-is-inference')],
                   resolved=[])
        return store

    def refusal(self, store, **changes):
        store.writes = []
        with self.assertRaises(ValueError) as caught:
            store.save(**changes)
        self.assertEqual(store.writes, [], 'a refused payload must not be written')
        return str(caught.exception)

    def test_item_dropped_without_resolution_names_the_id(self):
        store = self.seeded()
        said = self.refusal(store, open_items=[item('parts-4-5-not-built'),
                                               item('blocking-review-items-answered-rev5')],
                            resolved=[])
        self.assertIn('Carry every unresolved item forward', said)
        self.assertIn('neither carried in open_items nor resolved: integration-is-inference', said)
        self.assertIn('resolve or supersede every id named here', said)

    def test_item_resolved_and_still_carried_names_the_id_and_the_fix(self):
        """The shape the jjbp lane sent last: it named the item in `resolved` and kept
        carrying it, so old-now was empty while resolved was not."""
        store = self.seeded()
        said = self.refusal(store,
                            open_items=[item('parts-4-5-not-built'),
                                        item('blocking-review-items-answered-rev5'),
                                        item('integration-is-inference')],
                            resolved=[resolution('parts-4-5-not-built')])
        self.assertIn('parts-4-5-not-built is in resolved and also in open_items', said)
        self.assertIn('resolved by omitting its id from open_items', said)

    def test_id_never_open_names_the_id_and_says_a_direction_is_not_an_item(self):
        store = self.seeded()
        said = self.refusal(store,
                            open_items=[item('parts-4-5-not-built'),
                                        item('blocking-review-items-answered-rev5'),
                                        item('integration-is-inference')],
                            resolved=[resolution('jjbp-j02.67-c01a13406-f922')])
        self.assertIn('jjbp-j02.67-c01a13406-f922 is in resolved and not open', said)
        self.assertIn('A direction is dispositioned with directions, never with resolved', said)
        self.assertIn('re-opened under a new id', said)

    def test_an_item_the_parent_already_resolved_is_named_the_same_way(self):
        """A stored record cannot be resolved twice, and the refusal says which id."""
        store = Store()
        store.save(open_items=[item('answered-in-an-earlier-checkpoint')], resolved=[])
        store.save(open_items=[], resolved=[resolution('answered-in-an-earlier-checkpoint')])
        said = self.refusal(store, open_items=[],
                            resolved=[resolution('answered-in-an-earlier-checkpoint')])
        self.assertIn('answered-in-an-earlier-checkpoint is in resolved and not open', said)

    def test_the_refusal_stays_bounded_when_every_id_is_missing(self):
        store = Store()
        names = ['item-%02d' % number for number in range(40)]
        store.save(open_items=[item(name) for name in names], resolved=[])
        said = self.refusal(store, open_items=[], resolved=[])
        self.assertIn('(+32 more)', said)
        self.assertLess(len(said), 1200, said)

    def test_a_correct_payload_is_still_accepted(self):
        """The rule is not weakened: resolving means dropping from open_items."""
        store = self.seeded()
        answer = store.save(open_items=[item('parts-4-5-not-built'),
                                        item('blocking-review-items-answered-rev5'),
                                        item('a-new-item')],
                            resolved=[resolution('integration-is-inference')])
        self.assertEqual(answer['comment_id'], 'cp2')
        current = store.current()
        self.assertEqual([x['id'] for x in current[0]['open_items']],
                         ['parts-4-5-not-built', 'blocking-review-items-answered-rev5', 'a-new-item'])

    def test_carrying_every_item_unchanged_is_still_accepted(self):
        store = self.seeded()
        current = store.current()
        store.save(open_items=[dict(x) for x in current[0]['open_items']], resolved=[])
        self.assertEqual(store.current()[1]['id'], 'cp2')

    def test_changed_item_text_is_still_refused_under_the_other_sentence(self):
        store = self.seeded()
        with self.assertRaises(ValueError) as caught:
            store.save(open_items=[item('parts-4-5-not-built'), item('integration-is-inference'),
                                   dict(item('blocking-review-items-answered-rev5'), text='Rewritten')],
                       resolved=[])
        self.assertIn('Carry unresolved items unchanged', str(caught.exception))


class ChainRefusalNamesTheStoredPairTests(unittest.TestCase):
    """A stored pair is not this request, and says so."""

    def stored_comment(self, cid, payload):
        return dict(id=cid, text=b.PREFIX + canonical_bytes(payload).decode(),
                    author='alice/session', created_at=STAMP)

    def rows(self, comments):
        return [dict(id=TASK, title='A task', issue_type='task', status='in_progress',
                     assignee='alice/session', description='Intent',
                     acceptance_criteria='Acceptance', labels=[], comments=comments)]

    def test_the_chain_walk_names_the_pair_and_the_operator_fix(self):
        store = Store()
        store.save(open_items=[item('kept'), item('dropped-without-resolution')], resolved=[])
        parent = store.current()[0]
        child = dict(parent, previous='cp1', open_items=[item('kept')], resolved=[])
        rows = self.rows(store.comments[:1] + [self.stored_comment('cp1', parent),
                                               self.stored_comment('cp2', child)])
        with self.assertRaises(ValueError) as caught:
            b.brief(rows, PROJECT, TASK)
        said = str(caught.exception)
        self.assertIn('Checkpoint history is unreadable: stored pair cp1 -> cp2', said)
        self.assertIn('dropped-without-resolution', said)
        self.assertIn('This is a stored pair, not this request', said)
        self.assertIn('correction record and publishes a re-anchoring checkpoint', said)

    def test_a_clean_chain_still_reads(self):
        store = Store()
        store.save(open_items=[item('kept')], resolved=[])
        store.save(open_items=[item('kept'), item('added')], resolved=[])
        self.assertEqual([x['id'] for x in b.brief(store.rows(), PROJECT, TASK)['unresolved']['items']],
                         ['kept', 'added'])


class PersistentHarnessTests(unittest.TestCase):
    """The harness the runbook warns about, shown failing and passing."""

    def test_a_run_that_only_returns_an_id_never_forms_a_chain(self):
        """The harness mistake: nothing is persisted, so the next write reads no parent."""
        store = Store(persist=False)
        store.save(open_items=[item('first-item')], resolved=[])
        with self.assertRaises(ValueError) as caught:
            store.save(open_items=[item('another-item')], resolved=[])
        self.assertIn('Stale previous checkpoint', str(caught.exception))

    def test_a_run_that_persists_forms_the_chain_and_reaches_the_carry_rule(self):
        store = Store()
        store.save(open_items=[item('first-item')], resolved=[])
        with self.assertRaises(ValueError) as caught:
            store.save(open_items=[item('another-item')], resolved=[])
        said = str(caught.exception)
        self.assertIn('Carry every unresolved item forward', said)
        self.assertNotIn('Stale previous checkpoint', said)
        self.assertIn('first-item', said)


class OlderClientSentenceTests(unittest.TestCase):
    """Item (4): an older client's payload is answered with what it must do."""

    def store(self):
        store = Store()
        store.save(open_items=[item('carried-item')], resolved=[])
        return store

    def test_the_legacy_shape_needs_no_client_update_and_is_told_so(self):
        """The shape the jjbp lane sent: the item is carried AND named in resolved."""
        store = self.store()
        with self.assertRaises(ValueError) as caught:
            store.save(open_items=[item('carried-item')], resolved=[resolution('carried-item')])
        said = str(caught.exception)
        self.assertIn('carried-item is in resolved and also in open_items', said)
        self.assertIn('No client update is required', said)

    def test_a_legacy_shape_that_obeys_the_rule_is_written_without_directions(self):
        store = self.store()
        answer = store.save(open_items=[item('carried-item'), item('new-item')], resolved=[])
        self.assertEqual(answer['comment_id'], 'cp2')
        stored = json.loads(store.comments[-1]['text'][len(b.PREFIX):])
        self.assertNotIn('directions', stored)
        self.assertNotIn('carried', stored)
        self.assertNotIn('provenance', stored)

    def test_a_direction_request_while_the_switch_is_off_names_the_installation_switch(self):
        store = self.store()
        with self.assertRaises(ValueError) as caught:
            store.save(open_items=[item('carried-item')],
                       resolved=[],
                       directions=[dict(id=TASK + '-cfirst', state='resolved', digest='a' * 64,
                                        note='answered', evidence='comment first')])
        said = str(caught.exception)
        self.assertIn('checkpoint_provenance_writes is off', said)
        self.assertIn('omit provenance/directions to write an older-kit-compatible checkpoint', said)


class ExamplePayloadHelpTests(unittest.TestCase):
    """Item (3): one minimal example payload that disposes of one direction."""

    EXAMPLE = 'example-direction-resolution.json'

    def test_the_checkpoint_help_carries_the_example_and_the_rule(self):
        notes = ' '.join(b.help_notes('checkpoint'))
        self.assertIn(self.EXAMPLE, notes)
        self.assertIn('A direction is NEVER resolved by `resolved`', notes)
        self.assertIn('No client update is required', notes)

    def test_the_docs_carry_the_same_example(self):
        docs = (Path(__file__).resolve().parents[1] / 'docs' / 'BRIEFINGS.md').read_text(encoding='utf-8')
        self.assertIn(self.EXAMPLE, docs)
        self.assertIn('A direction is NEVER resolved by `resolved`', docs)

    def test_the_example_file_is_a_valid_payload_shape(self):
        example = json.loads((Path(__file__).resolve().parents[1] / 'templates' /
                              self.EXAMPLE).read_text(encoding='utf-8'))
        self.assertEqual(set(example),
                         {'schema_version', 'task', 'previous', 'activity_cursor', 'source_commit',
                          'branch', 'intent', 'acceptance', 'summary', 'next_action',
                          'open_items', 'resolved', 'directions'})
        self.assertEqual(example['resolved'], [])
        self.assertEqual([x['id'] for x in example['directions']],
                         ['example-task-c01a13320-b9ed-7af7-a8ab-512af3e56bed'])
        problems = b.checkpoint_problems(example, 'example-task')
        self.assertEqual(problems, [], problems)


@unittest.skipIf(rb.endpoint is None, 'real bd checkpoint test needs POSIX endpoint imports')
@unittest.skipIf(rb.BD is None, 'real bd not configured (ORCHESTRA_BD_BIN or bd on PATH)')
class RealBdCarryRefusalTests(rb.RealBdLabelAliasTests):
    """The same mechanism against real native comments in a disposable runtime.

    The scratch-Dolt fixture, and the class-scoped server it starts, are reused by
    inheriting from `tests/test_bd_label_aliases.py`. Calling that class's
    classmethods with a bare TestCase (`setUpClass.__func__(cls)`, as the first
    revision of this file and `tests/test_checkpoint_real_bd.py` both do) leaves
    `cls` without `_watch_signals` and the class errors in `setUpClass` before
    either test runs. The fixture's own three label-alias tests are disabled here
    so this module reports only its own two; they still run in their own module.
    """

    test_create_label_spellings_refuse_every_reserved_value = None
    test_update_label_spellings_refuse_every_reserved_value = None
    test_ordinary_labels_still_reach_real_bd = None

    def native(self, *args, actor='op'):
        """bd's global --actor is the audit actor; a comment author is separate."""
        if args[:2] == ('comments', 'add'):
            args = (*args, '--author', actor)
        result = self.bd(*args, actor=actor)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def make_task(self):
        result = self.bd('create', 'Carry refusal task', '--assignee', 'op', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['id']

    def write(self, task, **changes):
        """Build a payload from a live brief and write it through save_checkpoint."""
        rows = self.export_rows()
        read = b.brief(rows, 'pp', task)
        payload = dict(schema_version=1, task=task,
                       previous=(read['checkpoint'] or {}).get('comment_id'),
                       activity_cursor=read['activity_cursor'], source_commit='', branch='',
                       intent='carry refusal', acceptance='name the ids',
                       summary='checkpoint', next_action='continue',
                       open_items=[], resolved=[])
        payload.update(changes)
        return b.save_checkpoint(rows, 'pp', task, payload, 'op',
                                 lambda args: self.native(*args), provenance_writes=False)

    def legacy_item(self, name):
        return item(name)

    def test_the_persisted_refusal_names_the_id_on_real_comments(self):
        task = self.make_task()
        self.write(task, open_items=[self.legacy_item('kept'), self.legacy_item('dropped')])
        with self.assertRaises(ValueError) as caught:
            self.write(task, open_items=[self.legacy_item('kept')], resolved=[])
        said = str(caught.exception)
        self.assertIn('dropped', said)
        self.assertIn('neither carried in open_items nor resolved', said)
        # Nothing was written by the refusal.
        read = b.brief(self.export_rows(), 'pp', task)
        self.assertEqual([x['id'] for x in read['unresolved']['items']], ['kept', 'dropped'])

    def test_the_persisted_chain_accepts_the_correct_disposition(self):
        task = self.make_task()
        self.write(task, open_items=[self.legacy_item('kept'), self.legacy_item('dropped')])
        self.write(task,
                   open_items=[self.legacy_item('kept'), self.legacy_item('added')],
                   resolved=[resolution('dropped', evidence='comment ' + task)])
        read = b.brief(self.export_rows(), 'pp', task)
        self.assertEqual([x['id'] for x in read['unresolved']['items']], ['kept', 'added'])


if __name__ == '__main__':
    unittest.main()
