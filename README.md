# AI Customer Platform Installer

一个面向 Ubuntu 服务器的模块化 AI 客服平台安装器项目。项目通过一个入口脚本完成 Chatwoot 基础部署，并可按需启用 AI Runtime 和统一 `/admin` 管理后台，为后续知识库等组件保留清晰的扩展边界。

## 当前阶段

当前仓库已经实现单域名 HTTPS 部署所需的基础模块，包括：

- 安装入口和模块注册机制；
- 统一日志、配置读取、状态记录和基础校验接口；
- System 环境检测与 Docker 官方安装流程；
- Chatwoot、PostgreSQL、Redis、Sidekiq 的 Docker Compose Stack；
- 可选的 FastAPI AI Service 与 AI 管理后台 Docker Compose Stack；
- 基于 Service Instance 的 AI Runtime 请求路由与实例上下文隔离；
- AI 服务独立数据卷、环回地址默认绑定和敏感日志禁用约束；
- Chatwoot 网站收件箱真实 API 接入、签名 Webhook、AI 多轮回复与人工接管；
- 带版本、校验和、保留策略及显式恢复确认的本地备份与恢复；
- 带升级前备份、版本记录、健康检查和失败自动恢复的安全升级与回滚；
- Chatwoot 的 Nginx HTTP 反向代理与 WebSocket 支持；
- 单域名 DNS 检测、Let's Encrypt 证书申请和 HTTPS 切换；
- Certbot 自动续签、Nginx deploy hook 和续签 dry-run；
- 配置模板、fixture 测试和文档目录。

系统检测模块提供只读的 Ubuntu 版本、权限、CPU、内存、磁盘、架构和网络检查。Docker 模块可以检测现有环境，并在缺失时通过 Docker 官方 APT 仓库安装 Engine、CLI、containerd、Buildx 和 Compose Plugin。Chatwoot 模块会校验 Compose 配置、拉取固定版本镜像、先启动并等待 PostgreSQL/Redis、执行 `db:chatwoot_prepare`，再启动 Rails 与 Sidekiq。只有 `.env` 明确配置 AI 镜像时，AI Service 模块才会校验并启动 FastAPI 服务和独立管理后台。Nginx 模块负责安装服务和提供初始 HTTP 反向代理；domain 模块负责域名解析检测、Nginx 域名绑定验证、Certbot/Let's Encrypt 证书生命周期以及从 HTTP 到 HTTPS 的安全切换。

## 配置原则

真实配置统一保存在本地 `.env` 文件中。首次执行 `bash install.sh` 时，安装向导会自动从 `.env.example` 创建权限为 `0600` 的 `.env`。普通安装只询问网站域名和管理员邮箱，不需要填写镜像、版本、密码、Token、OpenAI API Key 或模型名称。

安装器按 `config/default.env`、`config/images.env`、`.env` 的顺序加载配置，后加载的用户配置优先。`config/images.env` 提供 Chatwoot、PostgreSQL 和 Redis 的固定版本，禁止使用 `latest`；`config/default.env` 保存非敏感运行参数。高级用户可以在 `.env` 中覆盖版本和镜像设置。

`DATABASE_PASSWORD`、`REDIS_PASSWORD`、`SECRET_KEY_BASE`、`AI_SERVICE_INTERNAL_TOKEN` 和 `SERVICE_MANAGEMENT_TOKEN` 可以留空；安装器首次运行时自动生成安全随机值，重复执行保留已有值，并将 `.env` 权限限制为 `0600`。统一后台账号固定为 `admin`，使用 `ADMIN_PASSWORD=admin` 作为公开出厂初始密码，`ADMIN_SESSION_SECRET` 仍安全随机生成。旧 `AI_ADMIN_*` 凭据只用于迁移同一个账号；旧 `ADMIN_PASSWORD_HASH` 仅为升级兼容输入，不再为新安装生成。除公开出厂值外，不得向 Git 提交真实域名、邮箱、密码、密钥、证书或 Token。

`DOMAIN` 是当前唯一部署域名；`LETSENCRYPT_EMAIL` 用于 Let's Encrypt 账户和证书通知，`EMAIL` 继续作为 Chatwoot 的业务发件地址。V1 固定使用 Let's Encrypt HTTP-01 验证，因此域名必须能通过公共 DNS 解析到部署入口，公网 TCP 80 端口必须可达。

`modules/domain/providers/` 和 `modules/domain/dns-providers/` 已作为后续扩展边界保留。当前不会读取 Cloudflare 凭据，也不实现 DNS-01、多域名或泛域名证书。

Chatwoot、pgvector/PostgreSQL 和 Redis 镜像均通过 `.env` 使用明确版本标签。PostgreSQL 与 Redis 不发布宿主机端口；Rails 默认只绑定到 `.env` 配置的本地地址，后续由 Nginx 模块接入。

### Chatwoot API 请求头与旧 Nginx 配置迁移

Chatwoot 使用 `api_access_token` 请求头。Nginx 默认将带下划线的请求头视为无效并过滤，Token 本身正确也会因此收到 401。HTTP / HTTPS 模板均开启 `underscores_in_headers on;`：托管站点文件由 Ubuntu 的 `sites-enabled/*` **在 `http {}` 内引入**，文件顶层设置让已有默认虚拟主机也继承该规则，覆盖 Token 出现在 Host 之前及 HTTPS 无匹配 SNI 的请求；新模板内各 server 也明确开启。依据 [Nginx 请求头指令](https://nginx.org/en/docs/http/ngx_http_core_module.html#underscores_in_headers)和[虚拟主机选择顺序](https://nginx.org/en/docs/http/server_names.html#virtual_server_selection)，不能只在某个 location 或命名站点里补发一个已被过滤的头。

无需手工修改 Nginx、密码或 Token。执行正常 `bash install.sh` 即可应用该策略，不要求更改镜像版本；已有部署按原流程执行 `bash upgrade.sh`（仍需不同的固定目标版本）时也自动迁移。重复安装已有 HTTPS 站点时只补齐头解析策略，保留其证书路径、TLS、重定向、Admin / Webhook 代理和 `/cable` 设置；升级在未启用 Admin 时也会刷新 Nginx。旧备份站点配置经过同一应用流程，因此恢复不会重新引入这一缺陷。

不抢占其他站点的 `default_server`，不修改 `nginx.conf`，不关闭其他非法头校验。候选配置仍先经过 `nginx -t`，成功才 reload，失败则恢复原配置。若自定义 Nginx 在同一 http 上下文已显式声明同名指令或在默认站点显式关闭下划线头，需要先解决其自定义策略冲突；安装器不会擅自改写非托管站点。标准 Ubuntu Nginx 默认配置无需额外操作。只更新此修复无需重建 Runtime / Admin 镜像。

`tests/chatwoot-headers-fixture-test.sh` 覆盖 HTTP/HTTPS 生成、重复安装、Chatwoot-only 升级、旧备份恢复和语法失败回滚；配套真实 Nginx + TLS 代理测试使用临时 Token 和 Chatwoot 认证替身，验证同一个 Token 直连及经代理均成功、默认主机/头顺序/SNI、非法头过滤、错误 Token 拒绝、HTTPS 重定向及 WebSocket。实际服务器和 Chatwoot 数据均不参与这些隔离测试。

AI Service 和统一后台不是 Chatwoot 基础安装必选项。未配置 AI 镜像且未启用统一后台时，安装器跳过全部 AI 步骤。启用统一后台参见下一节；高级用户仍可覆盖固定标签或 digest 的镜像。OpenAI API Key 不属于安装必填项，可在统一后台安全配置，也兼容 Runtime 已有环境变量凭据。

## 统一配置生命周期与旧部署升级

`install`、`upgrade`、`restore` 共用 `lib/lifecycle.sh` 的配置准备流程：重新建立默认配置 + 用户配置上下文，在 `0600` 候选文件中迁移、补齐允许生成的字段、校验，再原子保存。迁移失败不替换原 `.env`；重复执行保留已有密码、Secret 和后台数据。恢复使用备份配置重新建立完整上下文，不继承当前进程中的 Admin、Runtime 开关及旧用户扩展变量。

新安装可以生成核心 Secret；已有部署缺少 `DATABASE_PASSWORD`、`REDIS_PASSWORD` 或 `SECRET_KEY_BASE` 时拒绝自动重置，应找回原配置。新增的管理 Token、Session Secret 和 `ADMIN_API_URL` 可以自动补齐；已有非空值必须通过校验，不会因格式错误而偷偷换值。保留旧 `AI_ADMIN_*` 配置供迁移失败后的恢复，统一后台容器不读取这些旧字段，不会因此增加第二套账号。

旧 Chatwoot-only 部署启用统一后台：在本地 `.env` 设置 `ADMIN_ENABLED=true`，执行 `bash upgrade.sh` 并确认。无需填写 AI 镜像：缺少时选择固定的仓库 Runtime/Admin 本地构建标签；Chatwoot 升级目标留空时保留当前版本。升级按 Docker 项目标签识别当前部署，先备份旧栈，再构建/拉取目标镜像、应用配置、迁移并验证；新 Admin 的 Nginx 路由和维护执行器随升级部署。仍可通过 `UPGRADE_*` 明确选择固定版本，禁止 `latest`。

已部署的本地 Runtime/Admin 标签不变时复用原镜像，避免覆盖回滚镜像。升级它们的代码需要提供新的固定目标标签；从外部 Runtime 切换本地构建也必须选择不同标签。备份现在按启用模块保存数据：Chatwoot-only 备份不要求 AI 容器；旧版包含 AI 的备份仍可恢复。升级专用备份会保持应用停止写入，直到迁移和重启，避免回滚丢失备份之后的新消息；请预留构建/拉取期间的维护窗口。迁移前的镜像准备失败只恢复旧容器，不回放数据库。恢复不会删除额外 AI 数据卷，恢复到未启用 AI 的版本时会停止旧 AI 服务。

内部 Compose 调用统一加载默认配置、镜像配置和用户配置，生成仅所有者可读的临时有效环境快照，显式传入项目名、快照和所需覆盖文件；命令结束删除快照，sudo 和工作目录变化不改变值。日常运维统一使用根目录 `manage.sh`，不要进入 `stacks/` 裸执行 `docker compose`；Admin Compose 文件只是覆盖层，不是独立部署入口。不要把完整根 `.env` 配置为服务级 `env_file`。

dotenv 使用非执行的单行字面量格式。建议对 `$`、`#`、前后空格等特殊内容使用单引号；旧凭据中的 `$` 和行内 `#` 保留字面含义，不作为变量展开或注释。双引号不支持变量展开或转义，不能安全往返的写回会被拒绝，不会输出字段值。修改既有维护执行器的路径时，须先完成任务并停止配置指定的 unit，再重新执行安装；不会中断正在运行的维护任务。

Admin `/health` 检查本地存储，新增 `/ready` 验证 Service/Domain 列表、管理 Token 认证、拒绝未认证访问和维护连接。部署验证和 Compose 使用只读挂载的标准库兼容探针，旧 Admin 镜像即使没有 `/ready` 也能完成同等检查，不必覆盖旧镜像来获得新健康接口；依赖不兼容时不再报告就绪。Compose 2.24.4+ 的要求会在部署前检查。

完整回归（测试依赖安装在已有虚拟环境中）：

```bash
ADMIN_TEST_PYTHON="$PWD/runtime/admin-venv/bin/python" \
AI_RUNTIME_TEST_PYTHON="$PWD/runtime/admin-venv/bin/python" \
TEST_JOBS=4 bash tests/run-all.sh
```

包含 Shell 语法检查、全部 fixture/Python 套件和真实 Compose 配置检查；宿主部署操作使用隔离替身，另有下述真实 Nginx TLS 登录测试（需要测试工具）。测试数据库仅写临时目录，不代表真实服务器部署验证。报告保存在忽略 Git 的受保护 `runtime/tests/suite.*` 目录。

## 统一管理后台（第十四阶段）

统一管理后台是平台唯一管理入口：`https://<DOMAIN>/admin`。只有一个管理员账号，不创建 Service Admin、Domain Admin 或其他账号体系。Chatwoot 自身的客服业务登录不作为平台管理入口。

在本地 `.env` 设置 `ADMIN_ENABLED=true`，然后执行 `bash install.sh`。仍只询问域名、邮箱；没有 AI 镜像时会自动选择 `config/images.env` 中的固定本地构建标签，同时启用仓库内 Runtime。已有自定义 AI 镜像则继续保留，必须实现本仓库的 Service Management / profile API；如需切换到仓库 Runtime，显式设置 `AI_RUNTIME_BUILD=true`。镜像、端口、Session 有效期、维护目录均可通过 `.env` 覆盖，禁止 `latest`。需要支持 `!override` 的 Docker Compose 2.24.4+、宿主机 Python 3 和 systemd。

`modules/admin/` 管理部署与健康检查；`stacks/admin/compose.yaml` 是现有 AI Stack 的覆盖文件，**替换原 `ai-admin` 服务，而不是再创建一个后台**。保留其内部服务名和 `ai_admin_data` 卷，兼容 `manage.sh`、备份、升级与回滚。`AI_ADMIN_IMAGE` 现在是这个唯一管理组件的镜像版本，不表示第二个后台；旧外部后台镜像不再与统一后台同时启动。常用运维命令不变，无需进入子目录。

页面使用中文、响应式的服务端可视化表单，不需要手写 JSON，不引入前端框架。知识库页面仅使用同源静态脚本保护未保存草稿和确认清空，不在浏览器保存正文或内部 Token。可以查看、创建、编辑、启用/停用客服实例；管理域名绑定；管理 AI 模型、系统提示词、回复规则、Runtime 默认实例、知识库与渠道配置引用。域名页只修改绑定数据，不修改 DNS 或申请证书；知识库不提供文件上传/向量索引，渠道页不建立外部平台连接。

普通表单沿用已有 profile API；密钥和 Chatwoot 接入使用独立的受认证接口，不将敏感字段加入普通配置响应：

| 页面 | 实际支持的字段 / 操作 |
| --- | --- |
| 客服实例 | 手动填写 `id`、`name`、启用开关及四类配置下拉关联；创建后 ID 只读 |
| 域名管理 | `domain`、客服实例绑定、启用状态、默认入口；域名标识创建后只读 |
| AI 模型 | 配置 ID、任意模型 ID、API Base URL、可选环境变量引用；另提供只写 Key、前缀显示、动态模型查询和实际连接测试 |
| 提示词 | 配置 ID、`system_prompt`、最多 64 条独立回复规则；每条可多行，支持添加、删除 |
| 知识库 | 配置 ID、`source` 相对文件路径、`max_characters`（1–100000） |
| 渠道 | 配置 ID、启用开关、`provider` 自由文本、`config_ref` 引用 |
| 运行配置 | 仅编辑已有 `default` 记录的 `default_service_id`，不新增记录 |

下拉数据由 Admin 服务端实时读取真实管理 API，允许为空的关联提供“不选择”。旧配置中不在当前列表的引用仍显示原值并明确提示，不会静默清空；关联列表读取失败不会渲染一个可误保存的空表单。保存前读取最新已有记录并合并提交字段，保留未提交的合法字段、回复规则分组及未修改文本的换行。新增、编辑和启用/停用保留现有 API 的引用、唯一性和运行状态校验；失败显示中文原因并保留非敏感输入，成功后回到列表显示保存反馈。

现有配置 API **不支持**为模型/提示词/知识库/渠道增加独立名称、温度/最大输出量等模型参数，或查询固定渠道类型目录，因此不提供假控件；这些配置以现有 ID 区分。知识正文通过下述独立的实例正文接口管理；渠道页仍只保存引用，实际网站收件箱接入使用“Chatwoot 接入”页。已存完整 Key、机器人 Token 和内部 Token 不回显；原 Session、CSRF、Origin、安全 Cookie 和 CSP 均保留。仅模型页允许同源脚本连接自己的 Admin 后端，浏览器不直接调用提供商或内部 API。HTML 配置编辑入口保留 4 MiB、72 项限制；其他入口沿用既有请求限制。

### OpenAI / 兼容接口与网站客服自动回复

使用仓库内 Runtime 和统一 Admin 时，可在同一 `/admin` 完成接入，不需要 SSH 填写提供商密钥：

1. 在“AI 模型”创建配置，填写自选 API Base URL（留空使用已有 `AI_API_BASE_URL`，未设置则使用官方 `https://api.openai.com/v1`）及 Key。点击“获取模型列表”，按当前地址和凭据查询；可下拉选择，也可直接填写任意模型 ID。接口不支持模型查询时仍可手动保存，没有固定名单或固定模型。点击“测试模型连接”会实际请求当前选择的模型，可能产生少量调用费用。
2. 保存后仅显示 Key 前 8 位与星号。编辑时 Key 留空保留，输入新 Key 即替换；不要求每次编辑重新填写。未后台保存 Key 时兼容 `api_key_env` 指向的已有环境变量。每个实例通过原 `ai_config` 关联自己的模型配置；保存修改后下一条消息即生效，不需重建容器。
3. 在“客服实例”关联原有提示词、回复规则和知识库并启用实例。在“Chatwoot 接入”选择实例，输入 Chatwoot 网站地址、账户 ID、该账户**管理员**的 API Access Token；真实查询已有网站收件箱，再选择绑定。不使用 widget Token，不将 Agent Bot Token 误作可读取消息历史的用户 Token。
4. 后端创建账户级 Agent Bot，将其绑定到所选网站收件箱，并自动设置当前 HTTPS 入口下的 Webhook。Chatwoot 必须能访问此 HTTPS 地址。一个实例绑定一个网站收件箱，同一收件箱不能被两个实例复用；已有其他机器人时拒绝覆盖。V1 已绑定网站/账户/收件箱保持稳定，允许更换同一连接的管理员 Token；其他入口使用独立实例，不进行隐式搬迁。
5. 新访客会话由 Chatwoot 置为 `pending` 并分配机器人。Runtime 只处理经过签名认证、绑定校验的访客公开文本新消息，再从账户 API 读取实际消息，按 `service_id` 加载模型、提示词和实例独立知识正文，将公开回复通过机器人消息 API 发回原会话。现有已开放的人工会话不会被安装器强制抢回。

模型查询使用 [OpenAI Models API](https://developers.openai.com/api/reference/resources/models/methods/list)，生成与测试沿用 [Chat Completions 协议](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。兼容提供同等协议的服务；不假设仅支持 Responses 等其他协议的接口可以直接工作。普通 profile API 的 `model` / `api_base_url` / `api_key_env` 字段契约不变。新内部接口（全部使用现有 `SERVICE_MANAGEMENT_TOKEN`）为 `/management/models/save`（配置及可选只写 Key 同事务保存）、`/discover`、`/test` 和 `/{profile_id}/credential`（仅掩码元数据）；浏览器仅访问受 Session、Origin、CSRF 保护的 `/admin/models/*` 与原配置表单。

Chatwoot 接口依据 **v4.18.0** 源码核对：使用 [`api_access_token` 权限规则](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/controllers/concerns/access_token_auth_helper.rb)、[网站收件箱绑定接口](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/controllers/api/v1/accounts/inboxes_controller.rb)和[账户机器人接口](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/controllers/api/v1/accounts/agent_bots_controller.rb)。账户管理员 Token 用于读取历史与配置，机器人 Token 用于公开回复和转人工。没有假设账户普通 Webhook 具备签名：这里使用 Agent Bot 的真实 [`X-Chatwoot-Timestamp` / `X-Chatwoot-Signature`](https://github.com/chatwoot/chatwoot/blob/v4.18.0/lib/webhooks/trigger.rb)，校验原始请求体 HMAC、5 分钟时间窗口、账户与收件箱，之后才持久化消息 ID。Nginx 只公开 `/integrations/chatwoot/{service_id}/webhook` 的 POST，拒绝匿名/无效签名；管理 API 和 Runtime `/chat` 不对公网转发。

多轮历史只从当前账户、收件箱、会话读取，最多 20 条历史、合计 24000 字符，单条截断；排除私有备注、活动消息及非本实例机器人回复。系统提示词、原回复规则和知识库 `max_characters` 保持有效。已知凭据从发送上下文及回复中脱敏。不会建立跨访客或跨实例的共享对话缓存。

### 人工接管与失败处理

访客明确说“转人工”等请求，或人工客服公开回复后，Runtime 持久化暂停标志，并使用 Chatwoot 的[机器人转人工状态接口](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/controllers/api/v1/accounts/conversations_controller.rb)将机器人待处理会话转为 `open`，交由收件箱已有人工/自动分配规则。人工消息、私有备注和 AI 自身消息不会触发 AI 回复。后台可逐实例停用 AI；下一条新消息转人工。关闭浏览器、暂缓会话、单独切换回 `open` 或等待一段时间，都不会解除人工接管。

人工客服在 **Chatwoot 后台将会话标记为已解决**，即结束本轮接管。Runtime 通过原 HMAC 签名 Webhook 保存解决事件和消息分界；访客再次发送消息时，即使复用同一个 `conversation_id`，也仅处理分界后的新消息。Chatwoot 会将已解决会话重新置为 `pending`，但可能保留原人工分配，因此 Runtime 核对实时会话/收件箱及新消息后，调用实际[分配接口](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/controllers/api/v1/accounts/conversations/assignments_controller.rb)重新分配本实例机器人。无需管理员逐条恢复，不补发人工期间旧消息，历史上下文也从新一轮开始。重启、升级、恢复备份均保留本轮状态及去重记录。

此机制依据 v4.18.0 的实际 [AgentBotListener](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/listeners/agent_bot_listener.rb) 与 [Message 重新打开逻辑](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/models/message.rb)：**启用中的收件箱机器人**即使已不再分配给人工会话，仍收到 `conversation_resolved` / `conversation_status_changed` / `conversation_updated` 与访客消息。`status_changed` 的变更明细可能为空，`conversation_updated.previous_changes` 对应的状态变更同样被处理，不能假设每个状态事件都含旧值。保留原收件箱机器人绑定、出站 URL 和签名密钥，不需另建账户 Webhook。请勿在 Chatwoot 停用/解绑该机器人；否则无法投递事件，安装器不会以超时猜测恢复。状态事件按源时间去重，重新打开事件可补充解决事件的丢失/乱序；消息比状态事件先被处理时，只重新排队该次新消息，不重发已发送或结果不确定的消息。旧版内部受认证的手动恢复接口为 API 兼容保留，不再提供网页逐会话操作。

生成后、发送前再次检查人工状态、实时分配和本轮版本，并用事务执行最终本地发送校验；人工处理时 AI 保持暂停。Chatwoot 的外部消息 POST 不提供与人工点击共用的事务/条件更新，因而不能承诺跨系统零竞态或绝对 exactly-once；极短的最后请求窗口仍需真实服务器并发验收，不能通过放宽校验解决。

Webhook 接收后立即入持久队列，去重键为实例 + 会话 + 消息 ID；最多 3 次有限退避重试、4 个受限工作任务，同一会话按消息 ID 串行。持久化接收失败返回 500，以匹配 Chatwoot v4.18 Agent Bot 仅重试 429/500、最多 3 次的实际策略。读失败/生成失败不会无限重试，最终暂停并尽力转人工。Chatwoot 消息创建 API 没有幂等保证：发送前记录 `sending`，在消息 `content_attributes` 写入回复标记。若超时/5xx，先查询该标记；无法确认时**不盲目重发**，暂停并显示“发送结果待人工核对”，请在 Chatwoot 核对并解决本轮会话；下次新咨询自动接待。断电后的 `sending` 也只核对，不重发；最迟在 5 分钟租约后接续（这是队列租约，不是人工恢复定时器）。恢复旧备份后若记录过旧（24 小时）或首个 100 条后续消息页无法排除已发送回复，也转人工，不冒险重复发送。

### 密钥持久化、升级与验收边界

提供商 Key、Chatwoot 管理员 Token、机器人 Token / 签名 Secret 使用 Fernet 加密，存入现有 Runtime `ai_service_data` 卷内 `management/core.sqlite3` 的新增表；随机加密密钥位于同卷 `management/vault.key`（目录 `0700`、文件 `0600`）。普通 GET 配置不含完整凭据；页面仅回显允许的前缀掩码。丢失加密密钥时拒绝自动重建覆盖，必须恢复对应备份。加密保护与配置隔离不等于抵御拥有完整数据卷权限的服务器管理员。

schema 2 → 3 事务性追加表；3 → 4 只为已有会话表追加生命周期顺序、重新打开标志和本轮版本，不重写原 Service、Domain、知识库或 Chatwoot 业务数据。旧暂停不会无依据解除，后续解决/重新打开事件接入新流程。重复安装不清空数据卷。现有备份/恢复已归档整个 Runtime 数据卷，包含 SQLite、加密密钥、队列、绑定、本轮状态及知识正文；恢复必须保留两者配套，备份本身含解密能力和 `.env`，仍须作为敏感资料保护，不提交 Git。升级请按既有机制给本地 Runtime/Admin 选择**新的固定目标镜像标签**，重建两个组件并应用随附 Nginx 模板，不要仅复用旧镜像标签；原凭据和 Secret 不重置。外部自定义 Runtime 镜像须自行实现这些扩展接口。

自动化新增 `tests/openai-chatwoot-fixture-test.sh`：模拟提供商与按 v4.18.0 协议构造的 Chatwoot，覆盖 Key 创建/更换/前缀、动态模型/手动回退、选定模型测试、真实 HTTPS Nginx Webhook、Chromium 桌面/手机后台、多轮隔离、签名/重复投递、有限重试、人工接管/恢复；安装器 fixture 用实际 tar 备份验证知识正文、密钥和绑定在重复安装、升级及恢复后保留。模拟访客消息查询证明回复进入原会话，但**不是运行真实 Chatwoot widget 的联网验收**。

`tests/chatwoot-lifecycle-fixture-test.sh` 覆盖自动结束/恢复、同 ID 复用、状态乱序、人工竞争、旧消息分界及 schema 3 升级。`chatwoot_source_protocol_test.py` 直接执行固定 v4.18.0 Ruby 源码的监听器、HMAC 签名器、事件 presenter、重新打开方法和分配服务，再将生成的真实协议请求送入 Runtime 与 HTTPS Nginx；源码及 MIT 许可保存在 `tests/fixtures/chatwoot-v4.18.0/`，SHA-256 在 `SOURCE.json` 校验。测试需 MRI Ruby + Prism（例如 Ruby 3.3+），可设置 `TEST_RUBY_BINARY`；`REQUIRE_CHATWOOT_SOURCE_TEST=true` 禁止缺工具时跳过。该测试不是完整 Rails/PostgreSQL/Sidekiq 服务验收，源码中的存储和出站传输由隔离适配器代替。

最终需在 Ubuntu 服务器由用户填入真实 Key：确认模型连接成功、Chatwoot 可投递 HTTPS 签名 Webhook、访客窗口实际收到 AI 回复、连续提问隔离、转人工/恢复、真实容器重启及备份恢复。没有真实 Key、Chatwoot 服务或 Ubuntu Docker 主机时，不宣称这些真实端到端验证已通过。

### 按客服实例编辑知识正文

在 `/admin/manage/knowledge` 选择实例、点击“加载正文”，即可首次创建、读取、修改、保存或清空纯文本正文。保存成功有明确反馈，重新打开仍能读取；未保存修改时离页会提示。新知识库配置预填 `knowledge.txt`，无需 SSH 或填写绝对服务器目录；已有 `source` 不自动改写。**已启用的实例也可直接关联新知识库，无需先停用。**安全路径下正文文件或其目录尚不存在时按空知识处理，关联校验和 AI 请求不会自动创建文件；首次保存正文时，沿用安全的目录创建及原子写入流程，下次请求立即读取。只有文件不存在可以为空，权限、链接、非普通文件和 UTF-8 读取错误仍然拒绝；引用的配置档缺失仍属无效配置。未关联配置或原 `source` 为空时，页面提示先设置关联/文件名，不擅自改变旧配置。

正文实际存储在现有 Runtime 的 `ai_service_data` 持久化卷：`AI_SERVICE_DATA_PATH/services/<service_id>/<source>`。不同实例即使引用同一个 `knowledge_config`，也只共享相对文件名和 `max_characters`，不共享正文。清空保留原文件路径及关联；切换 `source` 不移动或删除旧文件。API/YAML 优先级、SQLite schema 和其他配置字段均不变。

内部接口 `/management/services/{service_id}/knowledge` 使用现有 `SERVICE_MANAGEMENT_TOKEN`：GET 返回 `content`、`exists`、`revision` 及关联元数据；PUT 接收 `content` 与 GET 得到的 `revision`；DELETE 接收 `revision`，将正文置空。并发旧版本、路径或关联变化返回 `knowledge_conflict`，不覆盖当前内容。浏览器仅提交 Session/CSRF 表单，由 Admin 服务端调用接口；内部 Token 不下发页面，也没有匿名写入口。

正文上限为 **1 MiB UTF-8 字节**，允许中文、多行、空正文，拒绝无效编码及空字符。旧合法相对路径和 UTF-8 文件（含 CRLF/BOM）可继续读取；超大旧文件不会被网页截断，Runtime 仍按原 `max_characters`（1–100000）读取前缀。禁止绝对/穿越路径、符号链接、硬链接、系统配置及凭据文件名。Ubuntu 上逐层使用不跟随链接的目录描述符，写入同目录 `0600` 临时文件并原子替换，失败不损坏原文件。日志仅包含稳定错误码，不记录正文。

Runtime 每次请求重新读取文件，无需重启，正文以知识上下文传给原模型；提示词、回复规则、模型、渠道及 `max_characters` 不改变。重启、重复安装和升级继续挂载原命名卷。现有备份已覆盖**整个 Runtime 数据目录**（`data/ai-service-data.tar[.gz]`），包括正文及管理数据库，不新增备份机制；恢复会在停止服务后还原该卷。备份含正文和凭据，须继续保密，不删除原卷或更换 Compose 项目名。

`tests/knowledge-fixture-test.sh` 覆盖真实 API/文件/Runtime 与 Chromium → TLS Nginx → Admin 表单；检查创建、编辑、清空、隔离、并发冲突、非法路径/链接、认证、UTF-8、大小、写入失败及桌面/手机布局。`tests/admin-installer-fixture-test.sh` 使用真实安装/备份/恢复/升级入口和实际 tar 正文字节，Docker/宿主机命令用 fixture 替代，校验重复安装与恢复后的内容和 Runtime 读取。截图仅保存于忽略 Git 的 `runtime/tests/knowledge-ui-*`。自定义旧 AI 镜像若无此新内部接口，需部署更新后的内置 Runtime，页面不会假报保存成功。

`tests/admin-forms-fixture-test.sh` 覆盖字段契约、新增/编辑/重新读取、配置关联、旧 YAML/字段保留、非法输入及安全校验；浏览器部分使用真实 Chromium → TLS Nginx → HTTP Admin → 真实管理 API，验证七类页面和 1440 / 390 / 320 像素布局。测试工具配置与下述 HTTPS 浏览器套件相同，截图仅保存在忽略 Git 的 `runtime/tests/admin-forms-ui-*` 供检查。

认证与安全：

- 初始账号/密码为 `admin/admin`。安装完成显示 `/admin` 地址、公开初始凭据及首次登录修改密码提示，不再生成随机初始密码或额外凭据文件。高级用户可在本地 `.env` 配置 `ADMIN_PASSWORD`；真实自定义密码不显示、不写日志、不提交 Git。默认密码是公开弱密码，请部署后立即修改，建议首次改密前限制管理入口的公网访问。
- 后台修改密码必须验证当前密码，新密码至少 **6 个字符**、不限制复杂格式（资源保护上限 1024 字符）；`admin` 仅作为出厂例外，不能作为新的 5 位密码保存。密码直接保存为数据卷 `auth.sqlite3` 中的 `admin_settings.ADMIN_PASSWORD` 设置，不再进行新密码哈希派生；该文件 `0600`、目录 `0700`，数据库与本地 `.env` 均忽略 Git。此简单模式会在受保护配置中保存明文密码，备份也含密码，请严格保护配置、数据卷和备份访问权限。
- 配置优先级：持久化 `ADMIN_PASSWORD` 设置 > 首次初始化时的 `.env` 值 > 出厂 `admin`。`.env` 是初始化输入，不会在重启、重新安装时覆盖后台已修改的密码；需要更新已有账号时，请通过后台修改密码。完成提示中的 `admin` 是初始值，不表示已修改的密码被重置。恢复旧备份会恢复当时的密码。
- 旧配置升级：先读取现有数据库里的有效账号哈希，其次才是 `ADMIN_PASSWORD_HASH` 环境变量；即使环境中存在默认 `ADMIN_PASSWORD=admin`，也不重置旧账号。哈希不可还原，因此仅保留旧格式的校验器，首次正确登录或通过已有 Session 修改密码后，事务性保存实际密码到新 `ADMIN_PASSWORD` 设置并退役旧账号记录。旧哈希字段之后不再用于认证，可从本地 `.env` 移除；忘记旧密码须另行明确重置，不会自动改成默认密码。升级前请备份，旧版本回滚使用原备份恢复流程。
- Session 为随机不透明 Cookie，数据库只存其摘要；默认一小时有效，登录轮换、退出撤销、修改密码撤销全部 Session。Cookie 使用 Secure、HttpOnly、SameSite=Strict，所有写请求验证 CSRF，登录失败有全局限速。仅支持 HTTPS 登录。
- 浏览器使用 `/admin/api/*` + Session；后台服务端再用 `SERVICE_MANAGEMENT_TOKEN` 调用内部 `/management/*`。不返回 Token、环境变量、完整异常或维护脚本输出。原 `/chat` 和 `/management` Token 认证继续独立工作。

### HTTPS 登录与来源校验

请求链路为浏览器 HTTPS → Nginx TLS 终止 → HTTP Admin 容器。Admin 的两个 Nginx location 保留原始 Host（包括外部端口），覆盖 `X-Forwarded-Proto` 为 Nginx 实际协议；不改写浏览器 Origin。Uvicorn 入口原本已启用代理头信任，Compose 继续只发布 loopback 端口：这条信任边界不能改为公网直连 Admin，也不能直接采信浏览器的 `X-Forwarded-Host`、`X-Forwarded-Port` 或 `Forwarded`。

写请求仍按 Session → 请求体/CSRF → Origin → 业务操作校验。来源按 `(scheme, host, effective_port)` 比较，DNS 大小写和默认 HTTPS `:443` 等价，不把 ASGI 路径前缀作为来源；其他协议、其他主机、其他有效端口，以及空、重复、`null`、带路径/用户信息的 Origin 均拒绝。没有 Origin 的既有客户端仍必须通过 Session 和 CSRF。HTTP 跳转使用配置的 `DOMAIN_HTTPS_PORT`，默认 443 的跳转格式保持不变。

此前 HTTP 客户端测试只证明：主动提供正确 Origin 时，标准 443 请求可以登录；它不能验证浏览器如何为原生 HTML Form POST 生成 Origin。此前修复的 `$host` 端口丢失、`request.base_url` 字符串比较问题仍有回归覆盖，但不是浏览器 `Origin: null` 的根因。

真实 Chromium（Chrome 155）现已复现另一条完整链路：登录页响应 `Referrer-Policy: no-referrer` → 浏览器原生表单 POST 自动发送 `Origin: null`，即使 `sec-fetch-site: same-origin` → 严格来源检查返回 `403 origin_rejected`。这是 [Fetch Origin 算法](https://fetch.spec.whatwg.org/#origin-header) 的规定，不是密码错误。Admin 统一改用 `Referrer-Policy: same-origin`，让同源表单保留有效 Origin，同时仍不向其他来源发送 Referer；参见 [same-origin 策略](https://w3c.github.io/webappsec-referrer-policy/#referrer-policy-same-origin)。保留原始 HTML 表单，不添加 JS 登录、不改代理或服务器配置，不接受 `Origin: null`，也不把 `sec-fetch-site` 当作来源校验的替代。

真实反代回归使用生产 Uvicorn 启动参数和仓库 Nginx 模板，启动临时 TLS Nginx（测试工具需支持 `-e stderr`，如 1.20+）与 HTTP Uvicorn。域名、密码、证书、端口和数据目录均在测试时生成；证书验证保持开启，不改系统 hosts、DNS、证书或现有 Nginx。覆盖标准/非默认 HTTPS authority、正常登录及改密、恶意来源、伪造转发头、CSRF/Session 和 HTTP 跳转。

```bash
# 使用真实 nginx 和 openssl（不要指向 tests/fixtures 的替身）。
ADMIN_TEST_PYTHON="$PWD/runtime/admin-venv/bin/python" \
TEST_NGINX_BINARY="$(command -v nginx)" \
TEST_OPENSSL_BINARY="$(command -v openssl)" \
REQUIRE_ADMIN_PROXY_TEST=true bash tests/admin-proxy-fixture-test.sh
```

缺少测试工具时仅跳过真实网络部分；设置 `REQUIRE_ADMIN_PROXY_TEST=true` 会改为失败，CI 应启用该选项。更新已有部署需要构建包含修复的新 Admin 镜像，并通过安装器重新渲染/验证/reload 站点；仅 `git pull` 或重启旧镜像不会更新代码和生效中的 Nginx。使用升级模块时须为本地 Admin 构建选择新的固定目标标签，保持原镜像可回滚，不重置密码或数据。

真实浏览器回归另外运行（Playwright 仅是测试依赖，不进入生产镜像）：

```bash
runtime/admin-venv/bin/python -m pip install -r tests/requirements-browser.txt
# 可以使用 Playwright 配套 Chromium，或通过 TEST_CHROMIUM_BINARY 指定
# 已安装的 Chromium/Chrome 可执行文件；始终启动临时隔离 profile。
runtime/admin-venv/bin/python -m playwright install chromium
ADMIN_TEST_PYTHON="$PWD/runtime/admin-venv/bin/python" \
TEST_NGINX_BINARY="$(command -v nginx)" \
TEST_OPENSSL_BINARY="$(command -v openssl)" \
REQUIRE_ADMIN_BROWSER_TEST=true bash tests/admin-browser-fixture-test.sh
```

浏览器点击真实表单，测试不手工提供 Origin/Referer，也不关闭 Web 安全、SameSite 或 CSRF。使用临时域名解析规则及仅匹配测试证书公钥的进程级 TLS 例外，不安装系统证书、不修改 hosts、现有 Nginx 或用户浏览器 profile；不记录 HAR、trace、密码、Cookie 或 Token。覆盖 HTTPS 登录/退出、强制 no-referrer 后的 null Origin 拒绝、带有效 CSRF/Session 的跨来源表单拒绝、真正跨站的 Strict Cookie 防护、CSRF/Session 校验和跨来源 Referer 隐私。缺少依赖时可跳过；CI 设置 `REQUIRE_ADMIN_BROWSER_TEST=true` 后缺少工具或浏览器即失败。本次策略修复仅需部署更新后的 Admin 应用，并重新打开登录页，不要求修改服务器配置或已有凭据。

配置 API：内部 `/management/profiles/{ai|prompts|knowledge|channels|runtime}` 支持 GET 列表、POST 创建；`/{id}` 支持 GET、PUT。AI 字段沿用 `model`、`api_base_url`、`api_key_env`；提示词沿用 `system_prompt`、`reply_rules`；知识库沿用 `source`、`max_characters`；渠道仅存 `enabled`、`provider`、`config_ref`；Runtime 仅有 `default` 记录的 `default_service_id`。不接收 API Key / 密码 / Token 字段。后台对应接口位于 `/admin/api/{kind}`，修改请求还需 `X-CSRF-Token`。浏览器从页面表单取得 CSRF，不取得内部认证 Token。

profile API 采用与现有配置相同的 SQLite 覆盖方式，不重写 YAML：API profile > 原 profile YAML；Runtime 默认实例为 API default > `AI_DEFAULT_SERVICE_ID` > 唯一启用实例。schema 按版本事务性迁移至 4，原 Service / Domain / profile 数据保留，下一次 Runtime 请求读取新配置。原来的单实例环境变量模式继续有效。

### 后台系统维护

`/admin/maintenance` 调用原 `backup.sh`、`restore.sh`、`upgrade.sh`、`rollback.sh`，不重写其数据处理逻辑。每次操作必须输入对应的 `BACKUP` / `RESTORE` / `UPGRADE` / `ROLLBACK`；恢复/回滚还必须选择有效版本 ID。升级目标由 `.env` 中固定的 `UPGRADE_*` 或当前版本决定；本地 Runtime/Admin 使用构建流程，外部镜像使用拉取流程，不自动发布镜像。

维护由宿主机 systemd 执行器运行，通过权限为 `0600` 的 Unix socket + 独立随机 Token 接收四种固定命令，拒绝任意路径、参数和 Shell。Web 容器不挂载 Docker socket、宿主 `.env` 或备份目录。执行器有维护主机所需的 root 权限，应部署在可信、受保护的仓库目录；其 socket 访问权限等同于授权执行这四种高权限操作，不是通用沙箱。

任务串行执行，状态写入 `ADMIN_MAINTENANCE_STATE_DIR`，容器停机/重建不会中断宿主机任务。恢复/升级时后台可能暂时不可达，稍后重新登录查看结果；后台只显示状态、版本 ID，不显示脚本日志。进程意外重启会标记未完成任务为 `interrupted`，不会自动重试破坏性操作，需操作者检查服务器。CLI 运维期间请勿并行提交后台任务。执行器安装时不强制重启已有服务，代码更新请在确认无维护任务时手动重启配置指定的 systemd unit。

统一后台与 Runtime 覆盖 Compose 文件、管理员配置数据库、Session 数据、API 配置均随已有备份保存/恢复；维护任务账本留在宿主机，不回滚任务历史。旧备份格式仍可读取。不开启定时任务、云存储、多管理员、角色权限、多租户或统计报表。未来在同一入口扩展多 AI、多渠道、多域名、多知识库与多平台。

测试：

```bash
python3 -m venv runtime/admin-venv
runtime/admin-venv/bin/python -m pip install -r platform_admin/requirements.txt -r ai_runtime/requirements.txt
ADMIN_TEST_PYTHON="$PWD/runtime/admin-venv/bin/python" bash tests/admin-fixture-test.sh
ADMIN_TEST_PYTHON="$PWD/runtime/admin-venv/bin/python" bash tests/admin-module-fixture-test.sh
ADMIN_TEST_PYTHON="$PWD/runtime/admin-venv/bin/python" bash tests/admin-installer-fixture-test.sh
runtime/admin-venv/bin/python tests/admin_compose_check.py
```

可用 `ADMIN_TEST_PYTHON` 指定 fixture 的 Python、`TEST_COMPOSE_BINARY` 指定独立 Compose 可执行文件；检查只输出结论，不打印解析后的敏感环境配置。Cookie 属性参考 [Starlette Cookie 接口](https://starlette.dev/responses/)。

`admin-installer-fixture-test.sh` 在隔离副本中执行真实 `install.sh` 入口，覆盖 Ubuntu 22.04 / 24.04 元数据、首次成功、重复安装、失败后续跑及完成提示；Docker、Nginx、Certbot、systemd 等外部操作使用替身，宿主写操作限制在临时目录，不是实际服务器部署测试。

## Service Management Core

Service Management Core 是未来统一 AI 客服管理后台的数据基础。它将入口关系拆分为“域名 → 客服实例 → AI 配置 / 提示词配置 / 知识库配置 / 渠道配置”，只保存通用标识和配置引用，不绑定任何品牌、真实域名、模型或外部平台。

客服实例默认配置位于 `config/services/services.yaml`，字段为 `id`、`name`、`enabled`、`ai_config`、`prompt_config`、`knowledge_config` 和 `channel_config`。域名绑定默认配置位于 `config/domains/domains.yaml`，字段为 `domain`、`service_id`、`enabled` 和 `default_entry`。仓库中的两个默认文件均为空；对应的 `*.example.yaml` 只展示禁用的通用结构。

实际配置分别写入 `config/services/local.yaml` 和 `config/domains/local.yaml`。两个文件均已被 Git 忽略，且必须使用 `0600` 或更严格的权限。加载顺序为默认配置后加载用户配置：相同客服实例 ID 或域名的用户字段覆盖默认字段，也可以追加新记录。域名加载时会验证 `service_id` 引用存在，启用的域名只能绑定启用的客服实例，并且最多只能有一个启用的默认入口。

`lib/services.sh` 提供客服实例加载、校验、ID 列表和按 ID 查询接口；`lib/domains.sh` 提供域名绑定加载、关系校验和按域名解析客服实例接口。配置结构不包含 API Key、密码或 Token 字段，也不会输出配置内容。安装流程加载并验证这些数据，AI Runtime 再按请求解析实例；域名配置目前仍只校验绑定关系。

这一核心层为未来统一后台管理多 AI、多域名、多入口、多平台以及多知识库关系保留了稳定边界。

## AI Runtime 多实例运行

Service Instance 已接入 `ai_runtime/` 中的 FastAPI Runtime。`POST /chat` 接收 `message` 和可选的 `service_id`，按实例加载模型、系统提示词、回复规则与知识库上下文，再调用配置指定的兼容 Chat Completions API。请求必须使用 `Authorization: Bearer <AI_SERVICE_INTERNAL_TOKEN>`；返回值仅包含 `service_id` 和 `reply`。API 适配约定参考 [OpenAI 官方文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

`config/services/services.yaml` 和本地 `local.yaml` 的格式保持不变。实例的 `ai_config`、`prompt_config`、`knowledge_config` 仍是配置引用，分别解析到同目录下 `ai/<引用>.yaml`、`prompts/<引用>.yaml`、`knowledge/<引用>.yaml`。每个目录均提供 `*.example.yaml`；实际配置文件已被 Git 忽略。`channel_config` 继续保留为未来入口连接引用。

例如，本地 `config/services/local.yaml` 可以配置两个独立实例：

```yaml
version: 1
services:
  - id: service_a
    name: Service A
    enabled: true
    ai_config: ai-a
    prompt_config: prompt-a
    knowledge_config: knowledge-a
    channel_config: ""
  - id: service_b
    name: Service B
    enabled: true
    ai_config: ai-b
    prompt_config: prompt-b
    knowledge_config: knowledge-b
    channel_config: ""
```

`ai/ai-a.yaml` 配置 `model`、`api_base_url` 和 `api_key_env`；后者只保存凭据的环境变量名。`prompts/prompt-a.yaml` 配置 `system_prompt` 和 `reply_rules` 字符串列表。`knowledge/knowledge-a.yaml` 配置 `source` 和 `max_characters`，`source` 是相对 `AI_SERVICE_DATA_PATH/services/service_a/` 的 UTF-8 文本文件路径。B 实例使用自己的引用和数据目录。空的提示词/知识库引用表示不附加该上下文；引用非空时对应配置档必须存在，但安全路径下尚未创建的正文按空知识处理，不阻止关联和运行。当前知识库实现为受大小限制的本地文本上下文，不包含向量检索。

省略 `service_id` 时，优先使用 API Runtime default 配置，其次为环境变量 `AI_DEFAULT_SERVICE_ID`；未指定且只有一个启用实例时使用该实例。多个实例启用时必须明确设置默认实例。只有实例集合完全为空时，才使用已有的 `AI_MODEL`、`OPENAI_API_KEY` 环境变量，以及可选的 `AI_SYSTEM_PROMPT` 和 `AI_API_BASE_URL`，保留单实例环境变量模式。显式无效、未知、禁用实例或缺失配置会返回明确错误，不会借用其他实例配置。

Runtime 每次请求独立加载默认配置和用户覆盖配置，不保存共享对话上下文；知识文件限制在对应实例的数据目录内。API Key 优先读取模型对应的加密凭据，未设置时沿用环境变量；配置/请求校验错误及上游错误均返回错误码，不输出完整配置、消息内容或上游响应。多实例隔离是运行配置与上下文隔离；未来的多租户权限管理仍需单独实现。

使用仓库内 Runtime 时，在本地 `.env` 设置 `AI_RUNTIME_BUILD=true`、固定版本的 `AI_SERVICE_IMAGE`（作为本地构建标签）和现有 `AI_ADMIN_IMAGE`。安装器会加载 `stacks/ai-service/compose.runtime.yaml`，构建 Runtime、拉取管理服务镜像，并将 `config/services/` 只读挂载。默认 `AI_RUNTIME_BUILD=false`，原有自定义镜像流程继续可用，AI Service 仍为可选模块。自定义外部镜像需要自行集成此 Runtime 才能处理多实例请求。

共享凭据通过本地 `.env` 的 `OPENAI_API_KEY` 传入；每实例独立凭据可写入项目根目录的 `.env.ai-runtime`，例如使用不同的环境变量名，并在各 AI 配置的 `api_key_env` 中引用。该文件只用于提供者凭据，已被 Git 忽略；也可用 `AI_RUNTIME_SECRETS_FILE` 指定受保护的环境文件。设置这些本地文件权限为 `0600`，更新凭据后重建 AI 容器使环境变量生效。请勿将完整安装器 `.env` 作为 Runtime 的额外环境文件。

本阶段提供多 AI 模型与多知识库的运行基础，未来可接入多入口和多平台。健康端点 `GET /health` 仅检查 Runtime 进程存活；配置、凭据和知识文件的可用性在请求时检查。离线测试无需真实 API Key：

```bash
python3 -m venv runtime/ai-runtime-venv
runtime/ai-runtime-venv/bin/python -m pip install -r ai_runtime/requirements.txt
AI_RUNTIME_TEST_PYTHON="$PWD/runtime/ai-runtime-venv/bin/python" bash tests/ai-runtime-fixture-test.sh
```

## Service Management API

Service Management API 是统一 AI 客服管理后台的后端基础，已集成到仓库内的 FastAPI Runtime。启用 `AI_RUNTIME_BUILD=true` 后，管理接口与 `/chat` 使用同一服务端口，默认仅监听宿主机环回地址。它管理客服实例、域名映射与非敏感 Runtime profile，供唯一 `/admin` 入口使用，不操作 DNS、SSL 或外部平台。

所有 `/management/*` 接口要求 `Authorization: Bearer <SERVICE_MANAGEMENT_TOKEN>`。该 Token 由安装器自动生成，保存在权限为 `0600` 的本地 `.env` 中，且与聊天接口的 `AI_SERVICE_INTERNAL_TOKEN` 分开使用；聊天 Token 无权修改管理数据。升级已有部署时重新运行安装器可补齐缺失 Token，已有值会保留。直接运行 Runtime 时须通过环境变量提供至少 32 字符的管理 Token，缺失或过短时管理请求拒绝访问。

| 对象 | 接口 | 用途 |
| --- | --- | --- |
| Service | `GET /management/services`、`POST /management/services` | 列表、创建 |
| Service | `GET /management/services/{id}`、`PUT`、`PATCH`、`DELETE` 同一路径 | 查询、完整替换、局部修改、删除 |
| Service | `POST /management/services/{id}/enable`、`/disable` | 启用、禁用 |
| Domain | `GET /management/domains`、`POST /management/domains` | 列表、创建 |
| Domain | `GET /management/domains/{domain}`、`PUT`、`PATCH`、`DELETE` 同一路径 | 查询、重新绑定或修改、删除 |
| Domain | `POST /management/domains/{domain}/enable`、`/disable` | 启用、禁用 |

Service 创建请求使用现有七个字段：`id`、`name`、`enabled`、`ai_config`、`prompt_config`、`knowledge_config`、`channel_config`。`id` 与 `name` 必填，`enabled` 默认 `false`，四个配置引用默认空。`PATCH` 只提交需修改字段，`PUT` 提交完整字段；ID 不可修改。启用的实例必须能加载引用的模型、提示词及知识库配置；正文尚不存在时允许关联，按空知识运行，已有文件必须可安全读取。配置无效时拒绝提交并保留原配置；禁用实例可先保存未完成的引用。提供者凭据仍支持沿用环境变量，不要求通过管理 API 设置。

Domain 创建请求使用 `domain`、`service_id`、`enabled`、`default_entry`，后两项默认 `false`。域名会统一为小写，拒绝 URL、通配符、IP 和无效标签。绑定的实例必须存在，启用域名只能指向启用实例，最多保留一个启用的默认入口。有域名引用的实例不能删除；有启用域名引用的实例不能禁用。禁用默认域名的专用接口会同时清除其 `default_entry` 标记。

请求示例（使用管理 Token 调用对应接口）：

```json
{"id":"service_a","name":"Service A","enabled":false,"ai_config":"ai-a","prompt_config":"prompt-a","knowledge_config":"knowledge-a","channel_config":""}
```

```json
{"domain":"entry.example.test","service_id":"service_a","enabled":false,"default_entry":false}
```

存储评估：静态 YAML 适合作为可审阅的初始配置，但 API 写入需要事务、并发控制以及删除记录。因此本阶段使用 Python 标准库 SQLite 保存 API 覆盖数据，保留两个原始 YAML 文件和本地覆盖文件不变。有效配置优先级为 **API SQLite 记录 > 本地 `local.yaml` > 默认 YAML**。API 修改会保存该条记录的完整有效值；未被 API 修改的记录继续读取 YAML，已被 API 修改的记录不会被后续 YAML 编辑覆盖。删除采用覆盖层删除标记，防止底层 YAML 记录重新出现。

SQLite 默认位于 `AI_SERVICE_DATA_PATH/management/core.sqlite3`，使用现有 AI 数据卷持久化，并纳入该数据卷的备份恢复。可用 `SERVICE_MANAGEMENT_DATABASE` 指定容器内的其他绝对路径；自定义路径需自行确保持久化。数据库目录和文件在 Ubuntu 上分别要求 `0700`、`0600`，数据库文件已被 Git 忽略。配置目录仍是只读挂载，管理 API 不写入原 YAML。API 与 Runtime 使用同一覆盖层，成功修改后的下一次 `/chat` 请求即可读取新配置，无需重启。

首次 API 写入会事务性地创建并迁移数据库结构，当前 schema 版本为 2。已有 YAML 无需强制导入即可继续使用；如需将当前有效 YAML 固定为 API 记录，可调用带管理认证的 `POST /management/migrations/import-yaml`，或在已配置环境的 Runtime 中运行 `python -m ai_runtime.management_migrate`。导入可重复执行，保留已有 API 修改与删除标记，不改写原文件。遇到不支持的较新 schema 版本时会拒绝读写，避免旧程序修改新数据库。

创建成功返回 `201`，删除成功返回 `204`；非法输入返回 `422`，认证失败返回 `401`，重复记录或绑定冲突返回 `409`，存储或认证配置不可用返回 `503`。错误响应只包含稳定错误码，不回显请求体或数据库异常。接口只接受配置引用，不接收 API Key、密码或 Token 字段，也不记录完整配置。

API 测试和 fixture 可直接使用上一节创建的 Python 环境：

```bash
AI_RUNTIME_TEST_PYTHON="$PWD/runtime/ai-runtime-venv/bin/python" bash tests/management-api-fixture-test.sh
```

备份版本保存在 `BACKUP_DIR`，目录和文件分别限制为仅所有者可访问。每个版本包含 Chatwoot PostgreSQL dump、Chatwoot storage、AI Service 与管理后台持久化数据、受保护的 `.env`、两套 Compose 文件、Nginx 站点配置、SSL 配置记录、版本 manifest 和 SHA-256 校验清单。证书私钥不会复制进备份；`.env` 含真实 Secret，因此备份目录仍应放在受控磁盘中。当前仅支持本地备份，不上传云存储，也不创建定时任务。

升级目标由 `UPGRADE_CHATWOOT_VERSION`、`UPGRADE_AI_SERVICE_IMAGE` 和 `UPGRADE_AI_ADMIN_IMAGE` 明确指定。移动标签 `latest` 会被拒绝。升级器先验证当前安装和目标 Compose 模型，再创建完整备份和升级记录；随后拉取目标镜像、原子更新 `.env`、停止应用服务、执行 Chatwoot 数据库迁移、重建服务并运行健康检查。数据库迁移、服务重建或健康检查失败时，会自动恢复刚创建的升级前备份。PostgreSQL 和 Redis 镜像不会被升级模块隐式跨版本升级。

## 安装流程规划

安装器按照以下顺序执行：

1. 创建或加载 `.env`，交互收集缺失的非敏感配置；
2. 自动生成缺失的 Secret，初始化管理员默认配置（不覆盖已有账号）；
3. 检查 Ubuntu 版本、权限和服务器资源；
4. 检查或安装 Docker 与 Docker Compose；
5. 配置并启动 PostgreSQL、Redis、Chatwoot 和 Sidekiq；
6. 如 `.env` 配置了 AI 镜像，则部署并验证 AI Service 与管理后台；否则安全跳过；
7. 配置 Nginx 反向代理及 WebSocket；
8. 检查域名解析与 Nginx HTTP-01 前置条件；
9. 申请 Let's Encrypt 证书并切换 HTTPS；
10. 安装续签 deploy hook、启用 Certbot timer 并执行 dry-run；
11. 执行健康检查并输出访问地址；启用统一后台时显示公开初始凭据及改密提醒。

入口脚本通过注册步骤调用各个模块。模块之间不直接依赖，只通过 `lib/` 中的公共接口共享日志、配置、状态和校验能力，为重复执行和断点恢复预留基础。

## 当前运行方式

直接运行统一入口：

```bash
bash install.sh
```

安装向导会创建 `.env`、只收集域名与邮箱并生成 Secret；随后执行只读系统检测，在 Docker 环境缺失时进入 Docker 官方安装流程，部署 Chatwoot Stack，安装 Nginx，并为单个域名申请证书和启用 HTTPS。未配置 `AI_SERVICE_IMAGE` 时不部署 AI Service；高级用户可以先在 `.env` 指定两个 AI 固定版本镜像再运行安装。证书续签由 Certbot systemd timer 执行，续签成功后的 deploy hook 会先运行 `nginx -t`，再 reload Nginx。
系统检测只支持 Ubuntu 22.04 和 24.04；在其他操作系统上运行会输出检测错误并安全退出。

首次部署前请确认域名已经解析且公网端口 80、443 可访问。默认会执行一次 `certbot renew --dry-run`；如需在受限测试环境暂时跳过，可设置 `CERTBOT_RENEW_DRY_RUN=false`，生产部署建议保持启用。

## 常用管理命令

安装完成后，可以直接在项目根目录统一管理服务，不需要进入 `stacks/chatwoot/` 或 `stacks/ai-service/`：

```bash
./manage.sh status
./manage.sh logs
./manage.sh logs-follow
./manage.sh restart
./manage.sh stop
./manage.sh start
./manage.sh health
./manage.sh reset-admin-password
./manage.sh reset-admin-password --manual
```

`status` 显示 Chatwoot、Sidekiq、PostgreSQL、Redis 以及可选 AI Service 的运行状态；`health` 额外检查 PostgreSQL、Redis 和 HTTP 健康端点。未配置 `AI_SERVICE_IMAGE` 时，管理脚本只管理 Chatwoot，并显示“AI Service 未启用，跳过。”。普通服务管理命令只读取现有 `.env` 和 Compose 文件，不会修改配置、生成 Secret、拉取镜像或执行数据库迁移。

### 官方管理员密码恢复

忘记统一后台密码时，在 Ubuntu 服务器上的本地控制终端运行 `./manage.sh reset-admin-password`（SSH 需分配 TTY）。命令生成 256 位安全随机密码，只在数据库确认提交成功后直接向控制终端显示一次，不写入标准输出、标准错误或 Docker 日志。请在安全终端妥善保存，不要录屏或启用终端会话记录。

`./manage.sh reset-admin-password --manual` 隐藏输入新密码并要求二次确认；最少 6 位，不限制复杂格式。不支持命令行、环境变量、管道输入密码，也没有公开 HTTP 恢复接口。需要服务器 root、Docker 管理权限或经授权的 sudo；sudo 授权发生在输入新密码之前。仅允许本机 Unix Docker socket，不接受远程 Docker context。

恢复始终针对已有统一 Admin 的 `admin` 账号：从配置读取 Compose 项目/服务标识，通过容器标签定位现有运行中的容器，并校验其可写持久化数据卷。不会创建替代容器或新认证库。后台未运行时先修复或启动原容器；缺少原数据库、卷、容器或无法识别的认证格式会安全拒绝。支持旧版密码哈希数据迁移。使用此新命令前需部署包含本功能的 Admin 镜像；若旧镜像尚不具备并发会话撤销保护，命令会在写入前拒绝并提示升级，避免仅更新宿主机脚本却仍运行旧认证代码。

密码更新、登录失败次数/429 锁定清除、全部旧会话注销在同一个 SQLite 事务中完成，数据库失败会完整回滚。正在进行的旧密码登录也不能重新建立已撤销的会话。使用现有受保护的认证数据卷持久化，不修改 `.env`、Secret、Token、Chatwoot、Runtime、Service Management、知识库或聊天数据。重启、重复安装和升级仍以卷内密码为准，不回退到 `.env` 的初始化密码；备份包含此认证卷，应同样保密。若 Docker 连接在提交期间中断，命令不宣称成功，请检查本机服务后使用手动模式重试；不要通过删除数据卷恢复密码。

手动创建备份、查看版本和恢复：

```bash
./backup.sh
./restore.sh --list
./restore.sh BACKUP_ID
```

恢复会要求输入 `RESTORE`，自动化环境必须显式传入 `--yes`。恢复开始前会校验 manifest、全部 SHA-256 和归档格式，然后停止配置指定的应用服务；成功恢复数据库、持久化数据与配置后才重新启动服务并执行 HTTP、PostgreSQL 和 Nginx 健康检查。

配置明确的目标版本后，可以执行安全升级、查看升级记录或回滚最近一次成功升级：

```bash
./upgrade.sh
./rollback.sh --list
./rollback.sh
./rollback.sh UPGRADE_ID
```

升级要求输入 `UPGRADE`，回滚要求输入 `ROLLBACK`。只有操作者明确指定 `--yes` 时才允许非交互执行。升级记录保存在 `UPGRADE_RECORD_DIR`，版本记录不包含凭据；同一受保护目录还保留 `0600` 的源/目标配置快照，含 Secret，禁止提交 Git 或公开分享。手动回滚会验证当前版本与记录中的升级目标一致，并通过升级前备份恢复数据库、持久化文件、`.env`、Compose 配置、Nginx 配置和服务状态。

## 后续扩展方向

- Chatwoot 单实例生产部署；
- 知识库与向量检索；
- 多 AI 与独立模型配置；
- 多消息入口和多知识库绑定；
- 多外部客服平台连接；
- 多客服渠道连接；
- 多域名入口；
- Cloudflare DNS API、DNS-01 自动验证与泛域名证书；
- 云备份、异地复制和自动调度；
- 更细粒度的组件升级路径和运维工具。

当前已实现多客服实例的配置与 AI Runtime 路由；多平台连接、多租户权限、多域名部署和自动化入口管理继续作为后续阶段。
