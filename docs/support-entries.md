# 多平台独立客服入口

## 后台配置

1. 在 Chatwoot 创建或确认两个现有 **Website Inbox**。不需要创建第二套 Chatwoot。
2. 在统一后台“客服实例”创建两个独立实例，分别选择已有 AI 模型、提示词和知识库配置。可以共用模型或知识配置档，但知识**正文**始终按实例隔离；在“知识库配置”页面分别选实例编辑正文。
3. 在“Chatwoot 接入”分别选择实例，配置现有 Chatwoot 连接、Account ID 和个人 API Access Token，实时获取网站收件箱，然后绑定不同的 Inbox。复用现有 Agent Bot、签名 Webhook、人工接管及解决后自动恢复流程。机器人已绑定的收件箱不允许改成另一个，避免打断已有会话。
4. 在“客服入口管理”填写平台名称、唯一 Slug，选择“客服实例 · Website Inbox”，保存。下拉来自现有已绑定连接的真实 Chatwoot API，不使用示例收件箱。未连接/绑定时请先完成上一步；不会静默创建或替换 Chatwoot 收件箱。
5. 每个入口生成同源 `/support/{slug}/` 链接，可一键复制。可编辑名称、Slug、绑定和启用状态；改 Slug 后旧链接失效。独立平台必须使用不同实例与 Inbox，一个实例不能重复用于两个独立入口。模型、提示词、知识库编辑继续使用现有实例页面，不重复保存第二套配置。
6. “设为默认客服”决定 `/support/` 的入口；原默认收件箱设置保留，选择下方兼容模式可重新使用原配置。当前显式默认入口不能直接停用或删除，请先改选默认。停用/删除仅影响公开入口，不删除 Chatwoot 收件箱、聊天历史、机器人、凭据、实例或知识文件；其他网站已嵌入的 Widget 不会因此被撤销。

所有已启用的网站主/备用域名都可使用这些相对链接，不要求每个平台新增域名。公开 Token 仅是 Chatwoot Widget 的 `website_token`，不是后台个人 API Token、机器人 Token 或管理 Token。访客身份保存在该浏览器当前站点、按公开 Token 区分的 Cookie 中；不同入口收件箱不会共享访客身份。不同域名 Cookie 不互通。

## 路由、安全与数据

- 命名入口严格匹配；不存在返回 404，停用返回 410，不回退默认。服务或收件箱失效时拒绝提供错误的 Widget。未知链接不会创建另一平台的访客。
- Admin 沿用登录 Session、CSRF、Origin；入口 API 在已有 Service Management Bearer 认证下。后台响应不含 API Token。入口只接受名称、Slug、实例及已验证的 account/inbox 绑定，不接受任意 URL、Shell 或文件路径。
- AI Webhook 先验证该实例机器人的 HMAC 签名与时间，再验证 `account_id + inbox_id`，由已验证实例 ID 解析模型/提示词/知识。对于同一 Chatwoot 的多个可信本地域名，禁止把相同 account/inbox 绑定到不同实例。已有歧义绑定也拒绝接收/处理，不猜测默认知识库。
- AI 引擎、人工接管状态、去重和有界重试继续复用，不按公开 Slug 直接接收 AI 请求；Slug 只选择网站 Inbox，真正消息路由取自 Chatwoot。
- Runtime 原 SQLite `core.sqlite3` 增量 Schema 4→5 **只新增 `support_entries`**，不修改或删除历史表、配置、凭据及 Chatwoot 数据。Runtime 首次写操作在原事务机制内迁移，失败回滚，重复迁移不重置。显式默认 Slug 仍保存在原 `runtime_profiles` 默认记录中，同时保留 service_id；旧的仅 service_id 记录无需转换。
- 入口、默认选项和旧知识文件均在原 AI 数据卷中。现有完整卷备份/恢复覆盖入口；安装、升级不清空该卷。回退到不支持 Schema 5 的旧 Runtime 需要按现有回滚流程恢复升级前数据库备份，不能仅换旧镜像后读取新 Schema。

## 安全更新现有 Ubuntu 服务器

本地提交尚未推送，以下步骤须等该提交进入远程 `master` 后才可执行。先进入现有项目根目录，停止其他并行维护，安排短暂维护窗口。不要运行 `down -v`、删除卷、重置数据库或重新获取凭据。

```bash
(
  set -e
  git status --short --branch
  test -z "$(git status --porcelain)"
  test "$(git branch --show-current)" = master
  git fetch origin master
  git merge-base --is-ancestor HEAD origin/master
  # 任一步失败均退出；用当前旧代码先做完整备份。
  ./backup.sh
  git merge --ff-only origin/master
  bash install.sh install
  ./manage.sh health
)
```

`install` 使用已保存配置和现有卷，重建本项目 Runtime/Admin 镜像并重用原 Nginx/SSL 配置，不重新生成已存在 Secret/Token 或重置管理员。此代码更新不要求更换 Chatwoot 版本；现有 `upgrade.sh` 对目标镜像版本完全相同的升级会拒绝，因此不应随意修改版本绕过。Runtime 首次写操作在同一事务中执行新增表迁移。保存备份 ID；出现异常保留现场并使用原备份/回滚机制，勿删除数据卷。

## 验收两个入口

在后台添加两个不同 Slug、不同实例和 Website Inbox。分别打开复制的链接，检查网页立即显示原生聊天组件，刷新不报 404；浏览器 URL 的公开 `website_token` 应分别对应各自 Inbox，后台 Token 不应出现在页面或网络请求中。

分别发送不同的测试问题，在 Chatwoot 核对收到消息的 Inbox，确认访客端收到 AI 回复。为两个实例写入不同知识标识与提示词，检查上下文不串库；分别转人工、人工回复、解决会话，再次咨询检查 AI 恢复。停用一个入口应显示停用提示；随机不存在的 Slug 应 404，不能显示另一 Inbox。测试桌面、手机、键盘、刷新后的会话延续以及备份恢复后的入口设置。

自动化使用真实 Chromium、Nginx/TLS、固定 v4.18 上游 Widget URL 构造函数，以及有明确标识的 Widget HTTP/页面协议替身；覆盖后台 CRUD、两 Inbox 的发送/历史/会话 Cookie、默认与错误路由、AI 知识隔离及安装备份恢复。它**不是**完整 Chatwoot 前端/真实 OpenAI/生产服务器端到端验收，真实编译资源、WebSocket、AI 回答和手机键盘仍须在 Ubuntu 上按上述步骤验收。

## 本次变更文件

- `ai_runtime/chatwoot.py`：规范化 Chatwoot 来源，account/inbox 歧义与 Webhook/队列校验。
- `ai_runtime/support_entries.py`：入口模型、CRUD、实时收件箱校验、默认选择。
- `ai_runtime/support_api.py`：精确公开入口解析、兼容原默认设置。
- `ai_runtime/management_store.py`：增量 Schema 5。
- `ai_runtime/management_api.py`：复用原管理 Token 注册入口 API。
- `platform_admin/website.py`：Admin 表单、动态 `/support/{slug}/`、原生 Widget 页面。
- `platform_admin/app.py`、`platform_admin/clients.py`：页面脚本策略与安全错误码。
- `platform_admin/templates/support.html`、`platform_admin/templates/base.html`：中文入口管理及导航。
- `platform_admin/static/support.js`、`platform_admin/static/support-entries.js`：Widget 参数初始化、独立访客 Cookie、复制链接。
- `tests/support_entries_test.py`、`tests/support_entries_browser_test.py`、`tests/support-entries-fixture-test.sh`：API、隔离、迁移及真实浏览器/TLS 回归。
- `tests/support_test.py`、`tests/website_proxy_test.py`、`tests/openai_chatwoot_test.py`：现有支持页、代理及 Chatwoot 协议回归适配。
- `tests/knowledge_persistence_check.py`：真实安装/备份/恢复/升级 fixture 验证入口持久化。
- `tests/fixtures/chatwoot-v4.18.0/SOURCE.json`、`widget_base_controller.rb`、`widget_endPoints.js`、`widget_urlParamsHelper.js`：固定版本未修改源代码与 SHA-256 来源记录。
- `README.md`、`docs/website-domains.md`、`docs/support-entries.md`：使用、兼容性、部署及验收说明。

未修改生产 `.env`、Nginx 站点、证书、Chatwoot Compose、管理员密码或凭据。Nginx 现有 `location ^~ /support/` 已覆盖动态子路径，根路径的原代理继续提供 Chatwoot 资源/API/WebSocket，无需新增冲突规则。
