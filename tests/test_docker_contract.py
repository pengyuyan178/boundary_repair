"""Docker command/archive/cleanup contract using a simulated Docker process boundary."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from helpers import config, context, git_repo, task
from boundary_repair.adapters.process import ProcessResult
from boundary_repair.adapters.workspace import DockerWorkspaceAdapter
from boundary_repair.domain.errors import ExternalServiceError


class DockerContractTests(unittest.TestCase):
    def test_base_export_command_and_cleanup(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);repo,commit=git_repo(root);conf=config(root,parser=False)
            images=root/'images.json';images.write_text(json.dumps({'instances':{'demo__ui-1':{
                'repo':'demo/ui','base_commit':commit,'image':'repo/demo@sha256:'+'a'*64,'repository_path':'/testbed'}}}))
            conf=replace(conf,image_manifest=images)
            calls=[]
            def fake(args,**kwargs):
                calls.append(args)
                if args[1]=='run':
                    subprocess.run(['git','-C',str(repo),'archive','--format=tar','--output='+str(kwargs['output_file']),commit],check=True)
                return ProcessResult(0,b'',b'')
            with patch('boundary_repair.adapters.workspace.run_process',side_effect=fake):
                with DockerWorkspaceAdapter(conf).open_base(task(commit),context()) as snap:
                    self.assertTrue((snap.root/'ui.js').is_file())
                    self.assertFalse((snap.root/'.git').exists())
            run=next(c for c in calls if c[1]=='run')
            self.assertIn('--network=none',run)
            self.assertIn('--cap-drop=ALL',run)
            self.assertIn('--read-only',run)
            self.assertNotIn('--mount',run)
            self.assertNotIn('-v',run)
            self.assertEqual(calls[-1][1:3],['rm','-f'])

    def test_cleanup_on_archive_failure(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);repo,commit=git_repo(root);conf=config(root,parser=False)
            images=root/'images.json';images.write_text(json.dumps({'instances':{'demo__ui-1':{
                'repo':'demo/ui','base_commit':commit,'image':'repo/demo@sha256:'+'a'*64}}}))
            conf=replace(conf,image_manifest=images);calls=[]
            def fake(args,**kwargs):
                calls.append(args)
                return ProcessResult(1 if args[1]=='run' else 0,b'',b'')
            with patch('boundary_repair.adapters.workspace.run_process',side_effect=fake):
                with self.assertRaises(ExternalServiceError):
                    with DockerWorkspaceAdapter(conf).open_base(task(commit),context()):
                        pass
            self.assertEqual(calls[-1][1:3],['rm','-f'])

if __name__=='__main__':
    unittest.main()
