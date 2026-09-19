# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

ParseHubBot：基于 Kurigram (pyrogram fork) 的 Telegram 多平台链接解析机器人，解析核心由外部库 `parsehub` 提供。本仓库是 Fork，包含两个独立进程入口：

- `bot.py`：原版交互 Bot（监听消息/内联查询）。
- `uv run python -m worker`：Fork 新增的 **HTTP Worker**，不监听 Telegram 更新，为 gptbot 提供解析及 preview Rich/raw/zip 直出。详见 `WORKER.md`（协议、缓存、租约、登录限流恢复）和 `COMPOSE_WORKER.md`（Compose 部署）。

Python 3.12+，依赖用 `uv` 管理，`uv.lock` 为准。

## 常用命令

```bash
uv sync --frozen                 # 安装依赖（含 dev 组）
uv run bot.py                    # 启动交互 Bot（需 .env 中 API_ID/API_HASH/BOT_TOKEN）
uv run python -m worker          # 启动 HTTP Worker（另需 WORKER_SERVICE_KEY ≥32 字符）

uv run pytest                    # 全部测试（testpaths=test，pythonpath=.）
uv run pytest test/test_worker_*.py test/test_delivery_transport.py test/test_media_orientation.py  # Worker + 共享投递层（同步上游后必跑）
uv run pytest test/test_worker_engine.py -k name  # 单个测试

ruff format && ruff check --fix && uv run mypy   # 提交前检查（README 约定；ruff 不在项目依赖中，用全局命令）
uv run alembic revision --autogenerate -m "..."  # 新建 SQLAlchemy 表迁移（alembic/env.py 从 .env 读 DATABASE_URL）
uv run python -m worker.clean_cache --stats      # Worker 缓存统计/清理（--dry-run/--url/--force；Worker 运行中需 --online）
uv run python i18n.py            # 用 LLM 重新生成 i18n/*.yaml（需 I18N_MODEL / API key）
uv lock --upgrade-package parsehub               # 升级解析库
```

mypy 配置为 strict-ish（`disallow_untyped_defs`），新代码必须带完整类型注解。ruff 行宽 120。

测试只使用临时目录、临时 SQLite 和模拟 Telegram/平台，不启动真实 Bot、不触碰 `data/`。不依赖 pytest-asyncio，异步用例用 `asyncio.run()` 包裹。导入 `core`/`services` 即实例化 `BotSettings`（读取 `.env`，缺 BOT_TOKEN/API_ID/API_HASH 会失败，并自动创建 `data/` 子目录），因此本机跑测试也需要 `.env`。

## 分支约定

- `worker` 是默认开发分支（Worker + 定制消息显示）；`main` 只镜像 `upstream/main`。
- 同步上游：`git fetch upstream` → 在 `main` 上 ff-only 合并 → `git switch worker && git merge main` → 跑 Worker 测试 → 推 `origin worker`。
- 本机不执行 Docker 构建/部署。

## 架构

### 分层（交互 Bot）

```
plugins/            pyrogram handlers（plugins={"root": "plugins"} 自动加载）
  parse/handlers.py   消息/内联入口 → handle_parse()
  parse/sender.py     MessageSender：把解析/缓存结果组装成 DeliveryEnvelope
  parse/delivery.py   Bot 侧适配器：TelegramTransport + send_envelope；使用进程内状态与 MemoryReferences，不写 Worker 回执表
  settings/           /settings 交互，按 target 分层（user/group/member/topic/channel）
services/           无 Telegram 依赖的业务层
  parser.py           ParseService（单例，包 parsehub.ParseHub，带 cookie/proxy 轮换与重试）
  pipeline.py         ParsePipeline：解析 → 下载 → services/media.py 处理（转码、切图、分段）
  cache.py            parse_cache（内存，解析结果）+ persistent_cache（DB，Telegram file_id）
  settings.py         SettingsService + 多级 target 模型
repo/               SQLAlchemy 仓储层；db/ 引擎、session、init（启动时 create_all + alembic upgrade head）
core/               bs（BotSettings，pydantic-settings 读 .env）、pl_cfg（platform_config.yaml）、watchdog
delivery/           Bot 与 Worker 共享的投递核心（无 update handler）：
  models.py           MediaAsset/DeliveryEnvelope/Destination/SendResult 与纯投递规划（相册分组、文字预算）
  assets.py           ProcessedMedia / CacheMedia → MediaAsset 适配
  preparation.py      Live Photo 动图最终准备（ffmpeg），供 services/media.py 调用
  transport.py        薄 Telegram 传输层（原生 MTProto 单媒体/相册/Live，Rich 走 Bot API）
  rich.py             由已上传引用确定性地构造 Rich 预览卡
  sender.py           唯一业务发送入口 send_envelope：逐批回执、已确认批次不重发、结果不明保留 unknown
utils/              media_processing_unit.py（ffmpeg/Pillow）、converter、rate_limit、event_loop
i18n/               easy-ai18n；源语言 zh-hans，代码里用 `_t`/`t_` 包裹字符串
```

解析请求流：`handle_parse()` → `ParseService.get_raw_url()` 归一化 → 查 `persistent_cache`（命中直接用 file_id 重发）→ 查 `parse_cache` → `ParsePipeline` 解析/下载/处理 → `MessageSender` 组装 `DeliveryEnvelope` → `delivery.sender.send_envelope` 发送 → 写回缓存。普通聊天用原生 Live/单媒体/相册；Inline 用 Rich Message，Live 表示为带 `video_cover` 的单个视频块。同 URL 并发请求通过 `pipeline._inflight` 合流。

日志分两套：Bot 侧用 loguru（`from log import logger; logger.bind(name=...)`）；`delivery/` 与 `worker/` 用 stdlib `logging.getLogger("parsehub.delivery" | "parsehub.worker")`，输出 `event=xxx key=value` 结构化行。导入 `log` 会把 stdlib root logger 重设为 ERROR，所以 Worker 在导入引擎之后才调用 `configure_logging()`。

### Worker（薄适配器）

`worker/` 不复制解析逻辑，只在原组件之外加 HTTP/幂等/租约/发送：

- `__main__.py`：文件锁防止多实例；先起 aiohttp，再后台登录发送客户端。
- `app.py`：Bearer 认证中间件；`/health`、`/capabilities`、`/config`（远程编辑 platform_config.yaml，sha256 冲突检查）、`/jobs`、`/leases`。协议版本 3。
- `jobs.py`：按幂等键去重、共享准备任务、每调用方独立租约。
- `engine.py` + `upstream_adapter.py`：调用原 `ParseService`/`ParsePipeline`，把 `ProcessedMedia` 适配为交付描述；不自行下载、不自行转码。
- `sender.py` / `sender_runtime.py` / `rich_delivery.py`：把任务转成顶层 `delivery/` 的 Envelope 后调用同一个 `send_envelope`，回执裁剪为有界、脱敏的 evidence；独立 session `worker_sender_<BotID>.session`，FloodWait 冷却持久化到 `.cooldown.json`。
- `store.py`：用原生 sqlite3 在同一数据库建 `worker_jobs/worker_cache/worker_aliases/worker_leases/worker_uploads/worker_owned_paths`（`CREATE TABLE IF NOT EXISTS`，不走 alembic）；`recover()` 只由持有数据目录文件锁的进程调用。`clean_cache.py` 按 48h/10GiB 清理未租约文件，CLI 争用同一把锁。
- `config.py`：`WorkerSettings` 与 `BotSettings` 独立，但复用同一 `.env`；只允许 loopback 绑定，容器需显式 `WORKER_ALLOW_CONTAINER_BIND=true`。
- 部署入口：`compose.worker.yaml`（把 Dockerfile 的 `bot.py` CMD 覆盖为 `python -m worker`，健康检查 `python -m worker.healthcheck`）和 `deploy/parsehub-worker.service`（systemd）。`test/test_worker_compose.py` 断言这些文件与 `.dockerignore` 的结构，改部署文件后要跑。

Worker 与交互 Bot 共用 Token、`data/`、`downloads/` 和数据库，但**不要同时运行两者处理同一消息**。

### 关键约束

- 平台 Cookie/代理只来自 `data/config/platform_config.yaml`（`core/platform_config.py`），Worker 不新增挑战检测或 Cookie 尝试。
- Worker 日志不得包含 URL、Cookie、代理凭据、异常原文；用 `reason=xxx` 结构化字段。
- Worker 不臆造 ParseHub 没有的元数据（作者、发布时间等）。
- 投递核心约束：发送前完成整项准备，上传无可见副作用；每个可见请求预登记随机 ID；已确认批次不重发。`DeliveryError` 只携带稳定错误码，不带上游异常原文。
- SQLAlchemy 表的迁移放在 `alembic/versions/`，启动时自动升级（`worker_*` 表除外，见上）。
- `SettingsConfig`（`repo/settings/schema.py`）结构变更必须递增 `CURRENT_SCHEMA_VERSION`，并在 `repo/settings/migrations/REGISTRY` 注册 `v→v+1` 的 JSON 迁移函数；`test_settings_schema_version.py` 强制 `CURRENT == max(REGISTRY) + 1`。

## 计划文档

`plans/<topic>-<date>/plan.md` 记录各轮设计与验收；`openspec/changes/` 为 Worker 规范变更。新功能先在此落计划再实现。
