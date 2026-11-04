import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest


def helpers(*names, **values):
    path = Path(__file__).resolve().parents[1] / 'handlers/main.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    namespace = {'Any': Any, 'Mapping': Mapping, 'Path': Path, 'copy': copy, 'json': json, **values}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def test_model_and_account_use_selected_sdk_preferences_without_file_fallback():
    calls = []
    profile = {'resource_ref': 'ai-resource:personal-chatgpt', 'binding_ref': 'selected-binding',
        'subscription_output_policy': 'provider_default', 'model': 'selected-account-model'}
    state = {'ok': True, 'builder_llm_model': profile['model'], 'llm_profile': profile}
    env = helpers('_builder_prompt_state', '_builder_llm_model_from_session', '_personal_turn_metadata',
        _chat_project_ref=lambda **kwargs: ('scenario', kwargs['session']['scenario_id']),
        developer_prompt_context=SimpleNamespace(get=lambda *args: calls.append(args) or state),
        _load_json_file=lambda *args: pytest.fail('Canonical settings read fell back to a mutable file'))
    session = {'scenario_id': 'selected-scene'}
    assert env['_builder_llm_model_from_session'](session) == profile['model']
    result = env['_personal_turn_metadata'](session, {})
    assert result['builder_ai_resource_ref'] == profile['resource_ref']
    assert result['builder_ai_expected_binding'] == profile['binding_ref']
    assert result['builder_llm_model'] == profile['model']
    assert calls == [('scenario', 'selected-scene')] * 2


def test_invalid_sdk_preferences_refuse_instead_of_selecting_old_source_api_profile():
    env = helpers('_builder_prompt_state',
        _chat_project_ref=lambda **kwargs: ('scenario', 'selected-scene'),
        developer_prompt_context=SimpleNamespace(get=lambda *args: {'ok': False}),
        _load_json_file=lambda *args: pytest.fail('Settings failure fell back to source'))
    with pytest.raises(ValueError, match='fallback is forbidden'):
        env['_builder_prompt_state']({'scenario_id': 'selected-scene'})


def test_project_profile_reaches_ordinary_turn_metadata_without_changing_its_model():
    controls = {'builder_generation_run_protocol': 'v1', 'builder_task_pipeline': '1'}
    state = {'prototype_generation_profile': 's2-inline-typed-v1', 'llm_profile': {
        'resource_ref': 'ai-resource:personal-chatgpt', 'binding_ref': 'selected-binding',
        'model': 'selected-model', 'subscription_output_policy': 'provider_default'}}
    env = helpers('_personal_turn_metadata', _builder_prompt_state=lambda session: state,
        sdk_builder_prototype=SimpleNamespace(generation_profile_metadata=lambda value: controls))
    result = env['_personal_turn_metadata']({}, {})
    assert result['builder_generation_run_protocol'] == 'v1'
    assert result['builder_llm_model'] == 'selected-model'
    assert result['builder_ai_resource_ref'] == 'ai-resource:personal-chatgpt'
    with pytest.raises(ValueError, match='conflicts'):
        env['_personal_turn_metadata']({}, {'builder_task_pipeline': '0'})


@pytest.mark.parametrize('resource', [None, 'api-default', 'ai-resource:other'])
def test_v1_account_guard_precedes_any_interpretation_call(resource):
    env = helpers('_prepare_native_prototype_task', sdk_builder_prototype=SimpleNamespace(
        prepare_generation_request=lambda *args, **kwargs: pytest.fail('API selection reached interpretation')))
    with pytest.raises(ValueError, match='API fallback is forbidden'):
        env['_prepare_native_prototype_task']({}, {}, {'builder_generation_run_protocol': 'v1',
            'builder_ai_resource_ref': resource})


def test_v1_sdk_preflight_precedes_any_interpretation_call():
    env = helpers('_prepare_native_prototype_task', sdk_builder_prototype=SimpleNamespace(
        prepare_generation_request=lambda *args, **kwargs: pytest.fail('Old SDK reached interpretation')))
    with pytest.raises(ValueError, match='updated Core publication SDK'):
        env['_prepare_native_prototype_task']({}, {}, {'builder_generation_run_protocol': 'v1',
            'builder_ai_resource_ref': 'ai-resource:personal-chatgpt'})


def test_v1_missing_preview_delivery_sdk_blocks_before_paid_interpretation():
    sdk = SimpleNamespace(**{name: lambda **kwargs: None for name in (
        'prepare_generation_publication', 'commit_generation_publication',
        'finish_generation_failure', 'prepare_generation_journal',
        'generation_journal_dir', 'generation_publication_projection', 'resolve_generation_candidate')})
    sdk.prepare_generation_request = lambda *args, **kwargs: pytest.fail('Missing Preview delivery reached interpretation')
    env = helpers('_prepare_native_prototype_task', sdk_builder_prototype=sdk,
        builder_preview=SimpleNamespace())
    with pytest.raises(ValueError, match='Preview delivery SDK'):
        env['_prepare_native_prototype_task']({}, {}, {'builder_generation_run_protocol': 'v1',
            'builder_ai_resource_ref': 'ai-resource:personal-chatgpt'})


def test_v1_journal_resolution_never_uses_the_disposable_source(tmp_path):
    ref = 'generation-journal:' + 'a' * 24
    calls = []
    env = helpers('_llm_job_journal_dir',
        sdk_builder_prototype=SimpleNamespace(generation_journal_dir=lambda value: calls.append(value) or tmp_path),
        _project_artifact_root=lambda session: pytest.fail('Core journal fell back to DEV source'))
    assert env['_llm_job_journal_dir']({'generation_journal_ref': ref}) == tmp_path
    assert calls == [ref]
    with pytest.raises(ValueError, match='lost its journal owner'):
        env['_llm_job_journal_dir']({'generation_run_protocol': 'v1'})


def test_v1_missing_resolver_refuses_without_source_fallback():
    env = helpers('_llm_job_journal_dir', sdk_builder_prototype=SimpleNamespace(),
        _project_artifact_root=lambda session: pytest.fail('Missing resolver fell back to source'))
    with pytest.raises(ValueError, match='source fallback is forbidden'):
        env['_llm_job_journal_dir']({'generation_journal_ref': 'generation-journal:' + 'a' * 24})


def test_v1_prepares_core_journal_before_any_paid_interpretation(tmp_path):
    calls = []
    ref = 'generation-journal:' + 'a' * 24

    def prepare(request, **options):
        calls.append('interpretation')
        assert options['journal_dir'] == tmp_path / 'core'
        return {'generation_plan': {'execution_run_required': True}}

    bridge = SimpleNamespace(
        prepare_generation_publication=lambda **kwargs: None,
        commit_generation_publication=lambda **kwargs: None,
        finish_generation_failure=lambda **kwargs: None,
        prepare_generation_journal=lambda **kwargs: calls.append('journal') or {'journal_ref': ref},
        generation_journal_dir=lambda value: tmp_path / 'core',
        generation_publication_projection=lambda **kwargs: None,
        resolve_generation_candidate=lambda **kwargs: None,
        prepare_generation_request=prepare)
    env = helpers('_prepare_native_prototype_task', '_llm_job_journal_dir',
        sdk_builder_prototype=bridge,
        builder_preview=SimpleNamespace(deliver_generation_publication=lambda **kwargs: None),
        _project_artifact_root=lambda session: tmp_path / 'dev',
        _builder_llm_model_for_session=lambda *args: 'selected',
        _builder_llm_reasoning_for_model=lambda *args: None,
        _builder_job_transport=lambda *args, **kwargs: object(),
        _semantic_context_mode=lambda metadata: 'append',
        _builder_llm_timeout_s=lambda: 60)
    session = {}
    metadata = {'builder_generation_run_protocol': 'v1',
        'builder_ai_resource_ref': 'ai-resource:personal-chatgpt'}
    request = {'task_source': {'intent': {'scope': {'project_ref': 'scenario:owned'}}}}
    result, meta = env['_prepare_native_prototype_task'](request, session, metadata)
    assert result['generation_plan']['execution_run_required']
    assert meta['builder_generation_plan'] == result['generation_plan']
    assert session['generation_journal_ref'] == ref
    assert calls == ['journal', 'interpretation']
    session['generation_journal_ref'] = 'generation-journal:' + 'b' * 24
    with pytest.raises(ValueError, match='owner changed'):
        env['_prepare_native_prototype_task'](request, session, metadata)
    assert calls == ['journal', 'interpretation', 'journal']


@pytest.mark.parametrize('session,metadata', [
    ({'generation_run_protocol': 'v1'}, {}),
    ({}, {'builder_generation_run_protocol': 'v1'}),
])
def test_v1_cannot_enter_the_source_first_finalizer(session, metadata):
    env = helpers('_finalize_scenario_update',
        _ensure_session_artifact_root=lambda *args: pytest.fail('v1 reached source writes'),
        _finalize_core_generation_publication=lambda **kwargs: {'ok': True, 'delegated': True})
    result = env['_finalize_scenario_update'](ws='own', session=session, binding={}, patch={},
        request_text='request', before_webui={}, llm_result={}, auto_apply=True, _meta=metadata)
    assert result == {'ok': True, 'delegated': True}


def test_missing_retained_result_cannot_reach_publication_or_source_writes():
    env = helpers('_finalize_core_generation_publication',
        sdk_builder_prototype=SimpleNamespace(prepare_generation_publication=lambda **kwargs: pytest.fail('Missing output reached preparation')))
    with pytest.raises(ValueError, match='retained generation result'):
        env['_finalize_core_generation_publication'](ws='own', session={}, patch={}, llm_result={})


@pytest.mark.parametrize('fault', ['wrong_owner', 'before_commit', 'after_commit', 'delivery_timeout', 'wrong_delivery_ref', 'delivered', None])
def test_core_finalizer_preserves_commit_and_uses_no_source_writes(fault, tmp_path):
    calls = []
    ref = 'prototype-publication:' + 'a' * 24
    journal = 'generation-journal:' + 'b' * 24
    scope = {'object_type': 'scenario', 'object_id': 'owned', 'project_ref': 'scenario:owned', 'manifest_digest': None}
    prepared = {'publication_ref': ref, 'revision': '002', 'source_scope': scope}
    if fault == 'wrong_owner':
        prepared['source_scope'] = {**scope, 'object_id': 'different'}

    def commit(**kwargs):
        calls.append('commit')
        if fault == 'before_commit':
            raise OSError('controlled atomic replace refusal')
        return {'committed': True, 'run_id': 'original'}

    def project(**kwargs):
        calls.append('projection')
        if fault == 'after_commit':
            raise OSError('controlled projection outage')
        return {'publication_ref': ref, 'revision': '002', 'is_current': True,
            'webui': {'canonical': True}, 'prototype_brief': {'canonical': 'brief'}, 'source_scope': scope}

    sdk = SimpleNamespace(prepare_generation_publication=lambda **kwargs: calls.append('prepare') or prepared,
        commit_generation_publication=commit, generation_publication_projection=project)

    def deliver(**kwargs):
        calls.append('delivery')
        assert kwargs == {'publication_ref': ref, 'source_webspace_id': 'own'}
        if fault == 'delivery_timeout':
            raise TimeoutError('Controlled ambiguous owner result')
        return {'ok': True, 'delivered': fault in {'delivered', 'wrong_delivery_ref'},
            'publication_ref': 'unrelated' if fault == 'wrong_delivery_ref' else ref,
            'revision': '002', 'scenario_id': 'owned'}

    env = helpers('_finalize_core_generation_publication', sdk_builder_prototype=sdk,
        builder_preview=SimpleNamespace(deliver_generation_publication=deliver),
        _llm_job_journal_dir=lambda session: tmp_path,
        _normalise_llm_webui_payload=lambda payload, **kwargs: (payload, {'core': True}),
        _save_session=lambda *args: calls.append('save'),
        _publish_prompt_project_changed=lambda ws, **kwargs: {'ok': ws == 'own'},
        _request_workbench_refresh=lambda meta: {'ok': meta.get('webspace_id') == 'own'},
        _write_webui_payload=lambda *args: pytest.fail('v1 reached Source mirror writes'),
        _materialize_prototype_resources=lambda *args: pytest.fail('v1 rematerialized unqualified resources'))
    session = {'scenario_id': 'owned', 'generation_journal_ref': journal}
    llm = {'payload': {'untrusted': True}, 'core_generation_result': {
        'journal_ref': journal, 'request_id': 'retained', 'output_ref': 'exact-output'}}
    if fault == 'wrong_owner':
        with pytest.raises(ValueError, match='authoring scope'):
            env['_finalize_core_generation_publication'](ws='own', session=session, patch={}, llm_result=llm)
        assert calls == ['prepare'] and 'prototype_publication_ref' not in session
    elif fault == 'before_commit':
        with pytest.raises(OSError):
            env['_finalize_core_generation_publication'](ws='own', session=session, patch={}, llm_result=llm)
        assert calls == ['prepare', 'commit'] and 'prototype_publication_ref' not in session
    else:
        result = env['_finalize_core_generation_publication'](ws='own', session=session, patch={}, llm_result=llm)
        assert result['committed']
        if fault == 'after_commit':
            assert calls == ['prepare', 'commit', 'projection']
            assert result['projection_pending']
            assert result['projection_error'] == 'OSError'
        else:
            assert calls == ['prepare', 'commit', 'projection', 'save', 'delivery']
            assert not result['projection_pending']
            assert result['delivery_pending'] is (fault != 'delivered')
            assert session['webui_payload'] == {'canonical': True}
            assert session['prototype_publication_ref'] == ref
            retry = env['_finalize_core_generation_publication'](ws='own', session=session, patch={}, llm_result=llm)
            assert len(session['patches']) == 1
            assert retry['committed']


@pytest.mark.parametrize('fault', ['output', 'binding', 'request', None])
def test_core_result_attachment_never_comes_from_model_candidate_fields(fault):
    env = helpers('_attach_core_generation_result')
    response = {'generation_output': {'output_ref': 'retained'},
        'execution_run_binding': {'binding_id': 'bound'}}
    if fault in {'output', 'binding'}:
        response.pop('generation_output' if fault == 'output' else 'execution_run_binding')
    elif fault == 'request':
        response['request_id'] = 'another-request'
    session = {'generation_run_protocol': 'v1', 'generation_journal_ref': 'journal'}
    candidate = {'ok': True, 'core_generation_result': {'output_ref': 'untrusted'}}
    if fault:
        with pytest.raises(ValueError):
            env['_attach_core_generation_result'](candidate, response, session=session, request_id='actual')
    else:
        result = env['_attach_core_generation_result'](candidate, response, session=session, request_id='actual')
        assert result['core_generation_result']['output_ref'] == 'retained'


def test_v1_repair_candidate_comes_from_the_core_replay_not_a_native_merge(tmp_path):
    calls = []
    expected = {'candidate': {'schema': 'from-protected-core-output'}, 'source_receipt': {'source_id': 'source'}}
    env = helpers('_resolve_core_generation_candidate', '_attach_core_generation_result',
        _llm_job_journal_dir=lambda session: tmp_path,
        sdk_builder_prototype=SimpleNamespace(resolve_generation_candidate=lambda **kwargs: calls.append(kwargs) or expected))
    session = {'generation_run_protocol': 'v1', 'generation_journal_ref': 'private-journal'}
    response = {'generation_output': {'output_ref': 'protected-output'},
        'execution_run_binding': {'binding_id': 'protected-binding'}, 'output_text': '{untrusted native parse}'}
    assert env['_resolve_core_generation_candidate'](session=session, response=response, request_id='repair') is expected
    assert calls == [{'journal_dir': tmp_path, 'request_id': 'repair', 'output_ref': 'protected-output'}]


def test_second_repair_uses_the_first_repair_job_and_request():
    calls = []

    def once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {'ok': False, 'repair_candidate': {'merged': True},
                'repair': {'kind': 'semantic_record_repair', 'job_id': 'record-job', 'request_id': 'record-request'},
                'validation': {'findings': [{'code': 'binding-defect'}]}}
        return {'ok': True, 'repair': {'kind': 'semantic_binding_repair', 'job_id': 'binding-job'}}

    env = helpers('_repair_llm_semantic_transform_output', _repair_llm_semantic_transform_once=once,
        _compact_json=lambda value: json.dumps(value, sort_keys=True),
        _semantic_scoped_repair=lambda *args: ('semantic_binding_repair', {'bounded': True}))
    result = env['_repair_llm_semantic_transform_output'](output_text='{}', job_id='primary-job', request_id='primary-request',
        validation_error={'findings': [{'code': 'record-defect'}]})
    assert result['ok'] and len(calls) == 2
    assert calls[1]['job_id'] == 'record-job'
    assert calls[1]['request_id'] == 'record-request'


def test_accepted_webui_and_semantics_never_read_or_recreate_source_files():
    ref = 'prototype-publication:' + 'a' * 24
    projection = {'publication_ref': ref, 'project_ref': 'scenario:owned',
        'source_scope': {'project_ref': 'project:logical'}, 'is_current': True,
        'revision': '002', 'webui': {'ui': {'canonical': True}},
        'semantic_document': {'canonical': 'semantic'}}
    calls = []
    sdk = SimpleNamespace(generation_publication_projection=lambda **kwargs: projection,
        semantic_revision_context=lambda *args, **kwargs: calls.append((args, kwargs)) or {'ok': True})
    env = helpers('_accepted_generation_projection', '_current_webui_payload', '_current_semantic_context',
        sdk_builder_prototype=sdk,
        _project_artifact_root=lambda session: pytest.fail('Committed read reached DEV sources'))
    session = {'scenario_id': 'owned', 'project_ref': 'project:logical', 'prototype_publication_ref': ref}
    payload = env['_current_webui_payload'](session, {})
    assert payload == projection['webui'] and payload is not projection['webui']
    assert env['_current_semantic_context'](session, payload, {'new': 'brief'}) == {'ok': True}
    assert calls == [((projection['semantic_document'], {'new': 'brief'}), {'source_ref': ref, 'revision': '002'})]
    with pytest.raises(ValueError, match='UI differs'):
        env['_current_semantic_context'](session, {'ui': {'changed': True}}, {})


@pytest.mark.parametrize('fault', ['missing_sdk', 'stale', 'wrong_owner', 'wrong_logical_owner'])
def test_accepted_projection_errors_never_fall_back_to_legacy(fault):
    ref = 'prototype-publication:' + 'a' * 24
    projection = {'publication_ref': ref, 'project_ref': 'scenario:owned', 'is_current': True}
    sdk = SimpleNamespace(generation_publication_projection=lambda **kwargs: projection)
    if fault == 'missing_sdk':
        sdk = SimpleNamespace()
    elif fault == 'stale':
        projection['is_current'] = False
    elif fault == 'wrong_owner':
        projection['project_ref'] = 'scenario:different'
    else:
        projection['source_scope'] = {'project_ref': 'project:different'}
    env = helpers('_accepted_generation_projection', '_current_webui_payload', sdk_builder_prototype=sdk,
        _project_artifact_root=lambda session: pytest.fail('Projection error reached Source fallback'))
    with pytest.raises(ValueError):
        env['_current_webui_payload']({'scenario_id': 'owned', 'prototype_publication_ref': ref}, {})


@pytest.mark.parametrize('status', ['queued', 'running', 'interrupted', 'unknown', 'succeeded'])
def test_nonterminal_or_successful_status_cannot_finish_a_failed_canonical_run(status):
    env = helpers('_finish_core_generation_failure',
        sdk_builder_prototype=SimpleNamespace(finish_generation_failure=lambda **kwargs: pytest.fail('Unconfirmed failure reached canonical completion')))
    assert env['_finish_core_generation_failure']({'generation_run_protocol': 'v1'}, request_id='original', status=status) is None


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
def test_terminal_failure_uses_frozen_journal_and_does_not_resubmit(status, tmp_path):
    calls = []
    env = helpers('_finish_core_generation_failure',
        _llm_job_journal_dir=lambda session: tmp_path,
        sdk_builder_prototype=SimpleNamespace(finish_generation_failure=lambda **kwargs: calls.append(kwargs) or {'ok': True}))
    assert env['_finish_core_generation_failure']({'generation_run_protocol': 'v1'}, request_id='original', status=status) == {'ok': True}
    assert calls == [{'journal_dir': tmp_path, 'request_id': 'original', 'outcome': status}]


def test_completion_storage_refusal_stays_pending_without_inference_or_source_fallback(tmp_path):
    def fail(**kwargs):
        raise OSError('controlled private journal outage')
    env = helpers('_finish_core_generation_failure',
        _llm_job_journal_dir=lambda session: tmp_path,
        sdk_builder_prototype=SimpleNamespace(finish_generation_failure=fail))
    result = env['_finish_core_generation_failure']({'generation_run_protocol': 'v1'}, request_id='original', status='failed')
    assert result == {'ok': False, 'reconciliation_pending': True,
        'error': 'core_generation_completion_pending', 'error_type': 'OSError'}
