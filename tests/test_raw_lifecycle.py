"""Contributor lifecycle namespace: final effects, aliases and failed reads."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import raw_lifecycle as raw


def row(**changes):
    return dict(dict(id='pp-task', title='ordinary', issue_type='task', description='', labels=[]), **changes)


class RawLifecycleTests(unittest.TestCase):
    def guard(self, args, rows=None, attachments=None):
        raw.guard(args, attachments or {}, lambda tokens: rows if rows is not None else [row()])

    def rejected(self, args, **kwargs):
        with self.assertRaisesRegex(ValueError, 'lifecycle action.*no native write'):
            self.guard(args, **kwargs)

    def test_every_namespace_and_label_spelling(self):
        for dim in raw.NAMES:
            for value in (dim+':passed', 'plain,'+dim+':passed', '"'+dim+':passed"'):
                for flag in ('--label', '--labels', '-l'):
                    for tokens in ([flag, value], [flag+'='+value]):
                        self.rejected(['create', 'ordinary', *tokens])
                for flag in ('--add-label', '--set-labels', '--remove-label'):
                    self.rejected(['update', 'pp-task', flag+'='+value])
        self.rejected(['create', 'ordinary', '-qlreviewed:passed'])

    def test_string_flag_values_are_not_scanned_as_flags(self):
        self.guard(['create', 'ordinary', '--description', '--labels=reviewed:passed'])
        self.guard(['create', '--', '--labels=reviewed:passed'])

    def test_both_event_spellings_without_parent_or_payload(self):
        for dim in raw.NAMES:
            for typeflag in (['--type', 'event'], ['--type=event'], ['-tevent'], ['-t=event'], ['-qtevent']):
                self.rejected(['create', 'State change: '+dim+' → passed', *typeflag])
                self.rejected(['create', 'ordinary', *typeflag, '-dChanged '+dim+' from unknown to passed'])
        self.rejected(['create', '--title=State change: reviewed → passed', '-tevent'])

    def test_conversion_and_mutation_judge_old_and_final_rows(self):
        plain = row(description='Set reviewed to passed')
        self.rejected(['update', 'pp-task', '-tevent'], rows=[plain])
        event = row(issue_type='event', title='State change: reviewed → passed')
        for args in (['--title', 'ordinary'], ['--type', 'task'], ['--description', 'ordinary'], ['--parent', 'pp-other']):
            self.rejected(['update', 'pp-task', *args], rows=[event])
        self.rejected(['update', 'pp-task', '--title=State change: deployed → failed'], rows=[row(issue_type='event')])
        self.rejected(['dep', 'remove', 'pp-task', 'pp-other'], rows=[event, row(id='pp-other')])
        self.rejected(['update', 'pp-task', 'pp-other', '-tevent'], rows=[row(), row(id='pp-other', description='Set tested to passed')])
        # Duplicate scalar flags have native last-value semantics.
        self.rejected(['create', 'State change: reviewed → passed', '-ttask', '--type=event'])
        self.guard(['create', 'State change: reviewed → passed', '-tevent', '--type=task'])

    def test_inheritance_replacement_and_legacy_labels(self):
        holder = row(labels=['reviewed:passed', 'review-ready'])
        # kittrial-5bb.260 F1: the inheritance refusal ends with the flag - the whole
        # remedy - and carries no lifecycle advice.
        for args in (['create', 'child', '--parent=pp-task'],
                     ['create', 'child', '--parent=pp-task', '--no-inherit-labels=false']):
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as refused:
                    self.guard(args, rows=[holder])
                sentence = str(refused.exception)
                self.assertIn('inheriting lifecycle labels requires --no-inherit-labels.'
                              ' Re-run with --no-inherit-labels.', sentence)
                self.assertNotIn('lifecycle action', sentence)   # the flag is the whole remedy
        self.guard(['create', 'child', '--parent=pp-task', '--no-inherit-labels=true'], rows=[holder])
        self.rejected(['update', 'pp-task', '--set-labels=review-ready'], rows=[holder])
        for label in ('review-ready', 'reviewed', 'live:resolved-other'):
            if label.startswith('live:'):
                self.rejected(['update', 'pp-task', '--add-label', label])
            else:
                self.guard(['update', 'pp-task', '--add-label', label], rows=[holder])
        self.guard(['update', 'pp-task', '--set-labels=reviewed,review-ready'])

    def test_description_attachment_and_ambiguities(self):
        item = {'body': {'flag': '--body-file', 'text': 'Set integrated to passed'}}
        self.rejected(['create', 'ordinary', '-tevent', '@attachment:body'], attachments=item)
        self.rejected(['update', 'pp-task', '@attachment:body'], rows=[row(issue_type='event')], attachments=item)
        self.rejected(['create', 'ordinary', '-tevent', '-dordinary', '@attachment:body'], attachments=item)
        for args in (['create', 'ordinary', '--stdin'], ['update', 'pp-task', '--type'], ['create', 'one', 'two'], ['create', 'one', '--title=two'], ['create', 'child', '--parent=pp-task', '--no-inherit-labels=maybe']):
            self.rejected(args)

    def test_incomplete_or_ambiguous_reads_refuse(self):
        for rows in ([], {}, [None], [row(issue_type=None)], [row(title=None)], [row(description=1)], [row(labels='reviewed:passed')], [row(labels=[None])], [row(malformed=True)], [row(), row()]):
            self.rejected(['update', 'pp-task', '--title=ordinary'], rows=rows)

    def test_two_resolved_tokens_for_one_healthy_row_are_allowed(self):
        self.guard(['update', 'pp-task', 'task', '--title=ordinary'], rows=[row(), row()])
        self.rejected(['update', 'pp-task', 'task', '--title=ordinary'], rows=[row(), row(description='disagrees')])

    def test_nonstructural_updates_cannot_edit_lifecycle_events(self):
        event = row(issue_type='event', title='State change: reviewed → passed')
        for flags in (['--priority=2'], ['--assignee=mallory'], ['--status=open'],
                      ['--add-label=ordinary'], ['--notes=ordinary'], []):
            self.rejected(['update', 'pp-task', *flags], rows=[event])
            self.guard(['update', 'pp-task', *flags])

    def test_ordinary_prose_and_non_lifecycle_events(self):
        self.guard(['create', 'Discuss State change: reviewed → passed', '-tevent', '-dWe discussed Set reviewed to passed'])
        self.guard(['create', 'State change: traffic → green', '-tevent', '-dSet traffic to green'])
        self.guard(['create', 'State change: reviewed → passed', '-ttask', '-dSet reviewed to passed'])
        self.guard(['update', 'pp-task', '--description=Discuss State change: tested → passed'])
        self.guard(['update', 'pp-task', '--description=Set reviewed to passed'])

    def test_event_membership_cannot_change_even_without_reserved_content(self):
        self.rejected(['update', 'pp-task', '--type=event'])
        self.rejected(['update', 'pp-task', '--type=task'], rows=[row(issue_type='event')])
        self.guard(['update', 'pp-task', '--type=event'], rows=[row(issue_type='event')])
        self.guard(['update', 'pp-task', '--type=event', '--type=task'])
        self.rejected(['update', 'pp-task', '--type=task', '--type=event'])

    def test_creation_and_parent_references_check_the_actual_event(self):
        rows = {'pp-task': row(), 'pp-event': row(id='pp-event', issue_type='event',
                                                title='State change: reviewed → failed')}
        for args in (['create', 'ordinary', '--deps=parent-child:pp-event'],
                     ['create', 'ordinary', '--waits-for=pp-event'],
                     ['update', 'pp-task', '--parent=pp-event']):
            with self.assertRaisesRegex(ValueError, 'linking.*lifecycle action.*no native write'):
                raw.guard(args, {}, lambda tokens: [rows[t] for t in tokens])
        raw.guard(['create', 'ordinary', '--deps=parent-child:pp-task'], {},
                  lambda tokens: [rows[t] for t in tokens])

    def test_ephemeral_update_last_boolean_value_and_persistence(self):
        for flag in ('--ephemeral', '--ephemeral=true', '--ephemeral=1', '--ephemeral=bad'):
            self.rejected(['update', 'pp-task', flag])
        self.guard(['update', 'pp-task', '--ephemeral=false'])
        self.guard(['update', 'pp-task', '--ephemeral', '--ephemeral=false'])
        self.rejected(['update', 'pp-task', '--ephemeral=false', '--ephemeral'])
        self.guard(['update', 'pp-task', '--persistent'])

    def test_duplicate_description_uses_native_last_scalar_value(self):
        self.guard(['update', 'pp-task', '--description=Set reviewed to passed',
                    '--description=ordinary'], rows=[row(issue_type='event')])
        self.rejected(['update', 'pp-task', '--description=ordinary',
                       '--description=Set reviewed to passed'], rows=[row(issue_type='event')])

    def test_label_case_is_not_folded_and_surrounding_space_is_checked(self):
        self.guard(['create', 'ordinary', '-lReviewed:passed'])
        for text in (' reviewed:passed ', '\nreviewed:passed', 'plain,reviewed:passed'):
            self.rejected(['create', 'ordinary', '-l', text])
        self.rejected(['create', 'ordinary', '-lplain', '-lreviewed:passed'])

    def test_terminal_commands_protect_every_named_event(self):
        for command in ('close', 'reopen'):
            for event in (row(issue_type='event', title='State change: reviewed → passed'),
                          row(issue_type='event', description='Changed tested from pending to passed')):
                for args in ([command, 'pp-task'], [command, '-rordinary', 'pp-task', '--json'],
                             [command, '--', 'pp-task']):
                    self.rejected(args, rows=[event])
                    self.guard(args)
                self.rejected([command, 'pp-other', 'pp-task'], rows=[row(id='pp-other'), event])
            self.rejected([command, 'pp-task'], rows=[row(title=None)])
            self.rejected([command])
            self.rejected([command, 'pp-task', '--unknown'])
        self.rejected(['close', '-qf', 'pp-task'], rows=[row(issue_type='event', title='State change: live → passed')])
        self.guard(['close', '-qf', 'pp-task'])


if __name__ == '__main__':
    unittest.main()
