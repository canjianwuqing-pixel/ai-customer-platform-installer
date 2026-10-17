# 网站多域名、SSL 与在线客服

## 使用

现有一键安装流程不变。在统一 `/admin` 的“域名管理”页添加备用域名；请先由域名持有人配置所有 A / AAAA 记录，并开放公网 TCP 80、443。无需手工编辑 `.env`、Nginx 或证书。暂存、签发和检查可能需要几分钟，页面显示维护任务状态，可刷新查看。

已启用的主/备用域名均提供同源的 Chatwoot、`/admin`、`/support/`、API、静态资源和 WebSocket。备用域名不跳回主域名。浏览器 Cookie 按域名隔离：首次在备用域名登录需要输入同一套管理员凭据；访客在同一域名刷新可延续会话，切换域名不共享访客 Cookie。

在“客服入口管理”选择默认网站收件箱，或创建独立 `/support/{slug}/` 平台入口。只有一个启用实例的网站绑定时可自动作为原默认候选；多个绑定必须明确选择。入口设置不修改收件箱、AI 开关、模型、提示词、知识库和机器人。未知 Slug 返回 404，已停用入口返回 410，绝不进入默认客服；连接异常返回 503。详见 [多平台客服入口](support-entries.md)。

## 域名生命周期

1. 浏览器仍经过原 Session、CSRF、Origin 校验。Admin 通过原维护 Token / Unix Socket 提交 `domain-add`、`domain-primary` 或 `domain-delete` 固定任务；参数只有严格验证的主机名，不接受 Shell、路径、证书或执行代码。
2. 添加域名时检测 DNS，拒绝空解析、内网及特殊地址。为该域名单独暂存 HTTP-01 站点，使用随机到达标记逐一验证全部公开 A / AAAA 记录到达本服务器，支持 NAT，不依赖外部 IP 查询服务。
3. 通过 Certbot `certonly --webroot` 签发独立 lineage，不使用插件重写其他软件站点。不获取 DNS API 权限。检查证书 SAN、有效期与私钥匹配。
4. 渲染所有本安装器管理的虚拟主机；复用既有 Nginx 站点所有权、配置测试、原子替换及 reload/回滚流程。不会接管 default_server，不会覆盖其他软件站点。保持下划线 API 请求头策略、Admin 代理和 WebSocket。
5. 使用系统 CA 严格验证新域名 TLS/SNI，不使用 `curl -k`；验证暂存标记、Admin 登录页、Chatwoot 原生资源的同源加载及真实 `/cable` WebSocket 101 握手后才写入正式域名清单。上述不是完整聊天端到端验收，实机验收清单见下方。
6. Certbot 系统定时器和 deploy hook 复用原模块。新增 lineage 保存 webroot 续签参数；HTTP-01 路径不会被整站 HTTP→HTTPS 重定向拦截。原始 lineage 的 authenticator 不被强行改写。保留原 `certbot renew --dry-run` 测试能力。

删除仅允许备用域名；当前主域名以及会导致没有可用主站的删除会被拒绝。若已有机器人回调或平台 API 连接使用待删备用域名，会先安全迁移到可用主域名并保留身份与凭据，再发布移除；失败保留原站点。删除不删除 lineage、私钥、Chatwoot 数据或实例数据，以免影响恢复/续签及审计。

## 主域名切换与故障恢复

原主域名不可访问时，可通过已启用备用域名进入 `/admin`，设其为主域名。切换前重新验证目标域名 DNS、TLS 和访问。不会探测或依赖失效旧域名。

仅修改受保护 `.env` 中的主域名及对应证书路径，使用现有 Compose 封装重建 Chatwoot Rails / Sidekiq 应用容器，以更新 `FRONTEND_URL` 及新生成链接；不执行数据库迁移、`down` 或重新安装。现有机器人通过实际 Agent Bot PATCH API 更新 `outgoing_url`，保留机器人 ID、Token、签名 Secret、收件箱绑定、AI 开关、会话状态和消息去重记录。外部 Chatwoot 连接保留原服务器地址；本平台连接通过私有 Docker 网络访问 Rails，不依赖旧主域名 DNS。

主域名切换不迁移浏览器 Cookie 或修改已发送邮件/历史消息里的链接。数据库、用户、聊天记录、OpenAI Key、模型及知识库不会被重建。旧域名证书失效时清单保留并显示“需要检查”，不会让损坏的旧 TLS vhost 阻止有效备用站点 reload。

维护执行器串行执行任务。域名操作另有跨进程锁、受保护的事务日志和原配置快照；失败恢复原站点和依赖，进程中断后下一次维护/执行器启动先恢复。老版本执行器的 Python allow-list 通过固定 systemd 一次性任务在无运行中作业时刷新，不中断正在执行的升级或恢复。

## 数据与兼容

- 网站域名清单：`ADMIN_MAINTENANCE_STATE_DIR/website-domains.json`（0600，目录 0700）。旧单域名从现有配置导入，发生正式网站操作后持久化。
- 证书与私钥：继续保存在配置的 Certbot 目录，**不进入 Git 或备份包**。备份记录其路径和网站清单，而不是复制私钥。
- `/support/` 选择与可信本地站点别名继续复用 Runtime SQLite 配置覆盖表；独立客服入口由增量 Schema 5 的 `support_entries` 表保存。均在原 Runtime 数据卷内，现有备份完整保存这些记录与凭据，不修改 Chatwoot 数据库。
- 重复安装和升级重新渲染注册的网站清单，不退回单域名。老备份不含网站清单时保留已有新清单。恢复前验证目标站点的 DNS、证书、TLS 与到达性；失败在停止服务或恢复数据库前退出。异机恢复必须先在该主机准备有效证书，不能用无效证书备份覆盖可访问站点。
- 历史 `config/domains/`、`/management/domains`、`/admin/api/domains` 的“域名→实例”数据与 API 保留，不能自动当作网站清单签发证书。新主界面不再展示该概念。
- Admin 与 Runtime 仅增加到既有 Chatwoot Docker 网络的私有连接，不向浏览器暴露管理 Token，不挂载 Docker Socket。

## `/support/` 实现与上游依据

直接使用 Chatwoot **v4.18.0** 的 `/widget` HTML、Vue 应用与 `/messages` 原生路由，在浏览器顶层呈现，而非 SDK iframe/悬浮气泡。Admin 通过私网读取原生页面，在 Vue 模块执行前同步设置正确的公开 `website_token` 查询参数、视口和按 Token 隔离的访客 Cookie。v4.18 Widget 的消息/历史 API 从 `location.search` 读取 Token，不能只在后端 `/widget` 请求中传入，否则 HTML 200 但消息请求失败。不实现新聊天协议，不查询或隐藏第三方 DOM 按钮。原生顶层模式不创建浮动气泡，关闭/弹出功能由上游组件自身的顶层模式控制。

公开页面只使用网站 `website_token` 与当前访客自己的签名会话；管理 Token、机器人 Token、Webhook Secret、OpenAI Key 不进入浏览器。静态资源、Widget API、ActionCable 都使用当前站点同源地址。视口脚本使用 `visualViewport` / `100dvh` 适配手机键盘，不添加导航和后台按钮。

核对的固定版本来源：

- [WidgetsController：website_token / cw_conversation](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/controllers/widgets_controller.rb)
- [Widget 原生 HTML](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/views/widgets/show.html.erb)
- [Widget 顶层与 iframe 初始化](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/javascript/widget/App.vue)
- [原生 HeaderActions 与关闭行为](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/javascript/widget/components/HeaderActions.vue)
- [原生路由](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/javascript/widget/router.js)、[相对 API 地址](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/javascript/widget/helpers/constants.js)、[ActionCable](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/javascript/widget/helpers/actionCable.js)

## 验证边界与 Ubuntu 实机验收

自动化覆盖：隔离 DNS/Certbot 失败注入与回滚、配置/清单持久化、实际 SQLite 备份恢复、现有安装/升级/恢复 fixture、真实 Nginx + 临时 TLS 双 SNI、Chromium 备用域名独立登录与手机/桌面视口、会话 Cookie 刷新，以及原有实际 v4.18 Ruby 事件生产代码驱动的 AI 回复/人工接管/解决后恢复协议回归。

本地 Widget HTML 上游是明确标识的协议替身，**不是完整编译的 Chatwoot 前端**。没有公网域名、有效 DNS 和真实部署时，不能宣称已验收公网证书签发、互联网故障切换或完整访客聊天。Ubuntu 上需最终验证：

1. 配置两个真实域名，添加备用域名；实际 Let's Encrypt 签发和 `renew --dry-run`；检查所有 A / AAAA 和各自证书 SNI。
2. 两个域名分别登录 Chatwoot 与 Admin，测试 API、实际静态资源和 WebSocket（非仅 HTML 200）。
3. 在真实 `/support/` 新访客直接发送消息、接收 AI 回复、转人工、解决后再次咨询恢复 AI；手机真机弹出键盘后发送、刷新延续会话。
4. 使原主域名 DNS 不可达，通过备用域名切换主站；确认页面、资源、API、Webhook 无旧域名依赖，机器人 ID 和 Key 不变。
5. 测试 DNS/签发失败、删除备用、重复安装、升级与备份恢复；确认既有数据和可访问站点不丢失。
