"""Hermetic package binding checks; not Core authorization or browser evidence.

The resolver below is a test double for action parameter interpolation. Root
schemas are the admitted operation contracts, and all input records are synthetic.
"""
import copy
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

PACKAGE = Path(__file__).resolve().parents[1]
UI = json.loads((PACKAGE / 'webui.json').read_text(encoding='utf-8'))
APP = UI['ui']['application']
PAGE = APP['desktop']['pageSchema']
STATE = PAGE['initialState']
WIDGETS = {w['id']: w for w in PAGE['widgets']}
WIDGETS.update({w['id']: w for m in APP['modals'].values() for w in m['schema']['widgets']})
CONTRACTS = {c['id']: c for c in json.loads((PACKAGE / 'tests/fixtures/root_contracts.json').read_text(encoding='utf-8'))}


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def actions(tool):
    return [x for x in walk(APP) if x.get('type') == 'callMcp' and x['target'] == tool]


def resolve(value, state, event):
    if isinstance(value, dict):
        return {k: resolve(v, state, event) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, state, event) for v in value]
    if isinstance(value, str) and value.startswith(('$state.', '$event.')):
        root, *parts = value.split('.')
        obj = state if root == '$state' else event
        for part in parts:
            obj = obj[part]
        return copy.deepcopy(obj)
    return value


def test_layout_keeps_collection_detail_and_scrollable_compact_modals():
    assert PAGE['layout']['pattern'] == 'collection-detail'
    regions = {r['id']: r for r in PAGE['layout']['regions']}
    assert regions['detail']['presentation']['compact'] == 'sheet'
    assert regions['metadata']['optional'] is True
    for modal in APP['modals'].values():
        assert modal['schema']['layout']['scroll'] == 'page'
    assert {'application-header', 'application-setup-summary', 'runtime-placement-summary',
            'application-access-summary', 'local-beta-status', 'cbs-lifecycle',
            'details-section'} <= WIDGETS.keys()


def test_cbs_lifecycle_is_a_compact_ordered_non_authoritative_projection():
    widget = WIDGETS['cbs-lifecycle']
    assert widget['type'] == 'item.details'
    assert widget['area'] == 'detail'
    assert widget['dataSource']['toolId'] == 'applications.show'
    assert widget['dataSource']['dryRun'] is True
    assert widget['dataSource']['resultPath'] == 'response.result.application'
    fields = widget['inputs']['fields']
    assert [field['path'] for field in fields] == [
        'cbs_lifecycle.requirement.summary',
        'cbs_lifecycle.resolution.summary',
        'cbs_lifecycle.plan.summary',
        'cbs_lifecycle.activation.summary',
        'cbs_lifecycle.lock.summary',
        'cbs_lifecycle.authority_note',
    ]
    assert [field['label'] for field in fields[:5]] == [
        '1. Requirement',
        '2. Resolution',
        '3. Plan',
        '4. Activation',
        '5. Workspace lock',
    ]


def test_catalog_complete_bounded_reads_and_local_query_selection():
    for id, flag in [('applications-list','installed_only'), ('marketplace-list','catalog_only'), ('developments-list','developed_only')]:
        w = WIDGETS[id]
        assert w['dataSource']['toolId'] == 'applications.list'
        assert w['dataSource']['arguments'] == {flag: True}
        assert w['dataSource']['resultPath'] == 'response.result.applications'
        assert w['inputs']['pagination'] and w['inputs']['filters']
        assert w['inputs']['selectedStateKey'] == 'selectedApplicationId'
        assert w['inputs']['iconKey'] == 'application.icon'
        assert w['inputs']['fallbackIcon']
        assert w['actions'][1]['params']['selectedApplicationId'] == '$event.application.application_id'


@pytest.mark.parametrize('widget', ['applications-list', 'marketplace-list', 'developments-list'])
def test_catalog_default_page_size_is_selectable(widget):
    inputs = WIDGETS[widget]['inputs']
    assert inputs['pageSize'] == 6
    assert inputs['pageSize'] in inputs['pageSizeOptions']
    assert inputs['pageSizeOptions'] == [6, 10, 25, 50, 100]


def test_no_fixture_fallback_or_shadow_domain_mutation():
    for key in ['installedApplications', 'marketplaceApplications', 'developmentApplications', 'prototypeBinding']:
        assert key not in STATE
    assert STATE['updateBatch'] == {} and STATE['updateAssessment'] == {}
    assert not any('prototype-update-plan' in str(v) for v in STATE.values())
    for node in walk(APP):
        if node.get('type') == 'updateState':
            assert not any('simulated' in str(v).lower() or 'in Prototype' in str(v) for v in node.get('params', {}).values())
        if node.get('kind') == 'mcp':
            assert node['dryRun'] is True


def test_selection_clears_prior_application_revision_and_editor_contexts():
    for id in ['applications-list', 'marketplace-list', 'developments-list']:
        clear = WIDGETS[id]['actions'][0]['params']
        for key in ['reviewedPlan', 'accessContext', 'accountContext', 'selectedApplication']:
            assert clear[key] == {}
        assert clear['selectedSetupId'] == ''
        assert clear['selectedReleaseDigest'] == ''
        assert clear['publicationEvidence'] is False
        assert clear['trialTarget'] is None
        assert clear['effectiveTarget'] is None
    detail = WIDGETS['application-details']
    assert detail['dataSource']['toolId'] == 'applications.show'
    bindings = detail['inputs']['stateBindings']
    for key, path in [('applicationDefinitionRevision','application.revision'),('installationRevision','installation.revision'),('subscriptionRevision','subscription.revision'),('selectedReleaseDigest','installation_summary.release_digest')]:
        assert bindings[key]['path'] == path


def test_every_root_call_has_admitted_input_names_and_required_arguments():
    for x in walk(APP):
        if x.get('type') == 'callMcp':
            contract = CONTRACTS[x['target']]
            schema = contract['input_schema']
            names = set(x.get('params', {}))
            if x.get('idempotencyKey'):
                names.add('idempotency_key')
            assert names <= set(schema.get('properties', {})), (x['target'], names)
            assert set(schema.get('required', [])) <= names, x['target']
            assert 'actor_ref' not in names and 'capability' not in names
        if x.get('kind') == 'mcp':
            schema = CONTRACTS[x['toolId']]['input_schema']
            names = set(x.get('arguments', {}))
            assert names <= set(schema.get('properties', {})), x['toolId']
            assert set(schema.get('required', [])) <= names


def test_lifecycle_plans_use_displayed_revisions_and_review_before_apply():
    planned = {a['params']['kind'] for a in actions('applications.plan')}
    assert planned == {'install','update','remove','select_track','relocate_component','install_component','remove_component'}
    for a in actions('applications.plan'):
        assert a['params']['expected_revision'].startswith(('$state.', '$event.'))
        assert a['idempotencyKey'] == 'auto'
        assert a['resultStateKey'] == 'reviewedPlan'
    for a in actions('applications.apply'):
        assert a['params'] == {'operation_id':'$state.reviewedPlan.operation.operation_id','plan_digest':'$state.reviewedPlan.operation.plan_digest', 'idempotency_key':'$state.reviewedPlan.operation.operation_id'}
        assert 'idempotencyKey' not in a
        assert "status == 'planned'" in a['enabledIf']
    assert not actions('applications.update_settings')
    for a in WIDGETS['lifecycle-actions']['actions']:
        if a['on'] in ['click:pin-home','click:unpin-home']:
            assert a['type'] == 'callMcp'


def test_exact_plan_digest_and_retry_key_survive_synthetic_retry():
    state = {'reviewedPlan': {'operation': {'operation_id':'appop.synthetic', 'plan_digest':'sha256:'+'a'*64}}}
    action = actions('applications.apply')[0]
    first = resolve(action['params'], state, {})
    retry = resolve(action['params'], state, {})
    assert retry == first
    jsonschema.validate(first, CONTRACTS['applications.apply']['input_schema'])
    assert first['idempotency_key'] == first['operation_id']


def test_application_detail_fields_project_from_published_record():
    record = {
        'attention': {'status': 'blocked', 'reason': 'setup', 'message': 'Configure'},
        'local_development': {
            'prototype_evidence': {'ref': 'prototype'}, 'automation_evidence': {'ref': 'automation'},
            'trial': {'status': 'accepted', 'candidate_id': 'candidate.synthetic',
                      'candidate_digest': 'sha256:' + 'a' * 64, 'evidence_present': True},
            'publication': {'status': 'not_started'},
            'source_registry': {
                'status': 'not_published',
                'semantic_publication': {'record_count': 0},
                'installable_catalog_status': 'not_published',
            },
            'readme': 'Synthetic README',
        },
        'effective_navigation': {'message': 'Unavailable', 'reason': 'not_installed_in_webspace'},
    }
    published = set(CONTRACTS['applications.show']['metadata']['webui_data_binding']['result_paths'].values())
    for widget_id, section in [('application-health-summary', 'attention'),
                               ('local-beta-status', 'local_development'),
                               ('application-open-unavailable', 'effective_navigation')]:
        widget = WIDGETS[widget_id]
        assert widget['dataSource']['resultPath'] in published
        for field in widget['inputs']['fields']:
            assert field['path'].startswith(section + '.')
            assert resolve('$state.' + field['path'], record, {}) is not None


def test_apply_retry_keys_use_parameters_and_preserve_modal_command_order():
    for node in walk(APP):
        if 'idempotencyKey' in node:
            assert node['idempotencyKey'] == 'auto'
    for modal_id in ['review-component-uninstall', 'review-runtime-relocation', 'review-lifecycle-operation']:
        for widget in APP['modals'][modal_id]['schema']['widgets']:
            steps = widget.get('actions', [])
            for step in steps:
                if step.get('target') != 'applications.apply':
                    continue
                command = [action for action in steps if action.get('on') == step['on']]
                assert [action['type'] for action in command] == ['callMcp', 'closeModal']
                assert step['params']['idempotency_key'] == step['params']['operation_id']


def test_setup_schema_editor_and_unsupported_field_gate():
    for id, kind, selected in [('setup-general-form','settings','selectedSetupId'),('credential-form','credentials','selectedCredentialId'),('setup-provider-form','providers','selectedProviderId')]:
        form = WIDGETS[id]
        assert form['dataSource']['toolId'] == 'applications.setup.show'
        assert form['inputs']['recordsPath'] == 'setup.editors.'+kind
        assert form['inputs']['fieldsPath'] == 'fields'
        assert form['inputs']['valuesPath'] == 'values'
        assert form['inputs']['selectedStateKey'] == selected
        assert form['inputs']['fields'] == []
    assert 'setupSupported == true' in WIDGETS['setup-general-form']['actions'][0]['enabledIf']
    assert any(m['key'] == 'unsupported_fields' for m in WIDGETS['setup-components']['inputs']['meta'])


@pytest.mark.parametrize('tool', ['applications.setup.configure','applications.setup.credential','applications.setup.provider'])
def test_setup_identity_and_cas_cannot_come_from_edited_values(tool):
    action = actions(tool)[0]
    record = {'application_id':'synthetic','release_digest':'sha256:'+'b'*64,'component_ref':'skill:synthetic','expected_revision':7,'slot':'synthetic_token','provider_id':'google.gmail'}
    event = {'record':record,'values':{'application_id':'wrong','expected_revision':999,'value':'synthetic-test-only'}}
    result = resolve(action['params'], {}, event)
    assert result['application_id'] == 'synthetic' and result['expected_revision'] == 7
    jsonschema.validate(result, CONTRACTS[tool]['input_schema'])
    assert 'resultStateKey' not in action


def test_credentials_do_not_enter_page_state_and_revoke_sends_null():
    assert actions('applications.setup.credential')[1]['params']['value'] is None
    for node in walk(APP):
        if node.get('type') == 'updateState':
            assert '$event.values.value' not in json.dumps(node)
    assert WIDGETS['credential-form']['inputs']['resetOnSuccess'] is True
    assert WIDGETS['setup-provider-form']['inputs']['resetOnSuccess'] is True
    assert actions('applications.setup.provider')[0]['params']['values'] == '$event.values'


def test_placement_uses_component_revision_and_only_live_eligible_nodes():
    assert WIDGETS['runtime-components']['dataSource']['toolId'] == 'applications.list_components'
    for id in ['primary-runtime-form','install-component-form']:
        field = WIDGETS[id]['inputs']['fields'][0]
        assert field['optionsDataSource']['resultPath'] == 'response.result.eligible_nodes'
        assert field['optionsDataSource']['arguments']['component_ref'] == '$state.selectedComponentRef'
        assert 'options' not in field
        assert WIDGETS[id]['actions'][0]['params']['expected_revision'] == '$state.componentRevision'
    assert WIDGETS['rejected-placement-nodes']['inputs']['subtitleKey'] == 'reason'


def test_access_projections_are_published_and_match_output_schema():
    contract = CONTRACTS['applications.access.show']
    published = contract['metadata']['webui_data_binding']['result_paths']
    consumed = {node['resultPath'] for node in walk(APP)
                if node.get('kind') == 'mcp'
                and node.get('toolId') == 'applications.access.show'}
    assert consumed <= set(published.values())
    for name in ['sections', 'permissions', 'release_readiness', 'activity']:
        path = 'response.result.access.sections'
        if name != 'sections':
            path += '.' + name
        assert published[name] == path
        assert path in consumed
        schema = contract['output_schema']
        for part in path.split('.')[1:]:
            schema = schema['properties'][part]
        types = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        assert set(types) & {'object', 'array'}


def test_subject_role_and_permission_choices_are_root_owned():
    for id in ['access-grant-form','access-change-form','access-simulation-form','connected-account-form']:
        for f in WIDGETS[id]['inputs']['fields']:
            if f['id'] in ['subject_ref','application_roles','permission_ceiling','explicit_denies','permission_id']:
                assert 'options' not in f
                assert f['optionsDataSource']['kind'] == 'mcp'
                if f['id'] == 'subject_ref':
                    assert f['optionValuePath'] == 'subject_ref'
                    assert f['optionLabelPaths'] == ['display_label']


def test_access_update_cas_is_separate_from_mutable_draft():
    state = {'accessContext':{'grant_id':'grant.synthetic','revision':3,'release_digest':'sha256:'+'a'*64}}
    event = {'values':{'grant_id':'wrong','revision':999,'application_roles':['viewer'],'permission_ceiling':['workspace.read'],'explicit_denies':[],'expires_at':None}}
    actual = resolve(actions('applications.access.change')[0]['params'], state, event)
    assert actual['grant_id'] == 'grant.synthetic' and actual['expected_revision'] == 3
    actual['idempotency_key'] = 'synthetic-edit'
    jsonschema.validate(actual, CONTRACTS['applications.access.change']['input_schema'])
    assert actions('applications.access.grant')[0]['params']['expected_revision'] == 0


def test_connected_account_create_and_update_have_distinct_cas():
    create, edit = actions('applications.access.connected_account')
    assert create['params']['expected_revision'] == 0
    assert edit['params']['expected_revision'] == '$state.accountContext.revision'
    assert edit['params']['account_id'] == '$state.accountContext.account_id'
    for form in ['connected-account-form','connected-account-edit']:
        assert not any(f['type'] == 'password' for f in WIDGETS[form]['inputs']['fields'])
    assert 'scopes' in create['params']


def test_forms_use_explicit_button_ids_and_failure_preserves_drafts():
    for w in WIDGETS.values():
        if w['type'] != 'ui.form':
            continue
        submitted = [a for a in w.get('actions',[]) if a.get('type') == 'callMcp']
        if submitted:
            buttons = {b['id'] for b in w['inputs']['buttons']}
            assert all(a['id'] in buttons for a in submitted)
            if w['id'] not in {'credential-form', 'setup-provider-form'}:
                assert w['inputs'].get('resetOnSuccess') is False


def test_builder_trial_target_is_logical_and_publication_gates_stable_actions():
    bindings = WIDGETS['application-details']['inputs']['stateBindings']
    assert bindings['trialTarget']['path'] == 'local_development.trial.navigation_target'
    assert bindings['publicationEvidence']['path'] == 'local_development.publication.evidence_present'
    life = WIDGETS['lifecycle-actions']
    trial = next(a for a in life['actions'] if a['on'] == 'click:open')
    assert trial['params']['workspaceId'] == '$state.trialTarget.webspace_id'
    assert trial['params']['expectedScenarioId'] == '$state.trialTarget.expected_scenario_id'
    assert 'trialAccepted == true' in trial['enabledIf']
    for b in life['inputs']['buttons']:
        if b['id'] in ['install','update']:
            assert 'publicationEvidence == true' in b['visibleIf']
    preview = next(a for a in life['actions'] if a['on'] == 'click:preview')
    assert preview['params']['expectedScenarioId'] == '$state.developmentObjectId'


def test_installed_open_does_not_guess_a_navigation_target():
    bindings = WIDGETS['application-details']['inputs']['stateBindings']
    assert bindings['effectiveTarget'] == {'path': 'effective_navigation.target', 'default': None}
    for tool in ('applications.list', 'applications.show'):
        record = CONTRACTS[tool]['metadata']['webui_data_binding']['record']
        assert record['effective_navigation_target_path'] == bindings['effectiveTarget']['path']
    life = WIDGETS['lifecycle-actions']
    button = next(b for b in life['inputs']['buttons'] if b['id'] == 'open-installed')
    guard = '$state.applicationInstalled == true && !($state.trialAccepted == true && $state.trialTarget)'
    assert button['visibleIf'] == guard
    action = next(a for a in life['actions'] if a['on'] == 'click:open-installed')
    assert action['enabledIf'] == button['enabledIf'] == guard + ' && $state.effectiveTarget'
    assert action['type'] == 'openWorkspace'
    target = {'intent': 'webspace.open', 'expected_scenario_id': 'different-scenario',
              'webspace_id': 'selected-workspace', 'space_kind': 'workspace',
              'application_id': 'synthetic-app', 'release_digest': 'sha256:' + 'a' * 64}
    assert resolve(action['params'], {'effectiveTarget': target}, {}) == {
        'workspaceId': 'selected-workspace', 'expectedScenarioId': 'different-scenario', 'newWindow': True}
    trial = next(b for b in life['inputs']['buttons'] if b['id'] == 'open')
    assert trial['visibleIf'] == '$state.trialAccepted == true && $state.trialTarget'
    unavailable = WIDGETS['application-open-unavailable']
    assert unavailable['visibleIf'] == guard + ' && !$state.effectiveTarget'
    assert unavailable['dataSource']['toolId'] == 'applications.show'
    assert unavailable['dataSource']['resultPath'] == 'response.result.application'
    assert STATE['effectiveTarget'] is None


def test_catalog_and_back_icons_use_ionicon_names():
    # Regression for the two missing SVG names in browser attempt-01.
    # Asset serving itself remains an independent Client/browser check.
    for widget_id in ['applications-list', 'marketplace-list', 'developments-list']:
        assert WIDGETS[widget_id]['inputs']['fallbackIcon'] == 'apps-outline'
        assert WIDGETS[widget_id]['inputs']['iconKey'] == 'application.icon'
    back = WIDGETS['compact-detail-close']
    assert back['inputs']['buttons'][0]['icon'] == 'arrow-back-outline'
    assert back['actions'][0]['on'] == 'click:back'


def test_setup_summary_read_preserves_exact_release_and_root_authority():
    source = WIDGETS['application-setup-summary']['dataSource']
    assert source['toolId'] == 'applications.setup.show'
    assert source['dryRun'] is True
    assert source['resultPath'] == 'response.result.setup'
    for digest in [None, 'sha256:' + 'a' * 64]:
        arguments = resolve(source['arguments'], {
            'selectedApplicationId': 'synthetic-app', 'selectedReleaseDigest': digest,
        }, {})
        assert arguments == {'application_id': 'synthetic-app', 'release_digest': digest}
        jsonschema.validate(arguments, CONTRACTS[source['toolId']]['input_schema'])


def test_root_authorization_delegation_has_no_local_role_store():
    for a in [x for x in walk(APP) if x.get('type') == 'callMcp']:
        contract = CONTRACTS[a['target']]
        assert contract['required_capability'].startswith('applications.')
        assert not {'actor_ref','issuer_ref','platform_role','owner'} & set(a.get('params',{}))
    assert not any(x.get('type') == 'callSkill' for x in walk(APP))
    scenario = yaml.safe_load((PACKAGE / 'scenario.yaml').read_text(encoding='utf-8'))
    assert scenario['runtime']['skills']['required'] == []


@pytest.mark.parametrize('installed,available', [(True, False), (True, True), (False, True)])
def test_configure_requires_installed_and_authoritative_setup(installed, available):
    summary = WIDGETS['application-setup-summary']
    binding = summary['inputs']['stateBindings']['setupSupported']
    assert binding == {'path': 'available', 'default': False}
    setup = {'available': available, 'reason': None if available else 'release_setup_contract_not_declared'}
    displayed = {f['path']: setup.get(f['path']) for f in summary['inputs']['fields']}
    assert displayed['available'] is available
    assert displayed['reason'] == setup['reason']
    button = next(b for b in WIDGETS['lifecycle-actions']['inputs']['buttons'] if b['id'] == 'configure')
    assert button['visibleIf'] == '$state.applicationInstalled == true && $state.setupSupported == true'
    # Bounded predicate test double; deployed visibility belongs to browser acceptance.
    predicate = button['visibleIf'].replace('$state.applicationInstalled', str(installed)).replace(
        '$state.setupSupported', str(setup[binding['path']])).replace('true', 'True').replace('&&', 'and')
    assert eval(predicate, {'__builtins__': {}}) is (installed and available)


def test_access_reads_follow_installed_release_after_versions_section_removal():
    installed, candidate = ['sha256:' + c * 64 for c in 'ab']
    record = {'installation_summary': {'release_digest': installed},
              'effective_release': {'release_digest': candidate}}
    bindings = WIDGETS['application-details']['inputs']['stateBindings']
    state = copy.deepcopy(STATE)
    state['selectedApplicationId'] = 'synthetic-app'
    for key in ['selectedReleaseDigest', 'effectiveReleaseDigest']:
        state[key] = resolve('$event.' + bindings[key]['path'], {}, record)
    assert state['selectedReleaseDigest'] == installed
    assert state['effectiveReleaseDigest'] == candidate
    modal = APP['modals']['manage-application-access']
    reads = [n for n in walk(modal) if n.get('kind') == 'mcp'
             and 'release_digest' in n.get('arguments', {})]
    assert reads
    assert 'releases-list' not in WIDGETS
    for read in reads:
        args = resolve(read['arguments'], state, {})
        assert args['release_digest'] == installed
        jsonschema.validate(args, CONTRACTS[read['toolId']]['input_schema'])
    assert state['effectiveReleaseDigest'] == candidate
    opening = [a for a in WIDGETS['lifecycle-actions']['actions'] if a.get('on') == 'click:manage-access']
    assert opening == [{'type': 'openModal', 'on': 'click:manage-access',
                        'params': {'modalId': 'manage-application-access'}}]


@pytest.mark.parametrize('operation', ['grant', 'change'])
def test_access_without_declared_roles_submits_empty_array(operation):
    form = WIDGETS['access-' + operation + '-form']
    roles = next(f for f in form['inputs']['fields'] if f['id'] == 'application_roles')
    assert roles['required'] is False
    assert roles['defaultValue'] == []
    state = {'selectedApplicationId': 'synthetic-app', 'selectedReleaseDigest': 'sha256:' + 'a' * 64,
             'accessContext': {'grant_id': 'grant.synthetic', 'revision': 3, 'release_digest': 'sha256:' + 'a' * 64}}
    event = {'values': {'subject_ref': 'user.synthetic', 'application_roles': roles['defaultValue'],
                        'permission_ceiling': ['applications.read'], 'explicit_denies': [], 'expires_at': None}}
    action = actions('applications.access.' + operation)[0]
    args = resolve(action['params'], state, event)
    assert args['application_roles'] == []
    assert args['permission_ceiling'] == ['applications.read']
    assert args['explicit_denies'] == []
    assert args['expected_revision'] == (0 if operation == 'grant' else 3)
    assert action['idempotencyKey'] == 'auto'
    args['idempotency_key'] = 'synthetic-no-roles'
    jsonschema.validate(args, CONTRACTS[action['target']]['input_schema'])


def test_manifest_has_one_ui_source_and_locales_survive():
    scenario = json.loads((PACKAGE / 'scenario.json').read_text(encoding='utf-8'))
    assert 'application' not in scenario.get('ui', {}) or scenario['ui']['application'] == APP
    manifest = yaml.safe_load((PACKAGE / 'scenario.yaml').read_text(encoding='utf-8'))
    assert manifest['ui']['manifest'] == 'webui.json'
    for locale in ['en','ru']:
        assert json.loads((PACKAGE / 'assets/i18n' / (locale+'.json')).read_text(encoding='utf-8'))
