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

### 临时到达验证与模板约束

HTTPS 模板和 HTTP-01 暂存模板各有唯一一行 `    # PLATFORM_DOMAIN_CHECK_LOCATION`，位于 `server {}` 顶层。仅有临时 nonce 时，此位置才生成精确匹配的 `/.__platform-domain-check`；正式配置和无 nonce 的 HTTP 证书恢复站点不生成该规则。它与 Admin、Support、WebSocket、Webhook location 平级。禁止按 `client_max_body_size` 等可重复指令定位插入点。自定义 HTTPS 模板也必须保留该唯一顶层占位符；缺少、重复或缩进不符合约定会拒绝渲染并保留原站点，不使用模糊插入兜底。

`tests/website_nginx_test.py` 使用真实 Nginx `-t`、master/worker reload、临时 TLS 证书和一主多备虚拟主机，调用现有 `nginx_apply_candidate`，覆盖 HTTP 暂存、HTTPS 到达验证、nonce 清理、重复执行和语法/签发/访问失败回滚。签名 Webhook 经过实际生成的 Nginx location，再由现有 Runtime 校验和处理；错误签名及非 POST 仍被拒绝。DNS、Certbot 签发和外部 Chatwoot/OpenAI 为隔离协议 fixture，不代表公网 DNS 或真实证书签发验收。

### HTTP 预验证就绪等待与服务器端诊断

reload 命令返回成功只代表控制操作已被接受，不代表新 Worker 已处理请求。[Nginx 官方说明](https://nginx.org/en/docs/control.html#reconfiguration)描述了检查配置、创建新 Worker、旧 Worker 优雅退出的过程。单条 404 不能单独证明时序问题，也可能是站点未被 include、同名虚拟主机冲突、公网回环/NAT 或其他转发路径错误。

现在会在暂存后通过 `nginx -T` 检查目标文件确实包含在配置中、目标 `server_name` 存在且没有同名冲突。只在内存中分析输出，**不记录完整配置或 stderr**。检查失败立即回滚，不覆盖其他软件的站点，也不通过调整 default_server 来抢占域名。

HTTP/HTTPS 的 nonce 验证分别使用 **30 秒总预算**，本机就绪检查与该轮全部 A/AAAA 地址共享预算，而非每个 IP 额外等待 30 秒。每次 curl 是新进程、新连接，单次最多 3 秒，重试间隔最多 250ms；404、连接失败、暂时旧响应可以重试，但始终要求 **HTTP 200、curl 成功且正文与 nonce 字节完全一致**。不跟随重定向，不发送 Cookie/Token，不使用 `-k`；TLS 失败即使重试也必须最终通过正常证书校验。本机验证成功不替代任何公网地址检查；所有公网地址通过后才允许执行 Certbot。超时明确失败并恢复原站点。

诊断文件位于配置的 `ADMIN_MAINTENANCE_STATE_DIR/website-diagnostics/<记录ID>.json`，目录 0700、文件 0600，仅供服务器管理员读取。保留最近 32 次操作、每次最多 512 个事件；只保存 DNS 地址、阶段、固定错误码、HTTP/curl 状态、重试次数、nonce 是否匹配的布尔值及耗时等白名单字段。**不保存 nonce 值、命令、配置快照、响应正文、Token、API Key、私钥、Cookie 或访客凭据**。记录ID与验证 nonce 独立生成。诊断目录不挂载到 Admin 容器，不新增网页或 HTTP 查询接口，现有维护接口仍只返回公开域名状态。

阶段包括 `dns`、`nginx_staging`、`nginx_test`、`nginx_reload`、`nginx_inspect`、`http_local/public`、`certbot`、`certificate`、`https_local/public`、`admin`、`static_assets`、`websocket`、`save` 和 `rollback`。Nginx 检查/reload 阶段使用子任务返回的固定状态回执，不采集原始日志；崩溃恢复另建 `recover` 记录。最终 `failed_stage`、`error_code` 指向首次失败，回滚结果也记录在 events 中。诊断磁盘写入故障不会阻止原站点回滚。

管理员在服务器上按时间查看最新 JSON，可据此区分：

- `nginx_site_not_included`：修改的站点没有被实际检查的 Nginx 配置包含。
- `nginx_server_name_conflict`：目标域名有虚拟主机冲突；其他软件的站点不会被自动删除。
- `domain_http_local_timeout`：配置检查/reload 成功，但本机正确 Host 仍未获得 nonce，不能认为已就绪。
- `domain_http_public_timeout`：本机已通过，但某个公网 A/AAAA 地址未通过；继续检查对应地址的公网回环/NAT/转发路径，**不能据此自动断定是哪一项**。
- `domain_https_*_timeout` 配合 `tls_verification_failed`：HTTPS 尚未通过严格 TLS/nonce 校验，不回退为 HTTP。

诊断文件是本机运维记录，不属于业务配置备份，恢复备份不导入旧主机的诊断；网站清单、凭据和业务数据继续走原有备份机制。没有新增 `.env` 配置或数据库迁移。测试依赖见 `tests/requirements-browser.txt`，新增 wsproto 仅供本地 WebSocket 协议夹具，不进入生产依赖。

### 显式 Certbot 续签测试与进程回收

备用域名正式证书签发/复用成功后，仍执行 `renew --dry-run`，`CERTBOT_RENEW_DRY_RUN=true` 的默认值不变。该显式验证现在附加 `--no-random-sleep-on-renew`。已核对 [Certbot v1.21.0 参数解析源码](https://github.com/certbot/certbot/blob/v1.21.0/certbot/certbot/_internal/cli/__init__.py)：该版本支持此参数；[同版本 renewal 实现](https://github.com/certbot/certbot/blob/v1.21.0/certbot/certbot/_internal/renewal.py)在非交互续签时默认随机等待 1–480 秒，可能超过维护子任务的 360 秒上限。关闭的只是本次显式测试的随机等待，不是证书校验，也没有关闭自动续签计时器或更改其日常调度策略。

网站维护子命令在生产 POSIX 环境中使用独立 session/process group。超时或调用被 Python 异常中断时，先向该任务组发送 TERM，最多给 3 秒退出宽限；仍存活的组再接收 KILL。即使直接启动的 Bash 已经退出或 stdout 管道关闭，也不会据此认定 Certbot 后代已退出。回收启动器及关闭管道后才报告失败，允许原事务回滚 Nginx；不按进程名执行全局 kill，不终止其他 Certbot/应用进程。360 秒上限保留，清理另有最多 3 秒的优雅退出宽限，强制终止后等待启动器回收再继续，不能先回滚再清理。

真实网络等原因仍可使 dry-run 失败或超时。受保护的服务器端诊断会将超时标为 `failed_stage=certbot`、`error_code=certbot_timeout`，随后记录原有回滚结果，不暴露 Certbot 原始输出、命令或凭据。已经签发的证书、私钥及 renewal 配置保留；失败不会撤销、删除或强制重新签发证书。重试继续使用原 `--keep-until-expiring`，通过全部 HTTP/HTTPS 检查后才正式发布备用域名。

`tests/website_certbot_test.py` 使用隔离的假 Certbot 模拟 368 秒随机等待（缩放为 5 秒）及多层实际子进程，覆盖参数、重复执行、证书保留、失败不跳过验证、超时 TERM/KILL、父进程先退出、其他进程组不受影响，以及回滚前确认无存活后代。进程组用例需要 POSIX Python；Windows 原生 Python 会明确跳过，不能算作通过。没有向真实 CA 发起请求，不代表公网续签已验收。

### 此域名修复的安全更新

先在后台确认没有进行中的维护任务，保存现有备份。待修复提交发布到远程后，在原项目目录以原服务器管理用户操作，工作区不干净或不能快进时停止：

```bash
(
  set -e
  test "$(git branch --show-current)" = master
  test -z "$(git status --porcelain)"
  git fetch origin master
  git merge-base --is-ancestor HEAD origin/master
  # 确认远程包含本次修复，再更新；备份失败则停止。
  ./backup.sh
  git merge --ff-only origin/master
  sudo nginx -t
  ./manage.sh health
)
```

本修复只调整宿主机域名验证、受保护诊断及 Nginx 子任务回执，不迁移数据库，不修改 `.env`、私钥、现有证书或管理员配置。现有维护执行器每次域名任务通过 `install.sh __website-domains` 启动新进程，直接读取更新后的代码和模板，因此仅为应用本修复**无需重新安装或重建任何业务容器，也无需中断维护执行器**。更新代码不会立即改写正在使用的站点；回到原 `/admin` 重试添加备用域名，任务会先验证暂存配置、按原流程签发证书及校验 HTTPS，全部成功后才发布，失败保留原站点。若远程还有其他版本变更，需先审阅这些变更并遵循相应升级说明。

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
