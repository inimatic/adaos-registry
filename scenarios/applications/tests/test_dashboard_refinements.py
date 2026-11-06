import json
from pathlib import Path

PAGE = json.loads((Path(__file__).parents[1] / 'webui.json').read_text(encoding='utf-8'))['ui']['application']['desktop']['pageSchema']
WIDGETS = {widget['id']: widget for widget in PAGE['widgets']}
APPLICATION = json.loads((Path(__file__).parents[1] / 'webui.json').read_text(encoding='utf-8'))['ui']['application']


def test_context_is_initialized_before_the_first_scoped_catalog_read():
    page = APPLICATION['desktop']['pageSchema']
    binding = next(a for a in page['autoActions'] if a['id'] == 'applications-client-context')
    assert binding['action']['type'] == 'updateState'
    assert binding['action']['params']['currentWebspaceId'] == '$client.webspaceId'
    assert binding['enabledIf'] == '!$state.currentWebspaceId'
    for identity in ('applications-list', 'marketplace-list', 'developments-list'):
        assert WIDGETS[identity]['dataSource']['arguments']['webspace_id'] == '$state.currentWebspaceId'


def test_application_change_clears_ticket_selection():
    for name in ('applications-list', 'marketplace-list', 'developments-list'):
        selection = [a for a in WIDGETS[name]['actions'] if a.get('on') == 'select' and a.get('type') == 'updateState']
        assert selection and all(a['params']['selectedTicketId'] == '' for a in selection)


def test_dashboard_groups_read_core_positions_without_fabricating_rows():
    positions = [
        {'id': 'private_alpha', 'scope': 'private', 'stage': 'alpha', 'version': '1'},
        {'id': 'private_beta', 'scope': 'private', 'stage': 'beta', 'version': '2'},
        {'id': 'private_stable', 'scope': 'private', 'stage': 'stable', 'version': '3'},
        {'id': 'public_beta', 'scope': 'public', 'stage': 'beta', 'version': '4'},
        {'id': 'public_stable', 'scope': 'public', 'stage': 'stable', 'version': '5'},
    ]
    widget = WIDGETS['dashboard-positions']
    assert widget['dataSource']['toolId'] == 'applications.show'
    assert widget['dataSource']['arguments'] == {'application_id': '$state.selectedApplicationId', 'webspace_id': '$state.currentWebspaceId', 'view': 'dashboard'}
    assert widget['dataSource']['dryRun'] is True
    assert widget['dataSource']['resultPath'] == 'response.result.application.dashboard.positions'
    contracts = {c['id']: c for c in json.loads((Path(__file__).parent / 'fixtures/root_contracts.json').read_text(encoding='utf8'))}
    paths = contracts['applications.show']['metadata']['webui_data_binding']['result_paths']
    assert widget['dataSource']['resultPath'] == paths['dashboard_positions']
    assert widget['inputs']['groupBy'] == 'scope'
    assert widget['inputs']['groupDisplay'] == 'sections'
    assert len(positions) == 5 and {p['scope'] for p in positions} == {'private', 'public'}
    recipient = [p for p in positions if p['scope'] == 'public']
    assert {p['scope'] for p in recipient} == {'public'}
    direct = next(b for b in widget['inputs']['buttons'] if b['id'] == 'position-open')
    assert (direct['whenKey'], direct['whenEquals']) == ('actions.open.mode', 'open')
    switch = next(b for b in widget['inputs']['buttons'] if b['id'] == 'position-switch-open')
    assert (switch['whenKey'], switch['whenEquals']) == ('actions.open.mode', 'switch_and_open')
    assert any(a.get('on') == 'click:position-open' and a['type'] == 'openWorkspace' for a in widget['actions'])


def test_tickets_are_scoped_to_selected_application_and_detail_selection():
    action = next(a for a in WIDGETS['shared-dev-tickets']['actions'] if a['type'] == 'openDevTickets')
    scope = action['params']['target_scope']
    assert scope == {'type': 'application', 'id': '$state.selectedApplicationId',
                     'scope_kind': 'builder_project', 'scope_mode': 'project_with_components',
                     'project_id': '$state.selectedApplicationId',
                     'project_ref': 'project:$state.selectedApplicationId', 'project_kind': 'project'}
    assert action['params']['origin_scope']['id'] == '$state.selectedApplicationId'
    assert not {'tickets-query', 'application-tickets', 'application-ticket-detail', 'application-ticket-history'} & WIDGETS.keys()

def test_embedded_ticket_forms_are_removed_without_changing_ticket_sdk():
    assert 'add-application-ticket' not in APPLICATION['modals']
    assert 'add-application-ticket-comment' not in APPLICATION['modals']

def test_finalize_has_one_confirmation_and_no_unconfirmed_write():
    row = [a for a in WIDGETS['dashboard-positions']['actions'] if a.get('on') == 'click:finalize']
    assert not any(a['type'] == 'callMcp' for a in row)
    assert next(a for a in row if a['type'] == 'navigateModal')['params']['modalId'] == 'finalize-application'
    modal = APPLICATION['modals']['finalize-application']['schema']
    commands = next(w for w in modal['widgets'] if w['id'] == 'finalize-confirmation')
    writes = [a for a in commands['actions'] if a.get('type') == 'callMcp' and not a.get('dryRun')]
    assert len(writes) == 1 and writes[0]['target'] == 'applications.lifecycle.finalize'
    assert writes[0]['params']['release_digest'] == '$state.finalizeContext.release_digest'
    assert writes[0]['params']['candidate_id'] == '$state.finalizeContext.candidate_id'
    assert writes[0]['idempotencyKeyStateKey'] == 'finalizeIntentKey'
    assert [a for a in commands['actions'] if a['on'] == 'click:close'] == [{'on':'click:close','type':'closeModal'}]
    assert not modal.get('autoActions')
    clock = next(a for a in APPLICATION['desktop']['pageSchema']['autoActions'] if a['id'] == 'applications-lifecycle-observation')
    assert clock['action']['target'] == 'applications.lifecycle.events' and clock['action']['dryRun']
    assert clock['intervalMs'] >= 15000
    assert clock['action']['params']['application_id'] == '$state.selectedApplicationId'
    assert WIDGETS['application-details']['inputs']['stateBindings']['finalizeLiveOperation']['path'] == 'lifecycle.operation'


def test_lifecycle_review_binds_captured_row_and_exact_operation():
    menu = next(w for w in APPLICATION['modals']['position-actions']['schema']['widgets'] if w['id'] == 'position-menu-actions')
    actions = menu['actions']
    for action_name in ('remove_beta', 'renew_beta', 'restore_stable'):
        plan = next(a for a in actions if a.get('on') == 'click:' + action_name and a.get('target') == 'applications.lifecycle.plan')
        assert plan['params']['application_id'] == '$state.positionPlanArgs.application_id'
        capture = next(a['params']['positionPlanArgs'] for a in actions if a.get('on') == 'click:' + action_name and 'positionPlanArgs' in a.get('params', {}))
        assert capture['application_id'] == '$state.positionApplicationId'
        assert capture['action'] == '$state.positionRow.actions.' + action_name + '.arguments.action'
        assert capture['candidate_id'] == '$state.positionRow.actions.' + action_name + '.arguments.candidate_id'
        assert capture['expected_revision'] == '$state.positionLifecycleRevision'
        assert plan['idempotencyKeyStateKey'] == 'positionIntentKey'
        assert plan['idempotencyKey'] == 'auto'
        assert plan['resultStateKey'] == 'positionReview'
        assert ('positionWriteLossAcknowledged == true' in plan['enabledIf']) == (action_name in ('remove_beta', 'restore_stable'))
    switch = next(a for a in actions if a.get('on') == 'click:switch-runtime' and a.get('target') == 'applications.lifecycle.plan')
    assert 'acknowledge_write_loss' not in switch['params']
    assert switch['params']['target_position_id'] == '$state.positionPlanArgs.target_position_id'
    for target, capability in [('applications.lifecycle.apply', 'applications.apply'), ('applications.lifecycle.recover', None), ('applications.cancel_operation', None)]:
        action = next(a for a in actions if a.get('target') == target)
        assert action['params'] == {key: '$state.positionReview.operation.' + key for key in ('operation_id', 'plan_digest', 'idempotency_key')}
        assert 'positionApplicationId == $state.selectedApplicationId' in action['enabledIf']
        if capability:
            assert capability in action['enabledIf']


def test_uncertain_acknowledgement_never_reapplies_or_clears_capture():
    actions = APPLICATION['modals']['position-actions']['schema']['widgets'][-1]['actions']
    for name in ('confirm-apply',):
        steps = [a for a in actions if a['on'] == 'click:' + name]
        assert steps[0]['type'] == 'updateState' and steps[0]['params']['positionMayHaveRun'] is True
        assert '!$state.positionMayHaveRun' in steps[0]['enabledIf']
        assert steps[1]['type'] == 'callMcp'
    observe = next(a for a in actions if a.get('target') == 'applications.get_operation')
    assert observe['params'] == {'operation_id': '$state.positionReview.operation.operation_id'}
    recover = next(a for a in actions if a.get('target') == 'applications.lifecycle.recover')
    for status in ('unknown', 'applying', 'reconciling'):
        assert status in recover['enabledIf']
    for widget_id in ('applications-list', 'marketplace-list', 'developments-list'):
        for action in WIDGETS[widget_id]['actions']:
            if action.get('on') == 'select' and action.get('type') == 'updateState':
                assert 'positionMayHaveRun' not in action['params']
    assert '!$state.positionMayHaveRun' in WIDGETS['dashboard-positions']['actions'][0]['enabledIf']


def test_lifecycle_review_discloses_exact_core_plan_and_freshness():
    modal = APPLICATION['modals']['position-actions']['schema']['widgets']
    review = next(w for w in modal if w['id'] == 'position-review')
    assert review['dataSource']['value'] == '$state.positionReview.operation'
    assert {'plan.source_version', 'plan.target_version', 'plan.summary', 'plan.data_disposition', 'plan.affected_webspace_ids', 'status', 'recovery_reason'} <= {f['path'] for f in review['inputs']['fields']}
    widget = WIDGETS['dashboard-positions']
    assert widget['dataSource']['preserveLastValue'] is True
    assert {m['key'] for m in widget['inputs']['meta'] if m.get('kind') == 'date'} >= {'updated_at', 'available_at'}


def test_synthetic_review_requests_preserve_row_and_plan_identity():
    import jsonschema
    contracts = {c['id']: c for c in json.loads((Path(__file__).parent / 'fixtures/root_contracts.json').read_text(encoding='utf8'))}
    actions = APPLICATION['modals']['position-actions']['schema']['widgets'][-1]['actions']
    digest = 'sha256:' + 'a' * 64
    revision = 'sha256:' + 'b' * 64
    state = {'positionApplicationId': 'app', 'dashboardLifecycleRevision': revision, 'positionLifecycleRevision': revision,
             'positionWriteLossAcknowledged': True, 'positionRow': {'actions': {
                 'finalize': {'arguments': {'action': 'promote_stable', 'candidate_id': 'candidate-48'}},
                 'remove_beta': {'arguments': {'action': 'deactivate_beta', 'candidate_id': 'candidate-48'}},
                 'open': {'arguments': {'action': 'select_runtime', 'target_position_id': 'private_stable', 'release_digest': digest}}}},
             'positionReview': {'operation': {'operation_id': 'applifecycle.fixture', 'plan_digest': digest, 'idempotency_key': 'fixture-intent'}}}
    def resolve(value):
        if isinstance(value, str) and value.startswith('$state.'):
            obj = state
            for part in value.split('.')[1:]:
                obj = obj[part]
            return obj
        return value
    for event in ('remove_beta', 'switch-runtime'):
        action = next(a for a in actions if a.get('on') == 'click:' + event and a.get('target') == 'applications.lifecycle.plan')
        capture = next(a['params']['positionPlanArgs'] for a in actions if a.get('on') == 'click:' + event and 'positionPlanArgs' in a.get('params', {}))
        state['positionPlanArgs'] = {k: resolve(v) for k, v in capture.items()}
        args = {k: resolve(v) for k, v in action['params'].items()}
        args['idempotency_key'] = 'fixture-intent'
        jsonschema.validate(args, contracts[action['target']]['input_schema'])
        assert args['application_id'] == 'app' and args['expected_revision'] == revision
        assert ('acknowledge_write_loss' in args) == (event == 'remove_beta')
    for target in ('applications.lifecycle.apply', 'applications.lifecycle.recover', 'applications.cancel_operation'):
        action = next(a for a in actions if a.get('target') == target)
        args = {k: resolve(v) for k, v in action['params'].items()}
        jsonschema.validate(args, contracts[target]['input_schema'])
        assert args == {'operation_id': 'applifecycle.fixture', 'plan_digest': digest, 'idempotency_key': 'fixture-intent'}


def test_foreign_release_switch_uses_core_review_and_exact_channel():
    actions = APPLICATION['modals']['position-actions']['schema']['widgets'][-1]['actions']
    plan = next(a for a in actions if a.get('on') == 'click:switch-public' and a.get('target') == 'applications.plan')
    capture = next(a['params']['positionPlanArgs'] for a in actions if a.get('on') == 'click:switch-public' and 'positionPlanArgs' in a.get('params', {}))
    for key in ('release_digest', 'update_track', 'kind'):
        assert capture[key] == '$state.positionRow.actions.open.arguments.' + key
        assert plan['params'][key] == '$state.positionPlanArgs.' + key
    confirm = next(a for a in actions if a.get('on') == 'click:confirm-install-position' and a.get('target') == 'applications.apply')
    assert confirm['params'] == {key: '$state.positionReview.operation.' + key for key in ('operation_id', 'plan_digest', 'idempotency_key')}
    assert 'positionPermissionApproved' in confirm['enabledIf']
    navigation = next(a for a in actions if a.get('on') == 'click:refresh-result' and a.get('type') == 'openWorkspace')
    assert 'positionObservation.application.dashboard.active_position_id == $state.positionRow.id' in navigation['enabledIf']
    assert "status == 'succeeded'" in navigation['enabledIf']


def test_lost_plan_acknowledgement_replays_only_captured_plan_and_key():
    actions = APPLICATION['modals']['position-actions']['schema']['widgets'][-1]['actions']
    plans = [a for a in actions if a.get('target') == 'applications.lifecycle.plan' and not a['on'].startswith('click:retry-plan-')]
    for plan in plans:
        event = plan['on'].split(':', 1)[1]
        steps = [a for a in actions if a['on'] == plan['on']]
        capture = next(a for a in steps if a['type'] == 'updateState' and a.get('params', {}).get('positionPlanning') is True)
        assert '!$state.positionPlanning' in capture['enabledIf']
        assert plan['idempotencyKeyStateKey'] == 'positionIntentKey'
        retry = next(a for a in actions if a.get('on') == 'click:retry-plan-' + event and a.get('target') == 'applications.lifecycle.plan')
        assert retry['params'] == plan['params']
        assert retry['idempotencyKeyStateKey'] == plan['idempotencyKeyStateKey']
        assert 'positionPlanning == true' in retry['enabledIf']
    cancel = [a for a in actions if a['on'] == 'click:cancel-plan']
    assert cancel[0]['params']['positionMayHaveRun'] is True
    assert cancel[1]['target'] == 'applications.cancel_operation'
    assert '!$state.positionPlanning' in WIDGETS['dashboard-positions']['actions'][0]['enabledIf']


def test_public_switch_plan_retains_exact_intent_for_recovery():
    menu = next(w for w in APPLICATION['modals']['position-actions']['schema']['widgets'] if w['id'] == 'position-menu-actions')
    plan = next(a for a in menu['actions'] if a.get('on') == 'click:switch-public' and a.get('target') == 'applications.plan')
    retry = next(a for a in menu['actions'] if a.get('on') == 'click:retry-plan-switch-public' and a.get('target') == 'applications.plan')
    assert plan['idempotencyKeyStateKey'] == retry['idempotencyKeyStateKey'] == 'positionIntentKey'
    assert plan['params'] == retry['params']
    assert all(v.startswith('$state.positionPlanArgs.') for v in plan['params'].values())


def test_release_notes_are_exact_and_missing_is_explicit():
    widgets = APPLICATION['modals']['release-notes']['schema']['widgets']
    assert len(widgets) == 1
    listing = widgets[0]
    assert listing['dataSource']['toolId'] == 'applications.list_releases'
    assert listing['dataSource']['arguments']['view'] == 'summary'
    assert listing['inputs']['filters'] == [{'key': 'release_digest', 'stateKey': 'positionRow.release_digest'}]
    assert listing['inputs']['selectionMode'] == 'none'
    assert {m['key'] for m in listing['inputs']['meta']} == {'release_notes', 'notes_status'}
    assert next(m for m in listing['inputs']['meta'] if m['key'] == 'notes_status')['valueI18nPrefix'] == 'dashboard.release.state.'

def test_position_review_shows_permission_and_data_implications():
    widgets = APPLICATION['modals']['position-actions']['schema']['widgets']
    review = next(w for w in widgets if w['id'] == 'position-review')
    paths = {field['path'] for field in review['inputs']['fields']}
    assert {'plan.review_summary', 'plan.components', 'plan.conflicts', 'plan.compatibility',
            'plan.snapshot', 'plan.access', 'plan.migration_required'} <= paths
    permissions = next(w for w in widgets if w['id'] == 'position-permission-review')
    assert permissions['dataSource']['value'] == '$state.positionReview.operation.plan.permission_review.items'
    assert permissions['visibleIf'] == '$state.positionReview.operation.plan.permission_review.items'


def test_public_release_selector_uses_authoritative_public_positions():
    widgets = APPLICATION['modals']['position-actions']['schema']['widgets']
    releases = [
        {'version': 'old', 'channels': [], 'public_stable_available': True, 'public_beta_available': False},
        {'version': 'private', 'channels': ['stable'], 'public_stable_available': False, 'public_beta_available': False},
        {'version': 'beta', 'channels': ['prerelease'], 'public_stable_available': False, 'public_beta_available': True},
    ]
    for position, flag, expected, track in [('public_stable', 'public_stable_available', ['old'], 'stable'), ('public_beta', 'public_beta_available', ['beta'], 'prerelease')]:
        widget = next(w for w in widgets if w['id'] == 'select-' + position + '-releases')
        assert widget['dataSource']['toolId'] == 'applications.list_releases'
        assert widget['dataSource']['resultPath'] == 'response.result.releases'
        assert widget['inputs']['filters'] == [{'key': flag, 'value': True, 'operator': 'equals'}]
        assert [r['version'] for r in releases if r[flag] is True] == expected
        select = widget['actions'][0]
        assert '$event.' + flag + ' == true' in select['enabledIf']
        assert select['params']['selectedPublicRelease']['release_digest'] == '$event.release_digest'
        assert select['params']['selectedPublicRelease']['kind'] == '$event.install_kind'
        assert select['params']['selectedPublicRelease']['update_track'] == track
        assert all(a['type'] != 'callMcp' for a in widget['actions'])


def test_release_review_freezes_intent_and_recovers_same_plan():
    import jsonschema
    actions = APPLICATION['modals']['position-actions']['schema']['widgets'][-1]['actions']
    steps = [a for a in actions if a.get('on') == 'click:review-selected-release']
    capture, plan = steps[:2]
    assert capture['type'] == 'updateState' and capture['params']['positionPlanning'] is True
    assert capture['params']['positionPlanArgs'] == {
        'application_id': '$state.positionApplicationId', 'expected_revision': '$state.installationRevision',
        'kind': '$state.selectedPublicRelease.kind', 'release_digest': '$state.selectedPublicRelease.release_digest',
        'update_track': '$state.selectedPublicRelease.update_track'}
    assert plan['target'] == 'applications.plan' and plan['idempotencyKeyStateKey'] == 'positionIntentKey'
    retry = next(a for a in actions if a.get('on') == 'click:retry-plan-select-release' and a.get('target') == 'applications.plan')
    assert retry['params'] == plan['params'] and retry['idempotencyKeyStateKey'] == plan['idempotencyKeyStateKey']
    contracts = {c['id']: c for c in json.loads((Path(__file__).parent / 'fixtures/root_contracts.json').read_text(encoding='utf8'))}
    args = {'application_id': 'app', 'expected_revision': 0, 'kind': 'install', 'release_digest': 'sha256:' + 'a' * 64, 'update_track': 'prerelease', 'idempotency_key': 'same-intent'}
    jsonschema.validate(args, contracts['applications.plan']['input_schema'])
    for kind in ('install', 'update'):
        args['kind'] = kind
        jsonschema.validate(args, contracts['applications.plan']['input_schema'])
    confirm = next(a for a in actions if a.get('on') == 'click:confirm-install-position' and a.get('target') == 'applications.apply')
    for key in ('release_digest', 'kind', 'idempotency_key', 'expected_revision'):
        assert key in confirm['enabledIf']
    assert 'positionPermissionApproved' in confirm['enabledIf']
    assert confirm['params'] == {k: '$state.positionReview.operation.' + k for k in ('operation_id', 'plan_digest', 'idempotency_key')}
    cancel = next(a for a in actions if a.get('target') == 'applications.cancel_operation')
    assert cancel['params'] == confirm['params']
    assert next(a for a in actions if a.get('target') == 'applications.get_operation')['params'] == {'operation_id': '$state.positionReview.operation.operation_id'}
    assert all('retry' not in a.get('on','') for a in actions if a.get('target') == 'applications.apply')
    review = next(w for w in APPLICATION['modals']['position-actions']['schema']['widgets'] if w['id'] == 'position-review')
    assert {'plan.compatibility.migration.required', 'plan.compatibility.migration.count', 'plan.compatibility.migration.rollback_mode', 'plan.snapshot', 'plan.access', 'plan.runtime_target', 'plan.removal', 'plan.permissions'} <= {f['path'] for f in review['inputs']['fields']}


def test_event_state_writes_are_unique_and_plans_keep_uncertain_intent():
    for widget in list(WIDGETS.values()) + APPLICATION['modals']['position-actions']['schema']['widgets']:
        seen = set()
        for action in widget.get('actions', []):
            if action.get('type') != 'updateState':
                continue
            for key in action.get('params', {}):
                pair = (action.get('on'), key)
                assert pair not in seen, (widget['id'], pair)
                seen.add(pair)
    menu = next(w for w in APPLICATION['modals']['position-actions']['schema']['widgets'] if w['id'] == 'position-menu-actions')
    for event in ('remove_beta', 'renew_beta', 'restore_stable', 'switch-runtime', 'update', 'switch-public', 'switch-runtime-candidate', 'review-selected-release'):
        steps = [a for a in menu['actions'] if a.get('on') == 'click:' + event]
        capture = next(i for i, a in enumerate(steps) if a.get('params', {}).get('positionPlanning') is True)
        plan = next(i for i, a in enumerate(steps) if a.get('target') in ('applications.plan', 'applications.lifecycle.plan'))
        assert capture < plan
        assert all(a.get('params', {}).get('positionPlanning') is not False for a in steps)


def test_plan_recovery_keeps_frozen_intent_after_another_unknown_response():
    menu = next(w for w in APPLICATION['modals']['position-actions']['schema']['widgets'] if w['id'] == 'position-menu-actions')
    retry = [a for a in menu['actions'] if a.get('on', '').startswith('click:retry-plan-')]
    assert retry and all(a['type'] == 'callMcp' for a in retry)
    assert all(a['idempotencyKeyStateKey'] == 'positionIntentKey' for a in retry)


def test_publication_requires_explicit_confirmation_and_retains_exact_intent():
    modal = APPLICATION['modals']['publication-actions']['schema']['widgets']
    controls = next(w for w in modal if w['id'] == 'publication-controls')
    assert PAGE['initialState']['publicationConfirmed'] is False
    assert PAGE['initialState']['publicationIntentKey'] == ''
    for kind, tool in [('stable', 'publish_to_registry'), ('beta', 'publish_prerelease')]:
        steps = [a for a in controls['actions'] if a['on'] == 'click:publish-' + kind]
        assert steps[0]['params']['publicationMayHaveRun'] is True
        assert steps[0]['params']['publicationSubmittedNotes'] == '$state.publicationNotesDraft'
        call = steps[1]
        assert call['target'] == 'applications.development.' + tool
        assert 'publicationConfirmed == true' in call['enabledIf']
        assert call['idempotencyKey'] == 'auto'
        assert call['idempotencyKeyStateKey'] == 'publicationIntentKey'
        assert call['resultStateKey'] == 'publicationResult'
        assert call['errorStateKey'] == 'publicationError'
        assert call['params']['application_id'] == '$state.publicationContext.application_id'
        assert call['params']['expected_revision'] == '$state.publicationContext.expected_revision'
        if kind == 'beta':
            assert set(call['params']) == {'application_id', 'expected_revision', 'candidate_id', 'expected_prerelease_digest'}
        else:
            assert call['params']['release_digest'] == '$state.publicationContext.arguments.release_digest'
            assert call['params']['release_notes'] == '$state.publicationSubmittedNotes'
        assert not call.get('invalidates')
    notes = next(w for w in modal if w['id'] == 'publication-notes')
    assert notes['inputs']['resetOnSuccess'] is False
    assert notes['inputs']['fields'][0]['validation']['maxLength'] == 20000
    assert "kind != 'publish_beta'" in notes['visibleIf']


def test_publication_recovery_observes_only_the_same_development_operation():
    modal = APPLICATION['modals']['publication-actions']['schema']['widgets']
    controls = next(w for w in modal if w['id'] == 'publication-controls')
    by_event = {a['on']: a for a in controls['actions'] if a.get('type') == 'callMcp'}
    assert by_event['click:check-request']['target'] == 'applications.development.list_operations'
    for event,tool in [('observe','get_operation'),('recover','reconcile_operation')]:
        call=by_event['click:'+event]
        assert call['target']=='applications.development.'+tool
        assert call['params']=={'operation_id':'$state.publicationResult.operation_id'}
        assert call['followups'][0]['params']['publicationResult']=='$state.publicationRead.operation'
        assert 'idempotency_key == $state.publicationIntentKey' in call['enabledIf']
        assert 'application_id == $state.publicationContext.application_id' in call['enabledIf']
        assert 'intent.candidate_id' in call['enabledIf'] and 'intent.release_digest' in call['enabledIf']
    refresh=by_event['click:refresh-dashboard']
    assert "status == 'succeeded'" in refresh['enabledIf']
    assert refresh['target']=='applications.show'
    journal=next(w for w in modal if w['id']=='publication-request-journal')
    assert {f['key'] for f in journal['inputs']['filters']}=={'application_id','idempotency_key','action'}
    assert 'publicationMayHaveRun' in WIDGETS['resume-publication']['visibleIf']
    close=next(a for a in controls['actions'] if a['on']=='click:close')
    assert close['type']=='closeModal'


def test_publication_eligibility_and_arguments_come_from_the_captured_core_position():
    menu=next(w for w in APPLICATION['modals']['position-actions']['schema']['widgets'] if w['id']=='position-menu-actions')
    for name in ('publish_initial_stable','publish_beta','promote_public_stable'):
        steps=[a for a in menu['actions'] if a['on']=='click:'+name]
        assert [a['type'] for a in steps]==['updateState','navigateModal']
        context=steps[0]['params']['publicationContext']
        assert context['arguments']=='$state.positionRow.actions.'+name+'.arguments'
        assert context['expected_revision']=='$state.applicationDefinitionRevision'
        assert steps[0]['params']['publicationNotesDraft']==''
        assert 'actions.'+name+'.eligible == true' in steps[0]['enabledIf']
        assert all(a['type']!='callMcp' for a in steps)

def test_lazy_dashboard_readme_and_toolbar_contract():
    source = WIDGETS['application-header']['dataSource']
    for name in ('application-details', 'dashboard-positions'):
        other = WIDGETS[name]['dataSource']
        assert other['arguments'] == source['arguments']
    assert not any(w.get('dataSource', {}).get('toolId') == 'applications.assess_updates' for w in WIDGETS.values())
    readme = WIDGETS['dashboard-readme']
    assert readme['type'] == 'static.markdown' and '$state.detailReadme' in readme['visibleIf']
    assert WIDGETS['application-details']['inputs']['stateBindings']['detailReadme']['path'] == 'readme'
    assert WIDGETS['dashboard-positions']['inputs']['actionsPresentation'] == 'autoOverflow'
    assert WIDGETS['dashboard-positions']['inputs']['compactIconOnly'] is True
    for name in ('update', 'select-release', 'release-notes'):
        capture = next(a for a in WIDGETS['dashboard-positions']['actions'] if a['on'] == 'click:' + name and a.get('params', {}).get('positionRow'))
        assert capture['params']['positionRow']['release_digest'] == '$event.release_digest'
        assert 'positionIntentKey' in capture['enabledIf']


def test_operation_feedback_and_commands_use_authoritative_dashboard_state():
    positions = WIDGETS['dashboard-positions']
    assert positions['inputs']['currentKey'] == 'active'
    assert not any(a.get('enabledIf') == 'false' for a in positions['actions'] if a['on'] in ('click:position-open', 'click:position-switch-open'))
    assert not {'release-preferences','dashboard-home'} & WIDGETS.keys()
    toolbar = WIDGETS['shared-dev-tickets']
    assert {'dev-tickets','manage-home','use-prerelease','disable-auto-update'} <= {b['id'] for b in toolbar['inputs']['buttons']}
    progress = WIDGETS['finalize-dashboard-progress']
    assert progress['type'] == 'feedback.taskProgress'
    assert progress['dataSource']['value'] == '$state.dashboardOperationTask'
    ordered = [w['id'] for w in APPLICATION['desktop']['pageSchema']['widgets']]
    assert ordered.index('finalize-dashboard-progress') < ordered.index('dashboard-positions')


def test_first_paint_reads_are_compact_and_details_are_separate():
    for identity in ('applications-list', 'marketplace-list', 'developments-list'):
        assert WIDGETS[identity]['dataSource']['arguments']['view'] == 'summary'
    assert WIDGETS['application-details']['dataSource']['arguments']['view'] == 'dashboard'


def test_subscription_captures_application_and_revision_before_planning():
    actions = WIDGETS['shared-dev-tickets']['actions']
    for command in ('use-prerelease', 'use-stable'):
        steps = [a for a in actions if a['on'] == 'click:' + command]
        capture = next(a for a in steps if a['type'] == 'updateState')
        intent = capture['params']['subscriptionIntent']
        assert intent['application_id'] == '$state.selectedApplicationId'
        assert intent['revision'] == '$state.subscriptionRevision'
        plan = next(a for a in steps if a.get('target') == 'applications.plan')
        assert plan['params']['application_id'] == '$state.subscriptionIntent.application_id'
        assert plan['params']['expected_revision'] == '$state.subscriptionIntent.revision'
        assert 'subscriptionIntent.application_id' not in plan['enabledIf']
        assert 'positionIntentKey' in plan['enabledIf']


def test_release_picker_uses_core_eligibility_and_review_has_no_generic_commands():
    button = next(b for b in WIDGETS['dashboard-positions']['inputs']['buttons'] if b['id'] == 'select-release')
    assert button['whenKey'] == 'actions.select_release.eligible' and button['whenEquals'] is True
    menu = next(w for w in APPLICATION['modals']['position-actions']['schema']['widgets'] if w['id'] == 'position-menu-actions')
    assert not {'preview', 'builder', 'dev-tickets', 'open', 'release-notes'} & {b['id'] for b in menu['inputs']['buttons']}
    assert all('applicationInstalled == true' in b.get('visibleIf','') for b in WIDGETS['shared-dev-tickets']['inputs']['buttons'] if b['id'] != 'dev-tickets')
    assert 'localBetaActive' not in WIDGETS['shared-dev-tickets']['visibleIf']
