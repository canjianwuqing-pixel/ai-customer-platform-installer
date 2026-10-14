# AI Customer Platform Installer

一个面向 Ubuntu 服务器的模块化 AI 客服平台安装器项目。项目通过一个入口脚本完成 Chatwoot 基础部署，并可按需启用独立 AI Service 和 AI 管理后台，为后续知识库等组件保留清晰的扩展边界。

## 当前阶段

当前仓库已经实现单域名 HTTPS 部署所需的基础模块，包括：

- 安装入口和模块注册机制；
- 统一日志、配置读取、状态记录和基础校验接口；
- System 环境检测与 Docker 官方安装流程；
- Chatwoot、PostgreSQL、Redis、Sidekiq 的 Docker Compose Stack；
- 可选的 FastAPI AI Service 与 AI 管理后台 Docker Compose Stack；
- 基于 Service Instance 的 AI Runtime 请求路由与实例上下文隔离；
- AI 服务独立数据卷、环回地址默认绑定和敏感日志禁用约束；
- Chatwoot Webhook、Secret 与 Inbox 集成参数预留；
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

`DATABASE_PASSWORD`、`REDIS_PASSWORD`、`SECRET_KEY_BASE`、`AI_SERVICE_INTERNAL_TOKEN`、`AI_ADMIN_SESSION_SECRET`、`AI_ADMIN_PASSWORD` 和 `SERVICE_MANAGEMENT_TOKEN` 可以留空；安装器首次运行时会自动生成安全随机值，重复执行时保留已有值，并将 `.env` 权限限制为 `0600`。启用 AI Service 时，管理员账号固定为 `admin`，初始密码只在该模块完整安装成功后显示一次。不得向 Git 提交真实域名、邮箱、数据库密码、Redis 密码、密钥、证书或 Token。`.env`、PEM 和私钥文件已加入 `.gitignore`。

`DOMAIN` 是当前唯一部署域名；`LETSENCRYPT_EMAIL` 用于 Let's Encrypt 账户和证书通知，`EMAIL` 继续作为 Chatwoot 的业务发件地址。V1 固定使用 Let's Encrypt HTTP-01 验证，因此域名必须能通过公共 DNS 解析到部署入口，公网 TCP 80 端口必须可达。

`modules/domain/providers/` 和 `modules/domain/dns-providers/` 已作为后续扩展边界保留。当前不会读取 Cloudflare 凭据，也不实现 DNS-01、多域名或泛域名证书。

Chatwoot、pgvector/PostgreSQL 和 Redis 镜像均通过 `.env` 使用明确版本标签。PostgreSQL 与 Redis 不发布宿主机端口；Rails 默认只绑定到 `.env` 配置的本地地址，后续由 Nginx 模块接入。

AI Service 不是基础安装必选项。未设置 `AI_SERVICE_IMAGE` 时，安装器会跳过全部 AI 部署步骤并在完成时提示可后续启用。高级用户可在 `.env` 中同时设置 `AI_SERVICE_IMAGE` 和 `AI_ADMIN_IMAGE`；两个镜像都必须使用固定标签或 SHA-256 digest，并遵守 Compose 中声明的环境变量与健康端点契约。OpenAI API Key 和模型选择改由 AI 管理后台配置，不属于安装必填项。内部 Token、管理密码和会话 Secret 仍只保存在本地 `.env`，安装器不会输出其值。

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

`ai/ai-a.yaml` 配置 `model`、`api_base_url` 和 `api_key_env`；后者只保存凭据的环境变量名。`prompts/prompt-a.yaml` 配置 `system_prompt` 和 `reply_rules` 字符串列表。`knowledge/knowledge-a.yaml` 配置 `source` 和 `max_characters`，`source` 是相对 `AI_SERVICE_DATA_PATH/services/service_a/` 的 UTF-8 文本文件路径。B 实例使用自己的引用和数据目录。空的提示词/知识库引用表示不附加该上下文；引用非空时文件必须存在。当前知识库实现为受大小限制的本地文本上下文，不包含向量检索。

省略 `service_id` 时，优先使用环境变量 `AI_DEFAULT_SERVICE_ID`；未指定且只有一个启用实例时使用该实例。多个实例启用时必须明确设置默认实例。只有实例集合完全为空时，才使用已有的 `AI_MODEL`、`OPENAI_API_KEY` 环境变量，以及可选的 `AI_SYSTEM_PROMPT` 和 `AI_API_BASE_URL`，保留单实例环境变量模式。显式无效、未知、禁用实例或缺失配置会返回明确错误，不会借用其他实例配置。

Runtime 每次请求独立加载默认配置和用户覆盖配置，不保存共享对话上下文；知识文件限制在对应实例的数据目录内。API Key 仅从环境变量读取，配置/请求校验错误及上游错误均返回错误码，不输出完整配置、消息内容或上游响应。多实例隔离是运行配置与上下文隔离；未来的多租户权限管理仍需单独实现。

使用仓库内 Runtime 时，在本地 `.env` 设置 `AI_RUNTIME_BUILD=true`、固定版本的 `AI_SERVICE_IMAGE`（作为本地构建标签）和现有 `AI_ADMIN_IMAGE`。安装器会加载 `stacks/ai-service/compose.runtime.yaml`，构建 Runtime、拉取管理服务镜像，并将 `config/services/` 只读挂载。默认 `AI_RUNTIME_BUILD=false`，原有自定义镜像流程继续可用，AI Service 仍为可选模块。自定义外部镜像需要自行集成此 Runtime 才能处理多实例请求。

共享凭据通过本地 `.env` 的 `OPENAI_API_KEY` 传入；每实例独立凭据可写入项目根目录的 `.env.ai-runtime`，例如使用不同的环境变量名，并在各 AI 配置的 `api_key_env` 中引用。该文件只用于提供者凭据，已被 Git 忽略；也可用 `AI_RUNTIME_SECRETS_FILE` 指定受保护的环境文件。设置这些本地文件权限为 `0600`，更新凭据后重建 AI 容器使环境变量生效。请勿将完整安装器 `.env` 作为 Runtime 的额外环境文件。

本阶段提供多 AI 模型与多知识库的运行基础，未来可接入多入口和多平台。健康端点 `GET /health` 仅检查 Runtime 进程存活；配置、凭据和知识文件的可用性在请求时检查。离线测试无需真实 API Key：

```bash
python3 -m venv runtime/ai-runtime-venv
runtime/ai-runtime-venv/bin/python -m pip install -r ai_runtime/requirements.txt
AI_RUNTIME_TEST_PYTHON="$PWD/runtime/ai-runtime-venv/bin/python" bash tests/ai-runtime-fixture-test.sh
```

## Service Management API

Service Management API 是未来统一 AI 客服管理后台的后端基础，已集成到仓库内的 FastAPI Runtime。启用 `AI_RUNTIME_BUILD=true` 后，管理接口与 `/chat` 使用同一服务端口，默认仅监听宿主机环回地址。它管理客服实例与域名映射，未来可供 Web 管理后台扩展多 AI、多渠道和多域名；当前提供后端 API，不创建前端页面，也不操作 DNS、SSL 或外部平台。

所有 `/management/*` 接口要求 `Authorization: Bearer <SERVICE_MANAGEMENT_TOKEN>`。该 Token 由安装器自动生成，保存在权限为 `0600` 的本地 `.env` 中，且与聊天接口的 `AI_SERVICE_INTERNAL_TOKEN` 分开使用；聊天 Token 无权修改管理数据。升级已有部署时重新运行安装器可补齐缺失 Token，已有值会保留。直接运行 Runtime 时须通过环境变量提供至少 32 字符的管理 Token，缺失或过短时管理请求拒绝访问。

| 对象 | 接口 | 用途 |
| --- | --- | --- |
| Service | `GET /management/services`、`POST /management/services` | 列表、创建 |
| Service | `GET /management/services/{id}`、`PUT`、`PATCH`、`DELETE` 同一路径 | 查询、完整替换、局部修改、删除 |
| Service | `POST /management/services/{id}/enable`、`/disable` | 启用、禁用 |
| Domain | `GET /management/domains`、`POST /management/domains` | 列表、创建 |
| Domain | `GET /management/domains/{domain}`、`PUT`、`PATCH`、`DELETE` 同一路径 | 查询、重新绑定或修改、删除 |
| Domain | `POST /management/domains/{domain}/enable`、`/disable` | 启用、禁用 |

Service 创建请求使用现有七个字段：`id`、`name`、`enabled`、`ai_config`、`prompt_config`、`knowledge_config`、`channel_config`。`id` 与 `name` 必填，`enabled` 默认 `false`，四个配置引用默认空。`PATCH` 只提交需修改字段，`PUT` 提交完整字段；ID 不可修改。启用的实例必须能加载引用的模型、提示词和知识文件，配置无效时拒绝提交并保留原配置；禁用实例可先保存未完成的引用。提供者凭据仍由环境变量提供，不要求通过管理 API 设置。

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

首次 API 写入会事务性地创建并迁移数据库结构，当前 schema 版本为 1。已有 YAML 无需强制导入即可继续使用；如需将当前有效 YAML 固定为 API 记录，可调用带管理认证的 `POST /management/migrations/import-yaml`，或在已配置环境的 Runtime 中运行 `python -m ai_runtime.management_migrate`。导入可重复执行，保留已有 API 修改与删除标记，不改写原文件。遇到不支持的较新 schema 版本时会拒绝读写，避免旧程序修改新数据库。

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
2. 自动生成缺失的 Secret 和管理员初始密码；
3. 检查 Ubuntu 版本、权限和服务器资源；
4. 检查或安装 Docker 与 Docker Compose；
5. 配置并启动 PostgreSQL、Redis、Chatwoot 和 Sidekiq；
6. 如 `.env` 配置了 AI 镜像，则部署并验证 AI Service 与管理后台；否则安全跳过；
7. 配置 Nginx 反向代理及 WebSocket；
8. 检查域名解析与 Nginx HTTP-01 前置条件；
9. 申请 Let's Encrypt 证书并切换 HTTPS；
10. 安装续签 deploy hook、启用 Certbot timer 并执行 dry-run；
11. 执行健康检查并输出访问地址；启用 AI Service 时一次性显示管理员初始密码。

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
```

`status` 显示 Chatwoot、Sidekiq、PostgreSQL、Redis 以及可选 AI Service 的运行状态；`health` 额外检查 PostgreSQL、Redis 和 HTTP 健康端点。未配置 `AI_SERVICE_IMAGE` 时，管理脚本只管理 Chatwoot，并显示“AI Service 未启用，跳过。”。管理脚本只读取现有 `.env` 和 Compose 文件，不会修改配置、生成 Secret、拉取镜像或执行数据库迁移。

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

升级要求输入 `UPGRADE`，回滚要求输入 `ROLLBACK`。只有操作者明确指定 `--yes` 时才允许非交互执行。升级记录保存在 `UPGRADE_RECORD_DIR`，只记录版本、状态和关联备份 ID，不复制 API Key、密码或 Token。手动回滚会验证当前版本与记录中的升级目标一致，并通过升级前备份恢复数据库、持久化文件、`.env`、Compose 配置、Nginx 配置和服务状态。

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
