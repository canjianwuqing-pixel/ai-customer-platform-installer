# AI Customer Platform Installer

一个面向 Ubuntu 服务器的模块化 AI 客服平台安装器项目。项目最终目标是通过一个入口脚本部署 Chatwoot，并为后续 AI Service、知识库和管理后台等组件保留清晰的扩展边界。

## 当前阶段

当前仓库已经实现前三个基础模块，包括：

- 安装入口和模块注册机制；
- 统一日志、配置读取、状态记录和基础校验接口；
- System 环境检测与 Docker 官方安装流程；
- Chatwoot、PostgreSQL、Redis、Sidekiq 的 Docker Compose Stack；
- 配置模板、fixture 测试和文档目录。

系统检测模块提供只读的 Ubuntu 版本、权限、CPU、内存、磁盘、架构和网络检查。Docker 模块可以检测现有环境，并在缺失时通过 Docker 官方 APT 仓库安装 Engine、CLI、containerd、Buildx 和 Compose Plugin。Chatwoot 模块会校验 Compose 配置、拉取固定版本镜像、先启动并等待 PostgreSQL/Redis、执行 `db:chatwoot_prepare`，再启动 Rails 与 Sidekiq。Nginx 和 SSL 模块仍是安全的 no-op 占位实现。

## 配置原则

真实配置统一保存在本地 `.env` 文件中。先复制 `.env.example`，再填写部署环境自己的值：

```bash
cp .env.example .env
```

不得向 Git 提交真实域名、邮箱、数据库密码、Redis 密码、密钥或 Token。`.env` 已加入 `.gitignore`。

Chatwoot、pgvector/PostgreSQL 和 Redis 镜像均通过 `.env` 使用明确版本标签。PostgreSQL 与 Redis 不发布宿主机端口；Rails 默认只绑定到 `.env` 配置的本地地址，后续由 Nginx 模块接入。

## 安装流程规划

后续完整安装器将按照以下顺序执行：

1. 加载并校验配置；
2. 检查 Ubuntu 版本、权限和服务器资源；
3. 检查或安装 Docker 与 Docker Compose；
4. 配置并启动 PostgreSQL、Redis、Chatwoot 和 Sidekiq；
5. 配置 Nginx 反向代理及 WebSocket；
6. 申请 Let's Encrypt 证书并配置自动续签；
7. 执行服务健康检查并输出部署结果。

入口脚本通过注册步骤调用各个模块。模块之间不直接依赖，只通过 `lib/` 中的公共接口共享日志、配置、状态和校验能力，为重复执行和断点恢复预留基础。

## 当前运行方式

填写 `.env` 后运行统一入口：

```bash
bash install.sh
```

该命令会执行只读系统检测，在 Docker 环境缺失时进入 Docker 官方安装流程，并部署 Chatwoot Stack。Nginx 和 SSL 步骤目前仍只输出占位日志，因此这一阶段只提供本机绑定的 Chatwoot HTTP 服务。
系统检测只支持 Ubuntu 22.04 和 24.04；在其他操作系统上运行会输出检测错误并安全退出。

## 后续扩展方向

- Chatwoot 单实例生产部署；
- AI Service；
- AI 管理后台；
- 知识库与向量检索；
- 多客服渠道连接；
- 多域名入口；
- 备份与恢复；
- 安装验证、升级和运维工具。

多平台、多租户和上述扩展能力不属于当前骨架阶段。
