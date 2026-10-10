"""How work is found: the served text, the served prompts and the order they state (kittrial-5bb.226).

One source for each text: the kit's files, served by the client's ``docs NAME`` and by the web
service's ``GET /v1/docs/NAME`` through the same reader, and shown as they are on the page "How
work is found". The order the text states for an agent is pinned here to the order
``GET /v1/agents/me/next`` gives, so that neither can change without the other.

It also carries the short recurring prompts of kittrial-5bb.219 (its item 1).
"""
import json
import os
import re
import shutil
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import onboarding
import test_agent_attention
import test_bd_label_aliases as rb
import test_claim_held as held_stack
import test_http_agents
import test_http_review_fixes as fixes
from test_http_agents import BASE, BUNDLE, COMMIT

NAMES = ('finding-work', 'worker-prompt', 'poll-prompt', 'poll-prompt-agent')
#: What a secret of this service looks like, and words that would mean one is in a text.
SECRET = re.compile(r'(?:orc|agt|cred|sess)_[A-Za-z0-9_-]{16,}|Bearer [A-Za-z0-9._-]{12,}|password\s*[:=]', re.I)


def step_heads(text):
    """The numbered steps of the document's "What a worker does at every run", in order."""
    part = text.split('## What a worker does at every run', 1)[1].split('\n## ', 1)[0]
    return re.findall(r'^### (\d)\. (.+)$', part, re.M)


class ServedTextTests(unittest.TestCase):
    """The files, the catalogue and the prompts, with no service."""

    def test_the_four_texts_are_in_the_catalogue_and_docs_serves_each_whole(self):
        self.assertEqual(onboarding.WEB_DOCUMENTS, NAMES)
        for name in NAMES:
            with self.subTest(document=name):
                served = onboarding.execute(KIT, KIT, 'example', 'worker', 'docs', [name])
                self.assertEqual(served, (KIT / onboarding.DOCUMENTS[name]).read_text(encoding='utf-8'))
                self.assertIn('  docs ' + name, onboarding.execute(KIT, KIT, 'example', 'worker', 'docs', []))
        with self.assertRaises(KeyError):
            onboarding.member_document(KIT, 'cli-contract')                          # catalogued, and not for the web

    def test_what_the_text_says_a_worker_does_and_in_what_order(self):
        text = (KIT / 'docs' / 'FINDING_WORK.md').read_text(encoding='utf-8')
        self.assertEqual([number for number, _ in step_heads(text)], ['1', '2', '3', '4'])
        heads = [head for _, head in step_heads(text)]
        self.assertIn('standing guidance', heads[0])
        self.assertIn('own tasks', heads[1])
        self.assertIn('Only when none of its own tasks needs action: the ready list', heads[2])
        self.assertIn('assigned to somebody else is held', heads[3])
        for said in ('**exactly one** task that nobody holds', 'is not an inbox', 'Silence is not a handoff',
                     'it cannot read the guidance yet', '**In the web interface today: no.**',
                     'set-guidance PROJECT --actor OPERATOR --file FILE', '`update TASK --assignee ACTOR --json`',
                     '`comments add TASK --file note.md --json`', 'kittrial-5bb.211', 'kittrial-5bb.212'):
            with self.subTest(said=said):
                self.assertIn(said, ' '.join(text.split()))
        # Three ways to reach a worker, each with what the web can do today and a command.
        reach = text.split('## How to reach a worker', 1)[1].split('\n## ', 1)[0]
        self.assertEqual(reach.count('**In the web interface today:'), 3)
        self.assertEqual(reach.count('**The command today,**'), 3)
        # Written in what the page renders: no table, and no list whose items wrap.
        self.assertNotIn('\n|', text)
        self.assertIsNone(re.search(r'^\s*[-*]\s', text, re.M))

    def test_what_the_text_says_is_not_there_is_not_there(self):
        """The text says three things the web cannot do yet. When a route for one of them arrives, this
        fails, and the text is to be corrected with it."""
        import http_service
        source = Path(http_service.__file__).read_text(encoding='utf-8')
        declared = re.findall(r"@route\('([A-Z]+)', r'([^']*)'", source)
        self.assertGreater(len(declared), 50)
        self.assertEqual([path for _, path in declared if 'guidance' in path], [])
        self.assertEqual([path for _, path in declared if 'comments' in path], [])
        self.assertEqual([path for _, path in declared if path.endswith("/assign'") or '/assignee' in path], [])
        self.assertEqual(http_service.ApiHandler.TASK_CHANGES, ('title', 'description', 'status', 'priority'))      # no assignee

    def test_a_prompt_is_the_fenced_block_up_to_the_last_fence(self):
        text = 'About.\n\n```text\nline one\n```sh\ninner\n```\nline two REPLACE_ONE and REPLACE_TWO_MORE, REPLACE_ONE again\n```\n\nAfter.\n'
        prompt = onboarding.prompt_of(text)
        self.assertEqual(prompt, 'line one\n```sh\ninner\n```\nline two REPLACE_ONE and REPLACE_TWO_MORE, REPLACE_ONE again\n')
        self.assertEqual(onboarding.placeholders(prompt), ['REPLACE_ONE', 'REPLACE_TWO_MORE'])
        for none in ('no block at all', '```text\nnever closed\n', '```\nclosed before\n```text\n', None, 5):
            with self.subTest(text=none):
                self.assertIsNone(onboarding.prompt_of(none))
        self.assertEqual(onboarding.placeholders('REPLACE_ lower REPLACE_x XREPLACE_NO'), [])

    def test_the_recurring_prompts_are_short_and_carry_no_state(self):
        """kittrial-5bb.219: a worker's poll prompt had grown to several thousand words of state."""
        self.assertEqual(onboarding.POLL_PROMPT_LIMIT, 2000)
        wanted = {'poll-prompt': ['REPLACE_PROJECT', 'REPLACE_WORKING_FOLDER', 'REPLACE_ACTOR_FILE', 'REPLACE_CLIENT_PREFIX',
                                  'REPLACE_INTERVAL'],
                  'poll-prompt-agent': ['REPLACE_AGENT_NAME', 'REPLACE_SERVER_URL', 'REPLACE_INTERVAL']}
        for name, placeholders in wanted.items():
            with self.subTest(prompt=name):
                document = onboarding.member_document(KIT, name)
                prompt = document['prompt']
                self.assertLessEqual(len(prompt), onboarding.POLL_PROMPT_LIMIT, 'the recurring prompt has grown: %d' % len(prompt))
                self.assertEqual(document['placeholders'], placeholders)
                lines = prompt.splitlines()
                order = [line[:2] for line in lines if re.match(r'\d\. ', line)]
                self.assertEqual(order, ['1.', '2.', '3.', '4.'])
                step = {line[0]: line for line in lines if re.match(r'\d\. ', line)}
                self.assertIn('guidance', step['1'])
                self.assertIn('own tasks', step['2'])
                self.assertTrue(step['3'].startswith('3. Only if none of your own tasks needs action'), step['3'])
                self.assertIn('exactly one', step['3'])
                self.assertIn('assigned to somebody else is held', step['3'])
                self.assertIn('not an inbox', step['3'])
                self.assertIn('checkpoint', step['4'])
                self.assertIn('Never edit this prompt and never add status to it.', prompt)
                self.assertIn('If nothing changed since your last run, say so in one line and stop.', prompt)
                self.assertIn('waiting for a person', prompt)
                self.assertIsNone(SECRET.search(document['text']))

    def test_every_command_in_the_ssh_prompt_is_one_the_client_takes(self):
        prompt = onboarding.member_document(KIT, 'poll-prompt')['prompt']
        client = (KIT / 'client.py').read_text(encoding='utf-8')
        listed = re.search(r"elif args\[:1\] in \((.*?)\):action=args\.pop\(0\)", client)
        actions = set(re.findall(r"\['([a-z]+)'\]", listed.group(1)))
        endpoint = (KIT / 'endpoint.py').read_text(encoding='utf-8')
        passed_on = set(re.findall(r"'([a-z]+)'", re.search(r"^ALLOWED=\{(.*?)\}", endpoint, re.M).group(1)))
        # Extract command slots from the actual prose, including parenthesized
        # commands; do not maintain a second list of what the prompt says.
        arg = r'(?:--[a-z-]+|[A-Z][A-Z_]*)'
        heads = '|'.join(map(re.escape, sorted(actions | passed_on, key=len, reverse=True)))
        literal = '|'.join(map(re.escape, sorted(set(onboarding.DOCUMENTS) | {'get', 'ack', 'resume'}, key=len, reverse=True)))
        commands = re.findall(r'\b((?:' + heads + r') (?:' + literal + r'|' + arg + r')(?: ' + arg + r')*)', prompt)
        self.assertGreaterEqual(len(commands), 10, commands)
        self.assertIn('session resume', commands)
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(command, prompt)
                self.assertIn(command.split()[0], actions | passed_on)
        for name in re.findall(r'docs ([a-z-]+)', prompt):
            self.assertIn(name, onboarding.DOCUMENTS, 'the prompt names a document `docs` does not serve: ' + name)
        # The served name of docs/WORKER_START.md is `start`; `worker-start` is no document.
        self.assertEqual(onboarding.DOCUMENTS['start'], 'docs/WORKER_START.md')
        self.assertNotIn('worker-start', onboarding.DOCUMENTS)

    def test_every_concrete_document_reference_is_catalogued(self):
        count = 0
        for name in NAMES:
            text = onboarding.member_document(KIT, name)['text']
            refs = re.findall(r'\bdocs ([a-z][a-z-]+)(?=[`.,;\n]| --)', text)
            count += len(refs)
            for ref in refs:
                self.assertIn(ref, onboarding.DOCUMENTS, '%s names unserved docs %s' % (name, ref))
        self.assertGreaterEqual(count, 8)

    def test_guidance_limit_setup_visibility_and_activity_are_truthful(self):
        doc = ' '.join((KIT / 'docs/FINDING_WORK.md').read_text(encoding='utf-8').split())
        prompt = onboarding.member_document(KIT, 'poll-prompt-agent')['prompt']
        guide = (KIT / 'web/js/agentSetup.js').read_text(encoding='utf-8')
        self.assertIn('it cannot read the guidance yet. No route', doc)
        self.assertIn('for owners and superusers only', doc)
        self.assertIn('does not reach you over the web yet', prompt)
        self.assertIn('does not reach you over this API yet', guide)
        for text in (doc, prompt, guide):
            self.assertIn('Blocked and in-progress tasks', text)
            self.assertIn('reading acknowledges nothing', text.lower())
            self.assertNotIn('does not carry comments yet', text)
        self.assertIn('kittrial-5bb.114', doc)
        self.assertIn('kittrial-5bb.207', doc)
        self.assertIn("Without a checkpoint, an in-progress task's", doc)
        self.assertIn('save its first checkpoint', doc)

    def test_the_first_prompt_is_served_as_it_is(self):
        document = onboarding.member_document(KIT, 'worker-prompt')
        self.assertTrue(document['prompt'].startswith('Work only in the dedicated working directory'))
        self.assertIn('REPLACE_PROJECT', document['placeholders'])
        self.assertIn('ready --json', document['prompt'])                       # past the commands it shows in a block of its own
        self.assertTrue(document['prompt'].rstrip().endswith('files.'))


class Members(test_http_agents.AgentHarness):
    """alex owns the project and has an agent; vera only reads."""

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.alex_id = self.create_account(self.admin, 'alex', 'alex-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.project = self.create_project(self.alex, 'Alpha')
        self.ids = {}
        self.tokens = {}
        for name, role in (('vera', 'viewer'), ('casey', 'contributor')):
            self.ids[name] = self.create_account(self.admin, name, name + '-password-1')
            self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, self.ids[name]),
                                               {'role': role}, token=self.alex).status)
            self.tokens[name] = self.login(name, name + '-password-1')[0]
        self.agent_id, self.secret, _ = self.agent_secret(self.alex, projects=[self.project], name='Kestrel')

    def task(self, title):
        made = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': title}, token=self.alex)
        self.assertEqual(201, made.status, made.data)
        return made.data['id']

    def post(self, path, body, token):
        answer = self.request('POST', '/v1/projects/%s/tasks/%s' % (self.project, path), body, token=token)
        self.assertIn(answer.status, (200, 201), answer.data)
        return answer.data


class ServedByTheWebTests(Members):

    def test_every_member_reads_them_a_viewer_too_and_nobody_else(self):
        listed = self.request('GET', '/v1/docs', token=self.tokens['vera'])
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual([item['name'] for item in listed.data['items']], list(NAMES))
        self.assertEqual([sorted(item) for item in listed.data['items']][0],
                         ['file', 'name', 'placeholders', 'served_as', 'title'])
        for name in NAMES:
            for who, token in (('a viewer', self.tokens['vera']), ('an owner', self.alex), ('an agent', self.secret)):
                with self.subTest(document=name, reader=who):
                    answer = self.request('GET', '/v1/docs/' + name, token=token)
                    self.assertEqual(200, answer.status, answer.data)
                    self.assertEqual(answer.data['text'], (KIT / onboarding.DOCUMENTS[name]).read_text(encoding='utf-8'))
                    self.assertEqual((answer.data['served_as'], answer.data['file']), ('docs ' + name, onboarding.DOCUMENTS[name]))
                    self.assertEqual(answer.data['prompt'], onboarding.prompt_of(answer.data['text']))
                    self.assertIsNone(SECRET.search(json.dumps(answer.data)))
            self.assertEqual(401, self.request('GET', '/v1/docs/' + name).status)           # nobody signed in
        self.assertEqual(401, self.request('GET', '/v1/docs').status)
        # The rest of the catalogue stays with the client, and a name that is none is said.
        for name in ('cli-contract', 'start', 'nothing', 'project'):
            with self.subTest(document=name):
                refused = self.request('GET', '/v1/docs/' + name, token=self.alex)
                self.assertEqual(404, refused.status, refused.data)
                self.assertIn('GET /v1/docs lists', refused.data['error']['message'])
        self.assertEqual(404, self.request('GET', '/v1/docs/..%2Fhttp_auth.py', token=self.alex).status)

    def test_the_address_is_the_configured_one_never_the_requests(self):
        self.service.public_url = None
        self.assertIsNone(self.request('GET', '/v1/docs/poll-prompt-agent', token=self.alex).data['server_url'])
        self.service.public_url = 'https://office.example'
        for path in ('/v1/docs', '/v1/docs/poll-prompt-agent'):
            self.assertEqual(self.request('GET', path, token=self.alex).data['server_url'], 'https://office.example')

    def test_a_document_that_cannot_be_read_is_said_not_shown_empty(self):
        import http_service
        from unittest import mock
        with mock.patch.object(http_service, 'KIT_DIRECTORY', KIT / 'no-such-kit'):
            answer = self.request('GET', '/v1/docs/finding-work', token=self.alex)
        self.assertEqual(503, answer.status, answer.data)
        self.assertIn('could not be read from the installed kit', answer.data['error']['message'])


class OrderTests(Members):
    """The order the text states is the order the route gives. Fails if either changes."""

    def own_and_free(self):
        feedback, blocked, working, free, held = (self.task(title) for title in ('feedback', 'blocked', 'working', 'free', 'held'))
        for own in (feedback, blocked, working):
            self.post(own + '/claim', {}, self.secret)
        self.post(feedback + '/reviews', {'operation': 'contribute', 'commit': COMMIT, 'base_commit': BASE,
                                          'bundle_sha256': BUNDLE, 'summary': 'delivered'}, self.secret)
        self.post(feedback + '/reviews', {'operation': 'request-changes', 'summary': 'please revise'}, self.alex)
        self.post(blocked + '/checkpoints', {'previous': None, 'summary': 'stuck',
                  'open_items': [{'kind': 'blocker', 'text': 'which page size?'}]},
                  self.secret)
        self.post(held + '/claim', {}, self.tokens['casey'])                     # somebody else's
        return feedback, blocked, working, free, held

    def check(self, feedback, blocked, working, free, held):
        answer = self.request('GET', '/v1/agents/me/next', token=self.secret)
        self.assertEqual(200, answer.status, answer.data)
        actions = [(action['kind'], action['task']) for action in answer.data['next_actions']]
        self.assertEqual(actions, [('changes-requested', feedback), ('blocked', blocked), ('in-progress', working),
                                   ('claimable-task', free)])
        self.assertEqual(answer.data['next_action']['task'], feedback)
        self.assertNotIn(held, [task for _, task in actions])                    # held by somebody else: never offered
        # Take the agent's own work away: only then is the free task what comes first.
        return answer.data

    def test_the_route_gives_own_feedback_then_blocked_then_in_progress_and_only_then_free_work(self):
        self.check(*self.own_and_free())

    def test_the_text_and_the_agents_steps_state_that_order(self):
        text = ' '.join((KIT / 'docs' / 'FINDING_WORK.md').read_text(encoding='utf-8').split())
        self.assertIn('The reply lists its own tasks first: changes requested, then a task blocked by its last checkpoint, '
                      'then a task in progress.', text)
        self.assertIn('the same reply lists the tasks it could claim, after its own', text)
        guide = (KIT / 'web' / 'js' / 'agentSetup.js').read_text(encoding='utf-8')
        self.assertIn("export const RUN_ORDER = ['guidance', 'own-tasks', 'ready-list', 'held', 'checkpoint'];", guide)
        said = ['The reply lists your own tasks first: review feedback to address, then a task you left blocked, then one in '
                'progress. Act on the first.', '3. Only when none of your own tasks needs action: the tasks the same reply says you '
                'could claim. Claim exactly one that nobody holds']
        self.assertLess(guide.index(said[0]), guide.index(said[1]))
        agent = onboarding.member_document(KIT, 'poll-prompt-agent')['prompt']
        self.assertIn('Your own tasks come first in the reply: review feedback, then a task you left blocked, then one in '
                      'progress.', agent)
        # The words of the three texts for the three own kinds, in the order of the kinds the route gives.
        kinds = [kind for kind, _ in [(action['kind'], action['task']) for action in
                                      self.check(*self.own_and_free())['next_actions']]]
        self.assertEqual(kinds, ['changes-requested', 'blocked', 'in-progress', 'claimable-task'])

    def test_the_agents_own_guide_is_built_with_the_order_and_no_secret(self):
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:
            print('NOTE: OrderTests.test_the_agents_own_guide_is_built_with_the_order_and_no_secret was SKIPPED: node is '
                  'not installed, so web/js/agentSetup.js was not run on this platform.', file=sys.stderr)
            self.skipTest('node is not installed; the agent guide is not built here')
        script = ('const m = await import(process.argv[1]);'
                  'const p = m.secretlessPayload({ agent: { id: "agent_1", name: "Kestrel", owner_display_name: "alex" } }, "https://office.example");'
                  'console.log(JSON.stringify({ guide: m.agentGuide(p), order: m.RUN_ORDER, steps: m.runSteps(p.server, p.secretFile).length })'
                  '.replace(/[^\\x00-\\x7f]/g, (c) => "\\\\u" + c.charCodeAt(0).toString(16).padStart(4, "0")));')
        done = run_node_module(self, node, script, (KIT / 'web' / 'js' / 'agentSetup.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-2000:])
        built = json.loads(done.stdout.strip().splitlines()[-1])
        guide = built['guide']
        self.assertEqual((built['order'], built['steps']), (['guidance', 'own-tasks', 'ready-list', 'held', 'checkpoint'], 5))
        numbered = re.findall(r'^(\d)\. (.{0,60})', guide, re.M)
        self.assertEqual([number for number, _ in numbered], ['1', '2', '3', '4', '5', '6', '7'])
        self.assertTrue(numbered[0][1].startswith("The coordinator's standing guidance does not"), numbered[0])
        self.assertTrue(numbered[1][1].startswith('Your own tasks: GET https://office.example/v1'), numbered[1])
        self.assertTrue(numbered[2][1].startswith('Only when none of your own tasks needs'), numbered[2])
        self.assertTrue(numbered[3][1].startswith('A task assigned to somebody else is held'), numbered[3])
        self.assertIn('GET https://office.example/v1/docs/finding-work', guide)
        self.assertIsNone(SECRET.search(guide))



def only_its_own_tests(*bases):
    """Borrows a harness that is a TestCase with tests of its own and runs none of them again."""
    def decorate(cls):
        for base in bases:
            for name in dir(base):
                if name.startswith('test_') and name not in cls.__dict__:
                    setattr(cls, name, None)
        return cls
    return decorate


@only_its_own_tests(test_agent_attention.EndpointAttentionTests)
class EndpointOrderTests(test_agent_attention.EndpointAttentionTests):
    """The same order on the ENDPOINT backend (the strict canonical stand-in): item 4 of the task.
    kittrial-5bb.114 was that this read knew no review state there, so an agent with changes
    requested was offered free work. tests/test_agent_attention.py covers every own state; this is
    the one statement the page makes, on the backend an installation runs."""

    def test_own_feedback_then_blocked_then_in_progress_and_only_then_free_work(self):
        feedback, blocked, working, free = self.tasks
        held = self.create_task(self.people['blair'], self.project, 'held').data['id']
        self.assertEqual(200, self.request('POST', self.base(held) + '/claim', {}, token=self.people['blair']).status)
        for own in (feedback, blocked, working):
            self.claim(own)
        contribution = self.contribute(feedback)
        self.review(self.people['blair'], feedback, 'request-changes', previous=contribution, contribution=contribution,
                    items=[{'id': 'item-1', 'text': 'please revise'}])
        self.checkpoint(blocked, [{'id': 'size', 'kind': 'blocker', 'source': 'task', 'text': 'which page size?'}])
        data = self.next()
        self.assertEqual(test_agent_attention.kinds(data),
                         [('changes-requested', feedback), ('blocked', blocked), ('in-progress', working), ('claimable-task', free)])
        self.assertEqual(data['next_action']['task'], feedback)
        self.assertNotIn(held, [task for _, task in test_agent_attention.kinds(data)])


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@only_its_own_tests(rb.RealBdLabelAliasTests, held_stack.RealStackTests)
class RealStackOrderTests(held_stack.RealStackTests):
    """And through the real stack: the web service, the real endpoint.py, a real bd."""

    @unittest.skipUnless(os.environ.get('ORCHESTRA_ATTENTION_SCALE') == '1',
                         'set ORCHESTRA_ATTENTION_SCALE=1 for the282-task native activity fixture')
    def test_flagged_task_is_not_hidden_by_282_owned_native_tasks(self):
        import time
        made = self.request('POST', '/v1/agents', {'name': 'Scale reader', 'projects': ['pp']},
                            token=self.tokens['casey'])
        self.assertEqual(201, made.status, made.data)
        secret = made.data['credential']['secret']; actor = made.data['agent']['actor']
        def native_ok(*args):
            done = self.bd(*args)
            self.assertEqual(0, done.returncode, done.stderr)
            return done.stdout
        owned = []
        for index in range(282):
            row = json.loads(native_ok('create', '--title', 'Owned scale %04d' % index,
                                           '--assignee', actor, '--json'))
            owned.append(row['id'])
        for offset in range(0, len(owned), 50):
            native_ok('update', *owned[offset:offset + 50], '--status', 'in_progress')
        target = sorted(owned)[-1]; base = self.tasks + '/' + target
        time.sleep(1.05)
        before = self.request('GET', base + '/brief', token=secret)
        self.assertEqual(200, before.status, before.data)
        cp = self.request('POST', base + '/checkpoints', dict(schema_version=1, previous=None,
            activity_cursor=before.data['activity_cursor'], intent='scale', acceptance='flag survives cap',
            summary='working', next_action='read feedback', open_items=[], resolved=[]), token=secret)
        self.assertEqual(201, cp.status, cp.data)
        native_ok('comments', 'add', target, 'Direction at the far end', '--author', 'scale-coordinator')
        for _ in range(2):
            answer = self.request('GET', '/v1/agents/me/next', token=secret)
            self.assertEqual(200, answer.status, answer.data)
            data = answer.data; first = data['next_actions'][0]
            self.assertEqual((target, True), (first['task'], first['newer_activity']))
            self.assertEqual((282, 1), (data['attention']['counts']['claimed'], data['attention']['counts']['newer_activity']))
            self.assertTrue(data['attention']['actions_truncated'])
            self.assertFalse(data['attention']['own_tasks_truncated'])
            brief = self.request('GET', first['links']['brief'], token=secret)
            self.assertEqual(200, brief.status, brief.data)
            self.assertGreaterEqual(brief.data['newer']['other_count'], 1)
            print('NATIVE_FLAGGED_SCALE ' + json.dumps(dict(claimed=282, flagged=1,
                  listed=len(data['next_actions']), first=first['task'], newer=brief.data['newer']['other_count'])), flush=True)

    def test_in_progress_comment_reaches_the_agent_and_brief_on_the_real_stack(self):
        import time
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/synthetic/kestrel', 'projects': ['pp']}, token=self.tokens['casey'])
        self.assertEqual(201, made.status, made.data)
        agent = made.data['credential']['secret']
        task = self.new('progress comment'); base = '%s/%s' % (self.tasks, task)
        self.assertEqual(200, self.claim('casey', task, token=agent).status)
        time.sleep(1.05)  # Native timestamps have second precision; isolate the checkpoint.
        before = self.request('GET', base + '/brief', token=agent)
        self.assertEqual(200, before.status, before.data)
        self.assertIn('newer', before.data)
        cp = self.request('POST', base + '/checkpoints', {'schema_version': 1, 'previous': None,
            'activity_cursor': before.data['activity_cursor'], 'intent': 'read feedback', 'acceptance': 'comment is visible',
            'summary': 'working', 'next_action': 'continue', 'open_items': [], 'resolved': []}, token=agent)
        self.assertEqual(201, cp.status, cp.data)
        answer = rb.endpoint.execute(self.root, {'project': 'pp', 'actor': 'session-synthetic-coordinator', 'action': 'bd',
            'args': ['comments', 'add', task, 'Review this <script> literally\u0085next line', '--json'], 'attachments': {}})
        self.assertEqual(0, answer['returncode'], answer)
        for _ in range(2):
            current = self.request('GET', '/v1/agents/me/next', token=agent)
            self.assertEqual(200, current.status, current.data)
            action = next(x for x in current.data['next_actions'] if x['task'] == task)
            self.assertEqual((action['kind'], action['newer_activity']), ('in-progress', True))
            self.assertEqual(base, action['links']['task'])
            brief = self.request('GET', action['links']['brief'], token=agent)
            self.assertEqual(200, brief.status, brief.data)
            self.assertGreaterEqual(brief.data['newer']['other_count'], 1)
            self.assertLessEqual(len(brief.data['newer']['entries']), 5)
            self.assertEqual(brief.data['checkpoint']['id'], cp.data['comment_id'])
            self.assertEqual(action['links']['history'], brief.data['newer']['history'])
            self.assertNotIn('history_new', brief.data['newer'])
            self.assertNotIn('verify', brief.data['newer'])
        history = self.request('GET', base + '/history?limit=100', token=agent)
        self.assertEqual(200, history.status, history.data)
        self.assertIn('Review this <script> literally', json.dumps(history.data, ensure_ascii=False))

    def test_own_feedback_comes_before_free_work_and_a_held_task_is_not_offered(self):
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/x/kestrel', 'projects': ['pp']},
                            token=self.tokens['casey'])
        self.assertEqual(201, made.status, made.data)
        agent = made.data['credential']['secret']
        feedback, working, free, held = (self.new(title) for title in ('feedback', 'working', 'free', 'held'))
        for own in (feedback, working):
            self.assertEqual(200, self.claim('casey', own, token=agent).status)
        self.assertEqual(200, self.claim('drew', held).status)
        reviews = '%s/%s/reviews' % (self.tasks, feedback)
        delivered = self.request('POST', reviews, dict(fixes.CONTRIBUTION, operation='contribute', schema_version=1,
                                                       operation_id='op-order-1', previous=None), token=agent)
        self.assertEqual(201, delivered.status, delivered.data)
        contribution = self.request('GET', '%s/%s/brief' % (self.tasks, feedback), token=self.tokens['alex']).data['review']['contribution']['id']
        asked = self.request('POST', reviews, dict(operation='request-changes', schema_version=1, operation_id='op-order-2',
                                                   previous=contribution, contribution=contribution,
                                                   items=[{'id': 'item-1', 'text': 'please revise'}]),
                             token=self.tokens['alex'])
        self.assertEqual(201, asked.status, asked.data)
        answer = self.request('GET', '/v1/agents/me/next', token=agent)
        self.assertEqual(200, answer.status, answer.data)
        kinds = [(action['kind'], action['task']) for action in answer.data['next_actions']]
        self.assertEqual(kinds, [('changes-requested', feedback), ('in-progress', working), ('claimable-task', free)])
        self.assertEqual(answer.data['next_action']['task'], feedback)
        self.assertNotIn(held, [task for _, task in kinds])


class PageTests(Members):
    """web/js/views/how.js run under Node, with a small DOM, against this real service."""

    def test_the_page_shows_what_the_kit_serves_to_an_owner_and_to_a_viewer(self):
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:
            print('NOTE: PageTests.test_the_page_shows_what_the_kit_serves_to_an_owner_and_to_a_viewer was SKIPPED: node '
                  'is not installed, so web/js/views/how.js was not run on this platform.', file=sys.stderr)
            self.skipTest('node is not installed; the page is not run here')
        self.service.public_url = 'https://office.example'
        web = KIT / 'web' / 'js'
        done = run_node_module(self, node, 'await import(process.argv[1])',
                               (KIT / 'tests' / 'web_how_screen.mjs').as_uri(),
                               (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(), (web / 'api.js').as_uri(),
                               (web / 'views' / 'how.js').as_uri(), 'http://127.0.0.1:%d' % self.port, self.project,
                               'alex=%s=%s' % (self.alex_id, self.alex), 'vera=%s=%s' % (self.ids['vera'], self.tokens['vera']))
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        text = (KIT / 'docs' / 'FINDING_WORK.md').read_text(encoding='utf-8')
        owner, viewer = seen['owner'], seen['viewer']
        # The text is the kit's, for both readers alike: every heading of the document, in order.
        wanted = [('H%d ' % (len(marks) + 1)) + head.replace('**', '').replace('`', '')
                  for marks, head in re.findall(r'^(#{2,4}) (.+)$', text, re.M)]
        self.assertEqual(owner['title'], 'How work is found')
        self.assertEqual(owner['headings'], wanted)
        self.assertEqual(viewer['headings'], wanted)
        self.assertEqual(viewer['howText'], owner['howText'])
        for said in ('1. The coordinator\'s standing guidance', '3. Only when none of its own tasks needs action: the ready list',
                     'In the web interface today: no.', 'comments add TASK --file note.md --json', 'Served by the kit as docs finding-work.'):
            with self.subTest(said=said):
                self.assertIn(said, owner['howText'])
        self.assertNotIn('**', owner['howText'])                                 # rendered, not shown as marks
        # The owner's agent: its resume prompt and its recurring prompt, with its name and the address in them.
        self.assertEqual((owner['agents'], owner['noAgents']), (1, 0))
        resume, poll = owner['agentBlocks']
        self.assertEqual((resume['prompt'], poll['prompt']), ('resume', 'agent'))
        self.assertIn('You are Kestrel, an Orchestra agent. Read .orchestra/AGENT.md', resume['text'])
        prompt = onboarding.member_document(KIT, 'poll-prompt-agent')['prompt']
        self.assertEqual(poll['text'], prompt.replace('REPLACE_AGENT_NAME', 'Kestrel').replace('REPLACE_SERVER_URL', 'https://office.example'))
        self.assertEqual((poll['left'], poll['served'], poll['buttons']), ('REPLACE_INTERVAL', ['docs poll-prompt-agent'], ['Copy']))
        # A viewer has no agent: the prompt is there, with the name left to replace.
        self.assertEqual((viewer['agents'], viewer['noAgents']), (0, 1))
        self.assertEqual([block['prompt'] for block in viewer['agentBlocks']], ['agent'])
        self.assertEqual(viewer['agentBlocks'][0]['left'], 'REPLACE_AGENT_NAME REPLACE_INTERVAL')
        self.assertIn('https://office.example/v1/agents/me/next', viewer['agentBlocks'][0]['text'])
        # A worker over SSH: the first prompt and the recurring one, as served, with the project in them.
        for reader in (owner, viewer):
            first, recurring = reader['workerBlocks']
            self.assertEqual((first['prompt'], recurring['prompt']), ('first', 'worker'))
            self.assertEqual(first['text'], onboarding.member_document(KIT, 'worker-prompt')['prompt'].replace('REPLACE_PROJECT', self.project))
            self.assertEqual(recurring['text'], onboarding.member_document(KIT, 'poll-prompt')['prompt'].replace('REPLACE_PROJECT', self.project))
            self.assertEqual(recurring['left'], 'REPLACE_WORKING_FOLDER REPLACE_ACTOR_FILE REPLACE_CLIENT_PREFIX REPLACE_INTERVAL')
            self.assertEqual((first['served'], recurring['served']), (['docs worker-prompt'], ['docs poll-prompt']))
            self.assertNotIn('REPLACE_PROJECT', first['text'] + recurring['text'])
        # No prompt on the page holds a secret: not the agent's, not anybody's session.
        everything = json.dumps(seen)
        for secret in (self.secret, self.alex, self.tokens['vera']):
            self.assertNotIn(secret, everything)
        self.assertIsNone(SECRET.search(everything))
        # A document that cannot be read: said, and no panel is shown half.
        self.assertEqual(seen['unreadable']['panels'], 0)
        self.assertIn('How work is found', seen['unreadable']['text'])
        # What the page fills in and what it leaves.
        self.assertEqual(seen['fill'], [{'text': 'A x b REPLACE_TWO c x', 'left': ['REPLACE_TWO']},
                                        {'text': 'A REPLACE_ONE', 'left': ['REPLACE_ONE']},
                                        {'text': 'nothing here', 'left': []}, {'text': '', 'left': []}])
        self.assertEqual(seen['body'], 'First paragraph.\n\n## Next\n')
        self.assertEqual(seen['once'], {'text': 'REPLACE_SERVER_URL visits https://office.example; 10 minutes', 'left': []})
        placeholder = seen['placeholderAgent'][1]
        self.assertIn('You are REPLACE_SERVER_URL, an Orchestra agent.', placeholder['text'])
        self.assertIn('https://office.example/v1/agents/me/next', placeholder['text'])
        self.assertEqual(placeholder['left'], 'REPLACE_INTERVAL')
        for status, failure in seen['errors'].items():
            with self.subTest(status=status):
                self.assertEqual((failure['injected'], failure['partial'], failure['recovered']), (0, 0, True))
                self.assertNotIn('whether this was saved', failure['before'])
                self.assertNotIn('same button again without changing', failure['before'])
                self.assertNotIn('<script>bad</script>', failure['before'])
                self.assertIn('documents', failure['before'])
        self.assertIn('update the installed kit', seen['errors']['404']['before'])
        self.assertIn('No access', seen['errors']['403']['before'])
        self.assertIn('Sign in again', seen['errors']['401']['before'])

    def test_the_page_is_reached_from_the_projects_navigation_by_every_member(self):
        app = (KIT / 'web' / 'js' / 'app.js').read_text(encoding='utf-8')
        routes = (KIT / 'web' / 'js' / 'routes.js').read_text(encoding='utf-8')
        self.assertIn("  ['how', '/p/{pid}/how'],\n", routes)
        self.assertIn('how: how.page,', app)
        line = "            link('/p/' + pid + '/how', 'How work is found'),\n"
        self.assertIn(line, app)
        # Not behind a role: the line before it is the owners' one, and this one has no condition.
        self.assertNotIn('?', line)


if __name__ == '__main__':
    unittest.main()
