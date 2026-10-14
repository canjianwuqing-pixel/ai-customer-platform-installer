# AI Customer Platform Installer

一个面向 Ubuntu 服务器的模块化 AI 客服平台安装器项目。项目通过一个入口脚本部署 Chatwoot、独立 AI Service 和 AI 管理后台，并为后续知识库等组件保留清晰的扩展边界。

## 当前阶段

当前仓库已经实现单域名 HTTPS 部署所需的基础模块，包括：

- 安装入口和模块注册机制；
- 统一日志、配置读取、状态记录和基础校验接口；
- System 环境检测与 Docker 官方安装流程；
- Chatwoot、PostgreSQL、Redis、Sidekiq 的 Docker Compose Stack；
- 独立的 FastAPI AI Service 与 AI 管理后台 Docker Compose Stack；
- AI 服务独立数据卷、环回地址默认绑定和敏感日志禁用约束；
- Chatwoot Webhook、Secret 与 Inbox 集成参数预留；
- Chatwoot 的 Nginx HTTP 反向代理与 WebSocket 支持；
- 单域名 DNS 检测、Let's Encrypt 证书申请和 HTTPS 切换；
- Certbot 自动续签、Nginx deploy hook 和续签 dry-run；
- 配置模板、fixture 测试和文档目录。

系统检测模块提供只读的 Ubuntu 版本、权限、CPU、内存、磁盘、架构和网络检查。Docker 模块可以检测现有环境，并在缺失时通过 Docker 官方 APT 仓库安装 Engine、CLI、containerd、Buildx 和 Compose Plugin。Chatwoot 模块会校验 Compose 配置、拉取固定版本镜像、先启动并等待 PostgreSQL/Redis、执行 `db:chatwoot_prepare`，再启动 Rails 与 Sidekiq。AI Service 模块随后校验并启动 FastAPI 服务和独立管理后台。Nginx 模块负责安装服务和提供初始 HTTP 反向代理；domain 模块负责域名解析检测、Nginx 域名绑定验证、Certbot/Let's Encrypt 证书生命周期以及从 HTTP 到 HTTPS 的安全切换。

## 配置原则

真实配置统一保存在本地 `.env` 文件中。先复制 `.env.example`，再填写部署环境自己的值：

```bash
cp .env.example .env
```

不得向 Git 提交真实域名、邮箱、数据库密码、Redis 密码、密钥、证书或 Token。`.env`、PEM 和私钥文件已加入 `.gitignore`。

`DOMAIN` 是当前唯一部署域名；`LETSENCRYPT_EMAIL` 用于 Let's Encrypt 账户和证书通知，`EMAIL` 继续作为 Chatwoot 的业务发件地址。V1 固定使用 Let's Encrypt HTTP-01 验证，因此域名必须能通过公共 DNS 解析到部署入口，公网 TCP 80 端口必须可达。

`modules/domain/providers/` 和 `modules/domain/dns-providers/` 已作为后续扩展边界保留。当前不会读取 Cloudflare 凭据，也不实现 DNS-01、多域名或泛域名证书。

Chatwoot、pgvector/PostgreSQL 和 Redis 镜像均通过 `.env` 使用明确版本标签。PostgreSQL 与 Redis 不发布宿主机端口；Rails 默认只绑定到 `.env` 配置的本地地址，后续由 Nginx 模块接入。

AI Service 和 AI 管理后台同样只接受带固定标签或 SHA-256 digest 的镜像引用。仓库不内置某个供应商镜像；部署镜像需要遵守 Compose 中声明的环境变量契约，并分别提供由 `AI_SERVICE_HTTP_CHECK_URL` 和 `AI_ADMIN_HTTP_CHECK_URL` 指向的 HTTP 健康端点。`OPENAI_API_KEY`、内部 Token、管理密码和会话 Secret 只保存在本地 `.env`，安装器不会输出其值。`AI_LOG_SENSITIVE_DATA` 必须保持为 `false`。

## 安装流程规划

后续完整安装器将按照以下顺序执行：

1. 加载并校验配置；
2. 检查 Ubuntu 版本、权限和服务器资源；
3. 检查或安装 Docker 与 Docker Compose；
4. 配置并启动 PostgreSQL、Redis、Chatwoot 和 Sidekiq；
5. 配置并启动 FastAPI AI Service 和 AI 管理后台；
6. 验证 AI 容器状态和两个 HTTP 健康端点；
7. 配置 Nginx 反向代理及 WebSocket；
8. 检查域名解析与 Nginx HTTP-01 前置条件；
9. 申请 Let's Encrypt 证书并切换 HTTPS；
10. 安装续签 deploy hook、启用 Certbot timer 并执行 dry-run；
11. 执行服务健康检查并输出部署结果。

入口脚本通过注册步骤调用各个模块。模块之间不直接依赖，只通过 `lib/` 中的公共接口共享日志、配置、状态和校验能力，为重复执行和断点恢复预留基础。

## 当前运行方式

填写 `.env` 后运行统一入口：

```bash
bash install.sh
```

该命令会执行只读系统检测，在 Docker 环境缺失时进入 Docker 官方安装流程，部署 Chatwoot 与 AI Service Stack，安装 Nginx，并为 `.env` 中的单个域名申请证书和启用 HTTPS。证书续签由 Certbot systemd timer 执行，续签成功后的 deploy hook 会先运行 `nginx -t`，再 reload Nginx。
系统检测只支持 Ubuntu 22.04 和 24.04；在其他操作系统上运行会输出检测错误并安全退出。

首次部署前请确认域名已经解析且公网端口 80、443 可访问。默认会执行一次 `certbot renew --dry-run`；如需在受限测试环境暂时跳过，可设置 `CERTBOT_RENEW_DRY_RUN=false`，生产部署建议保持启用。

## 后续扩展方向

- Chatwoot 单实例生产部署；
- 知识库与向量检索；
- 多客服渠道连接；
- 多域名入口；
- Cloudflare DNS API、DNS-01 自动验证与泛域名证书；
- 备份与恢复；
- 安装验证、升级和运维工具。

多 AI、多客服实例、多平台、多租户、多域名和上述扩展能力不属于当前阶段。
