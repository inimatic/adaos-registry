from pathlib import Path
import importlib.util
import yaml


def test_companion_has_no_provider_or_unrelated_tools():
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / 'skill.yaml').read_text(encoding='utf-8'))
    assert manifest['exports']['tools'] == []
    assert manifest['capabilities'] == []
    assert manifest['data_lifecycle']['databases'] == []
    spec = importlib.util.spec_from_file_location('companion', root / 'handlers/main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.lang_res() == {}
