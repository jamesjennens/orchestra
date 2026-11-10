"""The assign call in the coordinator's AGENT.md (kittrial-5bb.550, Part 1).

The coordinator's AGENT.md (web/js/agentSetup.js, ``assignSection``) describes the one call that hands a task to a
worker: the roster read first, the exact curl on both systems with the body piped in, one example body with the
route's five fields, the Idempotency-Key rule, and the commonest refusals in the server's own words. Every
expectation here is a fact written in this module or an answer of the real route (the web service in front of the
real endpoint and a real bd, as tests/test_claim_held.RealStackTests runs it; the in-process backend has no
``tasks.assign``). The text is pinned as a literal for one sample server and file: the Bridge's own copy of the
AGENT.md, which is not in this repository, follows that pin.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import field_limits
import http_service
import task_assignment
import test_bd_label_aliases as rb
import test_claim_held as held_stack
from test_coordinator_agent_md import BUNDLE, node_json

SERVER = 'https://office.example:7443'
#: The sample credential file of the pin: what agentSetup.secretFile gives an agent named kestrel.
FILE = "{ powershell: '$env:USERPROFILE\\\\.orchestra-agent-kestrel.curlrc', posix: '~/.orchestra-agent-kestrel.curlrc' }"
HEADING = '## Assigning a task'
#: The five body fields the route takes (http_service.ApiHandler.task_assign), and nothing else.
FIELDS = ('expected_assignee', 'assignee', 'reason', 'allow_pending_review', 'allow_held_slot')
#: A worker credential's actor, chosen by the owner who issues it: the one actor id a test can make real.
WORKER_ACTOR = 'worker-kestrel'
#: The example body the AGENT.md shows, as this module expects it.
EXAMPLE = {'expected_assignee': None, 'assignee': WORKER_ACTOR, 'reason': 'Next slice: the login form',
           'allow_pending_review': False, 'allow_held_slot': False}
#: A task id of the shape the server mints (actor_names.ROW_ID), for the validation the route runs.
TASK = 'pp-m7u'
#: The refusal lines: the status and the server's words the AGENT.md quotes, and what produces each on the real
#: route. A sentence that carries an id or a number (the unknown actor's name, the claim count) is quoted by its
#: fixed part only.
REFUSALS = (
    ('field-set', 422, 'Assignment requires its exact closed field set'),
    ('unknown-actor', 409, 'so the name cannot be stored: name the actor id to use'),
    ('stale-holder', 409, 'The task assignee changed; refresh the task before assigning it.'),
    ('claim-limit', 409, 'Deliver or release one before claiming another.'),
    ('not-a-coordinator', 403, 'Credential scope does not permit this operation'),
)
#: An actor id of the server's shape that no project has.
NOBODY = 'agent_0123456789abcdef'

#: The whole generated section for SERVER and FILE, word for word. The Bridge's copy follows this text.
PINNED = '''## Assigning a task

Read the roster first: GET https://office.example:7443/v1/projects/PROJECT/assignment-roster lists the actor ids you may assign to (each item's actor, with its name and kind). expected_assignee is the task's current assignee (task.assignee in GET https://office.example:7443/v1/projects/PROJECT/tasks/TASK/brief), or null when nobody holds it. Never use a display name: assignee and expected_assignee take an actor id exactly as the roster lists it.
- Windows (PowerShell): '{"expected_assignee":null,"assignee":"worker-kestrel","reason":"Next slice: the login form","allow_pending_review":false,"allow_held_slot":false}' | curl.exe -fsS --noproxy office.example --max-time 60 -K "$env:USERPROFILE\\.orchestra-agent-kestrel.curlrc" -X POST -H "Content-Type: application/json" -H "Idempotency-Key: KEY" --data-binary '@-' 'https://office.example:7443/v1/projects/PROJECT/tasks/TASK/assign'
- macOS/Linux: printf '%s' '{"expected_assignee":null,"assignee":"worker-kestrel","reason":"Next slice: the login form","allow_pending_review":false,"allow_held_slot":false}' | curl -fsS --noproxy office.example --max-time 60 -K ~/.orchestra-agent-kestrel.curlrc -X POST -H 'Content-Type: application/json' -H 'Idempotency-Key: KEY' --data-binary @- 'https://office.example:7443/v1/projects/PROJECT/tasks/TASK/assign'
The body has exactly these five fields: expected_assignee (the current holder's actor id, or null), assignee (the actor id from the roster that gets the task), reason (1 to 1000 characters of plain text, kept in the task), allow_pending_review (true only to take a task away from a holder whose delivery awaits review or has changes requested; the review items are kept) and allow_held_slot (true only to take a task away from a holder who holds the merge slot for it; the slot stays with its holder). KEY is your own Idempotency-Key for this one assignment, 8 to 128 URL-safe characters. Reuse the same Idempotency-Key and body after an uncertain answer (503 or 124); never mint a new key for the same intent.
If the server refuses:
- 422 "Assignment requires its exact closed field set": send the five fields above, all of them and nothing else.
- 409 "so the name cannot be stored: name the actor id to use": the assignee is not an actor id of this project; read the roster again and send an actor it lists.
- 409 "The task assignee changed; refresh the task before assigning it.": expected_assignee is stale; read the brief again and send its current assignee, or null.
- 409 "Deliver or release one before claiming another.": assigning a task to yourself is a claim, and you are at your claim limit; hand the task to a worker instead.
- 403 "Credential scope does not permit this operation": you hold no effective coordinator grant for PROJECT; stop and tell your owner.
'''

REFUSAL_LINE = re.compile(r'^- (\d{3}) "([^"]+)": .+$')
#: Part 2: the long-text paragraph of "## Every call", word for word, for the Bridge's copy. Its numbers are checked
#: against field_limits.DESCRIPTION_MAX and http_service.MAX_ATTACHMENT_BYTES, never written here as expectations.
LONG_TEXT_PINNED = 'Long text, such as a task description, a comment or a report, goes in a file in this folder, never inside one long command line: write the JSON body to the file and send it with --data-binary @FILE in place of the body, or upload it as a task file (the Task files section). The server takes a task description of up to 20000 characters and a comment of up to 65536 UTF-8 bytes (64 KiB), so never trim text to fit a command line. If a stored description reads shorter than you expected, read it again with GET .../tasks/TASK/brief (task.description) or the task-file route before concluding that it was cut.'
DESCRIPTION_LIMIT = re.compile(r'a task description of up to (\d+) characters')
COMMENT_LIMIT = re.compile(r'a comment of up to (\d+) UTF-8 bytes')

WINDOWS = re.compile(r"^- Windows \(PowerShell\): '(\{.*\})' \| curl\.exe .* -X POST .* --data-binary '@-' '(\S+)'$")
POSIX = re.compile(r"^- macOS/Linux: printf '%s' '(\{.*\})' \| curl .* -X POST .* --data-binary @- '(\S+)'$")


def guides(test, coordinates):
    """The AGENT.md for an agent named kestrel on SERVER, its steps, and the section for SERVER and FILE."""
    return node_json(test, (
        "const p = m.secretlessPayload(%s, %r);"
        "console.log(JSON.stringify({guide: m.agentGuide(p), coordinating: m.coordinating(p),"
        " steps: m.coordinatorSteps(p.server, p.secretFile, p.certificateFile),"
        " section: m.assignSection(%r, %s).join('\\n'), example: m.ASSIGN_EXAMPLE_BODY}))")
        % (BUNDLE % coordinates, SERVER, SERVER, FILE))


def section_of(guide):
    """The lines of the "## Assigning a task" section of a generated AGENT.md, up to the next heading."""
    lines = guide.split('\n')
    start = lines.index(HEADING)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith('## '))
    return lines[start:end]


def refusal_lines_of(section):
    """The (status, quoted words) of each refusal line of the section, in the section's order."""
    found = [REFUSAL_LINE.match(line) for line in section if REFUSAL_LINE.match(line)]
    return [(int(match.group(1)), match.group(2)) for match in found]


def bodies_of(section):
    """The JSON body each system line pipes to curl, as (windows body, posix body, windows url, posix url)."""
    windows = [WINDOWS.match(line) for line in section if line.startswith('- Windows (PowerShell): ')]
    posix = [POSIX.match(line) for line in section if line.startswith('- macOS/Linux: ')]
    if len(windows) != 1 or windows[0] is None or len(posix) != 1 or posix[0] is None:
        raise AssertionError('one Windows and one macOS/Linux line, each piping a JSON body: %r' % (section,))
    return (json.loads(windows[0].group(1)), json.loads(posix[0].group(1)),
            windows[0].group(2), posix[0].group(2))


class AgentMdTests(unittest.TestCase):
    """The generated text, under node."""

    @classmethod
    def setUpClass(cls):
        cls.got = None

    def coordinator(self):
        if self.got is None:
            type(self).got = guides(self, "[{ project: 'alpha', effective: true }]")
        return self.got

    def test_a_the_example_body_passes_the_validation_the_route_runs(self):
        section = section_of(self.coordinator()['guide'])
        windows, posix, _, _ = bodies_of(section)
        self.assertEqual(EXAMPLE, windows)
        self.assertEqual(EXAMPLE, posix)
        for body in (windows, posix):
            self.assertEqual(set(FIELDS), set(body))
            # Exactly as http_service.ApiHandler.task_assign calls it; a ValueError would fail the test.
            task_assignment.validate_request(dict(body, schema_version=1, operation_id='http-validation', task=TASK))
            self.assertRegex(body['assignee'], task_assignment.ACTOR)
            self.assertIsNone(body['expected_assignee'])
        # The exported constant is that same body.
        self.assertEqual(EXAMPLE, json.loads(self.coordinator()['example']))

    def test_c_both_system_lines_pipe_the_body_with_the_key_header_and_carry_no_secret(self):
        section = section_of(self.coordinator()['guide'])
        windows, posix, windows_url, posix_url = bodies_of(section)
        self.assertEqual(SERVER + '/v1/projects/PROJECT/tasks/TASK/assign', windows_url)
        self.assertEqual(SERVER + '/v1/projects/PROJECT/tasks/TASK/assign', posix_url)
        lines = {line.split(':', 1)[0]: line for line in section if line.startswith('- ')}
        win, nix = lines['- Windows (PowerShell)'], lines['- macOS/Linux']
        self.assertIn("' | curl.exe -fsS --noproxy office.example --max-time 60 -K \"$env:USERPROFILE\\.orchestra-agent-kestrel.curlrc\" -X POST", win)
        self.assertIn('-H "Content-Type: application/json" -H "Idempotency-Key: KEY" --data-binary \'@-\' ', win)
        self.assertIn("' | curl -fsS --noproxy office.example --max-time 60 -K ~/.orchestra-agent-kestrel.curlrc -X POST", nix)
        self.assertIn("-H 'Content-Type: application/json' -H 'Idempotency-Key: KEY' --data-binary @- ", nix)
        for line in (win, nix):
            self.assertNotIn('Authorization', line)
            self.assertNotIn('Bearer', line)
            self.assertNotIn('<your secret>', line)
            self.assertNotIn('--data-binary \'{', line, 'the body is piped, never a command-line argument')
            self.assertNotIn('-d ', line)

    def test_d_the_roster_read_comes_before_the_assign_call(self):
        guide = self.coordinator()['guide']
        section = '\n'.join(section_of(guide))
        roster = section.index(SERVER + '/v1/projects/PROJECT/assignment-roster')
        call = section.index(SERVER + '/v1/projects/PROJECT/tasks/TASK/assign')
        self.assertLess(roster, call)
        self.assertTrue(section_of(guide)[2].startswith('Read the roster first: GET ' + SERVER + '/v1/projects/PROJECT/assignment-roster'))
        self.assertIn('Never use a display name', section)
        self.assertIn('task.assignee in GET ' + SERVER + '/v1/projects/PROJECT/tasks/TASK/brief), or null when nobody holds it', section)
        # The section sits right after the first check and before the steps.
        self.assertLess(guide.index('## Your first check'), guide.index(HEADING))
        self.assertLess(guide.index(HEADING), guide.index('## Steps'))
        self.assertEqual(1, guide.count(HEADING + '\n'))

    def test_e_each_refusal_line_is_in_the_text(self):
        section = section_of(self.coordinator()['guide'])
        for _, status, words in REFUSALS:
            self.assertEqual(1, sum(line.startswith('- %d "%s": ' % (status, words)) for line in section), words)
        self.assertIn('Reuse the same Idempotency-Key and body after an uncertain answer (503 or 124); never mint a new key for the same intent.', section[5])

    def test_f_a_worker_does_not_get_the_section(self):
        worker = guides(self, '[]')
        self.assertFalse(worker['coordinating'])
        self.assertNotIn(HEADING, worker['guide'])
        self.assertNotIn('assignment-roster', worker['guide'])
        self.assertNotIn('Idempotency-Key: KEY', worker['guide'])
        self.assertIn(HEADING, self.coordinator()['guide'])

    def test_g_the_pinned_text(self):
        got = self.coordinator()
        self.assertEqual(PINNED, got['section'])
        self.assertIn(PINNED, got['guide'], 'the AGENT.md carries the pinned section whole')

    def test_steps_4_and_5_point_to_the_section(self):
        steps = self.coordinator()['steps']
        self.assertTrue(steps[4].startswith('5. Create a task with POST '))
        self.assertTrue(steps[4].endswith('Assign afterward with POST ' + SERVER + '/v1/projects/PROJECT/tasks/TASK/assign (see ## Assigning a task).'))
        self.assertTrue(steps[5].startswith('6. Your writes, each with curl -K as above and an Idempotency-Key: '))
        self.assertIn('POST .../tasks/TASK/assign to hand a task to a worker (see ## Assigning a task); ', steps[5])


class LongTextTests(unittest.TestCase):
    """Part 2: long text goes in a file, in every agent's AGENT.md."""

    @classmethod
    def setUpClass(cls):
        cls.both = None

    def guides(self):
        if self.both is None:
            type(self).both = {role: node_json(self, (
                "const p = m.secretlessPayload(%s, %r);"
                "console.log(JSON.stringify({guide: m.agentGuide(p), rule: m.LONG_TEXT_RULE}))")
                % (BUNDLE % coordinates, SERVER))
                for role, coordinates in (('worker', '[]'), ('coordinator', "[{ project: 'alpha', effective: true }]"))}
        return self.both

    def paragraph(self, role):
        guide = self.guides()[role]['guide']
        lines = guide.split('\n')
        start = lines.index('## Every call')
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith('## '))
        found = [line for line in lines[start:end] if line.startswith('Long text, ')]
        self.assertEqual(1, len(found), 'one long-text paragraph in "## Every call" of the %s AGENT.md' % role)
        return found[0]

    def test_the_paragraph_is_in_both_the_workers_and_the_coordinators_agent_md_once(self):
        for role in ('worker', 'coordinator'):
            paragraph = self.paragraph(role)
            self.assertEqual(1, self.guides()[role]['guide'].count(paragraph), role)
            self.assertIn('never inside one long command line', paragraph)
            self.assertIn('--data-binary @FILE', paragraph)
            self.assertIn('upload it as a task file', paragraph)
        self.assertIn('## You coordinate', self.guides()['coordinator']['guide'])
        self.assertNotIn('## You coordinate', self.guides()['worker']['guide'])

    def test_the_numbers_are_the_servers_own(self):
        paragraph = self.paragraph('worker')
        self.assertEqual(field_limits.DESCRIPTION_MAX, int(DESCRIPTION_LIMIT.search(paragraph).group(1)))
        self.assertEqual(http_service.MAX_ATTACHMENT_BYTES, int(COMMENT_LIMIT.search(paragraph).group(1)))
        self.assertIn('so never trim text to fit a command line', paragraph)

    def test_the_reread_sentence_names_the_brief_route(self):
        paragraph = self.paragraph('coordinator')
        sentence = [s for s in paragraph.split('. ') if s.startswith('If a stored description reads shorter than you expected')]
        self.assertEqual(1, len(sentence), paragraph)
        self.assertIn('read it again with GET .../tasks/TASK/brief (task.description)', sentence[0])
        self.assertIn('before concluding that it was cut', sentence[0])

    def test_the_pinned_paragraph(self):
        for role in ('worker', 'coordinator'):
            self.assertEqual(LONG_TEXT_PINNED, self.paragraph(role), role)
            self.assertEqual(LONG_TEXT_PINNED, self.guides()[role]['rule'])


def only_own_tests(cls):
    """The fixtures are TestCases with tests of their own; this class runs only its own."""
    for base in (rb.RealBdLabelAliasTests, held_stack.RealStackTests):
        for name in dir(base):
            if name.startswith('test_') and name not in cls.__dict__:
                setattr(cls, name, None)
    return cls


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@only_own_tests
class RealRouteTests(held_stack.RealStackTests):
    """The example body and the refusal lines against the real route: the web service, the real endpoint and a
    real bd. alex owns pp and casey contributes (the fixture); Coord is alex's agent with the coordinator grant."""

    KEYS = iter(range(1, 10 ** 6))

    def setUp(self):
        super().setUp()
        self.agent_id, self.secret = self.new_agent('Coord')
        granted = self.request('PUT', '/v1/projects/pp/agents/%s/coordinator' % self.agent_id, {}, token=self.tokens['alex'])
        self.assertIn(granted.status, (200, 201), granted.data)
        self.task = self.new('the login form')

    def worker(self):
        """The example's assignee made real: a worker credential of pp whose actor is WORKER_ACTOR. Issued by the one
        test that assigns to it, because a name the tracker already holds cannot be issued again."""
        issued = self.request('POST', '/v1/projects/pp/worker-credentials', {'label': 'kestrel worker', 'actor': WORKER_ACTOR},
                              token=self.tokens['alex'])
        self.assertEqual(201, issued.status, issued.data)
        self.assertEqual(WORKER_ACTOR, issued.data['credential']['actor'])

    def new_agent(self, name, token=None):
        made = self.request('POST', '/v1/agents', {'name': name, 'working_directory': '/home/x/' + name.lower(), 'projects': ['pp']},
                            token=token or self.tokens['alex'])
        self.assertEqual(201, made.status, made.data)
        return made.data['agent']['id'], made.data['credential']['secret']

    def assign(self, body, token=None, task=None, key=None):
        return self.request('POST', '%s/%s/assign' % (self.tasks, task or self.task), body, token=token or self.secret,
                            key=key or 'assign-550-key-%06d' % next(self.KEYS))

    def brief_assignee(self, task=None):
        brief = self.request('GET', '%s/%s/brief' % (self.tasks, task or self.task), token=self.secret)
        self.assertEqual(200, brief.status, brief.data)
        return brief.data['task'].get('assignee')

    def section(self):
        return section_of(guides(self, "[{ project: 'alpha', effective: true }]")['guide'])

    def body(self):
        """The example body as the generated AGENT.md carries it, from both system lines."""
        windows, posix, _, _ = bodies_of(self.section())
        self.assertEqual(windows, posix)
        return windows

    def test_b_the_example_body_assigns_the_task_through_the_real_route(self):
        body = self.body()
        self.worker()
        roster = self.request('GET', '/v1/projects/pp/assignment-roster', token=self.secret)
        self.assertEqual(200, roster.status, roster.data)
        self.assertIn(body['assignee'], [item['actor'] for item in roster.data['items']], 'the example assigns to a roster actor')
        self.assertIsNone(self.brief_assignee(), 'nobody holds the task, as the example expects')
        answer = self.assign(body, key='assign-550-example-00000001')
        self.assertEqual(200, answer.status, answer.data)
        self.assertEqual((True, None, WORKER_ACTOR), (answer.data['changed'], answer.data['previous_assignee'], answer.data['assignee']))
        self.assertEqual(WORKER_ACTOR, self.brief_assignee())
        self.assertEqual(('open', WORKER_ACTOR), self.held(self.task))
        # The same key and body again: the same answer, nothing new written.
        again = self.assign(body, key='assign-550-example-00000001')
        self.assertEqual(200, again.status, again.data)
        self.assertEqual(answer.data['notice']['sha256'], again.data['notice']['sha256'])

    def test_h_a_long_description_sent_from_a_file_reads_back_whole_through_the_brief(self):
        """Part 2's advice, done: a 5000-character description in a JSON body file, sent with curl -K CONF
        --data-binary @FILE as the granted agent, then read back through GET .../brief."""
        curl = shutil.which('curl')
        if not curl:
            self.skipTest('curl is not installed')
        folder = Path(self.tmp) / 'long-text'
        folder.mkdir()
        text = ''.join('Line %04d of the login form description: fields, validation and the error states.\n' % n
                       for n in range(1, 200))[:5000]
        self.assertEqual(5000, len(text))
        self.assertLessEqual(len(text), field_limits.DESCRIPTION_MAX)
        body = folder / 'create.json'
        body.write_text(json.dumps({'title': 'the login form, described', 'description': text}), encoding='utf-8')
        # The credential file of the paragraph's -K: the test agent's own secret, in the test's folder only.
        conf = folder / 'agent.curlrc'
        conf.write_text('header = "Authorization: Bearer %s"\n' % self.secret, encoding='utf-8')
        url = 'http://127.0.0.1:%d/v1/projects/pp/tasks' % self.port
        made = subprocess.run([curl, '-fsS', '--noproxy', '127.0.0.1', '--max-time', '60', '-K', str(conf), '-X', 'POST',
                               '-H', 'Content-Type: application/json', '-H', 'Idempotency-Key: long-text-550-00000001',
                               '--data-binary', '@' + str(body), url],
                              capture_output=True, text=True, timeout=120, env=dict(os.environ, HOME=str(folder)))
        self.assertEqual(0, made.returncode, made.stderr)
        created = json.loads(made.stdout)
        task = created['id']
        brief = self.request('GET', '%s/%s/brief' % (self.tasks, task), token=self.secret)
        self.assertEqual(200, brief.status, brief.data)
        self.assertEqual(text, brief.data['task']['description'], 'the whole description, as sent')
        self.assertEqual(5000, len(brief.data['task']['description']))

    def test_e_each_refusal_line_quotes_the_real_answer(self):
        body = self.body()
        held = self.new('held by Coord')
        self.assertEqual(200, self.claim(None, held, token=self.secret).status, 'Coord holds one task, its limit')
        plain_id, plain = self.new_agent('Plain', token=self.tokens['casey'])
        # Where the refusal is about something other than the assignee, the assignee is an account the roster lists.
        roster = self.request('GET', '/v1/projects/pp/assignment-roster', token=self.secret)
        self.assertEqual(200, roster.status, roster.data)
        self.assertIn(self.ids['drew'], [item['actor'] for item in roster.data['items']])
        without_last = dict(body)
        del without_last[FIELDS[-1]]
        produced = {
            'field-set': lambda: self.assign(without_last),
            'unknown-actor': lambda: self.assign(dict(body, assignee=NOBODY)),
            'stale-holder': lambda: self.assign(dict(body, expected_assignee=self.ids['casey'], assignee=self.ids['drew'])),
            'claim-limit': lambda: self.assign(dict(body, assignee=self.agent_id)),
            'not-a-coordinator': lambda: self.assign(dict(body), token=plain),
        }
        self.assertEqual(set(produced), {label for label, _, _ in REFUSALS})
        # The lines as the generated AGENT.md carries them, in the order REFUSALS gives them.
        lines = refusal_lines_of(self.section())
        self.assertEqual(len(REFUSALS), len(lines), lines)
        for (label, status, words), (line_status, line_words) in zip(REFUSALS, lines):
            with self.subTest(refusal=label):
                answer = produced[label]()
                self.assertEqual(line_status, answer.status, answer.data)
                self.assertIn(line_words, answer.data['error']['message'], 'the words the AGENT.md quotes are in the real answer')
                self.assertIsNone(self.brief_assignee(), 'a refused assignment writes nothing')
                self.assertEqual((status, words), (line_status, line_words), 'the line is the one this module expects')
        # The one the key rule covers: no Idempotency-Key at all.
        no_key = self.request('POST', '%s/%s/assign' % (self.tasks, self.task), dict(body), token=self.secret)
        self.assertEqual(422, no_key.status, no_key.data)
        self.assertIn('Idempotency-Key', no_key.data['error']['message'])


if __name__ == '__main__':
    unittest.main()
