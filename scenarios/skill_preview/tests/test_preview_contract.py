import json
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]

def test_preview_preserves_dev_answer_and_localization_without_duplicate_ui():
    manifest=yaml.safe_load((ROOT/'scenario.yaml').read_text(encoding='utf-8'))
    scenario=json.loads((ROOT/'scenario.json').read_text(encoding='utf-8'))
    document=json.loads((ROOT/'webui.json').read_text(encoding='utf-8'))
    assert manifest['version']==scenario['version']==document['ui']['version']=='0.1.5'
    assert scenario['ui']=={'manifest':'webui.json'}
    application=document['ui']['application']
    widgets={w['id']:w for w in application['desktop']['pageSchema']['widgets']}
    assert 'what-is-selected-note' in widgets
    assert widgets['selected-skill-readme']['dataSource']['name']=='builder_sdk_control_skill.get_skill_preview'
    assert widgets['what-is-selected-note']['title_i18n']=='skill_preview.answer.title'
    for locale in ('en','ru'):
        resource=application['resources']['skill_preview.i18n.'+locale]
        dictionary=json.loads((ROOT/resource['path']).read_text(encoding='utf-8'))
        assert dictionary['skill_preview.answer.title']
