"""kittrial-5bb.583: show each agent's id (copyable, searchable, narrow-screen findable)
wherever an agent is listed, show person account ids in Account column, reorder setup step 8
path+Copy before Save, and add neutral connect-to troubleshooting text.

9 named adversarial tests matching the PM brief (1)-(9) and conditions 1-4.
"""
import json
import re
import shutil
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_coordinator_grant
from test_http_web import run_node_module


class AgentIds583Tests(unittest.TestCase):
    """Adversarial cases (1)-(9) for kittrial-5bb.583."""

    @classmethod
    def setUpClass(cls):
        class _SharedEnv(test_coordinator_grant.Grants):
            def runTest(self):
                pass
        env = _SharedEnv()
        env.setUp()
        cls.env = env
        try:
            # Enable invites so invitePanel renders real invites with an agent_id.
            env.backend.invites_enabled = True
            # Create a second project Beta that `vera` (viewer) and `olive` (Alpha owner) have NO access to.
            beta = env.request('POST', '/v1/projects', {'name': 'Beta', 'id': 'beta'}, token=env.admin)
            assert beta.status in (200, 201), beta.data
            cls.beta = beta.data['id']
            # Create a viewer on Alpha only (no access to Beta).
            env.ids['vera'] = env.create_account(env.admin, 'vera', 'vera-password-1')
            env.tokens['vera'] = env.login('vera', 'vera-password-1')[0]
            assert env.member('vera', 'viewer').status == 200
            # Create an owner on Beta + an agent in Beta with coordinator grant in Beta.
            env.ids['bob'] = env.create_account(env.admin, 'bob', 'bob-password-1')
            env.tokens['bob'] = env.login('bob', 'bob-password-1')[0]
            assert env.member('bob', 'owner', token=env.admin, project=cls.beta).status == 200
            cls.beta_agent_secret, cls.beta_agent_id = env.agent_with_id('bob', 'Falcon', projects=[cls.beta])
            assert env.grant(agent_id=cls.beta_agent_id, token=env.tokens['bob'], project=cls.beta).status == 200

            # Grant olive's own agent Osprey on Alpha and give it a coordinator grant (creates audit row naming Osprey's id).
            cls.owner_agent_secret, cls.owner_agent_id = env.agent_with_id('olive', 'Osprey', projects=[env.project])
            assert env.grant(agent_id=cls.owner_agent_id, token=env.tokens['olive']).status == 200
            # Create a second agent owned by olive, then inject <script>, quotes, and Heron's exact id
            # (id-lookalike) into its stored name so the live HTTP service serves hostile text to the UI.
            cls.xss_agent_secret, cls.xss_agent_id = env.agent_with_id('olive', 'Lookalike', projects=[env.project])
            with env.service.store.lock:
                env.service.state['agents'][cls.xss_agent_id]['name'] = '<script>alert("x")</script> ' + cls.owner_agent_id

            # Put Heron on the lanes board by claiming a task in Alpha with Heron's credential.
            created_task = env.request('POST', '/v1/projects/%s/tasks' % env.project,
                                       {'title': 'Lane task for Heron'}, token=env.tokens['olive'])
            assert created_task.status in (200, 201), created_task.data
            tid = created_task.data['id']
            claimed = env.request('POST', '/v1/projects/%s/tasks/%s/claim' % (env.project, tid),
                                  {}, token=cls.owner_agent_secret)
            assert claimed.status == 200, claimed.data

            # Create and redeem an invite with a pre-approved agent in Alpha so invitePanel has an item with agent_id.
            inv = env.request('POST', '/v1/invites', {
                'display_name': 'Nora',
                'username': 'nora583',
                'projects': [env.project],
                'expires_hours': 24,
                'agent': {'name': 'Kestrel', 'project': env.project, 'role': 'worker', 'claim_limit': 1},
            }, token=env.tokens['olive'])
            assert inv.status == 201, inv.data
            redeemed = env.request('POST', '/v1/invites/redeem',
                                   {'token': inv.data['token'], 'password': 'nora-password-123'})
            assert redeemed.status in (200, 201), redeemed.data
            cls.redeemed_agent_id = redeemed.data['agent']['id']

            # Add an audit row whose own detail mentions owner_agent_id directly (tests detail parsing).
            with env.service.store.lock:
                env.service.state['audit'].append({
                    'time': '2026-11-20T12:00:00Z',
                    'user_id': env.ids['olive'],
                    'project_id': env.project,
                    'action': 'agents.custom.check',
                    'outcome': 'committed',
                    'reason': 'checked ' + cls.owner_agent_id + ' status',
                })

            cls.seen = None
            node = shutil.which('node')
            if node:
                web = KIT / 'web' / 'js'
                done = run_node_module(
                    env, node, 'await import(process.argv[1])',
                    (KIT / 'tests' / 'web_agent_ids_583.mjs').as_uri(),
                    (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
                    (web / 'api.js').as_uri(),
                    (web / 'ui.js').as_uri(),
                    (web / 'views' / 'project.js').as_uri(),
                    (web / 'views' / 'agents.js').as_uri(),
                    (web / 'views' / 'coordination.js').as_uri(),
                    (web / 'views' / 'invites.js').as_uri(),
                    (web / 'views' / 'how.js').as_uri(),
                    'http://127.0.0.1:%d' % env.port,
                    env.project, cls.beta,
                    env.tokens['olive'], env.tokens['vera'],
                    cls.owner_agent_id, cls.beta_agent_id, cls.xss_agent_id, env.ids['olive'],
                )
                assert done.returncode == 0, done.stderr[-3000:]
                cls.seen = json.loads(done.stdout.strip().splitlines()[-1])
        except Exception:
            env.doCleanups()
            raise

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'env'):
            cls.env.doCleanups()

    def _seen(self):
        if self.seen is None:
            self.skipTest('node is not installed')
        return self.seen

    # (1) Every listed place renders the agent id as visible TEXT (in textContent), not only data-*.
    def test_01_every_listed_place_renders_agent_id_as_visible_text(self):
        seen = self._seen()
        aid = self.owner_agent_id
        for place in ('projectAgents', 'coordination', 'myAgents', 'setupChooser',
                      'setupWizardPage', 'setupDialog', 'howPage', 'lanes', 'inviteSetupBlock', 'audit'):
            with self.subTest(place=place):
                self.assertIn(aid, seen[place]['text'], '%s must show %s in textContent' % (place, aid))
                chip_codes = [c['codeText'] for c in seen[place]['chips']]
                self.assertIn(aid, chip_codes, '%s must render %s in a visible id chip' % (place, aid))
        # Invite list shows the redeemed invite's agent_id as visible text.
        self.assertEqual(self.redeemed_agent_id, seen['invites']['redeemedAgentId'])
        self.assertIn(self.redeemed_agent_id, seen['invites']['text'])
        self.assertIn(self.redeemed_agent_id, [c['codeText'] for c in seen['invites']['chips']])
        # Condition 3: audit view must never call /v1/agents to enrich rows.
        self.assertEqual([], seen['audit']['agentLookupCalls'])

    # (2) Copy button copies exact id (no spaces, no newline, no name) + fallback without Clipboard API.
    def test_02_copy_button_copies_exact_id_and_fallback_without_clipboard(self):
        seen = self._seen()
        aid = self.owner_agent_id
        self.assertEqual([aid], seen['copy']['clipboardValues'])
        self.assertEqual([{'cmd': 'copy', 'value': aid}, {'cmd': 'copy', 'value': aid}], seen['copy']['execValues'])
        self.assertEqual(0, seen['copy']['remainingTextareasAfterSuccess'])
        self.assertEqual(0, seen['copy']['remainingTextareasAfterFail'])
        self.assertIn('Copy is blocked here: select the text and press Ctrl+C', seen['copy']['blockedToast'])
        # Every chip's data-copy-id matches its exact id without whitespace or name.
        for place in ('projectAgents', 'coordination', 'myAgents', 'lanes', 'invites', 'audit'):
            for chip in seen[place]['chips']:
                self.assertEqual(chip['id'], chip['copyId'])
                self.assertEqual(chip['id'], chip['codeText'])
                self.assertNotRegex(chip['copyId'], r'\s')

    # (3) Search/filter matches full id, id without agent_ prefix, >= 4 hex prefix, and name;
    #     Condition 4: < 4 hex chars (2 or 3) must NOT match via hex prefix.
    def test_03_search_and_filter_match_id_stripped_hex_prefix_min4_and_name(self):
        seen = self._seen()
        aid = self.owner_agent_id
        # Project agents panel
        ps = seen['projectAgentsSearch']
        self.assertEqual([aid], ps['fullId'])
        self.assertEqual([aid], ps['noPrefix'])
        self.assertEqual([aid], ps['hex4'])
        self.assertNotIn(aid, ps['hex2'], '2-char hex must not match via hex prefix')
        self.assertNotIn(aid, ps['hex3'], '3-char hex must not match via hex prefix')
        self.assertEqual([aid], ps['byName'])
        # My agents panel
        ms = seen['myAgents']['search']
        self.assertEqual([aid], ms['fullId'])
        self.assertEqual([aid], ms['noPrefix'])
        self.assertEqual([aid], ms['hex4'])
        self.assertNotIn(aid, ms['hex2'])
        self.assertEqual([aid], ms['byName'])
        # Lanes board
        ls = seen['lanes']['search']
        self.assertIsNotNone(ls)
        self.assertEqual([aid], ls['fullId'])
        self.assertEqual([aid], ls['noPrefix'])
        self.assertEqual([aid], ls['hex4'])
        self.assertNotIn(aid, ls['hex2'])
        self.assertEqual([aid], ls['byName'])
        # Invites panel
        ivs = seen['invites']['search']
        self.assertIsNotNone(ivs)
        self.assertEqual([self.redeemed_agent_id], ivs['fullId'])
        self.assertEqual([self.redeemed_agent_id], ivs['noPrefix'])
        self.assertEqual([self.redeemed_agent_id], ivs['hex4'])
        self.assertNotIn(self.redeemed_agent_id, ivs['hex2'])
        # Audit panel
        aus = seen['audit']['search']
        self.assertIsNotNone(aus)
        self.assertTrue(any(aid in row for row in aus['fullId']), aus)
        self.assertTrue(any(aid in row for row in aus['noPrefix']), aus)
        self.assertTrue(any(aid in row for row in aus['hex4']), aus)
        # 2-char hex never matches by id (only if those 2 letters occur in non-id text such as 'created').
        hex2 = aid.split('_', 1)[1][:2].lower()
        for row in aus['hex2']:
            non_id = re.sub(r'\b(?:agent|usr)_[0-9a-f]+\b', '', row.lower())
            self.assertIn(hex2, non_id, '2-char hex %r should not match audit row %r by id' % (hex2, row))

    # (4) Agent/person name with <script>, quotes, or id-lookalike renders as text and does NOT make a second agent match.
    def test_04_xss_and_id_lookalike_names_render_as_text_and_do_not_match_other_agent(self):
        seen = self._seen()
        # Text-only DOM: zero <script> tags created anywhere.
        self.assertEqual(0, seen['projectAgents']['scriptTags'])
        self.assertEqual(0, seen['coordination']['scriptTags'])
        self.assertEqual(0, seen['myAgents']['scriptTags'])
        self.assertIn('<script>', seen['projectAgents']['text'])
        # Searching for Heron's full id, stripped id, or 4-char hex matches ONLY Heron, NOT the lookalike agent.
        ps = seen['projectAgentsSearch']
        self.assertEqual([self.owner_agent_id], ps['fullId'])
        self.assertEqual([self.owner_agent_id], ps['noPrefix'])
        self.assertEqual([self.owner_agent_id], ps['hex4'])
        self.assertNotIn(self.xss_agent_id, ps['fullId'])
        self.assertNotIn(self.xss_agent_id, ps['noPrefix'])
        self.assertNotIn(self.xss_agent_id, ps['hex4'])
        # Searching for literal "<script>" still finds the XSS-named agent by its non-id name text.
        self.assertEqual([self.xss_agent_id], ps['byXssText'])

    # (5) Viewer sees ids only of agents in accessible projects (never cross-project leak).
    def test_05_viewer_sees_ids_only_of_accessible_projects(self):
        env = self.env
        # Vera is a viewer on Alpha only; she has NO access to Beta.
        alpha_coord = env.request('GET', '/v1/projects/%s/coordination' % env.project, token=env.tokens['vera'])
        self.assertEqual(200, alpha_coord.status, alpha_coord.data)
        alpha_agent_ids = [a['id'] for a in alpha_coord.data['agents']]
        self.assertIn(self.owner_agent_id, alpha_agent_ids)
        self.assertNotIn(self.beta_agent_id, alpha_agent_ids)
        # Beta coordination is refused to Vera.
        beta_coord = env.request('GET', '/v1/projects/%s/coordination' % self.beta, token=env.tokens['vera'])
        self.assertIn(beta_coord.status, (403, 404))
        # Vera's /v1/agents never includes Beta's agent.
        vera_agents = env.request('GET', '/v1/agents', token=env.tokens['vera'])
        self.assertEqual(200, vera_agents.status, vera_agents.data)
        self.assertNotIn(self.beta_agent_id, [a['id'] for a in vera_agents.data['items']])
        # Olive (owner of Alpha, no access to Beta) never sees Beta's agent in Alpha's agents, coordination, or audit.
        olive_pa = env.request('GET', '/v1/projects/%s/agents' % env.project, token=env.tokens['olive'])
        self.assertEqual(200, olive_pa.status)
        self.assertNotIn(self.beta_agent_id, [a['id'] for a in olive_pa.data['items']])
        olive_coord = env.request('GET', '/v1/projects/%s/coordination' % env.project, token=env.tokens['olive'])
        self.assertEqual(200, olive_coord.status)
        self.assertNotIn(self.beta_agent_id, [a['id'] for a in olive_coord.data['agents']])
        olive_audit = env.request('GET', '/v1/projects/%s/audit' % env.project, token=env.tokens['olive'])
        self.assertEqual(200, olive_audit.status)
        self.assertNotIn(self.beta_agent_id, json.dumps(olive_audit.data))
        # Agent owned by an Alpha owner with a coordinator grant record on Alpha, but whose live
        # projects list no longer includes Alpha (only Beta): must NOT appear in Alpha's coordination.
        _, moved_id = env.agent_with_id('olive', 'MovedAgent', projects=[env.project])
        self.assertEqual(200, env.grant(agent_id=moved_id, token=env.tokens['olive']).status)
        with env.service.store.lock:
            env.service.state['agents'][moved_id]['projects'] = [self.beta]
        after_move = env.request('GET', '/v1/projects/%s/coordination' % env.project, token=env.tokens['vera'])
        self.assertEqual(200, after_move.status)
        self.assertNotIn(moved_id, [a['id'] for a in after_move.data['agents']])

    # (6) Person's account id (usr_...) shown with copy and search in Account column & My agents.
    def test_06_account_column_shows_copyable_searchable_person_account_id(self):
        seen = self._seen()
        oid = self.env.ids['olive']
        self.assertTrue(oid.startswith('usr_'))
        # Shown as copyable id badge in Agents in this project and My agents.
        self.assertIn(oid, [c['codeText'] for c in seen['projectAgents']['chips']])
        self.assertIn(oid, [c['copyId'] for c in seen['projectAgents']['chips']])
        self.assertIn(oid, [c['codeText'] for c in seen['myAgents']['chips']])
        self.assertIn(oid, [c['copyId'] for c in seen['myAgents']['chips']])
        # Searchable by full usr_ id, stripped hex, and 4-char hex prefix (and NOT 2-char hex).
        ps = seen['projectAgentsSearch']
        self.assertIn(self.owner_agent_id, ps['byAccountFull'])
        self.assertIn(self.owner_agent_id, ps['byAccountBare'])
        self.assertIn(self.owner_agent_id, ps['byAccountHex4'])
        self.assertNotIn(self.owner_agent_id, ps['byAccountHex2'])
        self.assertIn(self.owner_agent_id, seen['myAgents']['search']['byAccountFull'])

    # (7) Narrow screens: full id stays in textContent and is never inside .hide-narrow or display:none.
    def test_07_narrow_screen_keeps_full_id_in_text_content_and_never_hidden(self):
        seen = self._seen()
        for place in ('projectAgents', 'coordination', 'myAgents', 'setupChooser',
                      'setupWizardPage', 'setupDialog', 'howPage', 'lanes', 'invites', 'inviteSetupBlock', 'audit'):
            self.assertTrue(seen[place]['chips'], '%s should have at least one id chip' % place)
            for chip in seen[place]['chips']:
                self.assertFalse(chip['inHideNarrow'], '%s chip %s must never be inside .hide-narrow' % (place, chip['id']))
                self.assertEqual(chip['id'], chip['codeText'])
        css = (KIT / 'web' / 'css' / 'app.css').read_text(encoding='utf-8')
        self.assertIn('.id-chip', css)
        self.assertIn('.id-code', css)
        self.assertIn('overflow-wrap: anywhere', css)
        self.assertNotRegex(css, r'\.id-(?:chip|code)[^{]*\{[^}]*display\s*:\s*none')

    # (8) Set-up step 8: path and Copy button come FIRST, then Save button; picker audit across web/js/.
    def test_08_setup_step8_path_and_copy_before_save_and_picker_audit(self):
        seen = self._seen()
        children = seen['step8']['children']
        self.assertTrue(children, seen['step8'])
        idx_step1 = next(i for i, c in enumerate(children) if c['stepLabel'] == 'copy-folder')
        idx_path = next(i for i, c in enumerate(children) if c['folderPath'])
        idx_copy = next(i for i, c in enumerate(children) if c['tag'] == 'BUTTON' and c['text'] == 'Copy')
        idx_step2 = next(i for i, c in enumerate(children) if c['stepLabel'] == 'save-folder')
        idx_save = next(i for i, c in enumerate(children) if c['action'] == 'save-files')
        self.assertTrue(idx_step1 < idx_path < idx_copy < idx_step2 < idx_save, children)
        self.assertIn('1. Copy the folder path', children[idx_step1]['text'])
        self.assertEqual('Save to agent folder…', children[idx_save]['text'])
        # Audit all picker calls in web/js/: only agents.js uses showDirectoryPicker / file pickers.
        picker_files = []
        for path in sorted((KIT / 'web' / 'js').rglob('*.js')):
            text = path.read_text(encoding='utf-8')
            if re.search(r'\bshow(?:Directory|SaveFile|OpenFile)Picker\b', text):
                picker_files.append(path.relative_to(KIT).as_posix())
        self.assertEqual(['web/js/views/agents.js'], picker_files)

    # (9) Set-up troubleshooting text has neutral connect-to sentence with no real host names or addresses.
    def test_09_setup_troubleshooting_has_neutral_connect_to_sentence(self):
        seen = self._seen()
        text = seen['troubleshooting']['constant']
        rendered = seen['troubleshooting']['rendered']
        self.assertIsNotNone(text)
        self.assertEqual(text, rendered)
        for phrase in ('NAT', 'forward', 'proxy', 'public_url', 'connect-to', '.curlrc'):
            self.assertIn(phrase, text)
        # No real host names, installation addresses, or IP literals.
        for forbidden in ('officewin', 'goomba', 'koopa', '192.168.', '10.0.', '127.0.0.1', 'http://', 'https://'):
            self.assertNotIn(forbidden, text.lower())

    # (10) Account (usr_...) lanes hide usr_ ids as text (kittrial-5bb.504 + .583 r2).
    def test_10_usr_lane_renders_no_usr_text_anywhere_in_block(self):
        seen = self._seen()
        blocks = {b['lane']: b for b in seen['usrLanes']['blocks']}
        oid = self.env.ids['olive']
        self.assertIn(oid, blocks)
        self.assertIn('usr_deadbeef00001111', blocks)
        self.assertIn(self.owner_agent_id, blocks)
        # Named usr_ lane: display_name shown, id only in title, zero usr_ text and zero id chips.
        named = blocks[oid]
        self.assertEqual('Olive', named['strongText'])
        self.assertEqual(oid, named['strongTitle'])
        self.assertNotIn('usr_', named['text'])
        self.assertEqual([], named['chips'])
        # Unnamed usr_ lane: 'Someone' fallback shown, id only in title, zero usr_ text and zero id chips.
        unnamed = blocks['usr_deadbeef00001111']
        self.assertEqual('Someone', unnamed['strongText'])
        self.assertEqual('usr_deadbeef00001111', unnamed['strongTitle'])
        self.assertNotIn('usr_', unnamed['text'])
        self.assertEqual([], unnamed['chips'])
        # Non-account agent_ lane alongside them still renders visible id + copy button.
        agent_blk = blocks[self.owner_agent_id]
        self.assertIn(self.owner_agent_id, agent_blk['text'])
        self.assertEqual([self.owner_agent_id], [c['copyId'] for c in agent_blk['chips']])


if __name__ == '__main__':
    unittest.main()
