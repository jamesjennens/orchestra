"""HTTP routes and web UI tests for Slice 2 (kittrial-5bb.598, §§2.2, 2.3, 6, Matrix #16).

Covers:
* ``GET /v1/projects/{pid}/waiting`` (CAP_READ, scoping, filters, pagination, zero metrics)
* ``POST /v1/projects/{pid}/waiting/snooze`` (CAP_COORDINATE, 422/409/404/403, anti-self-suppression, expiry)
* ``GET /v1/projects/{pid}/waiting/metrics`` (CAP_READ, zero task text, resolved_members, days validation)
* ``GET /v1/projects/{pid}`` (``overdue_escalations`` summary)
* ``GET /v1/me/work`` (``waiting_on_you`` across memberships)
* ``tests/web_waiting_page.mjs`` under Node (when node is installed)
"""
from datetime import datetime, timedelta, timezone
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))
if str(KIT / 'tests') not in sys.path:
    sys.path.insert(0, str(KIT / 'tests'))

import lane_state
from requirements import canonical_bytes
from test_http_service import ServerHarness


def _iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


class WaitingHttpRouteTests(ServerHarness):
    """HTTP route tests with owner (olive), coordinator (cora), reviewer (rory), contributor (carl), outsider (otto)."""

    SECRET_TITLE = 'TOP-SECRET-PROJECT-ALPHA-TITLE-99182'

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.ids = {}
        for name in ('olive', 'cora', 'rory', 'carl', 'otto'):
            self.ids[name] = self.create_account(self.admin, name, name + '-password-1')
        for name in ('olive', 'cora', 'rory', 'carl', 'otto'):
            setattr(self, name, self.login(name, name + '-password-1')[0])
        self.project = self.create_project(self.olive, 'Alpha')
        self.other = self.create_project(self.otto, 'Elsewhere')
        for name, role in (('cora', 'coordinator'), ('rory', 'contributor'), ('carl', 'contributor')):
            res = self.request('PUT', f'/v1/projects/{self.project}/members/{self.ids[name]}',
                               {'role': role}, token=self.olive)
            self.assertEqual(200, res.status, res.data)

        self.task_coord = self.create_task(self.olive, self.project, self.SECRET_TITLE).data['id']
        self.task_rev = self.create_task(self.olive, self.project, 'Reviewer task secret-rev-441').data['id']
        self.task_silent = self.create_task(self.olive, self.project, 'Silent task secret-sil-772').data['id']
        for tid in (self.task_coord, self.task_rev, self.task_silent):
            cl = self.request('POST', f'/v1/projects/{self.project}/tasks/{tid}/claim', {}, token=self.carl)
            self.assertEqual(200, cl.status, cl.data)

    def _now_dt(self):
        return datetime.fromtimestamp(self.service._now(), tz=timezone.utc)

    def _plant_wait(self, task_id, actor, *, waiting_on='coordinator', state='waiting-on-coordinator',
                    hours_ago=8, expect_hours=2, answered_by=None):
        now = self._now_dt()
        started = now - timedelta(hours=hours_ago)
        expect = started + timedelta(hours=expect_hours)
        cp_id = f'cp-{task_id}'
        cp_payload = {
            'schema_version': 1, 'task': task_id, 'intent': 'work',
            'summary': 'waiting on ' + waiting_on, 'next_action': 'wait',
            'acceptance': 'done', 'open_items': [], 'previous': None,
        }
        ls_payload = {
            'schema_version': 1, 'task': task_id, 'checkpoint': cp_id,
            'state': state, 'waiting_on': waiting_on, 'expect_by': _iso(expect),
        }
        comments = self.backend.state['tasks'][task_id].setdefault('comments', [])
        comments.append({
            'id': cp_id, 'issue_id': task_id, 'author': actor, 'created_at': _iso(started),
            'text': 'Kind: task-checkpoint-v1\n' + canonical_bytes(cp_payload).decode(),
        })
        comments.append({
            'id': f'ls-{task_id}', 'issue_id': task_id, 'author': actor,
            'created_at': _iso(started + timedelta(seconds=1)),
            'text': lane_state.PREFIX + canonical_bytes(ls_payload).decode(),
        })
        if answered_by is not None:
            comments.append({
                'id': f'ans-{task_id}', 'issue_id': task_id, 'author': answered_by,
                'created_at': _iso(started + timedelta(hours=1)),
                'text': 'VERIFIED: looks good.',
            })

    def _age_task_silent(self, task_id, hours_ago=10):
        now = self._now_dt()
        old = _iso(now - timedelta(hours=hours_ago))
        row = self.backend.state['tasks'][task_id]
        row['created_at'] = old
        row['updated_at'] = old
        row['comments'] = []

    def test_non_member_refused_with_no_task_text(self):
        """Matrix #16: outsider gets 403/404 across all 3 /waiting* routes with zero task text."""
        self._plant_wait(self.task_coord, self.ids['carl'], waiting_on='coordinator')
        until = _iso(self._now_dt() + timedelta(hours=6))

        for method, path, body in (
            ('GET', f'/v1/projects/{self.project}/waiting', None),
            ('GET', f'/v1/projects/{self.project}/waiting/metrics', None),
            ('POST', f'/v1/projects/{self.project}/waiting/snooze',
             {'task': self.task_coord, 'episode_id': 'a' * 32, 'reason': 'nope', 'until': until}),
        ):
            res = self.request(method, path, body, token=self.otto)
            self.assertIn(res.status, (403, 404), f'{method} {path}: {res.status} {res.data}')
            raw = json.dumps(res.data)
            self.assertNotIn(self.SECRET_TITLE, raw)
            self.assertNotIn(self.task_coord, raw)

    def test_waiting_read_scope_filters_pagination_and_display_names(self):
        self._plant_wait(self.task_coord, self.ids['carl'], waiting_on='coordinator', hours_ago=10)
        self._plant_wait(self.task_rev, self.ids['carl'], waiting_on=self.ids['rory'],
                         state='waiting-on-review', hours_ago=6)
        self._age_task_silent(self.task_silent, hours_ago=10)

        # Coordinator sees all waits + silent lane, can_snooze=True, oldest-first.
        res = self.request('GET', f'/v1/projects/{self.project}/waiting', token=self.cora)
        self.assertEqual(200, res.status, res.data)
        body = res.data
        self.assertEqual('all', body['scope'])
        self.assertTrue(body['can_snooze'])
        self.assertEqual(2, body['total'])
        self.assertEqual(2, body['overdue_count'])
        self.assertEqual(1, body['silent_count'])
        self.assertEqual([self.task_coord, self.task_rev], [i['task'] for i in body['items']])
        self.assertEqual('carl', body['items'][0]['display_name'])
        self.assertEqual('coordinator', body['items'][0]['waiting_on_name'])
        self.assertEqual('rory', body['items'][1]['waiting_on_name'])
        self.assertNotIn('by_target', body)

        # filter=on-me for coordinator shows coordinator wait + silent lane, not rory's named wait.
        on_me = self.request('GET', f'/v1/projects/{self.project}/waiting?filter=on-me', token=self.cora)
        self.assertEqual(200, on_me.status, on_me.data)
        self.assertEqual([self.task_coord], [i['task'] for i in on_me.data['items']])
        self.assertEqual(1, on_me.data['silent_count'])

        # Reviewer (non-coordinator) sees waits addressed to rory with scope=own and can_snooze=False.
        rev_on_me = self.request('GET', f'/v1/projects/{self.project}/waiting?filter=on-me', token=self.rory)
        self.assertEqual(200, rev_on_me.status, rev_on_me.data)
        self.assertEqual('own', rev_on_me.data['scope'])
        self.assertFalse(rev_on_me.data['can_snooze'])
        self.assertEqual([self.task_rev], [i['task'] for i in rev_on_me.data['items']])

        # Pagination via limit=1 + next_cursor.
        p1 = self.request('GET', f'/v1/projects/{self.project}/waiting?limit=1', token=self.cora)
        self.assertEqual(200, p1.status)
        self.assertEqual([self.task_coord], [i['task'] for i in p1.data['items']])
        self.assertIsNotNone(p1.data['next_cursor'])
        p2 = self.request('GET', f"/v1/projects/{self.project}/waiting?limit=1&cursor={p1.data['next_cursor']}",
                          token=self.cora)
        self.assertEqual(200, p2.status)
        self.assertEqual([self.task_rev], [i['task'] for i in p2.data['items']])
        self.assertIsNone(p2.data['next_cursor'])

        # Invalid query parameters refused 400.
        for bad_q in ('?filter=bogus', '?filter=on-me&role=coordinator', '?silent_hours=0', '?silent_hours=999'):
            bad = self.request('GET', f'/v1/projects/{self.project}/waiting{bad_q}', token=self.cora)
            self.assertEqual(400, bad.status, f'{bad_q}: {bad.data}')

    def test_snooze_http_route_validation_authority_renewal_and_expiry(self):
        self._plant_wait(self.task_coord, self.ids['carl'], waiting_on='coordinator', hours_ago=8)
        self._plant_wait(self.task_rev, self.ids['carl'], waiting_on=self.ids['rory'],
                         state='waiting-on-review', hours_ago=6)
        listing = self.request('GET', f'/v1/projects/{self.project}/waiting', token=self.cora).data
        ep_coord = next(i['episode_id'] for i in listing['items'] if i['task'] == self.task_coord)
        ep_rev = next(i['episode_id'] for i in listing['items'] if i['task'] == self.task_rev)
        now = self._now_dt()

        # Contributor refused 403.
        forb = self.request('POST', f'/v1/projects/{self.project}/waiting/snooze', {
            'task': self.task_coord, 'episode_id': ep_coord,
            'reason': 'not allowed', 'until': _iso(now + timedelta(hours=4)),
        }, token=self.carl)
        self.assertEqual(403, forb.status, forb.data)

        # Missing `until` refused 422 (#13).
        no_until = self.request('POST', f'/v1/projects/{self.project}/waiting/snooze', {
            'task': self.task_coord, 'episode_id': ep_coord, 'reason': 'no end time',
        }, token=self.cora)
        self.assertEqual(422, no_until.status, no_until.data)

        # Unknown episode refused 404.
        missing = self.request('POST', f'/v1/projects/{self.project}/waiting/snooze', {
            'task': self.task_coord, 'episode_id': 'deadbeef12345678',
            'reason': 'nope', 'until': _iso(now + timedelta(hours=4)),
        }, token=self.cora)
        self.assertEqual(404, missing.status, missing.data)

        # Coordinator snoozes coordinator wait -> waited_on_party=True, suppresses_owner_notice=False.
        before_marker = self.backend._wait_activity_marker(self.backend.state['tasks'][self.task_coord])
        s1 = self.request('POST', f'/v1/projects/{self.project}/waiting/snooze', {
            'task': self.task_coord, 'episode_id': ep_coord,
            'reason': 'Waiting on CI batch', 'until': _iso(now + timedelta(hours=4)),
        }, token=self.cora)
        self.assertEqual(201, s1.status, s1.data)
        self.assertTrue(s1.data['waited_on_party'])
        self.assertFalse(s1.data['suppresses_owner_notice'])
        self.assertEqual(1, s1.data['snooze_count'])
        # Wake marker unchanged after snooze (#31).
        after_marker = self.backend._wait_activity_marker(self.backend.state['tasks'][self.task_coord])
        self.assertEqual(before_marker, after_marker)

        # 2nd snooze succeeds; 3rd snooze returns 409 (#29).
        s2 = self.request('POST', f'/v1/projects/{self.project}/waiting/snooze', {
            'task': self.task_coord, 'episode_id': ep_coord,
            'reason': 'Second renewal', 'until': _iso(now + timedelta(hours=8)),
        }, token=self.cora)
        self.assertEqual(201, s2.status, s2.data)
        self.assertEqual(2, s2.data['snooze_count'])
        s3 = self.request('POST', f'/v1/projects/{self.project}/waiting/snooze', {
            'task': self.task_coord, 'episode_id': ep_coord,
            'reason': 'Third renewal', 'until': _iso(now + timedelta(hours=12)),
        }, token=self.cora)
        self.assertEqual(409, s3.status, s3.data)

        # Non-waited-on human owner (olive) snoozing rory's wait -> suppresses_owner_notice=True and may do 3rd.
        for idx in (1, 2, 3):
            r = self.request('POST', f'/v1/projects/{self.project}/waiting/snooze', {
                'task': self.task_rev, 'episode_id': ep_rev,
                'reason': f'Owner snooze {idx}', 'until': _iso(now + timedelta(hours=idx * 3)),
            }, token=self.olive)
            self.assertEqual(201, r.status, f'try {idx}: {r.data}')
            self.assertFalse(r.data['waited_on_party'])
            self.assertTrue(r.data['suppresses_owner_notice'])

        # Wait stays visible while snoozed, and auto-expires once clock advances past `until`.
        mid = self.request('GET', f'/v1/projects/{self.project}/waiting', token=self.cora).data
        self.assertEqual(2, mid['total'])
        self.assertEqual(2, mid['snoozed_count'])
        future_epoch = (now + timedelta(hours=20)).timestamp()
        orig_now = self.service._now
        try:
            self.service._now = lambda: future_epoch
            later = self.request('GET', f'/v1/projects/{self.project}/waiting', token=self.cora).data
        finally:
            self.service._now = orig_now
        self.assertEqual(2, later['total'])
        self.assertEqual(0, later['snoozed_count'])
        self.assertTrue(all(not i['snoozed'] for i in later['items']))

    def test_metrics_route_contains_no_task_text_and_includes_resolved_members(self):
        self._plant_wait(self.task_coord, self.ids['carl'], waiting_on='coordinator', hours_ago=8)
        self._plant_wait(self.task_rev, self.ids['carl'], waiting_on='coordinator', hours_ago=5,
                         answered_by=self.ids['cora'])

        # Readable by any CAP_READ member (contributor carl included).
        res = self.request('GET', f'/v1/projects/{self.project}/waiting/metrics?days=14', token=self.carl)
        self.assertEqual(200, res.status, res.data)
        raw = json.dumps(res.data)
        self.assertNotIn(self.SECRET_TITLE, raw)
        self.assertNotIn('Reviewer task secret-rev-441', raw)
        self.assertNotIn(self.task_coord, raw)
        self.assertEqual(14, res.data['days'])
        coord_rows = [r for r in res.data['by_target'] if r['waiting_on'] == 'coordinator']
        self.assertTrue(coord_rows)
        self.assertTrue(any(r.get('resolved_members') for r in coord_rows))
        self.assertTrue(any(r['actor'] == self.ids['cora'] and r['actor_name'] == 'cora'
                            for r in res.data['by_actor']))

        # Invalid days -> 400.
        self.assertEqual(400, self.request('GET', f'/v1/projects/{self.project}/waiting/metrics?days=0',
                                           token=self.carl).status)
        self.assertEqual(400, self.request('GET', f'/v1/projects/{self.project}/waiting/metrics?days=91',
                                           token=self.carl).status)

    def test_project_get_overdue_escalations_and_me_work_waiting_on_you(self):
        self._plant_wait(self.task_coord, self.ids['carl'], waiting_on='coordinator', hours_ago=8)
        self._age_task_silent(self.task_silent, hours_ago=10)

        proj = self.request('GET', f'/v1/projects/{self.project}', token=self.cora)
        self.assertEqual(200, proj.status, proj.data)
        oe = proj.data.get('overdue_escalations')
        self.assertIsInstance(oe, dict)
        self.assertEqual(1, oe['total'])
        self.assertEqual(1, oe['overdue_count'])
        self.assertEqual(1, oe['silent_count'])

        # My work for coordinator includes waiting_on_you with the coordinator wait + silent lane.
        work_cora = self.request('GET', '/v1/me/work', token=self.cora)
        self.assertEqual(200, work_cora.status, work_cora.data)
        woy = work_cora.data.get('waiting_on_you')
        self.assertIsInstance(woy, dict)
        self.assertEqual(1, woy['total'])
        self.assertEqual(1, woy['overdue_count'])
        self.assertEqual(1, woy['silent_lanes_count'])
        self.assertEqual([self.task_coord], [i['task'] for i in woy['items']])
        self.assertEqual([self.task_silent], [s['task'] for s in woy['silent']])

        # My work for outsider (not in Alpha) contains zero items from Alpha.
        work_otto = self.request('GET', '/v1/me/work', token=self.otto)
        self.assertEqual(200, work_otto.status, work_otto.data)
        self.assertEqual(0, work_otto.data['waiting_on_you']['total'])
        self.assertNotIn(self.SECRET_TITLE, json.dumps(work_otto.data))


class WaitingWebPageNodeTests(unittest.TestCase):
    """Runs tests/web_waiting_page.mjs under Node against web/js/views/waiting.js, work.js, project.js."""

    def test_waiting_web_ui_under_node(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed on this host')
        cmd = [
            node,
            str(KIT / 'tests' / 'web_waiting_page.mjs'),
            (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
            (KIT / 'web' / 'js' / 'api.js').as_uri(),
            (KIT / 'web' / 'js' / 'routes.js').as_uri(),
            (KIT / 'web' / 'js' / 'views' / 'waiting.js').as_uri(),
            (KIT / 'web' / 'js' / 'views' / 'work.js').as_uri(),
            (KIT / 'web' / 'js' / 'views' / 'project.js').as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(0, proc.returncode, f'node failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}')
        obs = json.loads(proc.stdout)

        # Routes & switcher
        self.assertEqual({'name': 'waiting', 'params': {'pid': 'alpha'}}, obs['matched'])
        self.assertEqual('Waiting', obs['sectionLabel'])
        self.assertEqual('/p/beta/waiting', obs['switched']['path'])
        self.assertFalse(obs['switched']['fellBack'])

        # SHOULD-9: default Open waits load NEVER calls /waiting/metrics
        self.assertFalse(obs['metricsCalledOnDefaultLoad'])

        # Oldest-first rows + silent section + explicit stage chips + XSS safety
        self.assertEqual(['alpha-1', 'alpha-2', 'alpha-3', 'alpha-4'], obs['waitRowOrder'])
        self.assertEqual(['wait', 'wait', 'wait', 'silent-lane'], obs['waitRowKinds'])
        stages = {c['stage']: c['text'] for c in obs['stageChipValues']}
        self.assertEqual('+24h Project-wide', stages['project-visible'])
        self.assertEqual('+4h Owner NOT reached', stages['owner-not-reached'])
        self.assertEqual('+4h Owner told', stages['owner-notified'])
        self.assertEqual('Overdue', stages['overdue'])
        self.assertEqual(1, obs['antiSuppressCount'])
        self.assertEqual(0, obs['scriptTagCount'])

        # Filter + snooze form 422 / 409 / 201
        self.assertTrue(obs['onMeCalled'])
        self.assertIn('until', obs['err422'].lower())
        self.assertIn('2 times', obs['err409'])
        self.assertEqual('', obs['errAfterOk'])
        self.assertEqual(3, len(obs['snoozePayloads']))

        # Metrics tab lazily loads /waiting/metrics?days=14
        self.assertTrue(obs['metricsCalledOnTab'])
        self.assertTrue(any('Coord Sam' in t for t in obs['targetTableText']))
        self.assertTrue(any('Coord Sam' in t for t in obs['actorTableText']))

        # My work Waiting on you panel & project overview lazy Wait times panel + banner
        self.assertTrue(obs['myWorkHasWaitingPanel'])
        self.assertIn('Waiting on you', obs['myWorkWaitingText'])
        self.assertIn('alpha-2', obs['myWorkWaitingText'])
        self.assertIn('alpha-4', obs['myWorkWaitingText'])
        self.assertIn('2waitonyou', obs['myWorkKeyText'].replace(' ', ''))
        self.assertIn('Wait times (14 days)', obs['overviewWaitTimesText'])
        self.assertIn('3 answered', obs['overviewWaitTimesText'])
        self.assertIn('2 overdue wait(s)', obs['overviewBannerText'])
        self.assertIn('1 owner NOT reached', obs['overviewBannerText'])


if __name__ == '__main__':
    unittest.main()
