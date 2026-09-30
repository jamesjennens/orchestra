import contextlib
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
