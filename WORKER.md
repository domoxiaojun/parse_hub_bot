# ParseHub HTTP Worker

## Fork 分支约定

本 Fork 的默认开发分支为 `worker`，包含 Worker 以及定制的消息显示；`main` 只同步原作者的 `upstream/main`。全部 Fork 代码位于 `ext/`，上游目录与 `upstream/main` 逐字节一致，布局与挂钩清单见 `ext/README.md`。`origin` 指向自己的 Fork，默认推送目标为 `origin`。

工作区干净时，按以下步骤同步上游：

```sh
git fetch upstream
git switch main
git merge --ff-only upstream/main
git push origin main
git switch worker
git merge main
uv sync --frozen
uv run pytest            # test/ext/test_upstream_hooks.py 会校验上游目录与挂钩目标
# 解决冲突并通过检查后，再推送定制分支
git push origin worker
```

ParseHub 解析库通过依赖锁文件管理；库发布新版本时使用 `uv lock --upgrade-package parsehub`，检查锁文件变化并验证后提交。本 Worker 不依赖 VPS 上的相邻 `../ParseHub` 源码；抖音视频经 `ext/douyin_quality.py` 与 `ext/hooks.py` 汇总接口候选、实测选流并核验下载文件。YouTube 解析由 `ext/youtube_quality.py`（挂到 `YtbParse.cli_args`）指定 yt-dlp `player_client=mweb`，避免 Premium 会话被 SABR-only 限制在 360p；运行主机需有 yt-dlp 支持的 JS 运行时（镜像内为 Deno）。

Worker是原版ParseHub与gptbot之间的薄适配器，不监听用户消息。URL归一化、原持久缓存、解析缓存、
`ParseService`、`ParsePipeline`、下载重试和媒体处理全部复用原版组件；抖音视频结果额外经过本地显式质量兜底。Worker只负责HTTP/幂等/租约、
把原版输出适配为投递信封，以及preview Rich Message、raw/zip原文件的Telegram发送。gptbot继续作为唯一update接收方；
直出解析轮不调用LLM，未携带delivery的旧工具请求仍使用文件交接。

gptbot 将已确认来源和回复目标的消息持久化到 `telegram.social-link`，由其 tool worker 池异步执行，主聊天 worker 不等待解析。Worker 的 HTTP 协议保持 v3：同一 job 的最多 10 个去重链接并发准备，与其他消息共用全局 `Semaphore(2)`。每个结果完成时立即保存租约及检查点，最终按输入顺序组织交付；不同消息可以先完成先回复各自原消息。

单链接失败不取消其他链接。取消整条任务会等待所有子任务退出后释放租约；相同来源共享的解析仅在最后一个等待者离开后取消。gptbot 重启后查询已记录的 job；若提交响应丢失，只允许原请求体和原幂等键重试。Worker 自身重启仍沿用现有中断/未知交付恢复规则，不盲目重发。

生产解析先按原版`handle_parse()`顺序调用`ParseService.get_raw_url()`，再检查原`persistent_cache`（仅直出）
和`parse_cache`，未命中时运行原`ParsePipeline`。抖音绕过不含质量证据的 Telegram 文件缓存，并拒用旧策略或未确认最高规格的解析缓存。`refresh=true`同时绕过两级原缓存。Worker不再调用
`ParseResult.download()`、不另加下载重试、也不调用自己的媒体转换器。Worker定制从原版结果适配、
消息组装和gptbot HTTP协议开始。源结果严格保留
ParseHub的`video/image/multimedia/richtext`类型；普通正文标记为plain，只有`markdown_content`标记为
markdown。ParseHub没有作者、发布时间或公开性证明字段，Worker不会臆造这些元数据。

## 抖音画质选择与容器日志

适用于抖音单视频。选择范围是当前请求发现的播放候选，不代表源站未开放的原始文件。

1. 保留本次网页/手机接口返回的播放地址和 `bit_rate` 候选，补充 `1080p/1440p/2160p/720p/540p/480p/default` 显式请求。
2. 使用同一解析代理读取候选 MP4 的有限头尾数据，每个候选最多 4 MiB、并发 3 个、单候选总超时 40 秒；通过 ffprobe 读取真实视频流参数。不能解析的候选记录失败原因，不猜测宽高。
3. 按实测像素面积、帧率、视频码率依次排序；`default` 也参与实测比较，不根据 ratio 标签或文件大小决定清晰度。不同编码的码率不能直接等同于主观画质，日志同时记录编码。
4. 下载后再用 ffprobe 核对尺寸、编码、帧率、码率和文件大小。与选中候选不一致时记录 `mismatch` 并报错；验证失败时记录 `unverified`。发送前对变更过的输出文件再次检查，原样输出复用已有测量。

日志使用 INFO/WARNING 输出到 Worker 标准日志，容器日志可直接看到，搜索 `event=quality.` 即可。以下为字段示例，数值仅用于说明：

```text
event=quality.ready platform=douyin policy=measured-resolution-fps-bitrate-v1 hooks=... entrypoint=ext.worker
event=quality.api platform=douyin trace=... video_id=... source=web status=failed http_status=403 cookie_valid=unknown
event=quality.candidate platform=douyin trace=... video_id=... requested=1080p status=verified width=1280 height=720 fps=30.0 codec=h264 bitrate=2000000 bytes=... at_least_1080p=False
event=quality.selected platform=douyin trace=... video_id=... highest_confirmed=False scope=discovered_candidates width=1920 height=1080 requested=stream_3 reason=max_measured_resolution_fps_bitrate
event=quality.verified platform=douyin trace=... video_id=... stage=download width=1920 height=1080 status=match highest_confirmed=False at_least_1080p=True
event=quality.final platform=douyin trace=... video_id=... width=1920 height=1080 codec=h264 status=match at_least_1080p=True highest_confirmed=False scope=discovered_candidates measurement=download
```

- `quality.ready`：新启动的 Worker 已加载画质扩展。实际入口应为 `ext.worker`；更新镜像源码后需要重新构建并重建容器，仅重启旧镜像不会应用代码。
- `quality.api`：区分网页/手机接口成功或失败。403 只记录 HTTP 状态，Cookie 有效性仍为 `unknown`，不擅自断定过期。
- `quality.candidate`：请求档位和真实尺寸分开显示。`requested=1080p`、`height=720` 表示服务端返回的仍是 720P。
- `quality.selected`：记录候选数量、已验证数量、选择策略及选中的参数。任何接口失败、候选探测失败或排序参数缺失，都会使 `highest_confirmed=False`；原始流无法探测时仍选择已验证候选中的最高规格，记录 `original_verified=False`。仅当没有任何候选通过验证时保留原流，并明确标记未验证。
- `quality.verified` / `quality.final`：下载文件和最终输出的证据。`at_least_1080p=True` 只表示短边至少 1080 像素；判断最高还需看 `highest_confirmed` 和 `scope`。`highest_confirmed=True` 仅在本次所有发现的候选均完整验证且文件核验一致时成立。
- `quality.cache`：Worker 缓存命中记录 `reprobed=false`，只沿用上次测量，不表示重新查询过源站。未确认最高规格的结果不作为可复用的 Worker 缓存。策略升级已更新缓存键；重新验收使用新的请求标识并设置 `refresh=true`。

新增质量日志不打印 Cookie、完整 CDN 地址或上游异常原文。图集、实况照片和其他平台不受本次选流策略影响。

## 原配置与路径

Worker直接沿用原项目布局，无需复制成第二套配置：

| 内容 | 读取位置 |
| --- | --- |
| Token、API_ID/API_HASH、BOT_PROXY等 | 原`.env`或进程环境 |
| 平台Cookie、解析/下载代理 | `DATA_PATH/config/platform_config.yaml`，默认`data/config/platform_config.yaml` |
| SQLite数据库 | 原`DATABASE_URL`，默认`sqlite+aiosqlite:///data/db/database.db` |
| 下载和48小时媒体缓存（交付回执保留7天） | 原`DOWNLOAD_DIR`，默认`downloads/` |
| 原 MTProto session | 保留原文件；Worker不再打开它 |

原SQLite表保留；Worker启动时运行原数据库初始化，任务、文件缓存和租约仍写入同一文件中的`worker_*`表，
不覆写原`cache`表。直出preview会复用原Bot的持久`file_id`缓存，因此命中时不会重新解析、下载或转码；
文件交接请求不使用该缓存，且两种交付使用不同Worker缓存键。下载目录和文件名由原`ParsePipeline`生成
（标题目录、重名后缀、原文件名），处理输出沿用同目录`processed`，归档调用原打包函数生成同名`.tar.gz`。
Worker只登记原流水线已经生成的输出用于租约和清理，未登记历史下载保留。原`ParsePipeline`固定写入`DOWNLOAD_DIR`，因此Worker的缓存根目录必须是同一路径，启动时校验。

Worker不监听消息；发送客户端使用独立持久会话data/sessions/worker_sender_<BotID>.session、in_memory=False、no_updates=True、plugins=None，不占用原bot_<BotID>.session。旧交互Bot不要同时处理同一消息，避免重复回复。

不要复制覆盖已有.env；只补充：

```dotenv
WORKER_SERVICE_KEY=<至少32字符且与gptbot服务密钥相同>
WORKER_HOST=127.0.0.1
WORKER_PORT=8080
WORKER_CACHE_MAX_BYTES=10737418240
WORKER_LOG_LEVEL=INFO
```

BOT_TOKEN、API_ID、API_HASH、BOT_PROXY、DATA_PATH、DOWNLOAD_DIR、DATABASE_URL继续用原值。本轮Worker要求持久SQLite DATABASE_URL，不会安装或切换数据库服务器。

Worker新增的结构化诊断行不含URL、Cookie、代理凭据和异常原文，例如`reason=login_required`。
需要查看安全裁剪后的异常链和源码位置时，可临时设置`WORKER_LOG_LEVEL=DEBUG`并重启Worker；DEBUG只作用于
`parsehub.worker`，不会打开依赖库的请求跟踪。诊断完成后恢复`INFO`。

## 原生启动与Compose

原生进程使用项目虚拟环境：

```sh
uv sync --frozen
uv run python -m ext.worker
```

原生ffmpeg/ffprobe需要部署环境预先提供。Compose从部署机器当前源码构建，见[Compose部署说明](COMPOSE_WORKER.md)。两个Compose项目独立启动和更新；共享网络是可选连接方式。本机不执行Docker、不构建镜像、不部署。

## 平台配置与Admin

Worker启动时读取原platform_config.yaml，缺失文件视为空配置。沿用原格式的default_parser_proxies/default_downloader_proxies、platforms、cookies、parser_proxies/downloader_proxies与disable_parser_proxy/disable_downloader_proxy。

gptbot仅保存PARSEHUB_WORKER_URL、PARSEHUB_WORKER_SECRET、PARSEHUB_WORKER_ACCOUNT_ID三个连接配置，不推送平台快照。Admin→ParseHub通过Worker接口读取脱敏状态并远程编辑原YAML。保存使用sha256冲突检查和原子替换，保留无关字段；**重启Worker后生效**，不需要重启gptbot应用平台配置。原来的gptbot平台Cookie字段不会覆盖Worker的YAML。

## API

所有/api/v1请求都需要Bearer服务密钥，协议版本3（API路由仍为/api/v1）。gptbot和Worker需要配套升级，旧协议会明确拒绝，不静默返回文字。原生默认loopback；容器仅显式WORKER_ALLOW_CONTAINER_BIND=true允许0.0.0.0，由Compose限制宿主暴露。

- GET /health：protocolVersion、botId、ready、version、configSource和directDelivery。directDelivery表示已配置发送能力；deliveryReady表示当前登录就绪，senderState为starting/cooldown/retrying/ready/error/stopped，retryAfterSeconds为剩余等待秒数。直出客户端同时检查能力和就绪状态。
- GET /capabilities：实际ParseHub版本、platforms及preview/raw/zip模式。
- GET /config：原YAML的脱敏状态、sha256、平台与默认代理；不返回Cookie原文或代理密码。
- PUT /config：带baseSha256的编辑；平台字段platform/cookies/parser/downloader，凭据用keep索引或新value；默认代理编辑scope=defaults。保存后requiresRestart=true。禁止旧整份快照覆盖。
- POST /jobs：text、accountId、mode、outputMode、refresh、requestId、idempotencyKey，以及可选delivery。message目标包含chatId、replyToMessageId、messageThreadId；inline/guest目标包含inlineMessageId。
- GET/DELETE /jobs/{id}：查询或取消当前调用方；单次取消不影响其他等待者。
- GET /leases/{id}/media/{mediaId}：带Bearer服务认证和有效lease读取已准备文件；只接受不透明媒体ID，不提供任意路径读取，不向Telegram暴露服务地址或密钥。
- PUT/DELETE /leases/{id}：续租或幂等释放；默认300秒，客户端每60秒续租，批量准备期间保护早期完成项。

同一幂等键表示一次交付尝试。带delivery的任务重查返回持久回执，即使租约已释放也不重新发送；无delivery的旧准备任务保持租约过期拒绝语义。Worker重启将未完成任务标记interrupted，发送中无可靠确认的交付标记unknown。已持久化sent回执保持成功。

直出任务在Worker内发送，不把媒体传回gptbot。preview普通聊天、Inline和Guest均由Worker组装一条Rich Message；raw/zip才使用原生Document发送。每个可见请求前持久化sending/inFlight及稳定随机ID，确认后保存messageIds、逐结果tasks及已交付证据。已确认批次不重发，网络结果不明记录unknown。缓存文件过期不会删除投递回执。直出HTTP响应清空results，并过滤内部随机ID；上传引用只保存在内部缓存。

## 输出与缓存

最多10个不同链接按原顺序返回，逐项失败隔离。preview直接适配原`ProcessedMedia`，图集、GIF、长图和视频的处理决定均来自原`ParsePipeline`；Live Photo在共享资产中始终保持一对静图与视频，Rich 预览按视频块保留真实尺寸与封面。raw把原流水线下载文件作为document，Live Photo保留静态图和视频；zip打包原流水线生成的目录与metadata；read_only只使用原解析服务和解析缓存。超限原文件/归档不静默改成预览。

固定172800秒从发布开始，访问不续期，容量默认10GiB，启动及每10分钟清理；持有租约的文件不会删除。Cookie与解析/下载代理均由原`platform_config.yaml`和原服务选择，Worker不增加挑战检测、额外Cookie尝试或网络栈改写。提交到Worker的入口URL仍拒绝本机、私网字面地址和内嵌凭据。
媒体处理规则带版本；缺少当前版本的旧Telegram媒体缓存不会复用，Worker缓存键升级后也会重新生成预览，避免继续发送方向错误的历史封面。

## 验证边界

测试使用临时目录、临时SQLite、模拟平台/Telegram及真实本地小样本转换，不能替代真实平台、Telegram发送或部署验收。没有启动真实Bot、没有修改用户已有配置/数据库/session，也未在本机构建。

解析、下载或媒体处理失败时，Worker沿用原流水线的成功/失败结果，不再自行构造部分媒体结果。

## 直出卡片与确认

共享核心只有一个send_envelope入口，输入DeliveryEnvelope/MediaAsset，返回SendResult。Worker使用同一Kurigram no-updates会话先上传媒体引用，再组装`InputRichMessage`并调用`send_rich_message`/`edit_inline_text`；raw/zip才走原生Document。无需向任何聊天发送用于上传的占位媒体。

| 目标和媒体 | 输出 |
| --- | --- |
| preview（普通聊天/Inline/Guest） | 一条Rich Message；视频传递最终文件真实宽、高、时长；平台脚注与可点击“查看原文”保留 |
| raw/zip | 原文件或同名归档作为Document；不走preview Rich媒体改写 |

Kurigram 的 `InputMediaVideo.video_cover` 直接绑定每个视频的 Telegram 封面引用；封面引用也按附件保守计数，最多50个附件；文字和blocks超过官方单条容量时失败，不截掉媒体或公开补发。每个视频使用自己的封面，不额外显示静图。Rich客户端实际封面显示仍需真机验收。

共享预处理先规范化EXIF/HEIF方向，再转换和缩放。Live封面保持单图，普通长图保留切片；视频读取实际显示尺寸并规范为H.264/yuv420p。原生Live超过10秒或10 MB时发送前失败，不裁掉视频；Inline视频预览不套用原生Live的限制。发送阶段不再旋转或转码。

标题、摘要和来源进入Rich块；来源使用可点击URL，平台标识位于页脚。超长文字使用摘要及来源/已有阅读链接，不自动发布第三方阅读页。缓存按Bot、资产和native/preview表示保存成对引用，过期时成对刷新；无法恢复完整引用时受控失败，可用refresh重新准备。上传失败日志记录阶段、批次、媒体序号、类型、大小、表示方式、异常类型和RPC标识，不记录URL、路径、文件名、正文、凭据或原始异常文本。

返回delivery状态pending/sending/sent/partial/failed/cancelled/unknown；messageIds仅来自Telegram确认，inline编辑记录confirmed。全部解析失败也可以发出受控错误卡，但job.status为failed、delivery为partial，不伪装解析成功。证据仅包含已确认交付的来源，mediaCount只统计已交付frame。网络异常原文不进入用户结果。

局部发送或无法确认发送的job重查只返回已有状态，由用户检查现有消息后明确新建尝试，不承诺自动恰好一次投递。gptbot保存普通/Guest的回执和外部证据，供后续分析；原生inline保持无会话历史。部署先升级Worker，再启用gptbot直出。

## 登录限流恢复

HTTP控制面先监听，Telegram登录在后台进行。遇到auth.ImportBotAuthorization的FloodWait时不退出进程，按Telegram指定秒数加1秒等待；截止时间原子保存到data/sessions/worker_sender_<BotID>.cooldown.json。重启也会遵守该期限。不要删除session或cooldown文件来绕过等待；已生效的Telegram限流仍需等候。

健康接口在等待期间保持HTTP 200、ready=true（配置就绪）、deliveryReady=false、senderState=cooldown，并返回retryAfterSeconds。此时新的delivery任务返回delivery_not_ready，不创建任务；已完成回执仍可查询，无delivery的准备接口继续可用。凭据或身份错误留在senderState=error，等待人工修正配置；网络失败有界退避。存活检查不因发送登录等待而促使容器重启。

将本次worker源码同步到服务器后沿用原方式启动。保留data持久挂载；不用重置Bot Token、不删除原会话、不安装新的数据库。没有执行真实登录或服务器部署验证。
