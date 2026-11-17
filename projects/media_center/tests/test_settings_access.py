"""Release-profile and native authorization regressions for Media Center settings."""
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from adaos.domain.application_access import (
    ApplicationAccessGrant, ApplicationPermissionProfile, evaluate_application_access,
    is_high_risk_permission,
)
from adaos.services.applications.access_management import ApplicationAccessManagementService
from adaos.services.builder.shared_delivery import owned_skill_effect_checks

ROOT = Path(__file__).resolve().parents[3]
PROJECT = yaml.safe_load((ROOT / 'projects/media_center/project.yaml').read_text(encoding='utf-8'))
PROFILE = ApplicationPermissionProfile.from_mapping(PROJECT['permission_profile'])
MANIFESTS = {name: yaml.safe_load((ROOT / f'skills/{name}/skill.yaml').read_text(encoding='utf-8'))
             for name in ('media_center_skill', 'media_control_skill', 'media_library_agent')}


def grant(kind='owner', *, ceiling=None, **constraints):
    return ApplicationAccessGrant(grant_id=f'fixture:{kind}', subject_ref=f'user:{kind}',
        application_id='media_center', application_roles=(),
        permission_ceiling=tuple(PROFILE.flat_permissions if ceiling is None else ceiling),
        explicit_denies=(), constraints={'subject_kind': kind, **constraints},
        issuer_ref='user:owner', reviewed_permission_profile_digest=PROFILE.digest)


def decide(permission='workspace.read', *, subject=None, capabilities=None):
    return evaluate_application_access(profile=PROFILE, roles=(), grant=subject,
        permission_id=permission, app_capability=permission,
        component_capabilities=tuple(MANIFESTS['media_center_skill']['capabilities']
                                     if capabilities is None else capabilities),
        actor_chain={'application_id': 'media_center', 'subject_ref': 'user:fixture',
                     'component_ref': 'skill:media_center_skill', 'webspace_id': 'desktop'})


@pytest.mark.parametrize('tool', ['status', 'get_metadata_settings'])
def test_settings_reads_have_explicit_component_and_application_permission(tool):
    manifest = MANIFESTS['media_center_skill']
    declaration = next(item for item in manifest['tools'] if item['name'] == tool)
    permission, _ = ApplicationAccessManagementService.runtime_permission(
        side_effects=declaration['side_effects'],
        application_access=declaration.get('application_access', {}),
        component_capabilities=tuple(manifest['capabilities']))
    assert permission == 'workspace.read'
    assert permission in manifest['capabilities'] and permission in PROFILE.flat_permissions
    assert decide(permission, subject=grant()).decision == 'allow'


@pytest.mark.parametrize('kind,expected', [('owner', 'allow'), ('member', 'allow'),
                                        ('child', 'pending_action'), ('guest', 'allow')])
def test_settings_access_matrix_uses_native_subject_floors(kind, expected):
    assert decide(subject=grant(kind)).decision == expected


def test_child_requires_review_for_external_data_and_then_can_read():
    assert decide(subject=grant('child')).reason_code == 'guardian_approval_required'
    assert decide(subject=grant('child', guardian_approval_id='fixture:guardian')).decision == 'allow'


def test_missing_grant_uses_pending_action_and_revoked_grant_stays_denied():
    assert decide().reason_code == 'application_grant_missing'
    assert decide(subject=replace(grant(), status='revoked')).reason_code == 'grant_revoked'


def test_legacy_missing_component_declaration_is_not_repaired_by_owner_grant():
    capabilities = [item for item in MANIFESTS['media_center_skill']['capabilities'] if item != 'workspace.read']
    result = decide(subject=grant(), capabilities=capabilities)
    assert result.decision == 'deny' and result.reason_code == 'component_capability_missing'


@pytest.mark.parametrize('permission', ['secrets.read', 'secrets.write', 'workspace.write'])
def test_guest_cannot_inherit_sensitive_owner_settings_permissions(permission):
    result = decide(permission, subject=grant('guest'))
    assert result.decision == 'deny' and result.reason_code == 'guest_floor_denied'


def test_profile_discloses_high_risk_component_capabilities_and_optional_providers():
    inferred = {cap for manifest in MANIFESTS.values() for cap in manifest['capabilities']
                if is_high_risk_permission(cap)}
    assert inferred <= set(PROFILE.flat_permissions)
    for permission in ('secrets.read', 'secrets.write'):
        declaration = PROFILE.declaration_for(permission)
        assert declaration.approval_policy == 'owner_only' and declaration.sensitive
    assert {item['id'] for item in PROFILE.external_providers} == {'tmdb', 'musicbrainz'}
    assert any(item['id'] == 'tmdb_api_credential' and not item['required'] for item in PROFILE.secrets)
    assert PROFILE.data_practices['sent_off_device']


def test_all_owned_public_tool_effects_are_disclosed():
    checks, errors = owned_skill_effect_checks(project=PROJECT, manifests=MANIFESTS)
    assert errors == []
    assert len(checks) == 3 and all(item['ok'] for item in checks)
