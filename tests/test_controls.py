"""Exercise every ablation composition with real algorithms and a labeled model double."""
from dataclasses import replace
from itertools import product
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from helpers import catalogue_response, config, context, aligned_evidence, parser_module, snapshot, task
from test_scoped_alignment import target_source
from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.algorithms.controls import PlainControls
from boundary_repair.algorithms.specification import SpecificationRecovery
from boundary_repair.algorithms.expressivity import ExpressivityLocalization
from boundary_repair.algorithms.synthesis import ScopeSynthesis
from boundary_repair.algorithms.rendering import HoleRenderer
from boundary_repair.domain.errors import ValidationError
from boundary_repair.domain.runtime import BudgetLimits
from boundary_repair.ports import ModelResponse
from boundary_repair.pipeline import RepairPipeline


class ModelDouble:
    """Fixed evidence and a known textual substitution test the integration, not model intelligence."""
    def complete(self, request, ctx):
        ctx.budget.begin_model_call(request.max_output_tokens)
        if request.schema_name=='evidence.v6':
            prompt=json.loads(request.prompt)
            data=catalogue_response(aligned_evidence(prompt), prompt)
        elif request.schema_name=='edits.v5':
            prompt=json.loads(request.prompt)
            block=next(b for b in prompt['blocks']
                       if 'active || hidden' in target_source(prompt, b['block_id']))
            source=target_source(prompt, block['block_id'])
            data={'edits':[{'operation':'replace_block', 'target':block['block_id'],
                            'old_text':'', 'destination':'',
                            'new_text':source.replace('active || hidden','active && !hidden')}]}
        else:
            holes=json.loads(request.prompt)['holes']
            data={'edits':[{'hole_id':h['hole_id'], 'operation':'replace', 'condition':None,
                            'new_source':h['old_source'].replace('active || hidden','active && !hidden')}
                           for h in holes if 'active || hidden' in h['old_source']]}
        text=json.dumps(data);ctx.budget.record_output_tokens(10)
        return ModelResponse(text,10,'model-double')


class TraceDouble:
    def __init__(self):
        self.events=[];self.artifacts={}
    def emit(self,event):
        self.events.append(event)
    def save(self,name,artifact):
        self.artifacts[name]=artifact


@unittest.skipUnless(parser_module(),'requires pinned TypeScript')
class ControlsTests(unittest.TestCase):
    def test_all_eight_combinations_execute(self):
        for use_spec,use_loc,use_synth in product((False,True),repeat=3):
            with self.subTest(modules=(use_spec,use_loc,use_synth)), TemporaryDirectory() as raw:
                root=Path(raw);snap=snapshot(root);program=ProgramAdapter(config(root));model=ModelDouble();logic=LogicAdapter()
                controls=PlainControls(model,program)
                pipeline=RepairPipeline(SpecificationRecovery(model,program,logic) if use_spec else controls,
                                        ExpressivityLocalization(program,logic) if use_loc else controls,
                                        ScopeSynthesis(model,program,logic) if use_synth else controls)
                trace=TraceDouble();ctx=context()
                result=pipeline.repair(task(),snap,ctx,trace)
                self.assertIn('diff --git',result.patch.unified_diff)
                self.assertEqual([event.stage for event in trace.events if event.status=='completed'],
                                 ['specification','expressivity','synthesis'])
                self.assertEqual(ctx.budget.model_calls,1 if use_synth else 2)

    def test_plain_specs_keep_first_interpretation_as_explicit_assumptions(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);snap=snapshot(root)
            result=PlainControls(ModelDouble(),ProgramAdapter(config(root))).recover(task(),snap,context())
            self.assertEqual(len(result.must), 3)
            self.assertEqual(len(result.frames), 1)
            self.assertEqual(len(result.witnesses), 4)
            self.assertEqual(result.specification_policy, 'first_sourced_interpretation')
            self.assertEqual(result.theory.consistency.value, 'unknown')

if __name__=='__main__':
    unittest.main()
