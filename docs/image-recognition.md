# Website Inbox 图片识别

## 审计结论与启用

原 `ai_runtime/chatwoot.py:is_incoming` 要求非空字符串正文，因此 `content: null` / 空字符串的图片事件被忽略；图文消息虽然进入队列，`engine.py` 也只构造字符串消息，未发送附件。现在复用原签名 Webhook、账户 API 验证、队列及原会话回复通道，不增加图片上传 API 或另一套聊天系统。

附件字段与下载行为按 Chatwoot **v4.18.0** 的 [Attachment 模型](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/models/attachment.rb)及 [API 附件序列化](https://github.com/chatwoot/chatwoot/blob/v4.18.0/app/views/api/v1/models/_attachment.json.jbuilder)核对：使用 `id`、`message_id`、`file_type`、`extension`、`data_url`、`file_size` 及可空宽高，不能假设每个 API 附件都含 account_id 或 content_type。`data_url` 可以是 Active Storage 跳转地址，因此只允许受限的本地磁盘跳转，而非直接开启自动跟随。

使用仓库内 Runtime（`AI_RUNTIME_BUILD=true`）及统一 Admin。在原模型页确认提供方支持**当前模型**的 Chat Completions 图片输入后，开启“此模型支持图片识别”，保存即可对后续消息生效。没有固定模型白名单，不更换模型或 API Key。旧配置没有 `supports_vision` 时视为 false，原文字流程不变；拒绝字符串等非布尔值。模型配置仍通过原 `ai_config` 与实例关联。

[OpenAI 模型查询](https://developers.openai.com/api/reference/resources/models/methods/retrieve)没有统一的视觉能力字段，所以不将“模型列表包含该名称”当作支持图片的证据。开关是管理员依据提供方能力作出的确认；开启后连接测试会向**实际选择的模型**发送一张内置小图，而不是只做文字测试。HTTP 成功只验证协议接受，不保证提供方真的具备视觉理解能力，需用真实图片验收。模型拒绝图片输入时明确说明没有完成识别，不改发纯文本请求假装看过图片。

图片输入使用 [Chat Completions 的 `image_url` 内容项](https://developers.openai.com/api/docs/guides/images-vision)，传入内存生成的 data URL，而不是让提供方下载 Chatwoot 私有链接。该能力支持遵循相同协议的服务，不保证仅支持其他 API 协议的服务兼容。

## 验证与资源边界

- 仅处理原签名认证、账户与收件箱检查通过的访客公开新消息。再通过该绑定的账户 API 验证会话和消息；检查会话、联系人、附件 ID、`message_id`，以及出现时的 account/inbox 字段。Webhook 附件 URL 不直接用于下载，不存在跨实例默认回退。
- 只接受 PNG / JPEG / WebP 静态图片；文件扩展名、HTTP MIME、真实解码格式必须匹配。SVG、GIF、动画、坏文件、格式伪装等明确拒绝；不提取可执行内容。
- 每条最多 3 张，单张最多 5 MiB、合计最多 10 MiB；任一边最多 8192，像素总数最多 800 万。API 中的尺寸仅用于提前拒绝，实际解码前重新检查真实尺寸；限制流式原始字节与长度，拒绝额外 HTTP 压缩。
- 单条全部附件下载/处理共 20 秒预算；连接最多 3 秒、单次网络读取最多 5 秒。最多一次经过验证的 Active Storage redirect → disk 跳转，不自动跟随任意重定向。
- 仅打开对应 Chatwoot 的受限 `/rails/active_storage/blobs/{redirect|proxy}/…` 或 `/disk/…` 路径，拒绝用户信息、片段、路径穿越和任意查询参数。仅允许 API 绑定来源；本平台的受保护站点别名复用原内部 Rails 地址，避免依赖失效旧主域名。
- 固定解析后的 IP 并保留正确 Host / TLS SNI，阻止重新解析引起的 DNS rebinding；外部 Chatwoot 的地址必须为公网地址。仅显式配置的内部 Rails 可以连接私有网络，仍拒绝 link-local、多播和未指定地址。不使用环境代理或关闭证书验证。
- 下载不携带 API Token、Authorization 或 Cookie。仅支持当前本地 Active Storage 布局；跳转到 S3/CDN 等不同来源会安全拒绝，不通过任意 URL 白名单兜底。若将来支持外部对象存储，需要单独设计来源和权限校验。
- 使用固定版本 Pillow，限制可解码格式、像素及帧数；每次只解码一张，最长边缩至 2048，并以无 EXIF / ICC / 注释元数据的 JPEG 发送。原图不改写。每张解码内存有上限但会有多个有限像素缓冲区；现有最多 4 个队列工作任务限制并发，不创建无限解码任务。缩放后小字可能不清晰，必要时请访客裁剪错误区域再发送。

HTTPX 的 IP 固定与证书主机名校验采用[官方 SNI 扩展](https://www.python-httpx.org/advanced/extensions/#sni_hostname)。Pillow 的[解压炸弹防护](https://pillow.readthedocs.io/en/stable/handbook/security.html)保持开启，实际像素限制更严格。

## 费用、失败与隔离

图片及连接测试由模型提供方按当前模型规则计费，不内置价格或承诺免费。图片 `detail=auto`，费用随图片、模型和提供方变化；缩放不代表固定费用。后台开关旁明确提醒费用。已提交但失败/超时的请求可能产生费用，因此图片生成失败不自动重复调用提供方，而是经原去重通道发送一次明确失败提示；Chatwoot 自身的发送重试及结果不确定保护仍保留，故障恢复后也无法承诺外部调用绝对仅计费一次。

不支持视觉的模型不下载图片；下载失败、超限、格式错误或模型拒绝均不声称看过图片。图片失败不会全局停用实例，下一条正常文字仍可回复。人工接管、解决后恢复、回复标记去重、发送前再次核对与事务校验保持原逻辑。无法识别或信息不足时，访客可继续文字或转人工。

每次只读取当前消息附件。历史图片只保留“曾有附件、未重新读取”的文字说明，不重复下载或累加 Base64；原文字历史、提示词、知识库 `max_characters` 与凭据脱敏保留。不同实例/Inbox 的图片、模型及知识上下文不共享。

## 持久化、隐私及兼容

不新增表或数据库迁移。`supports_vision` 是原模型 profile 的可选字段，复用现有配置存储、数据卷及备份恢复；不改写旧 YAML、`.env`、管理员认证、Key 或 Chatwoot 数据。重启、重复安装、备份恢复和升级 fixture 检查其保留。

附件仅存在当前请求内存，不保存原图、转换图、Base64、私有 URL 或下载 Token；队列仍只存原有消息 ID 和加密回复。禁止将图片数据回显持久化，即使提供方将 data URL 原样返回，也会在保存回复前隐藏。HTTPX/HTTPCore 日志继续受原应用限制，错误日志只记稳定代码。不更改已有图片 3 天清理任务、存储卷挂载或 Chatwoot 图片文件。

## 自动化与真实验收边界

`tests/chatwoot_images_test.py` 覆盖纯图、图文、多图及三种格式、模拟截图分析、两收件箱不同模型/提示词/知识库隔离、去重、人工优先、下载失败、SSRF/redirect/DNS rebinding、尺寸/字节/总量/动画/格式检查、日志脱敏、重启、模型开关及探测。`tests/chatwoot_images_proxy_test.py` 使用真实 Nginx、临时 TLS、真实流式下载及证书拒绝，Chatwoot 内容来源是明确标注的协议夹具。Chromium 测试实际表单保存及图片连接测试请求；安装器 fixture 使用真实归档恢复检查 profile、Key、知识及原登录密码保留。

OpenAI 回复是模拟的，不是实际识图效果验收；没有真实 Key 时不能声称软件截图已被真实模型理解。Ubuntu 更新后需用已配置且支持视觉的真实模型，从两个独立 `/support/{slug}/` 发送 PNG/JPEG/WebP、纯图片及软件错误截图，确认访客收到真实分析；再测试人工接管、解决后恢复、超限提示和原文字。不要将含个人隐私或密钥的图片用于测试。

## 服务器安全更新（本地提交发布后再执行）

先确认没有维护任务，保留现有工作与备份。仅在原项目目录、干净的 master 上快进更新到审核过的修复提交；不要 reset、down、删除卷或初始化数据库。以下只读取既有配置，不执行 Secret 生成或配置保存：

```bash
# 待本地提交发布后：git fetch origin master，再确认目标 SHA 并 git merge --ff-only <目标SHA>
./backup.sh && (
  set +x
  set -Eeuo pipefail
  export INSTALLER_ROOT="$(pwd -P)"
  source lib/common.sh
  source lib/config.sh
  source lib/validation.sh
  source modules/docker/check.sh
  source modules/ai-service/check.sh
  config_load_installer_configuration "${CONFIG_FILE:-${INSTALLER_ROOT}/.env}"
  ai_service_initialize_config
  test "${AI_RUNTIME_BUILD:-false}" = true
  test "${ADMIN_ENABLED:-false}" = true
  ai_service_compose config --quiet
  ai_service_compose build ai-service ai-admin
  ai_service_compose up -d --no-deps ai-service ai-admin
)
./manage.sh health
```

构建成功才替换 AI / Admin 容器；只会短暂重启这两个应用，不操作 Chatwoot/Rails/数据库/存储卷，不重新绑定机器人。请安排短维护窗口；外部交付仍由既有持久队列保护。仅 `manage.sh restart` 不会加载新源码及 Pillow 依赖。现有 Upgrade 在镜像版本不变时可能保留原镜像，所以本源码更新要显式构建这两个服务，不能假设只运行升级/重启就生效。若使用自定义 AI 镜像而非仓库 Runtime，应由该镜像提供方实现，不强制切换安装模式。

后台进入原模型配置，确认当前模型支持视觉后勾选开关，先测试再保存；Key 留空保留，保存后下一条消息生效。若有多个实例，可分别关联不同模型，无需修改 Inbox、知识库或现有 API Key。
