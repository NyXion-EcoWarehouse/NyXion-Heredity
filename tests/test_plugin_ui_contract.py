from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import unittest

from nyxion_heredity_sdk import (
    API_MAJOR, API_MINOR, CHALLENGE_KINDS, SESSION_STATES,
    Challenge, ContractError, PluginManifest, PluginUIPage, SessionUpdate, UIPage,
)
from nyxion_heredity_sdk.example import OfflineExamplePlugin


ROOT = Path(__file__).resolve().parents[1]


def manifest(**changes):
    fields = {
        "plugin_id": "test.ui", "name": "UI contract", "version": "1.0.0",
        "platform": "example", "actions": ("get_login_info",),
    }
    return PluginManifest(**(fields | changes))


class PluginUIContractTests(unittest.TestCase):
    def test_existing_thirteen_field_manifest_stays_identical(self):
        value = OfflineExamplePlugin.manifest
        original = value.to_dict()
        self.assertEqual(len(original), 13)
        self.assertNotIn("ui_pages", original)
        self.assertEqual(value.ui_pages, ())
        self.assertEqual(PluginManifest.from_dict(original), value)
        self.assertEqual((API_MAJOR, API_MINOR), (1, 0))

    def test_optional_ui_pages_round_trip_and_does_not_alias_source(self):
        source = [{"id": "accounts", "title": "平台账号", "view": "accounts", "icon": "users"}]
        value = manifest(ui_pages=source)
        source[0]["title"] = "changed"
        self.assertEqual(value.ui_pages, (PluginUIPage("accounts", "平台账号"),))
        self.assertEqual(PluginManifest.from_dict(value.to_dict()), value)
        self.assertEqual(value.to_dict()["ui_pages"][0]["title"], "平台账号")
        self.assertIs(UIPage, PluginUIPage)
        with self.assertRaises(FrozenInstanceError):
            value.ui_pages[0].title = "changed"

    def test_empty_optional_pages_normalize_to_legacy_manifest(self):
        original = manifest().to_dict()
        value = PluginManifest.from_dict(original | {"ui_pages": []})
        self.assertEqual(value.to_dict(), original)

    def test_route_segments_cannot_escape_or_claim_global_routes(self):
        for page_id in (
                "", "../accounts", "/accounts", "a/accounts", "accounts?x=1", "a#fragment",
                "https://example.invalid", "a.b", "a_b", "Accounts", "a--b", "-a", "a-",
                "a" * 65):
            with self.subTest(page_id=page_id), self.assertRaises(ContractError):
                PluginUIPage(page_id, "Accounts")
        self.assertEqual(PluginUIPage("example-accounts-2", "Accounts").id, "example-accounts-2")

    def test_pages_allow_only_host_owned_accounts_view_and_users_icon(self):
        for change in ({"view": "html"}, {"view": "javascript"}, {"view": "iframe"},
                       {"view": "https://example.invalid"}, {"icon": "script"},
                       {"icon": "../../icon"}):
            data = {"id": "accounts", "title": "Accounts", "view": "accounts", "icon": "users"}
            with self.subTest(change=change), self.assertRaises(ContractError):
                PluginUIPage.from_dict(data | change)

    def test_page_fields_are_exact_and_cannot_smuggle_ui_code_or_paths(self):
        data = {"id": "accounts", "title": "Accounts", "view": "accounts", "icon": "users"}
        for extra in ({"path": "/admin"}, {"url": "https://example.invalid"},
                      {"script": "execute"}, {"html": "<script>"}):
            with self.subTest(extra=extra), self.assertRaises(ContractError):
                PluginUIPage.from_dict(data | extra)
        for missing in data:
            partial = dict(data)
            partial.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ContractError):
                PluginUIPage.from_dict(partial)

    def test_titles_are_nonempty_visible_single_line_bounded_unicode(self):
        for title in ("", "  ", "\t", "line\nbreak", "return\r", "\x00", "\x7f", "a" * 81):
            with self.subTest(title=title), self.assertRaises(ContractError):
                PluginUIPage("accounts", title)
        self.assertEqual(PluginUIPage("accounts", "平台账号").title, "平台账号")
        self.assertEqual(len(PluginUIPage("accounts", "a" * 80).title), 80)

    def test_pages_have_count_uniqueness_and_container_bounds(self):
        for pages in (
                "accounts", None, {"id": "accounts"},
                [PluginUIPage("accounts", "A"), PluginUIPage("accounts", "B")],
                [PluginUIPage(f"page-{index}", f"Page {index}") for index in range(9)]):
            with self.subTest(pages=pages), self.assertRaises(ContractError):
                manifest(ui_pages=pages)
        value = manifest(ui_pages=tuple(
            PluginUIPage(f"page-{index}", f"Page {index}") for index in range(8)))
        self.assertEqual(len(value.ui_pages), 8)

    def test_ui_pages_are_the_only_new_optional_manifest_field(self):
        data = manifest().to_dict()
        for extra in ({"page": []}, {"navigation": []}, {"instance_key": "account"},
                      {"default_account": "123"}):
            with self.subTest(extra=extra), self.assertRaises(ContractError):
                PluginManifest.from_dict(data | extra)

    def test_challenge_and_session_constants_match_json_contracts(self):
        schema = json.loads(
            (ROOT / "sdk/schemas/contracts.v1.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(CHALLENGE_KINDS, {"qr", "input", "notice"})
        self.assertEqual(set(schema["$defs"]["challenge"]["properties"]["kind"]["enum"]),
                         CHALLENGE_KINDS)
        self.assertEqual(set(schema["$defs"]["sessionUpdate"]["properties"]["state"]["enum"]),
                         SESSION_STATES)
        self.assertIn("starting", SESSION_STATES)
        self.assertEqual(Challenge("ended", "notice", "Login", {"message": "Ended"}).kind, "notice")
        self.assertEqual(SessionUpdate(None, "starting").state, "starting")
        with self.assertRaises(ContractError):
            Challenge("bad", "text", "Not SDK", {})

    def test_schema_declares_optional_metadata_and_exact_ui_fields(self):
        schema = json.loads(
            (ROOT / "sdk/schemas/plugin-manifest.v1.schema.json").read_text(encoding="utf-8"))
        self.assertNotIn("ui_pages", schema["required"])
        self.assertEqual(schema["properties"]["ui_pages"]["maxItems"], 8)
        page = schema["$defs"]["uiPage"]
        self.assertEqual(set(page["required"]), {"id", "title", "view", "icon"})
        self.assertFalse(page["additionalProperties"])
        self.assertEqual(page["properties"]["view"], {"const": "accounts"})
        self.assertEqual(page["properties"]["icon"], {"const": "users"})

    def test_plugin_declaration_preserves_accounts_page_through_json_metadata(self):
        value = manifest(ui_pages=(PluginUIPage("accounts", "平台账号"),))
        metadata = json.loads(json.dumps(value.to_dict(), ensure_ascii=False))
        declaration = PluginManifest.from_dict(metadata)
        self.assertEqual(declaration, value)
        self.assertEqual(declaration.ui_pages, (PluginUIPage("accounts", "平台账号"),))


if __name__ == "__main__":
    unittest.main()
