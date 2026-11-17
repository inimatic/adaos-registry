import json
from pathlib import Path
D=json.loads((Path(__file__).resolve().parents[1]/'webui.json').read_text(encoding='utf-8'))
APP=D['ui']['application']
P=APP['desktop']['pageSchema']
W={w['id']:w for w in P['widgets']}

def test_details_never_owns_version_or_duplicate_pin_commands():
    ids={b['id'] for b in W['lifecycle-actions']['inputs']['buttons']}
    assert ids=={'configure','manage-placement','manage-access','remove','delete-development'}
    assert not {'update','manage-home','open','open-installed'} & ids
    assert sum(b['id']=='manage-home' for b in W['shared-dev-tickets']['inputs']['buttons'])==1

def test_runtime_diagnostics_depend_on_observed_health_not_settings_plans():
    visible=W['dashboard-runtime-actions']['visibleIf']
    assert 'dashboardDiagnostic' in visible and "dashboardSection == 'dashboard'" in visible
    assert 'dashboardLatestOperation.status' not in visible
    command=next(b for b in W['shared-dev-tickets']['inputs']['buttons'] if b['id']=='review-settings')
    assert "kind == 'select_track'" in command['visibleIf']
    assert 'planned' in command['visibleIf']

def test_public_update_captures_exact_core_identity_before_one_native_plan_request():
    steps=[a for a in W['dashboard-positions']['actions'] if a['on']=='click:update']
    requests=[a for a in steps if a.get('target')=='applications.plan']
    assert len(requests)==1
    capture=next(a for a in steps if a.get('params',{}).get('positionPlanIntent')=='update-public')
    args=capture['params']['positionPlanArgs']
    assert args['release_digest']=='$state.positionRow.actions.update.arguments.release_digest'
    assert args['update_track']=='$state.positionRow.actions.update.arguments.update_track'
    assert args['expected_revision']=='$state.installationRevision'
    assert steps.index(capture)<steps.index(requests[0])
    assert requests[0]['idempotencyKeyStateKey']=='positionIntentKey'
    assert requests[0]['pendingStateKey']=='positionRequestPending'
    menu=next(w for w in APP['modals']['position-actions']['schema']['widgets'] if w['id']=='position-menu-actions')
    confirm=next(b for b in menu['inputs']['buttons'] if b['id']=='confirm-install-position')
    assert "positionPlanIntent != 'update-public'" in confirm['enabledIf']
    assert 'plan.release_digest == $state.positionPlanArgs.release_digest' in confirm['enabledIf']

def test_preferences_open_before_request_and_cancel_the_durable_plan():
    toolbar=W['shared-dev-tickets']
    for name in ('disable-auto-update','enable-auto-update','use-prerelease','use-stable'):
        steps=[a for a in toolbar['actions'] if a['on']=='click:'+name]
        assert [a['type'] for a in steps]==['updateState','openModal','callMcp']
        assert steps[-1]['pendingStateKey']=='preferencesRequestPending'
    modal=APP['modals']['review-lifecycle-operation']['schema']
    commands=next(w for w in modal['widgets'] if w['id']=='modal-review-actions')
    cancel=[a for a in commands['actions'] if a['on']=='click:cancel-review']
    assert [a['type'] for a in cancel]==['callMcp','closeModal']
    assert cancel[0]['target']=='applications.cancel_operation'
    assert cancel[0]['params']['operation_id']=='$state.reviewedPlan.operation.operation_id'
    assert "status == 'cancelled'" in cancel[1]['enabledIf']
    assert not any(a['type']=='updateState' for a in cancel)
    assert any(w['id']=='preferences-review' for w in modal['widgets'])
