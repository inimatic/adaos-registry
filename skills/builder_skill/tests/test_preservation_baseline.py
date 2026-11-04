import ast
import json
from pathlib import Path
import pytest
from test_builder_skill import _load_module
from test_generation_publication import helpers


def test_all_compiler_and_recovery_callers_keep_the_exact_baseline_envelope():
    path=Path(__file__).resolve().parents[1]/'handlers/main.py'
    tree=ast.parse(path.read_text(encoding='utf-8'))
    values=[kw.value for node in ast.walk(tree) if isinstance(node,ast.Call)
            for kw in node.keywords if kw.arg=='previous_semantic']
    assert len(values)>=4
    for value in values:
        assert not any(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
            and node.func.attr=='get' and node.args and isinstance(node.args[0],ast.Constant)
            and node.args[0].value=='document' for node in ast.walk(value))


def test_parser_passes_the_complete_core_baseline_to_sdk_compilation(monkeypatch):
    skill=_load_module()
    previous={'schema':'adaos.builder.semantic_compiler_view.v1','view_digest':'pinned',
              'document':{'schema':'adaos.webui.semantic.v2'},'source_field_bindings':[{'identity':'retained'}]}
    monkeypatch.setattr(skill.sdk_builder_prototype,'expand_requirement_aliases',lambda value,**kw:value)
    class Observed(Exception):pass
    def compile(value,**kwargs):
        assert kwargs['previous'] is previous
        raise Observed()
    monkeypatch.setattr(skill.sdk_builder_prototype,'compile_semantic_candidate',compile)
    with pytest.raises(Observed):
        skill._parse_llm_webui_transform_output(output_text=json.dumps({
            'schema':'adaos.builder.semantic_prototype_candidate.v2','preserve_baseline':{'baseline_digest':'pinned'}}),
            previous_preview={},output_mode='semantic_v2',prototype_brief={},previous_semantic=previous)


def test_preservation_context_failure_never_becomes_model_repair_or_loses_its_finding():
    env=helpers('_repair_llm_semantic_transform_once',
        _semantic_output_mode=lambda value:value=='semantic_v2',_semantic_contract_version=lambda value:'v2',
        _extract_json_object=json.loads,_builder_llm_model_for_session=lambda *args:pytest.fail('No model repair'))
    failure={'detail':'Exact baseline is missing','findings':[{'code':'semantic.baseline_preservation_unproved'}]}
    result=env['_repair_llm_semantic_transform_once'](session={},instruction='Preserve',previous_preview={},
        output_text=json.dumps({'schema':'adaos.builder.semantic_prototype_candidate.v2','preserve_baseline':{}}),
        validation_error=failure,prototype_brief={'task_contract':{'source':{}}},project_ref=None,
        request_id='request',job_id='job',output_mode='semantic_v2',_meta={})
    assert result['repair_skipped'] and result['validation']==failure
    assert result['error']=='baseline_preservation_context_invalid'
