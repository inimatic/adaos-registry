"""Canonical Trial evidence for the trusted access boundary.

The scenario owns composition only. Root MCP remains the authority for people,
roles, grants, devices, sessions, invitations, application access, and audit.
"""
import json
from pathlib import Path
import unittest

import yaml


SCENARIO = Path(__file__).resolve().parents[1]
PROJECT = SCENARIO.parents[1] / "projects" / "users_access" / "project.yaml"


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


class ApplicationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = yaml.safe_load(PROJECT.read_text(encoding="utf-8"))
        cls.application = json.loads((SCENARIO / "webui.json").read_text(
            encoding="utf-8"))["ui"]["application"]
        cls.page = cls.application["desktop"]["pageSchema"]
        cls.widgets = {w["id"]: w for w in cls.page["widgets"]}

    def test_owner_governed_permissions_without_local_role_store(self):
        profile = self.project["permission_profile"]
        required = {p["id"]: p for p in profile["required"]}
        self.assertEqual(set(required), {
            "users_access.read",
            "users_access.invite",
            "users_access.manage",
        })
        for permission in ("users_access.invite", "users_access.manage"):
            self.assertEqual(required[permission]["approval_policy"], "owner_only")
        self.assertEqual(
            {p["id"] for p in profile["optional"]},
            {"applications.read", "applications.apply"},
        )
        self.assertFalse(self.project.get("application_roles"))
        self.assertEqual(profile["secrets"], [])
        self.assertEqual(profile["data_practices"]["collected"], ["access_metadata"])
        self.assertEqual(profile["data_practices"]["sent_off_device"], [])
        self.assertEqual(self.project["components"]["owned"][0]["ref"], "scenario:users_access")

    def test_all_access_operations_delegate_to_admitted_root_tools(self):
        mutations = {
            "users_access.create_invite": "users_access.invite",
            "users_access.create_device_pairing": "users_access.manage",
            "users_access.create_admin_recovery": "users_access.manage",
            "users_access.grant_role": "users_access.manage",
            "users_access.revoke_invite": "users_access.manage",
            "users_access.revoke_device": "users_access.manage",
            "users_access.revoke_session": "users_access.manage",
        }
        seen = []
        reads = []
        for node in objects(self.application):
            source = node.get("dataSource") if isinstance(node, dict) else None
            if source:
                if source["kind"] == "static":
                    self.assertIn("value", source)
                    continue
                self.assertEqual(source["kind"], "mcp")
                self.assertEqual(source["toolId"], "users_access.summary")
                self.assertNotIn("dryRun", source)
                self.assertNotIn("prototypeFixture", source)
                self.assertLessEqual(set(source["arguments"]), {"audit_limit", "sections", "detail"})
                reads.append(source["toolId"])
            actions = node.get("actions", []) if isinstance(node, dict) else []
            if isinstance(actions, str):
                self.assertEqual(actions, "adaptive")
                continue
            for action in actions:
                self.assertIn(
                    action["type"],
                    ("callMcp", "updateState", "openModal", "copyToClipboard", "openUrl"),
                )
                if action["type"] != "callMcp":
                    continue
                target = action["target"]
                self.assertIn(target, mutations)
                self.assertEqual(action["idempotencyKey"], "auto")
                self.assertEqual(action["invalidates"], ["users_access.summary"])
                self.assertTrue(set(action["params"]).isdisjoint(
                    {"caller", "actor", "permissions", "application_roles", "owner"}
                ))
                seen.append(target)
        self.assertTrue(reads)
        self.assertCountEqual(seen, mutations)

    def test_root_result_paths_match_admitted_summary_sections(self):
        contract_paths = {
            "people": "response.result.users_access.people",
            "guests": "response.result.users_access.guests",
            "children": "response.result.users_access.children",
            "subjects": "response.result.users_access.subjects",
            "devices": "response.result.users_access.devices",
            "sessions": "response.result.users_access.sessions",
            "application_access": "response.result.users_access.application_access",
            "permissions": "response.result.users_access.permissions",
            "invites": "response.result.administration.invites",
            "audit": "response.result.administration.audit",
        }
        self.assertEqual(set(contract_paths), {
            "people",
            "guests",
            "children",
            "subjects",
            "devices",
            "sessions",
            "application_access",
            "permissions",
            "invites",
            "audit",
        })
        expected = {
            "people": (["people"], contract_paths["people"]),
            "guests": (["people", "guests", "children"], contract_paths["guests"]),
            "children": (["people", "guests", "children"], contract_paths["children"]),
            "invitations": (["invites"], contract_paths["invites"]),
            "devices": (["devices"], contract_paths["devices"]),
            "sessions": (["sessions"], contract_paths["sessions"]),
            "application-access": (["application_access"], contract_paths["application_access"]),
            "activity": (["audit"], contract_paths["audit"]),
        }
        for widget_id, (sections, result_path) in expected.items():
            source = self.widgets[widget_id]["dataSource"]
            self.assertEqual(source["arguments"]["sections"], sections)
            self.assertEqual(source["resultPath"], result_path)

        serialized = json.dumps(self.application, sort_keys=True)
        for stale_path in (
            "result.people",
            "result.guests",
            "result.children",
            "result.devices",
            "result.sessions",
            "result.permissions",
            "result.invites",
            "result.audit",
        ):
            self.assertNotIn(stale_path, serialized)

    def test_application_access_keeps_person_selection_reachable(self):
        self.assertIn(
            "$state.activeSection == 'applications'",
            self.widgets["people"]["visibleIf"],
        )
        self.assertEqual(
            self.widgets["application-access"]["inputs"]["filters"],
            [{"key": "subject_ref", "stateKey": "selectedSubjectId"}],
        )

    def test_access_forms_resolve_scope_ids_from_root(self):
        expected = {
            "grant-role": "grantScopeKind",
            "create-invite": "inviteScopeKind",
        }
        for widget_id, state_key in expected.items():
            fields = {
                item["id"]: item
                for item in self.widgets[widget_id]["inputs"]["fields"]
            }
            self.assertEqual(fields["scope_kind"]["stateKey"], state_key)
            self.assertEqual(fields["scope_id"]["type"], "dropdown")
            source = fields["scope_id"]["optionsDataSource"]
            self.assertEqual(source["toolId"], "users_access.scope_options")
            self.assertEqual(
                source["arguments"]["scope_kind"], f"$state.{state_key}"
            )

    def test_initial_state_contains_no_fixture_collections_or_local_access_store(self):
        state = self.page["initialState"]
        self.assertEqual(state["activeSection"], "people")
        for forbidden in ("people", "devices", "sessions", "permissions", "roles", "grants"):
            self.assertNotIn(forbidden, state)
        self.assertEqual(state["selectedPerson"], {})
        self.assertEqual(state["selectedPermission"], {})
        self.assertEqual(state["selectedPermissionId"], "")
        self.assertNotIn("stores", self.application)
        self.assertNotIn("dataStores", self.application)

    def test_resources_are_bilingual_and_declared_as_core_i18n(self):
        resources = self.application["resources"]
        self.assertEqual(set(resources), {"users_access.i18n.en", "users_access.i18n.ru"})
        dictionaries = {}
        for locale in ("en", "ru"):
            resource = resources["users_access.i18n." + locale]
            self.assertEqual(resource["path"], "assets/i18n/" + locale + ".json")
            self.assertEqual(resource["role"], "i18n")
            dictionaries[locale] = json.loads((SCENARIO / resource["path"]).read_text(
                encoding="utf-8"))
        self.assertEqual(set(dictionaries["en"]), set(dictionaries["ru"]))

    def test_people_surfaces_use_canonical_avatar_keys_and_localized_detail_region(self):
        detail_region = next(
            region for region in self.page["layout"]["regions"] if region["id"] == "inspector"
        )
        self.assertEqual(detail_region["label"], "Details")
        self.assertEqual(
            detail_region["label_i18n"],
            {"key": "detail.region.label", "fallback": "Details"},
        )

        for widget_id in ("people", "guests", "children", "person-detail"):
            inputs = self.widgets[widget_id]["inputs"]
            self.assertNotIn("avatar", inputs)
            self.assertEqual(inputs["imageKey"], "profile.avatar_url")
            self.assertEqual(inputs["imageShape"], "avatar")
            self.assertEqual(inputs["initialsKey"], "initials")

        for node in objects(self.application):
            if isinstance(node, dict) and "avatar" in node:
                self.fail("Nested avatar declarations are unsupported in adaos.webui.v1")

    def test_activity_can_link_related_actor_back_to_person_detail(self):
        action = self.widgets["activity"]["actions"][0]
        self.assertEqual(action["on"], "select")
        self.assertEqual(action["type"], "updateState")
        self.assertEqual(action["params"]["activeSection"], "people")
        self.assertEqual(action["params"]["selectedSubjectId"], "$event.actor_ref")
        self.assertEqual(action["params"]["selectedPerson"], "$event.actor")


if __name__ == "__main__":
    unittest.main()
