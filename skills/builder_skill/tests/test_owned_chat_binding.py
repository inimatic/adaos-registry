import ast
from pathlib import Path
from types import SimpleNamespace
import pytest


def resolver(result):
    path = Path(__file__).resolve().parents[1]/'handlers/main.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name=='_conversation_id')
    calls = []
    def ensure(**kwargs):
        calls.append(kwargs)
        return result
    env = {'sdk_conversation':SimpleNamespace(ensure_builder_topic=ensure,
        communication_context=lambda **kwargs:{'status':'unavailable'}),
        'SKILL_ID':'builder_skill','DIALOG_CHANNEL_ID':'builder'}
    exec(compile(ast.Module(body=[node],type_ignores=[]), str(path), 'exec'), env)
    return env['_conversation_id'], calls


def test_owned_binding_uses_core_identity_when_legacy_id_is_foreign():
    resolve, calls = resolver({'stored':True,'owner':'skill:builder_skill',
        'conversation_id':'conv.skill.builder_skill.default.desktop'})
    assert resolve('desktop')=='conv.skill.builder_skill.default.desktop'
    assert calls==[{'webspace_id':'desktop'}]


@pytest.mark.parametrize('result', [{}, {'stored':False,'owner':'skill:builder_skill','conversation_id':'x'},
    {'stored':True,'owner':'core:general_assistant','conversation_id':'foreign'},
    {'stored':True,'owner':'skill:builder_skill','conversation_id':''}])
def test_unavailable_binding_never_guesses_a_legacy_global_conversation(result):
    resolve,_ = resolver(result)
    with pytest.raises(RuntimeError,match='builder_owned_conversation_unavailable'):
        resolve('desktop-dev')
