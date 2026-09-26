"""Hermetic Users & Access scenario composition checks.

These tests verify the admitted WebUI wiring and Project contract. Live Root
authorization, redaction, and browser rendering are validated by Builder gates.
"""
import json
from pathlib import Path
import unittest

import yaml


SCENARIO = Path(__file__).resolve().parents[1]
PROJECT = SCENARIO.parents[1] / "projects" / "users_access" / "project.yaml"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


class AutomationCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.webui = read_json(SCENARIO / "webui.json")
        cls.application = cls.webui["ui"]["application"]
        cls.page = cls.application["desktop"]["pageSchema"]
        cls.widgets = {w["id"]: w for w in cls.page["widgets"]}
        cls.project = yaml.safe_load(PROJECT.read_text(encoding="utf-8"))

    def data_sources(self):
        for node in walk(self.page):
            source = node.get("dataSource") if isinstance(node, dict) else None
            if source:
                yield source

    def call_actions(self):
        for node in walk(self.page):
            actions = node.get("actions", []) if isinstance(node, dict) else []
            if isinstance(actions, list):
                for action in actions:
                    if action.get("type") == "callMcp":
                        yield action

    def test_single_ui_authority_and_permission_profile_preserved(self):
        for name in ("scenario.json", "scenario.yaml"):
            text = (SCENARIO / name).read_text(encoding="utf-8")
            manifest = json.loads(text) if name.endswith(".json") else yaml.safe_load(text)
            self.assertEqual(manifest["ui"]["manifest"], "webui.json")
            self.assertNotIn("application", manifest["ui"])
            self.assertEqual(manifest["runtime"]["skills"], {"required": [], "optional": []})
            self.assertEqual(manifest["steps"], [])

        profile = self.project["permission_profile"]
        self.assertEqual(
            {p["id"] for p in profile["required"]},
            {"users_access.read", "users_access.manage", "users_access.invite"},
        )
        self.assertEqual(
            {p["id"] for p in profile["optional"]},
            {"applications.read", "applications.apply"},
        )
        self.assertFalse(self.project.get("application_roles"))
        self.assertEqual(self.project["components"]["dependencies"], [])

    def test_reads_use_authoritative_root_summary_without_prototype_leakage(self):
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
        expected_paths = {
            "people": contract_paths["people"],
            "guests": contract_paths["guests"],
            "children": contract_paths["children"],
            "invitations": contract_paths["invites"],
            "devices": contract_paths["devices"],
            "sessions": contract_paths["sessions"],
            "application-access": contract_paths["application_access"],
            "activity": contract_paths["audit"],
        }
        self.assertEqual(contract_paths["subjects"], "response.result.users_access.subjects")
        self.assertEqual(
            contract_paths["application_access"],
            "response.result.users_access.application_access",
        )
        self.assertEqual(
            {wid for wid, widget in self.widgets.items()
             if widget.get("dataSource", {}).get("kind") == "mcp"},
            set(expected_paths),
        )
        for widget_id, result_path in expected_paths.items():
            source = self.widgets[widget_id]["dataSource"]
            self.assertEqual(source["kind"], "mcp")
            self.assertEqual(source["toolId"], "users_access.summary")
            self.assertEqual(source["resultPath"], result_path)
            self.assertNotIn("dryRun", source)
            self.assertNotIn("prototypeFixture", source)
            self.assertLessEqual(set(source["arguments"]), {"audit_limit", "sections", "detail"})
            self.assertEqual(source["honestStates"]["retry"], "explicit")

        serialized = json.dumps(self.page, sort_keys=True)
        for forbidden in (
            "result.people",
            "result.guests",
            "result.children",
            "result.devices",
            "result.sessions",
            "result.permissions",
            "result.invites",
            "result.audit",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("initialStateFixture", serialized)
        self.assertNotIn("synthetic", serialized.lower())

    def test_item_details_have_resolvable_static_sources(self):
        expected_sources = {
            "person-detail": "$state.selectedPerson",
            "invitation-result": "$state.lastInvitation.invite",
            "device-pairing-result": "$state.lastDevicePairing.invite",
            "permission-detail": "$state.selectedPermission",
            "last-mutation": "$state.lastAccessMutation",
        }
        for widget_id, value in expected_sources.items():
            widget = self.widgets[widget_id]
            self.assertEqual(widget["type"], "item.details")
            self.assertEqual(widget["dataSource"], {
                "kind": "static",
                "value": value,
            })
            self.assertEqual(widget["inputs"]["source"], value)

    def test_people_workbench_uses_compact_open_detail_layout(self):
        toolbar_widgets = [w for w in self.page["widgets"] if w.get("area") == "toolbar"]
        self.assertEqual([w["id"] for w in toolbar_widgets], ["sections"])
        tabs = self.widgets["sections"]["inputs"]["buttons"]
        self.assertEqual(
            [tab["id"] for tab in tabs],
            ["people", "invitations", "devices", "sessions", "applications", "activity"],
        )
        section_navigation = self.widgets["sections"]["actions"][0]
        self.assertEqual(section_navigation["params"]["activeAccessModal"], "")

        layout = self.page["layout"]
        detail_region = next(region for region in layout["regions"] if region["id"] == "inspector")
        self.assertEqual(detail_region["role"], "detail")
        self.assertEqual(detail_region["label"], "Details")
        self.assertEqual(
            detail_region["label_i18n"],
            {"key": "detail.region.label", "fallback": "Details"},
        )
        self.assertEqual(layout["interaction"]["rowActivation"], "open-detail")
        self.assertEqual(layout["interaction"]["detail"], "sheet")
        self.assertIn("$state.selectedSubjectId", self.widgets["role-guide"]["visibleIf"])

    def test_people_master_detail_is_person_centric_and_bounded(self):
        people = self.widgets["people"]
        self.assertEqual(people["dataSource"]["arguments"]["detail"], "compact")
        self.assertEqual(people["dataSource"]["arguments"]["sections"], ["people"])
        self.assertEqual(people["inputs"]["titleKey"], "display_label")
        self.assertEqual(people["inputs"]["subtitleKey"], "primary_role")
        self.assertEqual(people["inputs"]["subtitleFallbackKeys"], ["kind"])
        self.assertEqual(people["inputs"]["previewKey"], "membership_summary")
        self.assertNotIn("avatar", people["inputs"])
        self.assertEqual(people["inputs"]["imageKey"], "profile.avatar_url")
        self.assertEqual(people["inputs"]["imageShape"], "avatar")
        self.assertEqual(people["inputs"]["initialsKey"], "initials")
        self.assertEqual(people["actions"][0]["params"]["selectedPerson"], "$event")
        self.assertEqual(
            {item["key"] for item in people["inputs"]["meta"]},
            {"subject_ref", "membership_summary", "membership_count", "application_access_count"},
        )
        serialized_inputs = json.dumps(people["inputs"], sort_keys=True)
        for forbidden in (
            '"previewKey": "memberships"',
            '"key": "application_access"',
            '"path": "application_access"',
            '"key": "roles"',
            '"key": "grants"',
        ):
            self.assertNotIn(forbidden, serialized_inputs)
        for widget_id in ("guests", "children"):
            person_rows = self.widgets[widget_id]["inputs"]
            self.assertEqual(person_rows["previewKey"], "membership_summary")
            self.assertNotIn("avatar", person_rows)
            self.assertEqual(person_rows["imageKey"], "profile.avatar_url")
            self.assertEqual(person_rows["imageShape"], "avatar")
            self.assertEqual(person_rows["initialsKey"], "initials")
            self.assertNotIn(
                "memberships",
                {item["key"] for item in person_rows["meta"]},
            )

        detail = self.widgets["person-detail"]
        self.assertEqual(detail["area"], "inspector")
        self.assertEqual(detail["role"], "detail")
        self.assertIn("$state.selectedSubjectId", detail["visibleIf"])
        self.assertEqual(detail["inputs"]["primaryTitlePath"], "display_label")
        self.assertNotIn("avatar", detail["inputs"])
        self.assertEqual(detail["inputs"]["imageKey"], "profile.avatar_url")
        self.assertEqual(detail["inputs"]["imageShape"], "avatar")
        self.assertEqual(detail["inputs"]["initialsKey"], "initials")
        self.assertEqual(
            {field["path"] for field in detail["inputs"]["fields"]},
            {
                "display_label",
                "kind",
                "primary_role",
                "subject_ref",
                "membership_summary",
                "membership_count",
                "application_access_count",
                "profile.email",
                "profile.handle",
            },
        )
        serialized_detail = json.dumps(detail["inputs"], sort_keys=True)
        for forbidden in (
            '"path": "profile"',
            '"path": "memberships"',
            '"path": "application_access"',
            '"path": "roles"',
            '"path": "grants"',
            '"path": "devices"',
            '"path": "sessions"',
            '"path": "recent_activity"',
        ):
            self.assertNotIn(forbidden, serialized_detail)

    def test_mutations_are_explicit_modal_gated_root_calls(self):
        self.assertEqual(
            self.widgets["person-actions"]["inputs"]["buttons"][0]["id"],
            "grant_role",
        )
        self.assertEqual(self.widgets["person-actions"]["actions"], [{
            "on": "click:grant_role",
            "type": "updateState",
            "params": {"activeAccessModal": "grant-role"},
        }])
        self.assertIn("activeAccessModal == 'grant-role'", self.widgets["grant-role"]["visibleIf"])
        self.assertIn("activeAccessModal == 'create-invite'", self.widgets["create-invite"]["visibleIf"])
        self.assertIn("$state.activeSection == 'devices'", self.widgets["pair-device"]["visibleIf"])
        self.assertIn("activeAccessModal == 'pair-device'", self.widgets["pair-device"]["visibleIf"])
        self.assertIn("activeAccessModal == 'admin-recovery'", self.widgets["admin-recovery"]["visibleIf"])
        for widget_id in ("revoke-invite", "revoke-device", "revoke-session"):
            self.assertIn("activeAccessModal == '" + widget_id + "'", self.widgets[widget_id]["visibleIf"])

        expected = {
            "users_access.grant_role": ["users_access.summary"],
            "users_access.create_invite": ["users_access.summary"],
            "users_access.create_device_pairing": ["users_access.summary"],
            "users_access.create_admin_recovery": ["users_access.summary"],
            "users_access.revoke_invite": ["users_access.summary"],
            "users_access.revoke_device": ["users_access.summary"],
            "users_access.revoke_session": ["users_access.summary"],
            "applications.access.change": ["users_access.summary", "applications.access"],
            "applications.access.revoke": ["users_access.summary", "applications.access"],
        }
        seen = []
        for action in self.call_actions():
            self.assertIn(action["target"], expected)
            self.assertEqual(action["idempotencyKey"], "auto")
            self.assertEqual(action["invalidates"], expected[action["target"]])
            self.assertTrue(set(action["params"]).isdisjoint(
                {"caller", "actor", "owner", "permissions"}
            ))
            seen.append(action["target"])
        self.assertCountEqual(seen, expected)

    def test_invitation_and_pairing_results_are_immediately_shareable(self):
        self.assertEqual(
            [item["id"] for item in self.widgets["invitations"]["inputs"]["buttons"]],
            ["show-invite-qr", "copy-invite", "share-invite-telegram", "revoke-invite"],
        )
        invitation_events = {
            action["on"] for action in self.widgets["invitations"]["actions"]
        }
        self.assertTrue({
            "click:show-invite-qr",
            "click:copy-invite",
            "click:share-invite-telegram",
            "click:revoke-invite",
        }.issubset(invitation_events))
        self.assertEqual(
            [item["id"] for item in self.widgets["devices"]["inputs"]["buttons"]],
            ["revoke-device"],
        )
        self.assertEqual(
            [item["id"] for item in self.widgets["sessions"]["inputs"]["buttons"]],
            ["revoke-session"],
        )
        invitation_actions = self.widgets["invitation-result-actions"]
        pairing_actions = self.widgets["device-pairing-result-actions"]
        self.assertEqual(
            [item["id"] for item in invitation_actions["inputs"]["buttons"]],
            ["show_qr", "copy_link", "share_telegram"],
        )
        self.assertEqual(
            [item["id"] for item in pairing_actions["inputs"]["buttons"]],
            ["show_pairing_qr", "copy_pairing_link", "share_pairing_telegram"],
        )
        self.assertEqual(
            self.widgets["device-actions"]["actions"][0]["params"],
            {"activeAccessModal": "pair-device"},
        )
        pair_call = next(
            action
            for action in self.call_actions()
            if action["target"] == "users_access.create_device_pairing"
        )
        self.assertEqual(pair_call["resultStateKey"], "lastDevicePairing")

    def test_application_access_is_selected_person_grant_view(self):
        access = self.widgets["application-access"]
        self.assertIn("$state.selectedSubjectId", access["visibleIf"])
        self.assertEqual(access["dataSource"]["arguments"]["sections"],
                         ["application_access"])
        self.assertEqual(
            access["dataSource"]["resultPath"],
            "response.result.users_access.application_access",
        )
        self.assertEqual(access["inputs"]["filters"], [
            {"key": "subject_ref", "stateKey": "selectedSubjectId"}
        ])
        self.assertEqual(access["inputs"]["itemIdKey"], "grant_id")
        self.assertEqual(access["inputs"]["titleKey"], "application_id")
        self.assertEqual(access["inputs"]["subtitleKey"], "status")
        self.assertEqual(access["inputs"]["previewKey"], "updated_at")
        self.assertNotIn("detailsPath", access["inputs"])
        self.assertNotIn("readOnlyReasonKey", access["inputs"])
        self.assertEqual(
            {item["key"] for item in access["inputs"]["meta"]},
            {"revision", "created_at", "updated_at"},
        )
        self.assertEqual(access["actions"][0]["params"]["selectedPermission"], "$event")
        self.assertEqual(
            access["actions"][0]["params"]["selectedPermissionId"],
            "$event.grant_id",
        )
        self.assertNotIn("permissions first", self.widgets["application-access-help"]["inputs"]["fields"][0]["content"])

        detail = self.widgets["permission-detail"]
        self.assertEqual(detail["role"], "detail")
        self.assertEqual(
            {field["path"] for field in detail["inputs"]["fields"]},
            {
                "application_id",
                "status",
                "revision",
                "created_at",
                "updated_at",
                "application_roles",
                "permission_ceiling",
                "explicit_denies",
                "constraints",
            },
        )
        scalar_preview = json.dumps(access["inputs"], sort_keys=True)
        for forbidden in ("application_roles", "permission_ceiling", "explicit_denies", "constraints"):
            self.assertNotIn(forbidden, scalar_preview)

    def test_sessions_devices_activity_and_i18n_are_bounded(self):
        devices = self.widgets["devices"]["inputs"]
        self.assertEqual(devices["titleKey"], "display_name")
        self.assertIn("device_id", devices["titleFallbackKeys"])
        self.assertIn("last_seen_at", {item["key"] for item in devices["meta"]})

        sessions = self.widgets["sessions"]["inputs"]
        self.assertEqual(sessions["titleKey"], "device_name")
        self.assertIn("session_id", {item["key"] for item in sessions["meta"]})
        self.assertIn("location", {item["key"] for item in sessions["meta"]})

        activity = self.widgets["activity"]
        self.assertEqual(activity["dataSource"]["arguments"]["audit_limit"], 100)
        self.assertEqual(activity["inputs"]["bounded"], {"pageSize": 25, "maxPageSize": 100})
        self.assertEqual(activity["actions"][0]["params"]["selectedSubjectId"], "$event.actor_ref")

        dictionaries = {lang: read_json(SCENARIO / "assets" / "i18n" / (lang + ".json"))
                        for lang in ("en", "ru")}
        self.assertEqual(set(dictionaries["en"]), set(dictionaries["ru"]))
        self.assertEqual(dictionaries["en"]["detail.region.label"], "Details")
        self.assertEqual(dictionaries["ru"]["detail.region.label"], "Подробности")
        for lang, dictionary in dictionaries.items():
            for key, value in dictionary.items():
                self.assertNotIn("??", value, (lang, key))
                self.assertNotIn("\ufffd", value, (lang, key))
        for node in walk(self.page):
            for key, child in list(node.items()) if isinstance(node, dict) else []:
                if key.endswith("_i18n"):
                    ref = child["key"] if isinstance(child, dict) else child
                    for dictionary in dictionaries.values():
                        self.assertTrue(dictionary[ref])


if __name__ == "__main__":
    unittest.main()
