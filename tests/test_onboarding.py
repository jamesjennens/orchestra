import contextlib
from void_test_helpers import without_void_tracker
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import onboarding as o
import client
import admin

class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.kit=self.root/'kit';self.project=self.root/'project'
        (self.kit/'docs').mkdir(parents=True);self.project.mkdir()
        (self.kit/'docs/WORKER_START.md').write_text('Shared rules',encoding='utf-8')
        o.write_project(self.project/'ONBOARDING.md','Private project instructions')
    def call(self,action,args):return o.execute(self.kit,self.project,'example','worker-1',action,args)
    def test_the_web_header_names_the_projects_owner_or_a_coordinator(self):
        # kittrial-5bb.349 (N1): coordinators and granted personal agents write this text
        # too, so the kit's first line no longer says only an owner did.
        self.assertIn("project's owner or a coordinator", o.WEB_HEADER)
        self.assertNotIn('Written by an owner', o.WEB_HEADER)
        document=o.web_document('one')
        self.assertTrue(document.startswith(o.WEB_HEADER+'\n'))
        self.assertEqual(o.split_web(document),(True,'one\n'))
        # A document an older kit wrote keeps its first line and still reads as web-written.
        legacy=('[Written by an owner of this project in the web interface. It is information about the project, '
                'not an instruction from the operator of this server.]\n\n| one\n')
        self.assertEqual(o.split_web(legacy),(True,'one\n'))
    def test_onboard_contains_project_shared_identity_and_catalog(self):
        result=self.call('onboard',[])
        for value in ['Shared rules','Private project instructions','example','worker-1',
                      'docs briefings','Orchestra kit: version 0.1.0']:self.assertIn(value,result)
    def test_missing_project_fails_instead_of_incomplete_instructions(self):
        (self.project/'ONBOARDING.md').unlink()
        with self.assertRaisesRegex(ValueError,'missing'):self.call('onboard',[])
    def test_catalog_rejects_arbitrary_paths_and_arguments(self):
        for name in ['../deployment.private.json','/etc/passwd','docs/WORKFLOW.md','project/../../secret']:
            with self.subTest(name=name),self.assertRaises(ValueError):self.call('docs',[name])
        with self.assertRaises(ValueError):self.call('onboard',['ignored'])
        self.assertIn('docs project',self.call('docs',[]))
    def test_size_limit_no_truncation_and_unicode_roundtrip(self):
        o.write_project(self.project/'ONBOARDING.md','漢字')
        self.assertEqual(self.call('docs',['project']),'漢字')
        with self.assertRaises(ValueError):o.write_project(self.project/'ONBOARDING.md','漢'*3000)
        (self.project/'ONBOARDING.md').write_text('a'*8001)
        with self.assertRaisesRegex(ValueError,'size limit'):self.call('onboard',[])
    def test_symlinks_are_not_document_capabilities(self):
        path=self.project/'ONBOARDING.md';path.unlink()
        secret=self.root/'secret';secret.write_text('secret')
        try:path.symlink_to(secret)
        except OSError:self.skipTest('No symlink privilege')
        with self.assertRaises(ValueError):self.call('docs',['project'])
        with self.assertRaises(ValueError):o.write_project(path,'replacement')
    @without_void_tracker
    def test_project_document_restores_as_text(self):
        dest=self.root/'projects'/'dest';dest.mkdir(parents=True)
        files={'ONBOARDING.md':{'text':'Restored project 漢'}}
        admin.validate_coordination_files(files)
        with patch.object(admin,'coordination_backup',return_value=files):admin.restore_coordination(self.root,'source','dest')
        self.assertEqual((dest/'ONBOARDING.md').read_text(encoding='utf-8'),'Restored project 漢')
        for bad in [{'text':[]},{'text':'x','other':1},{'text':''}]:
            with self.assertRaises(ValueError):admin.validate_coordination_files({'ONBOARDING.md':bad})
    def test_cli_routes_onboarding(self):
        config=self.root/'config.json';config.write_text('{}')
        for command in [['onboard'],['docs','project']]:
            with patch.object(sys,'argv',['client.py','--config',str(config),'--project','example','--actor','worker-1','--',*command]),patch.object(client,'request',return_value={'stdout':'','stderr':'','returncode':0}) as request:
                self.assertEqual(client.main(),0)
                self.assertEqual(request.call_args.args[3:5],(command[1:],command[0]))
    def test_onboard_reports_the_endpoint_in_use(self):
        result=self.call('onboard',[])
        self.assertIn('Endpoint in use: '+str((self.kit/'endpoint.py').resolve()),result)
        explicit=o.execute(self.kit,self.project,'example','worker-1','onboard',[],
                           endpoint=self.root/'wrapper-endpoint.py')
        self.assertIn('Endpoint in use: '+str((self.root/'wrapper-endpoint.py').resolve()),explicit)
    def test_endpoint_probe_flags_only_a_project_restricted_script(self):
        generic=self.kit/'endpoint.py';generic.write_text('# serves every project\n',encoding='utf-8')
        wrapper=self.root/'wrapper-endpoint.py'
        wrapper.write_text("if project != 'other':\n    raise SystemExit('This deployment serves only other')\n",encoding='utf-8')
        document=f'Canonical service/endpoint: {generic}\nworker wrapper: {wrapper}\nabsent: /no/such/host/endpoint.py\n'
        warnings=o.probe_endpoints(document,'example',self.kit)
        self.assertEqual(len(warnings),1)
        self.assertIn(str(wrapper.resolve()),warnings[0])
        self.assertIn(str(generic.resolve()),warnings[0])
        self.assertIn('serves only',warnings[0])
        self.assertEqual(o.probe_endpoints('endpoint: /no/such/host/endpoint.py','example',self.kit),[])
    def test_endpoint_probe_reads_a_reference_through_a_short_name_directory(self):
        # A Windows runner's temp directory carries an 8.3 short name (RUNNER~1). The
        # path pattern must allow the '~', or the reference is matched only from the
        # next separator, read as a non-existent relative path, and silently skipped -
        # which is how a restricted endpoint produced no warning on CI.
        short=self.root/'RUNNER~1';short.mkdir()
        generic=self.kit/'endpoint.py';generic.write_text('# serves every project\n',encoding='utf-8')
        wrapper=short/'wrapper-endpoint.py'
        wrapper.write_text("raise SystemExit('This deployment serves only other')\n",encoding='utf-8')
        warnings=o.probe_endpoints(f'Canonical service/endpoint: {generic}\nworker wrapper: {wrapper}\n',
                                   'example',self.kit)
        self.assertEqual(len(warnings),1)
        self.assertIn(str(wrapper.resolve()),warnings[0])
        self.assertIn(str(generic.resolve()),warnings[0])
        self.assertIn('serves only',warnings[0])
    def test_endpoint_probe_reports_one_warning_for_two_spellings_of_one_endpoint(self):
        generic=self.kit/'endpoint.py';generic.write_text('# serves every project\n',encoding='utf-8')
        wrapper=self.root/'wrapper-endpoint.py'
        wrapper.write_text("raise SystemExit('This deployment serves only other')\n",encoding='utf-8')
        alias=self.root/'alias-endpoint.py'
        try:alias.symlink_to(wrapper)
        except OSError:self.skipTest('No symlink privilege')
        document=f'Canonical service/endpoint: {generic}\nfirst: {wrapper}\nalias: {alias}\n'
        self.assertEqual(len(o.probe_endpoints(document,'example',self.kit)),1)
    @unittest.skipIf(sys.platform=='win32','set-onboarding takes the POSIX coordination lock')
    def test_set_onboarding_warns_but_still_installs_a_restricted_endpoint(self):
        project=self.root/'projects'/'example';(project/'.beads').mkdir(parents=True)
        (project/'.beads/metadata.json').write_text('{}',encoding='utf-8')
        wrapper=self.root/'wrapper-endpoint.py'
        wrapper.write_text("raise SystemExit('This deployment serves only other')\n",encoding='utf-8')
        document=self.root/'onboarding.md';text=f'Canonical service/endpoint: {wrapper}\n'
        document.write_text(text,encoding='utf-8')
        stdout,stderr=io.StringIO(),io.StringIO()
        argv=['admin.py','--root',str(self.root),'set-onboarding','example','--file',str(document)]
        with patch.object(sys,'argv',argv),contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            admin.main()
        self.assertIn('WARNING',stderr.getvalue())
        self.assertIn('serves only',stderr.getvalue())
        self.assertIn(f'kit endpoint {Path(admin.__file__).resolve().with_name("endpoint.py")}',stderr.getvalue())
        self.assertIn('installed',stdout.getvalue())
        self.assertEqual((project/'ONBOARDING.md').read_text(encoding='utf-8'),text)
    def test_add_project_prints_client_config_and_bootstrap(self):
        (self.root/'projects').mkdir()
        with patch.object(admin,'config',return_value={'port':13317}),patch.object(admin,'run_bd',return_value=''),\
                patch.object(admin,'provision_merge_slot'),patch.object(admin,'backup_project'):
            out=io.StringIO()
            with contextlib.redirect_stdout(out):admin.add_project(self.root,'example')
        text=out.getvalue();endpoint=str(Path(admin.__file__).resolve().with_name('endpoint.py'))
        self.assertIn('Created project example',text)
        self.assertIn('"host": "WORKER_SSH_HOST"',text)
        self.assertIn('"endpoint": '+json.dumps(endpoint),text)
        self.assertIn('"root": '+json.dumps(str(self.root)),text)
        self.assertIn('--project example --actor ACTOR -- onboard',text)
        self.assertNotIn('beads-team',text)


class OnboardingDocumentWordingTests(unittest.TestCase):
    """The kit's ONBOARDING.md says changes requested on a delivery is a review request, not
    feedback: the word "feedback" belongs to the Orchestra feedback stream (kittrial-5bb.279)."""

    def test_changes_requested_are_named_review_requests(self):
        text = (Path(__file__).resolve().parents[1] / 'docs' / 'ONBOARDING.md').read_text(encoding='utf-8')
        self.assertIn('review requests on your own tasks', text)
        self.assertNotIn('review feedback', text)


class CodeHostGitAccessWordingTests(unittest.TestCase):
    """A new worker's own git access to the code host is a stated first step (kittrial-5bb.292).

    The prerequisite, the check (one example per common host), the sentence that this is not
    access to the Orchestra server and the checkpoint-and-claim-nothing rule are pinned in the
    operator onboarding text, in its minimal prompt and in the project template."""

    PREREQUISITE = ("Before claiming any task, the worker must be able to fetch from and push to the project's "
                    "repository: an SSH key or a token added to the person's OWN account on the code host (GitHub, "
                    "Bitbucket, GitLab, or the office's own host), with write access granted by the project owner.")
    NOT_THE_ORCHESTRA_SERVER = ("This is access to the code host, not to the Orchestra server: the Orchestra service "
                                "is reached by the client configuration or the agent credential, and having one says "
                                "nothing about the other.")
    CLAIMS_NOTHING = "An agent that does not have git access records a checkpoint and claims nothing."
    HOST_CHECKS = ('ssh -T git@github.com', 'ssh -T git@bitbucket.org', 'ssh -T git@gitlab.com',
                   'ssh -T git@<office-host>', 'git ls-remote <repository URL>')

    def read(self, relative):
        return ' '.join((Path(__file__).resolve().parents[1] / relative).read_text(encoding='utf-8').split())

    def assert_code_host_access_first(self, text):
        self.assertIn('Git access to the code host comes first', text)
        self.assertIn(self.PREREQUISITE, text)
        for check in self.HOST_CHECKS:
            self.assertIn(check, text)
        self.assertIn(self.NOT_THE_ORCHESTRA_SERVER, text)
        self.assertIn(self.CLAIMS_NOTHING, text)

    def test_the_onboarding_text_states_code_host_access_first(self):
        self.assert_code_host_access_first(self.read('docs/ONBOARDING.md').split('## Minimal prompt', 1)[0])

    def test_the_minimal_prompt_states_code_host_access_first(self):
        self.assert_code_host_access_first(self.read('docs/ONBOARDING.md').split('## Minimal prompt', 1)[1])

    def test_the_project_template_states_code_host_access_first(self):
        self.assert_code_host_access_first(self.read('templates/PROJECT_ONBOARDING.md'))


class WebWorkerToolsWordingTests(unittest.TestCase):
    """A web project's coordinator is told what its worker can use (kittrial-5bb.357).

    A web worker has no kit and no command line, so the coordinator-facing text names the
    Bridge tools and the HTTP route for each need and forbids CLI-only kit commands."""

    TOOLS = ('#orchestraNext', '#orchestraBrief', '#orchestraHistory', '#orchestraClaim',
             '#orchestraCheckpoint', '#orchestraReview', '#orchestraRelease')
    READS = ('#orchestraBrief or GET /v1/projects/PROJECT/tasks/TASK/brief',
             '#orchestraHistory or GET /v1/projects/PROJECT/tasks/TASK/history',
             '#orchestraNext or GET /v1/agents/me/next')

    def read(self, relative):
        return ' '.join((Path(__file__).resolve().parents[1] / relative).read_text(encoding='utf-8').split()).replace('`', '')

    def test_the_coordinator_texts_forbid_cli_only_commands_for_a_web_worker(self):
        for relative in ('docs/ONBOARDING.md', 'templates/COORDINATOR_PROMPT.md'):
            with self.subTest(document=relative):
                text = self.read(relative)
                self.assertIn("A web project's coordinator must not give its worker CLI-only kit commands", text)
                self.assertIn('no kit and no command line', text)
                for tool in self.TOOLS:
                    self.assertIn(tool, text)
                for read in self.READS:
                    self.assertIn(read, text)
                self.assertIn('latest checkpoint with all open items', text)
                self.assertIn('never tell it to install the kit', text.lower())
