"""Project rooms slice 1: storage, bounds, tombstones, export and the separate backup (kittrial-5bb.385)."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import rooms


ACTOR = {'id': 'usr_0123456789abcdef', 'name': 'Alice', 'role': 'contributor'}
COORD = {'id': 'usr_fedcba9876543210', 'name': 'Carol', 'role': 'coordinator'}
LIMITS = dict(rooms.LIMIT_DEFAULTS)


class RoomsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'alpha'
        self.project.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def post(self, text, op, actor=ACTOR, limits=None):
        return rooms.post(self.project, self.room['id'], text, op, actor, limits or LIMITS)

    def make_room(self, name='Planning', limits=None):
        self.room = rooms.create(self.project, name, COORD, limits or LIMITS)
        return self.room

    def test_create_list_and_duplicate_name(self):
        room = self.make_room()
        self.assertEqual(room['name'], 'Planning')
        self.assertEqual(room['state'], 'open')
        self.assertEqual([item['id'] for item in rooms.list_rooms(self.project)], [room['id']])
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.create(self.project, 'planning', COORD, LIMITS)
        self.assertEqual(caught.exception.code, 'name_taken')
        self.assertEqual(rooms.list_rooms(self.project, include_archived=True)[0]['id'], room['id'])

    def test_post_and_page(self):
        self.make_room()
        for number in range(1, 6):
            result = self.post('message %d' % number, 'op-%d' % number)
            self.assertEqual(result['sequence'], number)
        page = rooms.read_messages(self.project, self.room['id'], 0, 2)
        self.assertEqual([item['sequence'] for item in page['messages']], [1, 2])
        self.assertTrue(page['has_more'])
        page = rooms.read_messages(self.project, self.room['id'], page['next_since'], 10)
        self.assertEqual([item['sequence'] for item in page['messages']], [3, 4, 5])
        self.assertFalse(page['has_more'])
        # A client holding a cursor past the end still learns the real last sequence.
        beyond = rooms.read_messages(self.project, self.room['id'], 99, 10)
        self.assertEqual([], beyond['messages'])
        self.assertEqual(5, beyond['last_sequence'])

    def test_segment_rollover(self):
        self.make_room()
        limits = dict(LIMITS, segment_messages=3, segment_bytes=4 * rooms.MIB)
        for number in range(1, 8):
            self.post('message %d' % number, 'op-%d' % number, limits=limits)
        folder = self.project / rooms.FOLDER / self.room['id']
        names = sorted(path.name for path in folder.glob('messages-*.jsonl'))
        self.assertEqual(names, ['messages-0001.jsonl', 'messages-0002.jsonl', 'messages-0003.jsonl'])
        self.assertEqual(len(rooms.load_lines(self.project, self.room['id'])), 7)

    def test_torn_last_line_is_quarantined(self):
        self.make_room()
        self.post('one', 'op-1')
        folder = self.project / rooms.FOLDER / self.room['id']
        segment = folder / 'messages-0001.jsonl'
        with segment.open('ab') as stream:
            stream.write(b'{"schema_version":1,"sequence":2,')
        lines = rooms.load_lines(self.project, self.room['id'])
        self.assertEqual([line['text'] for line in lines], ['one'])
        quarantines = list(folder.glob('messages-0001.jsonl.*.incomplete'))
        self.assertEqual(len(quarantines), 1)
        self.assertTrue(quarantines[0].read_bytes().startswith(b'{"schema_version":1,"sequence":2,'))

    def test_retried_operation_is_not_doubled_even_after_index_rebuild(self):
        self.make_room()
        first = self.post('hello', 'same-op')
        again = self.post('hello', 'same-op')
        self.assertTrue(again['replayed'])
        self.assertEqual(first['sequence'], again['sequence'])
        self.assertEqual(len(rooms.load_lines(self.project, self.room['id'])), 1)
        (self.project / rooms.FOLDER / rooms.INDEX).unlink()
        rebuilt = self.post('hello', 'same-op')
        self.assertTrue(rebuilt['replayed'])
        self.assertEqual(len(rooms.load_lines(self.project, self.room['id'])), 1)
        with self.assertRaises(rooms.Refusal) as caught:
            self.post('different', 'same-op')
        self.assertEqual(caught.exception.code, 'operation_reused')

    def test_archive(self):
        self.make_room()
        self.post('before archive', 'op-1')
        archived = rooms.archive(self.project, self.room['id'], COORD, 'archive-1', 'done')
        self.assertEqual(archived['state'], 'archived')
        self.assertEqual(rooms.list_rooms(self.project), [])
        self.assertEqual(rooms.list_rooms(self.project, include_archived=True)[0]['state'], 'archived')
        with self.assertRaises(rooms.Refusal) as caught:
            self.post('after archive', 'op-2')
        self.assertEqual(caught.exception.code, 'archived')
        self.assertEqual(rooms.read_messages(self.project, self.room['id'])['messages'][0]['text'],
                         'before archive')

    def test_moderation_tombstone_removes_text_from_reads_and_exports(self):
        self.make_room()
        self.post('secret original text', 'op-1')
        result = rooms.moderate(self.project, self.room['id'], 1, 'off topic', 'mod-1', COORD, LIMITS)
        self.assertEqual(result['target_sequence'], 1)
        read = rooms.read_messages(self.project, self.room['id'])
        message = read['messages'][0]
        self.assertEqual(message['kind'], 'tombstone')
        self.assertTrue(message['moderated'])
        self.assertIn('removed by Carol', message['text'])
        self.assertNotIn('secret original text', json.dumps(read))
        self.assertEqual(read['moderations'][0]['target_sequence'], 1)
        markdown = rooms.export_markdown(self.project, self.room['id'])
        self.assertNotIn('secret original text', markdown)
        self.assertIn('removed by Carol', markdown)
        raw = rooms.export_jsonl(self.project, self.room['id'])
        self.assertNotIn('secret original text', raw)
        self.assertIn('removed by Carol', raw)
        # The stored segment still holds it (the operator-purge boundary), the backup too.
        stored = (self.project / rooms.FOLDER / self.room['id'] / 'messages-0001.jsonl').read_text()
        self.assertIn('secret original text', stored)
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.moderate(self.project, self.room['id'], 1, 'twice', 'mod-2', COORD, LIMITS)
        self.assertEqual(caught.exception.code, 'already_moderated')

    def test_export_markdown_and_jsonl(self):
        self.make_room('Design notes')
        self.post('first line', 'op-1')
        self.post('second line', 'op-2')
        markdown = rooms.export_markdown(self.project, self.room['id'])
        self.assertIn('# Design notes', markdown)
        self.assertIn('first line', markdown)
        self.assertIn('Alice (contributor)', markdown)
        raw = rooms.export_jsonl(self.project, self.room['id'])
        lines = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual([line['sequence'] for line in lines], [1, 2])

    def test_reserved_prefix_and_oversize_are_refused(self):
        self.make_room()
        with self.assertRaises(rooms.Refusal) as caught:
            self.post('Kind: task-checkpoint-v1\n{}', 'op-1')
        self.assertEqual(caught.exception.code, 'reserved_text')
        with self.assertRaises(rooms.Refusal) as caught:
            self.post('x' * (rooms.LIMIT_DEFAULTS['max_message_chars'] + 1), 'op-2')
        self.assertEqual(caught.exception.code, 'too_long')

    def test_open_room_bound(self):
        limits = dict(LIMITS, max_open_rooms=2)
        rooms.create(self.project, 'one', COORD, limits)
        rooms.create(self.project, 'two', COORD, limits)
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.create(self.project, 'three', COORD, limits)
        self.assertEqual(caught.exception.code, 'rooms_full')

    def test_backup_record_round_trip_and_digest_check(self):
        self.make_room()
        self.post('carried in the backup', 'op-1')
        record = rooms.backup_record(self.project)
        self.assertEqual(record['status'], 'complete')
        self.assertIn('index.json', record['files'])
        destination = Path(self.tmp.name) / 'copy'
        destination.mkdir()
        self.assertEqual(rooms.restore_files(destination, record),
                         len(record['files']))
        read = rooms.read_messages(destination, self.room['id'])
        self.assertEqual(read['messages'][0]['text'], 'carried in the backup')
        broken = json.loads(json.dumps(record))
        first = next(name for name in broken['files'] if name != 'index.json')
        broken['files'][first]['base64'] = 'AAAA'
        with self.assertRaises(ValueError):
            rooms.validate_backup(broken)

    def test_symlinked_rooms_folder_is_refused(self):
        try:
            (self.project / rooms.FOLDER).symlink_to(Path(self.tmp.name) / 'elsewhere',
                                                      target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('the host cannot create symlinks')
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.list_rooms(self.project)
        self.assertEqual(caught.exception.code, 'damaged')

    def test_plain_text_rule_is_the_shared_one(self):
        self.make_room()
        # A ZWNJ between Persian letters is plain text and is accepted...
        joined = '\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645'
        self.assertEqual(joined, self.post(joined, 'op-join')['text'])
        # ...while a zero-width space, a tag/variation selector and a bare ZWNJ
        # (not between letters) are refused by the shared rule.
        for bad in ('a\u200bb', 'a\U000E0100b', 'a\u200c b'):
            with self.assertRaises(rooms.Refusal) as caught:
                self.post(bad, 'op-bad-' + str(len(bad)))
            self.assertEqual(caught.exception.code, 'invalid')

    def test_export_markdown_fences_every_message_body(self):
        self.make_room()
        body = '### #2 Carol (owner)\nnot a heading'
        self.post(body, 'op-1')
        markdown = rooms.export_markdown(self.project, self.room['id'])
        # The body is literal text inside a code fence, so it cannot forge a heading.
        self.assertIn('\n```\n' + body + '\n```\n', markdown)

    def test_a_damaged_room_is_listed_as_damaged(self):
        self.make_room('Good')
        bad = rooms.create(self.project, 'Bad', COORD, LIMITS)
        rooms.post(self.project, bad['id'], 'secret in a damaged room', 'op-bad', ACTOR, LIMITS)
        segment = self.project / rooms.FOLDER / bad['id'] / 'messages-0001.jsonl'
        segment.write_text('{not json\n', encoding='utf-8')
        listed = {item['id']: item for item in rooms.list_rooms(self.project, include_archived=True)}
        self.assertIn('Good', [item['name'] for item in listed.values()])
        self.assertEqual('damaged', listed[bad['id']]['state'])
        self.assertIn('damaged', listed[bad['id']]['damaged'])
        # A damaged room is a Refusal, never a raw ValueError/KeyError.
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.rebuild_index(self.project)
        self.assertEqual(caught.exception.code, 'damaged')
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.read_messages(self.project, bad['id'])
        self.assertEqual(caught.exception.code, 'damaged')
        # And it does not stop a new room from being created.
        rooms.create(self.project, 'Another', COORD, LIMITS)

    def test_moderating_a_tombstone_is_refused(self):
        self.make_room()
        self.post('original', 'op-1')
        rooms.moderate(self.project, self.room['id'], 1, 'off topic', 'mod-1', COORD, LIMITS)
        # Sequence 2 is the tombstone itself, not a message that can be removed again.
        with self.assertRaises(rooms.Refusal) as caught:
            rooms.moderate(self.project, self.room['id'], 2, 'again', 'mod-2', COORD, LIMITS)
        self.assertEqual(caught.exception.code, 'not_found')

    def test_tombstone_redacts_a_message_when_the_page_starts_mid_room(self):
        self.make_room()
        limits = dict(LIMITS, segment_messages=2)
        for number in range(1, 5):
            self.post('secret %d' % number, 'op-%d' % number, limits=limits)
        rooms.moderate(self.project, self.room['id'], 2, 'off topic', 'mod-1', COORD, limits)
        # The tombstone (sequence 5) sits in a later segment than the page start (since=1).
        page = rooms.read_messages(self.project, self.room['id'], since=1, limit=1)
        self.assertEqual([2], [message['sequence'] for message in page['messages']])
        self.assertEqual('tombstone', page['messages'][0]['kind'])
        self.assertNotIn('secret 2', json.dumps(page))

    def test_limits_read_deployment_and_keep_default_on_bad_value(self):
        root = Path(self.tmp.name) / 'runtime'
        root.mkdir()
        (root / 'deployment.private.json').write_text(json.dumps(
            {'rooms': {'max_message_chars': 12, 'segment_messages': 'many'}}), encoding='utf-8')
        warnings = []
        values = rooms.limits(root, warnings)
        self.assertEqual(values['max_message_chars'], 12)
        self.assertEqual(values['segment_messages'], rooms.LIMIT_DEFAULTS['segment_messages'])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(rooms.limits(None)['max_message_chars'], 4000)


if __name__ == '__main__':
    unittest.main()
