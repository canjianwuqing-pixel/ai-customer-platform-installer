"""Native form posts against real Admin, management API, SQLite and YAML."""

from html.parser import HTMLParser
import unittest

import admin_test as fixtures
from ai_runtime.management_api import ServiceCreate, DomainCreate
from ai_runtime.profile_api import MODELS
from platform_admin import forms


class Editor(HTMLParser):
    """Collect successful controls from the editor, never the logout form."""
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.active = False
        self.data, self.controls, self.options = {}, {}, {}
        self.current, self.option = None, None
        self.text = ""
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.active = attrs.get("id") == "config-form"
        if not self.active:
            return
        name = attrs.get("name")
        if tag == "input" and name:
            self.controls[name] = attrs
            if attrs.get("type") != "checkbox" or "checked" in attrs:
                self.data[name] = attrs.get("value", "")
        if tag in {"select", "textarea"} and name:
            self.current, self.text = name, ""
            self.controls[name] = attrs
            if tag == "select":
                self.options[name] = []
        if tag == "option" and self.current:
            self.option = attrs
            self.text = ""

    def handle_data(self, data):
        if self.active and self.current:
            self.text += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False
        if not self.active:
            return
        if tag == "option" and self.option is not None:
            value = self.option.get("value", "")
            self.options[self.current].append((value, self.text.strip()))
            if self.current not in self.data or "selected" in self.option:
                self.data[self.current] = value
            self.option = None
        if tag == "textarea" and self.current:
            value = self.text.replace("\r\n", "\n").replace("\r", "\n")
            self.data[self.current] = value[1:] if value.startswith("\n") else value
            self.current = None
        if tag == "select":
            self.current = None


class VisualFormFixtures(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdminFixtures()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.assertEqual(self.fixture.login().status_code, 303)

    def api(self, kind, key=""):
        response = self.client.get("/admin/api/" + kind + ("/" + key if key else ""))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def edit(self, kind, key="_new"):
        response = self.client.get("/admin/edit/" + kind + "/" + key)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('name="record"', response.text)
        return Editor(response.text)

    def save(self, kind, key="_new", changes=None, *, expected=303, data=None):
        form = self.edit(kind, key)
        payload = form.data if data is None else {"csrf": form.data["csrf"], **data}
        payload.update(changes or {})
        result = self.client.post("/admin/edit/" + kind + "/" + key, data=payload,
                                  headers={"origin": "https://platform.example.test"})
        self.assertEqual(result.status_code, expected, result.text[:250] if expected != 303 else "Form did not save")
        return result

    def profiles(self):
        for kind, record in (("ai", {"id": "group/model_a", "model": "model-alpha", "api_key_env": "PROVIDER_CREDENTIAL"}),
            ("prompts", {"id": "prompt_a", "system_prompt": "通用提示词", "reply_rules": ["准确回复"]}),
            ("knowledge", {"id": "knowledge_a", "source": "", "max_characters": 3000}),
            ("channels", {"id": "channel_a", "enabled": False, "provider": "custom-provider", "config_ref": "group/settings"})):
            self.assertEqual(self.fixture.post("/admin/api/" + kind, record).status_code, 201)

    def service(self, key="service_a", enabled=True):
        self.save("services", changes={"id": key, "name": "通用客服实例", "enabled": str(enabled).lower(),
                   "ai_config": "group/model_a", "prompt_config": "prompt_a", "knowledge_config": "knowledge_a", "channel_config": "channel_a"})

    def test_form_fields_exactly_match_existing_api_contracts(self):
        models = {"services": ServiceCreate, "domains": DomainCreate, **MODELS}
        for kind, model in models.items():
            expected = set(model.model_fields)
            presented = {field.name for field in forms.FIELDS[kind]}
            if kind in {"services", "domains"}:
                presented.add(forms.identity(kind).name)
            self.assertEqual(presented, expected)
        for kind, model in MODELS.items():
            self.assertEqual(forms.defaults(kind), model.model_construct(**({"model": ""} if kind == "ai" else {})).model_dump())

    def test_create_edit_reopen_and_real_api_dropdown_associations(self):
        self.profiles()
        form = self.edit("services")
        for field, expected in (("ai_config", "group/model_a"), ("prompt_config", "prompt_a"),
                                ("knowledge_config", "knowledge_a"), ("channel_config", "channel_a")):
            self.assertEqual({value for value, _ in form.options[field]}, {"", expected})
            self.assertEqual(form.options[field][0], ("", "不选择"))
        self.service()
        result = self.save("services", "service_a", {"name": "修改后的实例"})
        self.assertIn("配置已保存", self.client.get(result.headers["location"]).text)
        reopened = self.edit("services", "service_a")
        self.assertNotIn("id", reopened.data)
        self.assertEqual(reopened.data["name"], "修改后的实例")
        self.assertEqual(reopened.data["ai_config"], "group/model_a")
        self.assertEqual(self.fixture.resolver.resolve("service_a").model, "model-alpha")
        result = self.save("services", "service_a", {"enabled": "false"})
        self.assertFalse(self.api("services", "service_a")["enabled"])
        self.save("services", "service_a", {"enabled": "true"})
        self.assertTrue(self.api("services", "service_a")["enabled"])

    def test_unchecked_switch_partial_posts_and_immutable_identity(self):
        self.profiles()
        self.service()
        before = self.api("services", "service_a")
        self.save("services", "service_a", data={"name": "仅修改名称"})
        after = self.api("services", "service_a")
        self.assertEqual({**before, "name": "仅修改名称"}, after)
        self.save("services", "service_a", data={"id": "renamed"}, expected=422)
        self.assertEqual(self.api("services", "service_a"), after)
        self.save("services", "service_a", data={"_present_enabled": "1"})
        self.assertFalse(self.api("services", "service_a")["enabled"])

    def test_missing_old_reference_is_visible_and_preserved(self):
        self.save("services", changes={"id": "old_service", "name": "旧配置", "ai_config": "absent/model", "enabled": "false"})
        form = self.edit("services", "old_service")
        self.assertIn(("absent/model", "absent/model（原有关联，当前列表未找到）"), form.options["ai_config"])
        self.save("services", "old_service", {"name": "更新名称"})
        self.assertEqual(self.api("services", "old_service")["ai_config"], "absent/model")

    def test_duplicates_bad_ids_extra_fields_and_required_fields_do_not_write(self):
        self.save("services", changes={"id": "service_a", "name": "现有实例"})
        response = self.save("services", changes={"id": "service_a", "name": "另一名称"}, expected=409)
        self.assertIn("该标识已存在", response.text)
        self.assertEqual(Editor(response.text).data["name"], "另一名称")
        for changes in ({"id": "../invalid", "name": "未保存"}, {"id": "valid", "name": "   "},
                        {"id": "valid", "name": "未保存", "invented_field": "rejected"}):
            self.save("services", changes=changes, expected=422)
        self.assertEqual(len(self.api("services")), 1)
        self.assertEqual(self.api("services", "service_a")["name"], "现有实例")

    def test_domain_create_edit_default_entry_and_invalid_binding(self):
        self.profiles()
        self.service()
        self.service("service_b", False)
        self.save("domains", changes={"domain": "Entry.Example.Test", "service_id": "service_a", "enabled": "true", "default_entry": "true"})
        self.assertTrue(self.api("domains", "entry.example.test")["default_entry"])
        self.save("domains", "entry.example.test", {"service_id": "service_b"}, expected=409)
        self.assertEqual(self.api("domains", "entry.example.test")["service_id"], "service_a")
        self.save("domains", "entry.example.test", {"service_id": "service_b", "enabled": "false", "default_entry": "false"})
        self.assertEqual(self.edit("domains", "entry.example.test").data["service_id"], "service_b")
        self.save("domains", "entry.example.test", {"service_id": "absent"}, expected=409)

    def test_each_profile_create_edit_and_preserve_unsubmitted_legal_fields(self):
        examples = (("ai", {"id": "model_a", "model": "model-one", "api_base_url": "https://provider.example.test/custom", "api_key_env": "CUSTOM_CREDENTIAL"}, {"model": "model-two"}),
                    ("knowledge", {"id": "knowledge_a", "source": "folder/knowledge.txt", "max_characters": "4567"}, {"max_characters": "9876"}),
                    ("channels", {"id": "channel_a", "enabled": "true", "provider": "custom", "config_ref": "group/connection"}, {"provider": "custom-updated"}),
                    ("prompts", {"id": "prompt_a", "system_prompt": "第一行\n第二行", "_rules_present": "1", "reply_rule_0": "规则一\n补充", "reply_rule_1": "规则二"}, {"system_prompt": "已修改"}))
        for kind, create, update in examples:
            with self.subTest(kind=kind):
                self.save(kind, changes=create)
                key = create["id"]
                before = self.api(kind, key)
                self.save(kind, key, data=update)
                after = self.api(kind, key)
                converted = {field: int(value) if field == "max_characters" else value for field, value in update.items()}
                self.assertEqual({**before, **converted}, after)
                self.assertEqual(self.client.get("/admin/edit/" + kind + "/" + key).status_code, 200)

    def test_prompt_multiline_rules_crlf_and_64_rule_limit(self):
        rules = ["  首条规则\r\n允许多行  ", "\n第二条\r换行", "同一内容\n", "同一内容\r\n"] + ["规则 " + str(i) for i in range(60)]
        self.assertEqual(self.fixture.post("/admin/api/prompts", {"id": "old_prompt", "system_prompt": "\n原提示词\r\n", "reply_rules": rules}).status_code, 201)
        self.save("prompts", "old_prompt")
        stored = self.api("prompts", "old_prompt")
        self.assertEqual(stored["reply_rules"], rules)
        self.assertEqual(stored["system_prompt"], "\n原提示词\r\n")
        result = self.save("prompts", "old_prompt", {"_action": "add_rule"}, expected=422)
        self.assertIn("最多 64 条", result.text)
        self.assertEqual(self.api("prompts", "old_prompt"), stored)

    def test_add_remove_rule_only_changes_draft_until_save(self):
        self.save("prompts", changes={"id": "prompt_a", "system_prompt": "原提示词", "reply_rule_0": "规则一"})
        before = self.api("prompts", "prompt_a")
        result = self.save("prompts", "prompt_a", {"_action": "add_rule", "system_prompt": "未保存提示词"}, expected=200)
        draft = Editor(result.text)
        self.assertIn("reply_rule_1", draft.data)
        self.assertEqual(draft.data["system_prompt"], "未保存提示词")
        self.assertEqual(self.api("prompts", "prompt_a"), before)
        result = self.save("prompts", "prompt_a", data={**draft.data, "reply_rule_1": "规则二", "_action": "remove_rule_0"}, expected=200)
        draft = Editor(result.text)
        self.assertEqual(draft.data["reply_rule_0"], "规则二")
        self.assertEqual(self.api("prompts", "prompt_a"), before)
        self.save("prompts", "prompt_a", data=draft.data)
        self.assertEqual(self.api("prompts", "prompt_a")["reply_rules"], ["规则二"])

    def test_removing_rule_preserves_line_endings_of_shifted_remaining_rules(self):
        self.assertEqual(self.fixture.post("/admin/api/prompts", {"id": "prompt_a", "reply_rules": ["删除此规则", "保留\r\n多行规则"]}).status_code, 201)
        response = self.save("prompts", "prompt_a", {"_action": "remove_rule_0"}, expected=200)
        self.save("prompts", "prompt_a", data=Editor(response.text).data)
        self.assertEqual(self.api("prompts", "prompt_a")["reply_rules"], ["保留\r\n多行规则"])

    def test_long_unicode_form_and_original_api_body_limit(self):
        prompt = "通用提示词" * 4000
        self.save("prompts", changes={"id": "long_prompt", "system_prompt": prompt})
        self.assertEqual(self.api("prompts", "long_prompt")["system_prompt"], prompt)
        self.assertEqual(self.client.post("/admin/api/prompts", content="x" * 65537).status_code, 413)
        self.save("prompts", "long_prompt", {"reply_rule_64": "超出规则数"}, expected=422)

    def test_bad_form_metadata_is_rejected_without_writes(self):
        self.save("prompts", changes={"id": "prompt_a"})
        before = self.api("prompts", "prompt_a")
        self.save("prompts", "prompt_a", {"_rules_present": "invalid"}, expected=422)
        self.save("prompts", "prompt_a", {"_action": "unsupported"}, expected=422)
        self.assertEqual(self.api("prompts", "prompt_a"), before)

    def test_runtime_is_existing_singleton_and_selects_enabled_service(self):
        self.profiles()
        self.service()
        self.save("runtime", "default", {"default_service_id": "service_a"})
        self.assertEqual(self.fixture.resolver.resolve().service_id, "service_a")
        self.assertEqual(self.client.get("/admin/edit/runtime/_new").headers["location"], "/admin/edit/runtime/default")
        self.assertNotIn("/runtime/_new", self.client.get("/admin/manage/runtime").text)
        self.save("runtime", "default", {"default_service_id": "missing"}, expected=409)
        self.assertEqual(self.api("runtime", "default")["default_service_id"], "service_a")
        self.save("runtime", "default", {"default_service_id": ""})

    def test_numeric_and_profile_validation_feedback_and_xss_escaping(self):
        for number in ("0", "100001", "1.5", "bad"):
            self.save("knowledge", changes={"id": "knowledge_a", "max_characters": number}, expected=422)
        result = self.save("ai", changes={"id": "model_a", "model": "<script>alert(1)</script>", "api_base_url": "file:///bad"}, expected=409)
        self.assertNotIn("<script>", result.text)
        self.assertIn("&lt;script&gt;", result.text)
        self.assertIn("配置未通过校验", result.text)
        self.assertEqual(self.api("ai"), [])

    def test_legacy_yaml_remains_byte_identical_after_form_overrides(self):
        original = self.fixture.resolver.config_dir / "prompts" / "legacy.yaml"
        original.parent.mkdir()
        original.write_text('system_prompt: "原提示词"\nreply_rules:\n  - "保留规则"\n', encoding="utf-8")
        snapshot = original.read_bytes()
        self.save("prompts", "legacy", data={"system_prompt": "新提示词"})
        self.assertEqual(original.read_bytes(), snapshot)
        self.assertEqual(self.api("prompts", "legacy")["reply_rules"], ["保留规则"])

    def test_security_session_csrf_origin_tokens_and_unavailable_options(self):
        form = self.edit("services")
        path = "/admin/edit/services/_new"
        data = {**form.data, "id": "safe", "name": "普通配置"}
        self.assertEqual(self.client.post(path, data={**data, "csrf": "bad"}).status_code, 403)
        self.assertEqual(self.client.post(path, data=data, headers={"origin": "null"}).status_code, 403)
        token = self.fixture.env["SERVICE_MANAGEMENT_TOKEN"]
        result = self.client.post(path, data={**data, "name": token, "unsupported": "x"})
        self.assertEqual(result.status_code, 422)
        self.assertNotIn(token, result.text)
        self.assertEqual(self.api("services"), [])
        self.fixture.management.token = "invalid-fixture-token-not-real-123456789"
        result = self.client.get(path)
        self.assertNotEqual(result.status_code, 200)
        self.assertNotIn('id="config-form"', result.text)
        self.fixture.management.token = token
        self.client.cookies.clear()
        self.assertEqual(self.client.post(path, data=data).status_code, 303)
        self.assertEqual(self.api_status("/admin/api/services"), 401)

    def test_maintenance_status_is_chinese_without_changing_script_calls(self):
        response = self.fixture.post("/admin/system/run", {"operation": "backup", "version": "", "confirmation": "BACKUP"})
        self.assertEqual(response.status_code, 202)
        page = self.client.get("/admin/maintenance")
        self.assertIn("已完成", page.text)
        self.assertIn('data-status="succeeded"', page.text)
        state = self.client.get("/admin/system/state").json()
        self.assertEqual(state["jobs"][0]["operation"], "backup")
        self.assertEqual(state["jobs"][0]["status"], "succeeded")

    def api_status(self, path):
        return self.client.get(path).status_code


if __name__ == "__main__":
    unittest.main()
