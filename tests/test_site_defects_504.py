"""Site defects from review .502 (kittrial-5bb.504): switcher fallback, words for people,
room and task list layout, and the CSS that keeps long text wrapping.

The pure helpers (routes.switchTarget, ui.actorWords) run under Node when it is installed;
the source checks run everywhere, since they pin the markup and CSS that the browser uses.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / 'web'
SHIM = (Path(__file__).resolve().parent / 'web_dom_shim.mjs')


class SwitchTargetCase(unittest.TestCase):
    def test_switch_target_under_node(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed; the source checks still apply')
        routes = (WEB / 'js' / 'routes.js').as_uri()
        script = (
            "await import(process.argv[1]);"
            "const r = await import(process.argv[2]);"
            "const keep = r.switchTarget('/p/a/reviews?x=1', 'b', ['reviews', 'rooms']);"
            "const missing = r.switchTarget('/p/a/requirements', 'b', ['reviews']);"
            "const task = r.switchTarget('/p/a/t/a-1', 'b', ['reviews']);"
            "const tasks = r.switchTarget('/p/a', 'b', []);"
            "const off = r.switchTarget('/projects', 'b', []);"
            "console.log(JSON.stringify([keep, missing, task, tasks, off]));"
        )
        out = subprocess.run([node, '--input-type=module', '-e', script, SHIM.as_uri(), routes],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(0, out.returncode, out.stderr)
        keep, missing, task, tasks, off = json.loads(out.stdout)
        self.assertEqual({'path': '/p/b/reviews', 'fellBack': False, 'section': 'reviews'}, keep)
        self.assertEqual({'path': '/p/b', 'fellBack': True, 'section': 'requirements'}, missing)
        self.assertEqual({'path': '/p/b', 'fellBack': True, 'section': ''}, task)
        self.assertEqual({'path': '/p/b', 'fellBack': False, 'section': ''}, tasks)
        self.assertEqual({'path': '/p/b', 'fellBack': False, 'section': ''}, off)


class ActorWordsCase(unittest.TestCase):
    def test_actor_words_under_node(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed; the source checks still apply')
        ui = (WEB / 'js' / 'ui.js').as_uri()
        script = (
            "await import(process.argv[1]);"
            "const u = await import(process.argv[2]);"
            "console.log(JSON.stringify(["
            "u.actorWords('Ada', 'usr_0123456789abcdef', 'someone'),"
            "u.actorWords(null, 'usr_0123456789abcdef', 'someone'),"
            "u.actorWords(null, 'session-42', 'someone'),"
            "u.actorWords(null, 'coordinator', 'someone'),"
            "u.actorWords(undefined, { text: 'usr_0123456789abcdef' }, 'someone')]));"
        )
        out = subprocess.run([node, '--input-type=module', '-e', script, SHIM.as_uri(), ui],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(0, out.returncode, out.stderr)
        named, account, session, word, obj = json.loads(out.stdout)
        self.assertEqual('Ada', named)
        self.assertEqual('someone', account)
        self.assertEqual('session-42', session)
        self.assertEqual('coordinator', word)
        self.assertEqual('someone', obj)


class LayoutSourceCase(unittest.TestCase):
    def read(self, rel):
        return (WEB / rel).read_text(encoding='utf-8')

    def test_room_lists_use_the_plain_timeline(self):
        rooms = self.read('js/views/rooms.js')
        for label in ('Damaged rooms', 'Open rooms', 'Archived rooms', 'Room messages', 'Room items'):
            self.assertIn("class: 'timeline timeline-plain', 'aria-label': '%s'" % label, rooms)
        self.assertNotIn("class: 'timeline', 'aria-label'", rooms)

    def test_lanes_hide_account_ids_as_text(self):
        project = self.read('js/views/project.js')
        self.assertIn("title: isAccountId(lane.lane) ? lane.lane : null", project)
        self.assertIn("isAccountId(lane.lane) ? null : h('span', { class: 'mono small muted' }, lane.lane)", project)

    def test_switch_notice_is_a_toast_not_a_banner(self):
        app = self.read('js/app.js')
        self.assertIn("toast(label ? `${name} has no ${label} page", app)
        self.assertNotIn("class: 'banner', role: 'status'", app)
        self.assertNotIn('ctx.notice', app)

    def test_plain_timeline_is_one_column_without_the_rule(self):
        css = self.read('css/app.css')
        self.assertIn('.timeline.timeline-plain li { display: block;', css)
        self.assertIn('.timeline.timeline-plain li::before { display: none; }', css)

    def test_who_acts_wraps_and_panel_head_can_wrap(self):
        css = self.read('css/app.css')
        self.assertIn('white-space: normal; max-width: 100%; overflow-wrap: anywhere;', css)
        self.assertIn('.panel-head { display: flex; flex-wrap: wrap;', css)

    def test_optional_nodes_are_not_passed_as_null(self):
        project = self.read('js/views/project.js')
        self.assertIn('...(partial ? [partial] : []),', project)
        self.assertIn('...(leftOut ? [leftOut] : []),', project)
        self.assertNotIn('      partial,\n', project)
        self.assertNotIn('      leftOut,\n', project)

    def test_raw_account_ids_are_kept_out_of_the_visible_text(self):
        project = self.read('js/views/project.js')
        task = self.read('js/views/task.js')
        self.assertIn("title: a.user_id || null }, actorWords(a.user_name, a.user_id, 'someone')", project)
        self.assertIn("actorWords(cp.author_name, cp.author, 'someone')", task)
        self.assertNotIn('cp.author_name || cp.author)', task)


if __name__ == '__main__':
    unittest.main()
