import pytest
import ast
import copy
from pathlib import Path
from typing import Any, Mapping


def test_context_allowance_is_explicit_bounded_and_preserves_default():
    path=Path(__file__).resolve().parents[1]/'handlers/main.py'
    tree=ast.parse(path.read_text(encoding='utf-8'))
    helper=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_builder_llm_context_capacity')
    env={'Any':Any,'Mapping':Mapping}
    exec(compile(ast.Module(body=[helper],type_ignores=[]),str(path),'exec'),env)
    capacity=env['_builder_llm_context_capacity']
    assert capacity() == 64000
    assert capacity({'builder_llm_context_tokens': 96000}) == 96000
    for invalid in [True, '96000', 8192, 256001, None]:
        with pytest.raises(ValueError, match='builder_llm_context_tokens'):
            capacity({'builder_llm_context_tokens': invalid})


def test_original_state_migration_checksum_remains_immutable():
    from adaos.domain.relational_storage import RelationalMigration
    path=Path(__file__).resolve().parents[1]/'handlers/main.py'
    tree=ast.parse(path.read_text(encoding='utf-8'))
    migration=next(n for n in tree.body if isinstance(n,ast.Assign)
                   and any(isinstance(t,ast.Name) and t.id=='_STATE_MIGRATIONS' for t in n.targets))
    env={'RelationalMigration':RelationalMigration}
    exec(compile(ast.Module(body=[migration],type_ignores=[]),str(path),'exec'),env)
    assert env['_STATE_MIGRATIONS'][0].checksum=='sha256:3d9a12613ef7a91a29a40ab5b43edd10ed1c7d2a1f3597a08aa8909b670ea9a2'


def test_validation_repair_keeps_exact_verified_reference_material():
    path=Path(__file__).resolve().parents[1]/'handlers/main.py'
    tree=ast.parse(path.read_text(encoding='utf-8'))
    helper=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_builder_repair_references')
    env={'Any':Any,'Mapping':Mapping,'copy':copy}
    exec(compile(ast.Module(body=[helper],type_ignores=[]),str(path),'exec'),env)
    carry=env['_builder_repair_references']
    refs=[{'name':'design.html','text':'<main>Слои и серии</main>','authority':'reference_only','content_sha256':'retained'}]
    request={'dynamic_request':{'reference_artifacts':refs}}
    result=carry(request)
    assert result['reference_artifacts']==refs
    result['reference_artifacts'][0]['text']='changed by repair'
    assert refs[0]['text']=='<main>Слои и серии</main>'
    assert carry({'dynamic_request':{}})=={}
