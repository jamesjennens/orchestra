"""Automatic lane nudges (kittrial-5bb.514): slice 4 of docs/design/LANE_NUDGE_DESIGN.md.

Off by default per project; only a project owner or superuser by session may change it (audited).
Reads (``GET /v1/projects/<pid>/lanes`` and ``wait --coordinator``) stay strictly read-only:
automatic nudges are evaluated only in the carrier path (``GET /v1/me/nudges`` and
``POST /v1/me/carrier``), reading each enabled project's lanes as the service with scope ``all``.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import nudges
import test_lane_nudge_carrier as carrier_base

WAIT = '/v1/agents/me/wait?form=coordinator&timeout=0'


class AutoNudgeCase(carrier_base.CarrierCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.grant().status)

    def set_auto_nudge(self, enabled=True, token=None, project=None):
        return self.request('PUT', '/v1/projects/%s/auto-nudge' % (project or self.project),
                            {'auto_nudge': enabled}, token=token if token is not None else self.tokens['olive'])

    def project_info(self, token=None, project=None):
        return self.request('GET', '/v1/projects/%s' % (project or self.project),
                            token=token if token is not None else self.tokens['olive'])

    def coordinator_wait(self, token=None, idle_minutes=15):
        answer = self.request('GET', WAIT + '&idle_minutes=%d' % idle_minutes, token=token or self.agent)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def comment(self, token, task, text='progress'):
        made = self.request('POST', self.base(task) + '/comments', {'text': text}, token=token)
        self.assertEqual(201, made.status, made.data)
        return made.data


class SettingTests(AutoNudgeCase):
    def test_setting_is_off_by_default_only_owner_or_superuser_by_session_may_set_it_and_it_is_audited(self):
        # Default off on GET /v1/projects/<pid>; can_set_auto_nudge is True only for owner/superuser by session.
        olive_view = self.project_info(self.tokens['olive']).data
        self.assertEqual({'enabled': False, 'set_by': None, 'set_at': None, 'set_by_display_name': None},
                         olive_view['auto_nudge'])
        self.assertIs(True, olive_view['can_set_auto_nudge'])
        self.assertIs(True, self.project_info(self.admin).data['can_set_auto_nudge'])
        for who in ('cora', 'carl', 'vera'):
            with self.subTest(who=who):
                info = self.project_info(self.tokens[who]).data
                self.assertIs(False, info['can_set_auto_nudge'])
                self.assertEqual(403, self.set_auto_nudge(True, token=self.tokens[who]).status)

        # An agent credential (even one owned by an owner or holding a coordinator grant) is refused.
        olive_agent, _ = self.agent_with_id('olive', 'Owl')
        for cred in (self.agent, olive_agent):
            with self.subTest(credential=cred[:12]):
                self.assertIs(False, self.project_info(cred).data['can_set_auto_nudge'])
                refused = self.set_auto_nudge(True, token=cred)
                self.assertEqual(403, refused.status, refused.data)

        # Non-member is refused with 404; bad payloads are refused with 422.
        self.assertEqual(404, self.set_auto_nudge(True, token=self.tokens['otto']).status)
        for bad in ({}, {'auto_nudge': 'yes'}, {'auto_nudge': 1}, {'auto_nudge': True, 'extra': 1}):
            with self.subTest(payload=bad):
                self.assertEqual(422, self.request('PUT', '/v1/projects/%s/auto-nudge' % self.project,
                                                   bad, token=self.tokens['olive']).status)

        # Owner by session turns it on; superuser by session turns it off; both are audited.
        turned_on = self.set_auto_nudge(True, token=self.tokens['olive'])
        self.assertEqual(200, turned_on.status, turned_on.data)
        self.assertIs(True, turned_on.data['enabled'])
        self.assertEqual(self.ids['olive'], turned_on.data['set_by'])
        self.assertTrue(turned_on.data['set_at'])

        turned_off = self.set_auto_nudge(False, token=self.admin)
        self.assertEqual(200, turned_off.status, turned_off.data)
        self.assertIs(False, turned_off.data['enabled'])
        self.assertEqual(self.service.authenticate(self.admin).user_id, turned_off.data['set_by'])

        events = self.events(action='projects.auto_nudge', outcome='committed')
        self.assertEqual(2, len(events))
        self.assertEqual([True, False], [e.get('auto_nudge') for e in events])

        # Damaged stored setting fails closed as enabled: False, damaged: True.
        self.service.state['auto_nudge'][self.project] = {'enabled': 'yes'}
        damaged = self.project_info(self.tokens['olive']).data['auto_nudge']
        self.assertEqual((False, True), (damaged['enabled'], damaged.get('damaged')))
        self.service.state['auto_nudge'] = 'not-a-dict'
        self.assertEqual(503, self.set_auto_nudge(True, token=self.tokens['olive']).status)

    def test_lanes_page_sentence_shows_automatic_and_settings_panel_renders(self):
        import shutil
        import test_http_web
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        script = """
            await import(process.argv[1]);
            const view = await import(process.argv[2]);
            const calls = [];
            const ctx = {
                href: (p) => '#' + p,
                api: {
                    setAutoNudge: async (pid, enabled) => {
                        calls.push({ pid, enabled });
                        return { enabled, set_by: 'usr_olive', set_by_display_name: 'Olive', set_at: '2026-11-05T12:00:00Z' };
                    },
                },
            };
            const line = view.nudgeLine({
                nudge: {
                    id: 'nudge-auto-1',
                    message: 'resume',
                    state: 'delivered',
                    requested_at: '2026-11-05T12:00:00Z',
                    requested_by_display: 'automatic',
                    outcome_at: '2026-11-05T12:00:10Z',
                    outcome_detail: null,
                },
            });
            const roPanel = view.autoNudgePanel(ctx, {
                id: 'alpha',
                can_set_auto_nudge: false,
                auto_nudge: { enabled: false, set_by: null, set_at: null, set_by_display_name: null },
            });
            const rwPanel = view.autoNudgePanel(ctx, {
                id: 'alpha',
                can_set_auto_nudge: true,
                auto_nudge: { enabled: false, set_by: null, set_at: null, set_by_display_name: null },
            });
            document.body.append(rwPanel);
            const sel = rwPanel.querySelector('#auto-nudge-value');
            sel.value = 'on';
            const btn = rwPanel.all((e) => e.tagName === 'BUTTON')[0];
            await btn.dispatch('click');
            await new Promise((r) => setTimeout(r, 10));
            console.log(JSON.stringify({
                lineText: line.textContent,
                roControls: roPanel.all((e) => e.tagName === 'SELECT').length,
                rwTextAfter: rwPanel.textContent,
                calls,
            }));
        """
        done = test_http_web.run_node_module(
            self, node, script,
            (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
            (KIT / 'web' / 'js' / 'views' / 'project.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertIn('by automatic (resume): delivered', out['lineText'])
        self.assertEqual(0, out['roControls'])
        self.assertEqual([{'pid': 'alpha', 'enabled': True}], out['calls'])
        self.assertIn('Set to on by Olive', out['rwTextAfter'])


class EligibilityAndReadOnlyTests(AutoNudgeCase):
    def test_setting_off_means_no_automatic_nudge(self):
        secret, _ = self.carrier('carl')
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.later(3700)
        self.assertEqual(200, self.lanes(self.tokens['olive']).status)
        wait = self.coordinator_wait()
        self.assertEqual(sorted([self.ids['carl'], self.ids['dave']]),
                         sorted(row['actor'] for row in wait['triggers']['lane-quiet']))
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual([], self.mine(secret))
        self.assertEqual([], self.records())

    def test_reads_stay_read_only_get_lanes_and_coordinator_wait_create_no_record_carrier_poll_creates_one(self):
        self.assertEqual(200, self.set_auto_nudge(True).status)
        secret, _ = self.carrier('carl')
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        # Advance just before 2x threshold (1800s for 15m default), send a heartbeat so carrier stays fresh,
        # then step past 1800s so carl is eligible before the carrier's next poll.
        self.later(1760)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual([], self.records())
        self.later(50)  # total 1810s > 1800s; carrier was seen 50s ago (within 90s CARRIER_WINDOW)

        # 1. A GET of the lanes page by an owner, with the setting on and an eligible lane, creates no record.
        page = self.lanes(self.tokens['olive'])
        self.assertEqual(200, page.status, page.data)
        self.assertEqual([], self.records())

        # 2. A coordinator wait creates no record.
        wait = self.coordinator_wait()
        self.assertEqual([], self.records())
        # carl is withheld while waiting for the carrier's auto-nudge; dave (no carrier) wakes at threshold.
        self.assertEqual([self.ids['dave']], [row['actor'] for row in wait['triggers']['lane-quiet']])

        # 3. The carrier's next poll creates exactly one automatic nudge and returns it.
        items = self.mine(secret)
        self.assertEqual(1, len(items))
        self.assertEqual(1, len(self.records()))
        record = self.records()[0]
        self.assertEqual((self.ids['carl'], 'resume', 'stalled-at-prompt', 'pending'),
                         (record['lane'], record['message'], record['reason'], record['state']))
        self.assertEqual({'kind': 'auto', 'user_id': None, 'agent_id': None}, record['requested_by'])
        [event] = self.events(action='lanes.nudge.auto', outcome='committed')
        self.assertEqual((None, self.ids['carl'], self.project),
                         (event['user_id'], event['actor'], event['project_id']))
        self.assertIn(self.carl_task, event['reason'])

        # The Lanes page shows "automatic" as the requester on the nudge sentence.
        rows = {row['actor']: row for row in self.lanes(self.tokens['olive']).data['items']}
        self.assertEqual('automatic', rows[self.ids['carl']]['nudge']['requested_by_display'])

    def test_never_on_waiting_too_long_or_expect_by_passed(self):
        self.assertEqual(200, self.set_auto_nudge(True).status)
        secret, _ = self.carrier('carl')
        planted = self.backend.lanes_read

        def read(*args, **kwargs):
            answer = planted(*args, **kwargs)
            for row in answer['items']:
                if row['actor'] == self.ids['carl']:
                    row['your_move_idle'] = False
                    row['waiting_too_long'] = True
                    row['expect_by_passed'] = True
                    row['flag_tasks'] = {'waiting_too_long': [self.carl_task],
                                         'expect_by_passed': [self.carl_task]}
                    for entry in row.get('held') or []:
                        entry.update(whose_move='coordinator', next_actor='coordinator',
                                     flags=['waiting_too_long', 'expect_by_passed'])
            return answer
        self.backend.lanes_read = read
        self.later(3700)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual([], self.mine(secret))
        self.assertEqual([], self.records())
        wait = self.coordinator_wait()
        carl_quiet = [row for row in wait['triggers']['lane-quiet'] if row['actor'] == self.ids['carl']]
        self.assertEqual(1, len(carl_quiet))
        self.assertEqual(['expect_by_passed', 'waiting_too_long'], carl_quiet[0]['flags'])

    def test_never_on_a_declared_wait_of_either_kind_in_section_5(self):
        self.assertEqual(200, self.set_auto_nudge(True).status)
        secret, _ = self.carrier('carl')
        planted = self.backend.lanes_read
        for move in ('coordinator', 'you'):
            with self.subTest(whose_move=move):
                def read(*args, move=move, **kwargs):
                    answer = planted(*args, **kwargs)
                    for row in answer['items']:
                        if row['actor'] == self.ids['carl']:
                            for entry in row.get('held') or []:
                                entry.update(declared=True, inferred=False, state='waiting-on-review',
                                             waiting_on='reviewer', state_since='2026-10-25T08:00:00Z',
                                             whose_move=move,
                                             flags=['your_move_idle'] if move == 'you' else ['waiting_too_long'])
                    return answer
                self.backend.lanes_read = read
                self.later(3700)
                self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
                self.assertEqual([], self.mine(secret))
                self.assertEqual([], self.records())

    def test_never_on_a_holder_retired_lane(self):
        self.assertEqual(200, self.set_auto_nudge(True).status)
        secret, _ = self.carrier('carl')
        self.later(3700)
        # Retire carl's lane by marking holder_retired on the board row (or disabling another lane's holder).
        planted = self.backend.lanes_read

        def read(*args, **kwargs):
            answer = planted(*args, **kwargs)
            for row in answer['items']:
                if row['actor'] == self.ids['carl']:
                    row['holder_retired'] = True
                    row.setdefault('flag_tasks', {})['holder_retired'] = [self.carl_task]
                    for entry in row.get('held') or []:
                        entry['flags'] = sorted(set((entry.get('flags') or []) + ['holder_retired']))
            return answer
        self.backend.lanes_read = read
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual([], self.mine(secret))
        self.assertEqual([], self.records())

    def test_no_carrier_means_no_nudge_and_coordinator_wakes_at_threshold(self):
        self.assertEqual(200, self.set_auto_nudge(True).status)
        # Advance just past 1x threshold (1801s for 30m default), with no carrier registered.
        self.later(1805)
        wait = self.coordinator_wait()
        self.assertIn(self.ids['carl'], [row['actor'] for row in wait['triggers']['lane-quiet']])
        self.later(2000)  # now > 2x threshold, still no carrier
        self.assertEqual(200, self.lanes(self.tokens['olive']).status)
        self.assertEqual([], self.records())


class EpisodeAndCapTests(AutoNudgeCase):
    def test_once_per_idle_episode_and_resets_when_lane_writes(self):
        self.assertEqual(200, self.set_auto_nudge(True).status)
        secret, _ = self.carrier('carl')
        self.later(3610)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        first = self.mine(secret)
        self.assertEqual(1, len(first))
        nudge_id = first[0]['id']
        self.assertEqual(200, self.claim(secret, nudge_id).status)
        self.assertEqual(200, self.answer(secret, nudge_id, 'delivered').status)

        # Further polls in the same idle episode — even past 6 hours if the lane never wrote — create no nudge.
        for advance in (120, 1800, nudges.AUTO_GAP_SECONDS + 600):
            self.later(advance)
            self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
            self.assertEqual([], self.mine(secret))
            self.assertEqual(1, len(self.records()))

        # Once the lane writes on its task, the idle episode resets; after 2x threshold again, it is nudged.
        self.comment(self.tokens['carl'], self.carl_task, 'working on step 2')
        self.later(100)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual([], self.mine(secret))
        self.later(3600)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        second = self.mine(secret)
        self.assertEqual(1, len(second))
        self.assertNotEqual(nudge_id, second[0]['id'])
        self.assertEqual(2, len(self.records()))

    def test_per_6h_and_per_24h_caps_hold_across_projects(self):
        beta = self.create_project(self.admin, 'Beta')
        self.assertEqual(200, self.member('olive', 'owner', token=self.admin, project=beta).status)
        self.assertEqual(200, self.member('carl', 'contributor', token=self.tokens['olive'], project=beta).status)
        beta_task = self.request('POST', '/v1/projects/%s/tasks' % beta, {'title': 'beta work'},
                                 token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (beta, beta_task),
                                           {}, token=self.tokens['carl']).status)
        self.assertEqual(200, self.set_auto_nudge(True, project=self.project).status)
        self.assertEqual(200, self.set_auto_nudge(True, project=beta).status)
        secret, _ = self.carrier('carl', projects=[self.project, beta])

        # 1st automatic nudge in Alpha at t = 3610.
        self.later(3610)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual(1, len(self.records(self.project)) + len(self.records(beta)))

        # Carl writes in both projects and goes idle again for 4000s (< 6h since 1st nudge): blocked by 6h cap!
        self.comment(self.tokens['carl'], self.carl_task, 'alpha write 1')
        self.request('POST', '/v1/projects/%s/tasks/%s/comments' % (beta, beta_task),
                     {'text': 'beta write 1'}, token=self.tokens['carl'])
        self.later(4000)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual(1, len(self.records(self.project)) + len(self.records(beta)))

        # Advance past 6 hours from the 1st nudge: 2nd automatic nudge in 24h is allowed.
        self.later(nudges.AUTO_GAP_SECONDS)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual(2, len(self.records(self.project)) + len(self.records(beta)))

        # Carl writes again in both projects, and we advance another 7 hours (14h total < 24h):
        # blocked by the 2-per-24h cap across projects!
        self.comment(self.tokens['carl'], self.carl_task, 'alpha write 2')
        self.request('POST', '/v1/projects/%s/tasks/%s/comments' % (beta, beta_task),
                     {'text': 'beta write 2'}, token=self.tokens['carl'])
        self.later(nudges.AUTO_GAP_SECONDS + 3700)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual(2, len(self.records(self.project)) + len(self.records(beta)))

        # Once 24 hours have passed since the 1st nudge, a 3rd automatic nudge is allowed.
        self.later(nudges.AUTO_DAY_SECONDS)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual(3, len(self.records(self.project)) + len(self.records(beta)))


class CoordinatorWaitWithholdingTests(AutoNudgeCase):
    def test_coordinator_wakes_one_threshold_after_a_nudge_that_changed_nothing_with_nudge_outcome_and_stable_withheld_fingerprint(self):
        # Unassign dave's task so carl is the only held lane on the board.
        dave_row = self.backend._task(self.project, self.dave_task)
        dave_row['assignee'] = None
        dave_row['status'] = 'open'
        self.assertEqual(200, self.set_auto_nudge(True).status)
        secret, _ = self.carrier('carl')
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)

        # Past 1x threshold (900s for 15m default) but before 2x threshold (1800s): your_move_idle is withheld,
        # no record is created, and two coordinator waits have the same fingerprint.
        self.later(1000)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        wait_pre_1 = self.coordinator_wait()
        self.later(30)
        wait_pre_2 = self.coordinator_wait()
        self.assertEqual([], self.records())
        self.assertEqual([], wait_pre_1['triggers']['lane-quiet'])
        self.assertEqual([], wait_pre_2['triggers']['lane-quiet'])
        self.assertEqual(wait_pre_1['fingerprint'], wait_pre_2['fingerprint'])

        # Advance past 2x threshold (total 1830s > 1800s); carrier beat creates and delivers the auto-nudge.
        self.later(800)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        [item] = self.mine(secret)
        nudge_id = item['id']
        self.assertEqual(200, self.claim(secret, nudge_id).status)
        self.assertEqual(200, self.answer(secret, nudge_id, 'delivered').status)

        # Inside the 1-threshold window after the automatic nudge (< 900s after nudge):
        # lane-quiet stays withheld and two coordinator waits have the exact same fingerprint.
        self.later(300)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        wait_withheld_1 = self.coordinator_wait()
        self.later(30)
        wait_withheld_2 = self.coordinator_wait()
        self.assertEqual([], wait_withheld_1['triggers']['lane-quiet'])
        self.assertEqual([], wait_withheld_2['triggers']['lane-quiet'])
        self.assertEqual(wait_withheld_1['fingerprint'], wait_withheld_2['fingerprint'])
        self.assertEqual(wait_pre_1['fingerprint'], wait_withheld_1['fingerprint'])

        # One threshold (900s) after the automatic nudge that changed nothing:
        # lane-quiet wakes the coordinator with the nudge and its outcome on the row!
        self.later(600)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        wait_woken = self.coordinator_wait()
        self.assertNotEqual(wait_withheld_2['fingerprint'], wait_woken['fingerprint'])
        [quiet_row] = wait_woken['triggers']['lane-quiet']
        self.assertEqual(self.ids['carl'], quiet_row['actor'])
        self.assertEqual(['your_move_idle'], quiet_row['flags'])
        self.assertEqual({'id': nudge_id, 'message': 'resume', 'state': 'delivered',
                          'requested_by_display': 'automatic'},
                         {k: quiet_row['nudge'][k] for k in ('id', 'message', 'state', 'requested_by_display')})
        self.assertTrue(quiet_row['nudge']['requested_at'])
        self.assertTrue(quiet_row['nudge']['outcome_at'])


if __name__ == '__main__':
    unittest.main()
