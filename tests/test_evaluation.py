"""Evaluator contract tests. Docker and official harness launches are simulated, clearly labeled."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from boundary_repair.experiments.evaluation import EvaluationRequest, OfficialDockerEvaluator, parse_case_reports, validate_predictions
from boundary_repair.adapters.process import ProcessResult
from boundary_repair.adapters.storage import file_sha256, write_json
from boundary_repair.domain.errors import ConfigurationError, ValidationError


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.batch=self.root/'batch';self.batch.mkdir()
        self.dataset=self.root/'dataset.json';self.dataset.write_text('[]')
        self.predictions=self.batch/'predictions.jsonl'
        self.predictions.write_text(json.dumps({'instance_id':'demo__ui-1','model_name_or_path':'demo/model','model_patch':'synthetic-diff'})+'\n')
        write_json(self.batch/'manifest.json',{'selected_instances':['demo__ui-1','demo__ui-2'],
                    'execution_mode':'http','dataset_sha256':file_sha256(self.dataset)})
        self.images=self.root/'images.json'
        write_json(self.images,{'instances':{'demo__ui-1':{'image':'repo/image@sha256:'+'a'*64,'harness_image':'repo/alias:latest'}},'evaluation':{}})
        self.request=EvaluationRequest(self.dataset,self.predictions,self.batch,'test-grade',Path(sys.executable),'version:5.0.0',self.images)

    def fake_process(self,args,**kwargs):
        if args[0]=='docker':
            return ProcessResult(0,b'sha256:local-image\n',b'')
        if '-c' in args:
            return ProcessResult(0,json.dumps({'version':'5.0.0','file':'/safe/swebench/__init__.py'}).encode(),b'')
        if '--help' in args:
            return ProcessResult(0,b'--dataset_name --predictions_path --run_id --max_workers --timeout',b'')
        root=kwargs['cwd'];report=root/'logs/evaluation/run/model/demo__ui-1/report.json'
        write_json(report,{'demo__ui-1':{'resolved':True}})
        (report.parent/'test_output.txt').write_text(
            '>>>>> Start Test Output\nsynthetic test passed\n'
            '>>>>> End Test Output\n>>>>> Test Exit Code: 0\n')
        kwargs['output_file'].write_text('synthetic harness stdout')
        return ProcessResult(0,b'',b'')

    def test_evaluate_uses_report_not_returncode(self):
        with patch('boundary_repair.experiments.evaluation.run_process',side_effect=self.fake_process):
            result=OfficialDockerEvaluator().evaluate(self.request)
        self.assertEqual((result.submitted,result.graded,result.resolved,result.selected),(1,1,1,2))
        self.assertTrue((self.batch/'cases/demo__ui-1/result_data/evaluation_test-grade.json').is_file())
        self.assertEqual(validate_predictions(self.predictions)[0]['model_patch'],'synthetic-diff')

    def test_fixture_batch_is_never_official_score(self):
        write_json(self.batch/'manifest.json',{'execution_mode':'fixture'})
        with self.assertRaisesRegex(ConfigurationError,'fixture'):
            OfficialDockerEvaluator().evaluate(self.request)

    def test_mapping_is_converted_losslessly_only_for_harness(self):
        raw = {'demo__ui-1': {'instance_id': 'demo__ui-1', 'patch': 'synthetic-gold',
                            'test_patch': 'synthetic-test', 'FAIL_TO_PASS': ['demo-test'],
                            'image_assets': {'problem_statement': ['https://example.invalid/image.png']}}}
        write_json(self.dataset, raw)
        before = file_sha256(self.dataset)
        manifest = json.loads((self.batch/'manifest.json').read_text())
        manifest['dataset_sha256'] = before
        write_json(self.batch/'manifest.json', manifest)
        with patch('boundary_repair.experiments.evaluation.run_process', side_effect=self.fake_process):
            OfficialDockerEvaluator().evaluate(self.request)
        root = self.batch/'evaluation/test-grade'
        self.assertEqual(json.loads((root/'dataset.json').read_text()), list(raw.values()))
        self.assertEqual(file_sha256(self.dataset), before)
        saved = json.loads((root/'request.json').read_text())
        args = saved['arguments']
        self.assertEqual(args[args.index('--dataset_name')+1], str(root/'dataset.json'))
        self.assertEqual(saved['dataset_sha256'], before)
        self.assertEqual(saved['harness_dataset_sha256'], file_sha256(root/'dataset.json'))

    def test_harness_revision_mismatch_stops_before_run(self):
        from dataclasses import replace
        with patch('boundary_repair.experiments.evaluation.run_process',side_effect=self.fake_process):
            with self.assertRaisesRegex(ConfigurationError,'version_mismatch'):
                OfficialDockerEvaluator().evaluate(replace(self.request,harness_revision='version:4.0'))

    def test_duplicate_predictions_rejected(self):
        self.predictions.write_text(self.predictions.read_text()*2)
        with self.assertRaises(ValidationError):
            validate_predictions(self.predictions)

    def test_nonboolean_grade_not_coerced(self):
        write_json(self.root/'report.json',{'demo__ui-1':{'resolved':'false'}})
        self.assertEqual(parse_case_reports(self.root,('demo__ui-1',)),{})

    def test_reuse_of_evaluation_id_rejected(self):
        with patch('boundary_repair.experiments.evaluation.run_process',side_effect=self.fake_process):
            OfficialDockerEvaluator().evaluate(self.request)
            with self.assertRaises(FileExistsError):
                OfficialDockerEvaluator().evaluate(self.request)

    def test_generation_worker_cannot_start_grader(self):
        with patch.dict('os.environ', {'BOUNDARY_GENERATION_ROLE': 'isolated_worker'}):
            with self.assertRaisesRegex(ConfigurationError, 'evaluation_forbidden'):
                OfficialDockerEvaluator().evaluate(self.request)

    def freeze_isolated_batch(self):
        """Create a synthetic immutable batch with distinct public and evaluation dataset hashes."""
        manifest = json.loads((self.batch/'manifest.json').read_text())
        manifest.update(protocol='isolated-generation-v1', dataset_sha256='public-input-hash',
                        evaluation_dataset_sha256=file_sha256(self.dataset))
        write_json(self.batch/'manifest.json', manifest)
        (self.batch/'results.jsonl').write_text('{}\n{}\n')
        frozen = {'protocol': 'isolated-generation-v1', 'attempted': 2,
                  'image_manifest_sha256': file_sha256(self.images)}
        for name in ('manifest', 'predictions', 'results'):
            suffix = '.json' if name == 'manifest' else '.jsonl'
            frozen[name+'_sha256'] = file_sha256(self.batch/(name+suffix))
        write_json(self.batch/'generation_frozen.json', frozen)

    def test_separate_dataset_hash_and_frozen_submission(self):
        self.freeze_isolated_batch()
        with patch('boundary_repair.experiments.evaluation.run_process', side_effect=self.fake_process):
            self.assertEqual(OfficialDockerEvaluator().evaluate(self.request).resolved, 1)

    def test_changed_frozen_prediction_stops_before_harness(self):
        self.freeze_isolated_batch()
        self.predictions.write_text(self.predictions.read_text().replace('synthetic-diff', 'changed-diff'))
        with patch('boundary_repair.experiments.evaluation.run_process') as launch:
            with self.assertRaisesRegex(ValidationError, 'frozen_generation_artifact_changed'):
                OfficialDockerEvaluator().evaluate(self.request)
            launch.assert_not_called()

if __name__=='__main__':
    unittest.main()
