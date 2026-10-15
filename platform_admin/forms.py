"""HTML presentation/decoding only; the management API remains authoritative.

No storage, credentials or new business fields live here. Contract tests compare
these controls with the existing Service Management API models.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    control: str = "text"
    default: object = ""
    required: bool = False
    maximum: int | None = None
    minimum: int | None = None
    pattern: str = ""
    hint: str = ""
    choices: str = ""


SERVICE_PATTERN = r"[a-z][a-z0-9_\-]{0,63}"
REFERENCE_PATTERN = r"[a-z][a-z0-9._/\-]{0,127}"
# HTML pattern uses the Unicode-v syntax, which also requires escaping '/'.
HTML_REFERENCE_PATTERN = r"[a-z][a-z0-9._\/\-]{0,127}"
IDENTITY_HINT = "请手动填写，以小写字母开头，可含数字、下划线和连字符。创建后不可修改。"
PROFILE_HINT = "配置 ID 由小写字母开头，可含数字、点、下划线、连字符及目录分隔符；不能使用空路径或相对路径段。创建后不可修改。"

TITLES = {"services": "客服实例", "domains": "域名管理", "ai": "AI 模型配置", "prompts": "提示词配置",
          "knowledge": "知识库配置", "channels": "渠道配置", "runtime": "运行配置"}

FIELDS = {
    "services": (
        Field("name", "实例名称", required=True, maximum=128),
        Field("enabled", "启用实例", "checkbox", False, hint="启用前必须关联有效的 AI 模型配置；如关联知识库，其文件也必须可读取。"),
        Field("ai_config", "AI 模型", "select", choices="ai"),
        Field("prompt_config", "提示词", "select", choices="prompts"),
        Field("knowledge_config", "知识库", "select", choices="knowledge"),
        Field("channel_config", "渠道配置", "select", choices="channels"),
    ),
    "domains": (
        Field("service_id", "绑定客服实例", "select", required=True, choices="services"),
        Field("enabled", "启用域名", "checkbox", False, hint="启用时必须绑定已启用的客服实例。此处不操作 DNS 或证书。"),
        Field("default_entry", "设为默认入口", "checkbox", False, hint="只能设置一个默认入口，且该域名必须处于启用状态。"),
    ),
    "ai": (
        Field("model", "模型名称", required=True, maximum=256, hint="填写模型提供方支持的模型标识，不预设特定模型。"),
        Field("api_base_url", "API 基础地址", "url", maximum=2048, hint="可留空以使用服务端默认地址。仅支持 HTTP / HTTPS，不填写账号、密码、查询参数或 Token。"),
        Field("api_key_env", "凭据环境变量名称", default="OPENAI_API_KEY", required=True,
              pattern=r"[A-Z_][A-Z0-9_]*", hint="这里只填写已有环境变量的名称，不填写 API Key 的值。凭据需由服务器环境配置。"),
    ),
    "prompts": (
        Field("system_prompt", "系统提示词", "textarea", maximum=32000),
        Field("reply_rules", "回复规则", "rules", (), maximum=4000, hint="每条规则可包含多行；最多 64 条。留空的规则不会保存。"),
    ),
    "knowledge": (
        Field("source", "知识库文件来源", maximum=1024, hint="各实例独立数据目录内的相对文本文件路径。新配置默认使用 knowledge.txt，无需了解服务器目录；原有路径保持不变，留空表示不使用文件。正文在知识库页面按实例编辑。"),
        Field("max_characters", "最多读取字符数", "number", 12000, True, maximum=100000, minimum=1),
    ),
    "channels": (
        Field("enabled", "启用渠道配置", "checkbox", False),
        Field("provider", "渠道类型 / 提供方", maximum=128, hint="现有 API 使用自由文本，没有固定渠道目录。保存配置不会建立外部平台连接。"),
        Field("config_ref", "关联配置引用", maximum=128, pattern=HTML_REFERENCE_PATTERN,
              hint="可留空；仅填写配置引用，不填写密码、Token 或连接凭据。"),
    ),
    "runtime": (
        Field("default_service_id", "默认客服实例", "select", choices="services",
              hint="留空沿用 Runtime 的默认选择规则；显式选择时，该实例必须已启用。"),
    ),
}

ERRORS = {
    "unknown_service_id": "客服实例不存在，请重新选择。",
    "knowledge_not_configured": "该实例尚未关联知识库，或配置的文件来源为空。请先编辑知识库配置并为实例设置关联；新配置可直接使用默认文件名。",
    "unsafe_knowledge_path": "知识文件路径不安全，不能使用链接、系统配置或凭据文件，请检查知识库配置。",
    "knowledge_too_large": "知识正文最多为 1 MiB UTF-8 文本，超出时不会截断或覆盖原文件。",
    "invalid_knowledge_content": "正文必须为有效的 UTF-8 纯文本，不能包含空字符。原文件未修改。",
    "knowledge_conflict": "正文或知识库关联已被修改，本次未覆盖。请先保留下方草稿，再重新加载最新正文。",
    "knowledge_storage_unavailable": "无法安全读写知识文件，请检查数据卷权限或文件链接；未确认保存成功。",
    "invalid_request": "请求字段或数据格式不正确，请检查后重试。",
    "invalid_identifier": "配置标识不符合现有规则。",
    "record_already_exists": "该标识已存在，请使用其他标识。",
    "record_not_found": "配置已不存在，请返回列表刷新。",
    "invalid_service_binding": "绑定的客服实例不存在，请重新选择。",
    "bound_service_is_disabled": "绑定的客服实例已停用，不能启用此域名。",
    "multiple_default_domains": "已有其他默认域名，请先取消原默认入口。",
    "default_domain_must_be_enabled": "默认入口必须启用，请检查域名启用状态。",
    "service_has_domain_bindings": "该客服实例仍被域名引用，不能删除。",
    "invalid_profile_configuration": "配置未通过校验，请检查字段格式、地址、文件来源和回复规则。",
    "invalid_runtime_configuration": "运行配置不可用，请检查模型、配置引用及知识库文件是否有效。",
    "invalid_default_service": "默认客服实例必须存在且已启用。",
    "default_service_must_remain_enabled": "当前实例是默认实例，请先修改运行配置，再停用此实例。",
    "internal_operation_rejected": "管理 API 拒绝了操作，请检查字段格式和配置关联。",
    "internal_api_unavailable": "暂时无法连接管理 API，请稍后重试；未确认保存成功。",
    "sensitive_input_rejected": "配置中包含受保护的凭据，已拒绝保存且不回显输入。",
    "unsafe_upstream_response": "管理 API 返回了受保护的内容，已阻止显示。",
    "origin_rejected": "请求来源校验未通过，请从本站 HTTPS 后台重新打开页面。",
    "csrf_rejected": "表单校验未通过，请刷新页面后重试。",
    "authentication_required": "登录已失效，请重新登录。",
    "invalid_credentials": "账号或密码不正确。",
    "login_throttled": "登录尝试过多，请稍后再试。",
    "password_must_be_6_to_1024_characters": "新密码需为 6 至 1024 个字符。",
    "request_too_large": "提交内容过大，请缩减内容后重试。",
}


def error_message(code):
    return ERRORS.get(code, "操作未完成，请检查配置或稍后重试。")


class FormError(Exception):
    def __init__(self, message, field=""):
        self.message, self.field = message, field


def identity(kind):
    if kind == "domains":
        return Field("domain", "域名", required=True, maximum=253, hint="仅填写域名，不包含协议、端口或路径；保存后转为小写。绑定标识创建后不可修改。")
    if kind == "services":
        return Field("id", "实例 ID", required=True, maximum=64, pattern=SERVICE_PATTERN, hint=IDENTITY_HINT)
    return Field("id", "配置 ID", required=True, maximum=128, pattern=HTML_REFERENCE_PATTERN, hint=PROFILE_HINT)


def defaults(kind):
    return {field.name: list(field.default) if field.control == "rules" else field.default for field in FIELDS[kind]}


def keep_newlines(old, submitted):
    # Native HTML textareas normalize CRLF and CR. Do not rewrite a legal old
    # multi-line rule/prompt merely because the user edited a different field.
    normalize = lambda value: value.replace("\r\n", "\n").replace("\r", "\n")
    return old if isinstance(old, str) and normalize(old) == normalize(submitted) else submitted


def decode(kind, data, existing, *, creating, validate=True):
    """Merge onto a freshly fetched record; absent controls never erase fields."""
    fields = FIELDS[kind]
    key_field = identity(kind)
    allowed = {field.name for field in fields if field.control != "rules"}
    allowed |= {"_present_" + field.name for field in fields if field.control == "checkbox"}
    if creating:
        allowed.add(key_field.name)
    rules_keys = {key for key in data if re.fullmatch(r"reply_rule_(?:[0-9]|[1-5][0-9]|6[0-3])", key)}
    if kind == "prompts":
        allowed |= {"_rules_present", "_action"} | rules_keys
    if set(data) - allowed or any(not isinstance(value, str) for value in data.values()):
        raise FormError("表单包含不支持的字段，请刷新页面后重试。")
    record = {**defaults(kind), **existing}
    if creating:
        record[key_field.name] = data.get(key_field.name, "")
    for field in ((key_field,) if creating else ()) + fields:
        if field.control == "rules":
            if "_rules_present" in data and data["_rules_present"] != "1":
                raise FormError("回复规则字段不完整，请刷新后重试。", "reply_rules")
            if rules_keys and data.get("_rules_present") != "1":
                raise FormError("回复规则字段不完整，请刷新后重试。", "reply_rules")
            if data.get("_rules_present") == "1":
                old = existing.get("reply_rules", [])
                rows = []
                for index, key in sorted((int(key[11:]), key) for key in rules_keys):
                    value = keep_newlines(old[index] if index < len(old) else None, data[key])
                    if index < len(old) and value is old[index]:
                        rows.append(value)
                        continue
                    # A draft removal may shift the remaining rules' indexes.
                    # Preserve their original line endings as well as grouping.
                    for previous in old:
                        preserved = keep_newlines(previous, value)
                        if preserved is previous:
                            value = preserved
                            break
                    rows.append(value)
                record[field.name] = [rule for rule in rows if rule != ""]
            continue
        if field.control == "checkbox":
            if "_present_" + field.name in data:
                if data["_present_" + field.name] != "1":
                    raise FormError("开关字段格式不正确。", field.name)
                record[field.name] = False
            if field.name in data:
                if data[field.name] not in {"true", "false"}:
                    raise FormError("开关字段格式不正确。", field.name)
                record[field.name] = data[field.name] == "true"
            continue
        if field.name in data:
            value = data[field.name]
            if field.control == "number":
                if not re.fullmatch(r"[0-9]+", value):
                    raise FormError("请输入有效的整数。", field.name)
                value = int(value)
            elif field.control == "textarea":
                value = keep_newlines(existing.get(field.name), value)
            record[field.name] = value
        value = record.get(field.name, "")
        if not validate:
            continue
        if field.required and isinstance(value, str) and not value.strip():
            raise FormError("请填写" + field.label + "。", field.name)
        if field.control == "number":
            if not field.minimum <= value <= field.maximum:
                raise FormError(f"请输入 {field.minimum} 至 {field.maximum} 之间的整数。", field.name)
        elif isinstance(value, str):
            if field.maximum and len(value) > field.maximum:
                raise FormError(f"{field.label}不能超过 {field.maximum} 个字符。", field.name)
            pattern = field.pattern.replace(r"\/", "/")
            if pattern and value and not re.fullmatch(pattern, value):
                raise FormError(field.label + "格式不正确。", field.name)
            if (field.name in {"id", "config_ref"} and value
                    and any(part in {"", ".", ".."} for part in value.split("/"))):
                raise FormError(field.label + "不能包含空路径或相对路径段。", field.name)
    if validate and kind == "prompts" and any(not rule.strip() or len(rule) > 4000 for rule in record["reply_rules"]):
        raise FormError("每条回复规则不能为空白，且不能超过 4000 个字符。", "reply_rules")
    return record


def choices(field, records, current):
    options = []
    if not field.required:
        options.append({"value": "", "label": "不选择"})
    for entry in records:
        key = entry["id"]
        detail = entry.get("name") or entry.get("model") or ""
        label = (detail + "（" + key + "）") if detail else key
        if "enabled" in entry:
            label += " · 已启用" if entry["enabled"] else " · 已停用"
        options.append({"value": key, "label": label})
    if current and not any(option["value"] == current for option in options):
        options.append({"value": current, "label": current + "（原有关联，当前列表未找到）"})
    return options


def draft(kind, data, existing, *, creating):
    """Public, known controls only, for validation feedback without data loss."""
    record = {**defaults(kind), **existing}
    for field in FIELDS[kind]:
        if field.control == "checkbox":
            if field.name in data or "_present_" + field.name in data:
                record[field.name] = data.get(field.name) == "true"
        elif field.control != "rules" and field.name in data:
            record[field.name] = data[field.name]
    if creating:
        record[identity(kind).name] = data.get(identity(kind).name, "")
    rows = None
    if kind == "prompts" and data.get("_rules_present") == "1":
        rows = [data[key] for _, key in sorted((int(key[11:]), key) for key in data
                if re.fullmatch(r"reply_rule_(?:[0-9]|[1-5][0-9]|6[0-3])", key))]
    return record, rows
