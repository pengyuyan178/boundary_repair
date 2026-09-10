"""Full pipeline integration with real Git/compiler/diff I/O and explicitly recorded model evidence."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from helpers import config, git_repo, task, write_fixture, parser_module
from boundary_repair.adapters.storage import write_json
from boundary_repair.bootstrap import build_pipeline, build_workspace
from boundary_repair.experiments.runner import run_generation
from boundary_repair.cli import main


@unittest.skipUnless(parser_module(),'install pinned TypeScript compiler')
class EndToEndTests(unittest.TestCase):
    def test_real_pipeline_produces_applied_behavioral_fix(self):
        with TemporaryDirectory(prefix='中文-boundary-test-') as temporary:
            root=Path(temporary);repo,commit=git_repo(root)
            dataset=root/'dataset/dev/SWE-bench_Multimodal.json';dataset.parent.mkdir(parents=True)
            t=task(commit)
            write_json(dataset,[{'instance_id':t.instance_id,'repo':t.repo,'base_commit':t.base_commit,
                                'problem_statement':t.problem_statement,'patch':'GOLD_MUST_NOT_BE_USED','test_patch':'PRIVATE_TEST'}])
            (root/'dataset/SOURCE.txt').write_text('SYNTHETIC-NOT-SWE-BENCH')
            repositories=root/'repositories.json'
            write_json(repositories,{'repositories':{'demo/ui':{'path':str(repo)}}})
            conf=config(root)
            conf=replace(conf,integration=replace(conf.integration,model_mode='fixture',fixture_file=write_fixture(root),
                                                  repository_manifest=repositories))
            report=run_generation(conf,(t,),'end-to-end',build_pipeline(conf),build_workspace(conf))
            self.assertEqual((report.generated,report.attempted,report.unattempted),(1,1,0))
            self.assertIsNone(report.resolved)
            prediction=json.loads((report.batch_path/'predictions.jsonl').read_text())
            self.assertNotIn('GOLD_MUST_NOT_BE_USED',prediction['model_patch'])
            patch=report.batch_path/'cases/demo__ui-1/patch/final.patch'
            # Evaluation here is an independent test assertion after generation, never an input to it.
            subprocess.run(['git','-C',str(repo),'apply','--check',str(patch)],check=True,capture_output=True)
            subprocess.run(['git','-C',str(repo),'apply',str(patch)],check=True,capture_output=True)
            code="const f=require(process.argv[1]).shouldShow;console.log(JSON.stringify([[true,false],[true,true],[false,false],[false,true]].map(v=>f(...v))))"
            outputs=json.loads(subprocess.check_output(['node','-e',code,str(repo/'ui.js')]))
            self.assertEqual(outputs,[True,False,False,False])
            trajectory=report.batch_path/'cases/demo__ui-1/trajectory'
            self.assertTrue(all((trajectory/name).is_file() for name in ('contracts.json','localization.json','synthesis.json','stages.jsonl')))
            self.assertTrue(json.loads((report.batch_path/'manifest.json').read_text())['not_benchmark_evidence'])

if __name__=='__main__':
    unittest.main()
