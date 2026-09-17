# ParseHub HTTP Worker

## Fork 分支约定

本 Fork 的默认开发分支为 `worker`，包含 Worker 以及定制的消息显示；`main` 只同步原作者的 `upstream/main`。`origin` 指向自己的 Fork，默认推送目标为 `origin`。

工作区干净时，按以下步骤同步上游：

```sh
git fetch upstream
git switch main
git merge --ff-only upstream/main
git push origin main
git switch worker
git merge main
uv sync --frozen
uv run pytest test/test_worker_*.py
# 解决冲突并通过检查后，再推送定制分支
git push origin worker
```

ParseHub 解析库通过依赖锁文件管理；库发布新版本时使用 `uv lock --upgrade-package parsehub`，检查锁文件变化并验证后提交。更新旁边的 ParseHub 源码对照目录不会自动更新本项目依赖。

Worker复用ParseHub全部平台注册能力，负责解析、下载、原文件/打包、媒体转换、固定48小时文件缓存和受认证的媒体文件读取，不监听用户消息。gptbot继续作为唯一update接收方。命中直出的解析轮由Worker通过独立no-updates客户端上传并发送RichMessage，返回回执和解析证据；该轮不调用LLM。未携带delivery的旧工具请求仍使用文件交接。

## 原配置与路径

Worker直接沿用原项目布局，无需复制成第二套配置：

| 内容 | 读取位置 |
| --- | --- |
| Token、API_ID/API_HASH、BOT_PROXY等 | 原`.env`或进程环境 |
| 平台Cookie、解析/下载代理 | `DATA_PATH/config/platform_config.yaml`，默认`data/config/platform_config.yaml` |
| SQLite数据库 | 原`DATABASE_URL`，默认`sqlite+aiosqlite:///data/db/database.db` |
| 下载和48小时媒体缓存 | 原`DOWNLOAD_DIR`，默认`downloads/` |
| 原 MTProto session | 保留原文件；Worker不再打开它 |

原SQLite表保留；Worker任务、缓存、租约、上传引用放在同一文件的`worker_*`表中，不覆写原cache表。原Bot缓存记录不当作Worker已验证缓存直接复用。下载目录和文件名直接由原ParseHub生成（标题目录、重名后缀、原文件名），处理输出放在同目录processed，归档调用原打包函数生成同名.tar.gz；不添加worker-*、original/、media-001或固定media.tar.gz。Worker在数据库登记本次实际创建路径，取消/启动恢复/过期清理只删除登记路径，未登记历史下载保留。

Worker不监听消息；发送客户端使用独立持久会话data/sessions/worker_sender_<BotID>.session、in_memory=False、no_updates=True、plugins=None，不占用原bot_<BotID>.session。旧交互Bot不要同时处理同一消息，避免重复回复。

不要复制覆盖已有.env；只补充：

```dotenv
WORKER_SERVICE_KEY=<至少32字符且与gptbot服务密钥相同>
WORKER_HOST=127.0.0.1
WORKER_PORT=8080
WORKER_CACHE_MAX_BYTES=10737418240
```

BOT_TOKEN、API_ID、API_HASH、BOT_PROXY、DATA_PATH、DOWNLOAD_DIR、DATABASE_URL继续用原值。本轮Worker要求持久SQLite DATABASE_URL，不会安装或切换数据库服务器。

## 原生启动与Compose

原生进程使用项目虚拟环境：

```sh
uv sync --frozen
uv run python -m worker
```

原生ffmpeg/ffprobe需要部署环境预先提供。Compose从部署机器当前源码构建，见[Compose部署说明](COMPOSE_WORKER.md)。两个Compose项目独立启动和更新；共享网络是可选连接方式。本机不执行Docker、不构建镜像、不部署。

## 平台配置与Admin

Worker启动时读取原platform_config.yaml，缺失文件视为空配置。沿用原格式的default_parser_proxies/default_downloader_proxies、platforms、cookies、parser_proxies/downloader_proxies与disable_parser_proxy/disable_downloader_proxy。

gptbot仅保存PARSEHUB_WORKER_URL、PARSEHUB_WORKER_SECRET、PARSEHUB_WORKER_ACCOUNT_ID三个连接配置，不推送平台快照。Admin→ParseHub通过Worker接口读取脱敏状态并远程编辑原YAML。保存使用sha256冲突检查和原子替换，保留无关字段；**重启Worker后生效**，不需要重启gptbot应用平台配置。原来的gptbot平台Cookie字段不会覆盖Worker的YAML。

## API

所有/api/v1请求都需要Bearer服务密钥，协议版本2（API路由仍为/api/v1）。gptbot和Worker需要配套升级，旧协议会明确拒绝，不静默返回文字。原生默认loopback；容器仅显式WORKER_ALLOW_CONTAINER_BIND=true允许0.0.0.0，由Compose限制宿主暴露。

- GET /health：protocolVersion、botId、ready、version、configSource和directDelivery。directDelivery表示已配置发送能力；deliveryReady表示当前登录就绪，senderState为starting/cooldown/retrying/ready/error/stopped，retryAfterSeconds为剩余等待秒数。直出客户端同时检查能力和就绪状态。
- GET /capabilities：实际ParseHub版本、platforms及preview/raw/zip模式。
- GET /config：原YAML的脱敏状态、sha256、平台与默认代理；不返回Cookie原文或代理密码。
- PUT /config：带baseSha256的编辑；平台字段platform/cookies/parser/downloader，凭据用keep索引或新value；默认代理编辑scope=defaults。保存后requiresRestart=true。禁止旧整份快照覆盖。
- POST /jobs：text、accountId、mode、outputMode、refresh、requestId、idempotencyKey，以及可选delivery。message目标包含chatId、replyToMessageId、messageThreadId；inline/guest目标包含inlineMessageId。
- GET/DELETE /jobs/{id}：查询或取消当前调用方；单次取消不影响其他等待者。
- GET /leases/{id}/media/{mediaId}：带Bearer服务认证和有效lease读取已准备文件；只接受不透明媒体ID，不提供任意路径读取，不向Telegram暴露服务地址或密钥。
- PUT/DELETE /leases/{id}：续租或幂等释放；默认300秒，客户端每60秒续租，批量准备期间保护早期完成项。

同一幂等键表示一次交付尝试。带delivery的任务重查返回持久回执，即使租约已释放也不重新发送；无delivery的旧准备任务保持租约过期拒绝语义。Worker重启将未完成任务标记interrupted，发送中无可靠确认的交付标记unknown。已持久化sent回执保持成功。

直出任务在Worker内发送，不把媒体传回gptbot。每条RichMessage发送前持久化sending/inFlight，确认后记录messageIds或inline确认及已交付证据；终态回执保存后才释放lease。部分和未知发送不盲目自动补发。无delivery的旧工具任务仍由gptbot读取文件、上传发送并释放租约。媒体描述包含mediaId、sizeBytes、mimeType等，不再包含fileId。媒体缓存使用v2-files命名空间，旧注册引用不会命中。

## 输出与缓存

最多10个不同链接按原顺序返回，逐项失败隔离。preview处理图集、GIF、Live Photo配对、长图及视频合流/转换/分段；raw保留原文件为document；zip生成附metadata的.tar.gz；read_only不新增下载或上传。超限原文件/归档不静默改成预览。

固定172800秒从发布开始，访问不续期，容量默认10GiB，启动及每10分钟清理；持有租约的文件不会删除。匿名优先，明确挑战后最多一次Cookie尝试；无法确认公开性的内容拒绝交付。HTTP与yt-dlp请求受域名/私网/凭据边界约束，配置代理属于可信基础设施。

## 验证边界

测试使用临时目录、临时SQLite、模拟平台/Telegram及真实本地小样本转换，不能替代真实平台、Telegram发送或部署验收。没有启动真实Bot、没有修改用户已有配置/数据库/session，也未在本机构建。

原项目批量下载在单项下载失败时会移除整次下载目录；Worker保留这一原生行为并返回正文与媒体失败计数，转换阶段仍逐项保留成功文件。

## 直出卡片与确认

排版使用中等字号标题、平台/作者/时间、小段正文或可折叠长正文、媒体主体和底部“查看原文”。外部正文作为literal RichText，不解释为HTML/Markdown结构。原始文件和归档以文档块呈现；已有阅读版链接时可展示。普通消息超过50项媒体拆分发送，Guest/inline合并编辑一条，超出其单条限制明确失败而不另行公开补发。

返回delivery状态pending/sending/sent/partial/failed/cancelled/unknown；messageIds仅来自Telegram确认，inline编辑记录confirmed。全部解析失败也可以发出受控错误卡，但job.status为failed、delivery为partial，不伪装解析成功。证据仅包含已确认交付的来源，mediaCount只统计已交付frame。网络异常原文不进入用户结果。

局部发送或无法确认发送的job重查只返回已有状态，由用户检查现有消息后明确新建尝试，不承诺自动恰好一次投递。gptbot保存普通/Guest的回执和外部证据，供后续分析；原生inline保持无会话历史。部署先升级Worker，再启用gptbot直出。

## 登录限流恢复

HTTP控制面先监听，Telegram登录在后台进行。遇到auth.ImportBotAuthorization的FloodWait时不退出进程，按Telegram指定秒数加1秒等待；截止时间原子保存到data/sessions/worker_sender_<BotID>.cooldown.json。重启也会遵守该期限。不要删除session或cooldown文件来绕过等待；已生效的Telegram限流仍需等候。

健康接口在等待期间保持HTTP 200、ready=true（配置就绪）、deliveryReady=false、senderState=cooldown，并返回retryAfterSeconds。此时新的delivery任务返回delivery_not_ready，不创建任务；已完成回执仍可查询，无delivery的准备接口继续可用。凭据或身份错误留在senderState=error，等待人工修正配置；网络失败有界退避。存活检查不因发送登录等待而促使容器重启。

将本次worker源码同步到服务器后沿用原方式启动。保留data持久挂载；不用重置Bot Token、不删除原会话、不安装新的数据库。没有执行真实登录或服务器部署验证。
