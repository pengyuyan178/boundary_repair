"""Actual local Git exports and archive/process edge cases; Docker transport is separately mocked."""
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from helpers import config, context, git_repo, task
from boundary_repair.adapters.workspace import GitArchiveWorkspaceAdapter, extract_sources, validate_commit
from boundary_repair.adapters.process import run_process
from boundary_repair.domain.errors import BudgetExceeded, ConfigurationError, ValidationError
from boundary_repair.kernel.files import tree_digest


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_git_export_is_base_only_filtered_and_cleaned(self):
        repo, commit = git_repo(self.root)
        manifest = self.root/'repos.json'
        manifest.write_text(json.dumps({'repositories':{'demo/ui':{'path':str(repo)}}}))
        conf = config(self.root, parser=False)
        conf = replace(conf, integration=replace(conf.integration, repository_manifest=manifest))
        original = (repo/'ui.js').read_text()
        (repo/'ui.js').write_text('WORKTREE CHANGES MUST NOT LEAK')
        with GitArchiveWorkspaceAdapter(conf).open_base(task(commit), context()) as snapshot:
            exported = snapshot.root
            self.assertEqual((exported/'ui.js').read_text(),original)
            self.assertFalse((exported/'.git').exists())
            self.assertFalse((exported/'.env').exists())
            self.assertFalse((exported/'tests').exists())
            self.assertEqual(snapshot.tree_sha256,tree_digest(exported))
        self.assertFalse(exported.exists())
        self.assertEqual((repo/'ui.js').read_text(),'WORKTREE CHANGES MUST NOT LEAK')

    def test_revision_option_injection_rejected(self):
        for value in ('--help', 'HEAD', 'a'*39, '../x'):
            with self.assertRaises(ValidationError):
                validate_commit(value)

    def test_tar_traversal_rejected(self):
        archive=self.root/'bad.tar'
        with tarfile.open(archive,'w') as stream:
            item=tarfile.TarInfo('../escape.js'); item.size=1
            stream.addfile(item,io.BytesIO(b'x'))
        target=self.root/'target';target.mkdir()
        with self.assertRaises(ValidationError):
            extract_sources(archive,target,100)
        self.assertFalse((self.root/'escape.js').exists())

    def test_tar_source_symlink_rejected(self):
        archive=self.root/'bad.tar'
        with tarfile.open(archive,'w') as stream:
            item=tarfile.TarInfo('escape.js');item.type=tarfile.SYMTYPE;item.linkname='/etc/passwd'
            stream.addfile(item)
        target=self.root/'target';target.mkdir()
        with self.assertRaises(ValidationError):
            extract_sources(archive,target,100)

    def test_tar_budget_checked(self):
        archive=self.root/'big.tar'
        with tarfile.open(archive,'w') as stream:
            item=tarfile.TarInfo('a.js');item.size=4;stream.addfile(item,io.BytesIO(b'true'))
        target=self.root/'target';target.mkdir()
        with self.assertRaises(ValidationError):
            extract_sources(archive,target,3)

    def test_subprocess_timeout_terminates(self):
        with self.assertRaises(BudgetExceeded):
            run_process([sys.executable,'-c','import time;time.sleep(30)'],timeout=0.1)

    def test_subprocess_output_file_not_loaded_into_result(self):
        path=self.root/'output.bin'
        result=run_process([sys.executable,'-c','import sys;sys.stdout.buffer.write(b"a"*100)'],timeout=5,output_file=path)
        self.assertEqual(result.stdout,b'')
        self.assertEqual(path.stat().st_size,100)

    def test_subprocess_no_shell_expansion(self):
        result=run_process([sys.executable,'-c','import sys;print(sys.argv[1])','$(echo compromised)'],timeout=5)
        self.assertEqual(result.stdout.strip(),b'$(echo compromised)')

if __name__=='__main__':
    unittest.main()
