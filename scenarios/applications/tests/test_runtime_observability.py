import json
from pathlib import Path
D=json.loads((Path(__file__).resolve().parents[1]/'webui.json').read_text(encoding='utf-8'))
P=D['ui']['application']['desktop']['pageSchema']
W={w['id']:w for w in P['widgets']}
def find(x,identity):
    if isinstance(x,dict):
        if x.get('id')==identity:return x
        for child in x.values():
            hit=find(child,identity)
            if hit:return hit
    elif isinstance(x,list):
        for child in x:
            hit=find(child,identity)
            if hit:return hit
def test_header_preserves_installed_and_selected_version_independently_of_public_position():
    fields={f['path'] for f in W['application-header']['inputs']['fields']}
    assert {'dashboard.runtime.installed_version','dashboard.runtime.selected_version'}<=fields
def test_operation_check_opens_progress_surface_before_transport_and_retains_receipt():
    actions=W['dashboard-runtime-actions']['actions']
    selected=[a for a in actions if a['on']=='click:operation']
    assert [a['type'] for a in selected]==['updateState','openModal','callMcp']
    assert selected[-1]['target']=='applications.get_operation'
    assert selected[-1]['pendingStateKey']=='positionRequestPending'
    assert selected[-1]['errorStateKey']=='positionError'
    assert find(D,'position-request-progress')['visibleIf']=='$state.positionRequestPending == true'
def test_blocked_plan_cannot_apply_and_remains_cancellable():
    menu=find(D,'position-menu-actions')
    for b in menu['inputs']['buttons']:
        if b['id'] in ('confirm-apply','confirm-install-position'):
            assert 'plan.conflicts.length > 0' in b['enabledIf']
            assert 'positionRequestPending' in b['enabledIf']
        if b['id']=='cancel-plan':assert b['visibleIf']=="$state.positionReview.operation.status == 'planned'"
    for a in menu['actions']:
        if a.get('target') in ('applications.apply','applications.lifecycle.apply'):
            assert 'plan.conflicts.length > 0' in a['enabledIf']
def test_repair_retry_uses_captured_arguments_and_retained_intent():
    menu=find(D,'position-menu-actions')
    retry=next(a for a in menu['actions'] if a.get('on')=='click:retry-plan-repair')
    assert retry['idempotencyKeyStateKey']=='positionIntentKey'
    assert retry['params']['release_digest']=='$state.positionPlanArgs.release_digest'
    assert 'positionApplicationId == $state.selectedApplicationId' in retry['enabledIf']
def test_same_digest_is_a_restoration_review_and_component_details_are_separate():
    review=find(D,'position-restore-review')
    assert 'plan.release_digest == $state.runtimeInstalledDigest' in review['visibleIf']
    assert not any(f['path']=='plan.source_version' for f in review['inputs']['fields'])
    assert "dashboardSection == 'details'" in W['dashboard-runtime-issues']['visibleIf']
