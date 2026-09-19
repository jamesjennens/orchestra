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
