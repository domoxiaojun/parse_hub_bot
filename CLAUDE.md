# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

ParseHubBot：基于 Kurigram (pyrogram fork) 的 Telegram 多平台链接解析机器人，解析核心由外部库 `parsehub` 提供。本仓库是 Fork，包含两个独立进程入口：

- `bot.py`：原版交互 Bot（监听消息/内联查询）。
- `python -m worker`：Fork 新增的 **HTTP Worker**，不监听 Telegram 更新，为 gptbot 提供解析 + Rich Message 直出。详见 `WORKER.md`（协议、缓存、租约、登录限流恢复）和 `COMPOSE_WORKER.md`（Compose 部署）。

Python 3.12+，依赖用 `uv` 管理，`uv.lock` 为准。

## 常用命令

```bash
uv sync --frozen                 # 安装依赖（含 dev 组）
uv run bot.py                    # 启动交互 Bot（需 .env 中 API_ID/API_HASH/BOT_TOKEN）
uv run python -m worker          # 启动 HTTP Worker（另需 WORKER_SERVICE_KEY ≥32 字符）

uv run pytest                    # 全部测试（testpaths=test，pythonpath=.）
uv run pytest test/test_worker_*.py            # 仅 Worker 测试（同步上游后必跑）
uv run pytest test/test_worker_engine.py -k name  # 单个测试

ruff format && ruff check --fix && uv run mypy   # 提交前检查（README 约定）
uv run python i18n.py            # 用 LLM 重新生成 i18n/*.yaml（需 I18N_MODEL / API key）
uv lock --upgrade-package parsehub               # 升级解析库
```

mypy 配置为 strict-ish（`disallow_untyped_defs`），新代码必须带完整类型注解。ruff 行宽 120。

测试只使用临时目录、临时 SQLite 和模拟 Telegram/平台，不启动真实 Bot、不触碰 `data/`。

## 分支约定

- `worker` 是默认开发分支（Worker + 定制消息显示）；`main` 只镜像 `upstream/main`。
- 同步上游：`git fetch upstream` → 在 `main` 上 ff-only 合并 → `git switch worker && git merge main` → 跑 Worker 测试 → 推 `origin worker`。
- 本机不执行 Docker 构建/部署。

## 架构

### 分层（交互 Bot）

```
plugins/            pyrogram handlers（plugins={"root": "plugins"} 自动加载）
  parse/handlers.py   消息/内联入口 → handle_parse()
  parse/sender.py     MessageSender：preview/raw/zip/cached 发送逻辑
  settings/           /settings 交互，按 target 分层（user/group/member/topic/channel）
services/           无 Telegram 依赖的业务层
  parser.py           ParseService（单例，包 parsehub.ParseHub，带 cookie/proxy 轮换与重试）
  pipeline.py         ParsePipeline：解析 → 下载 → services/media.py 处理（转码、切图、分段）
  cache.py            parse_cache（内存，解析结果）+ persistent_cache（DB，Telegram file_id）
  settings.py         SettingsService + 多级 target 模型
repo/               SQLAlchemy 仓储层；db/ 引擎、session、init（启动时 create_all + alembic upgrade head）
core/               bs（BotSettings，pydantic-settings 读 .env）、pl_cfg（platform_config.yaml）、watchdog
utils/              media_processing_unit.py（ffmpeg/Pillow）、converter、rate_limit、event_loop
i18n/               easy-ai18n；源语言 zh-hans，代码里用 `_t`/`t_` 包裹字符串
```

解析请求流：`handle_parse()` → `ParseService.get_raw_url()` 归一化 → 查 `persistent_cache`（命中直接用 file_id 重发）→ 查 `parse_cache` → `ParsePipeline` 解析/下载/处理 → `MessageSender` 发送 → 写回缓存。同 URL 并发请求通过 `pipeline._inflight` 合流。

### Worker（薄适配器）

`worker/` 不复制解析逻辑，只在原组件之外加 HTTP/幂等/租约/发送：

- `__main__.py`：文件锁防止多实例；先起 aiohttp，再后台登录发送客户端。
- `app.py`：Bearer 认证中间件；`/health`、`/capabilities`、`/config`（远程编辑 platform_config.yaml，sha256 冲突检查）、`/jobs`、`/leases`。协议版本 3。
- `jobs.py`：按幂等键去重、共享准备任务、每调用方独立租约。
- `engine.py` + `upstream_adapter.py`：调用原 `ParseService`/`ParsePipeline`，把 `ProcessedMedia` 适配为交付描述；不自行下载、不自行转码。
- `rich_delivery.py` / `reading_delivery.py` / `sender.py` / `sender_runtime.py`：Rich Message 组装与发送，独立 session `worker_sender_<BotID>.session`，FloodWait 冷却持久化到 `.cooldown.json`。
- `store.py`：任务、文件缓存、租约写入原 SQLite 的 `worker_*` 表；`clean_cache.py` 定期按 48h/10GiB 清理未租约文件。
- `config.py`：`WorkerSettings` 与 `BotSettings` 独立，但复用同一 `.env`；只允许 loopback 绑定，容器需显式 `WORKER_ALLOW_CONTAINER_BIND=true`。

Worker 与交互 Bot 共用 Token、`data/`、`downloads/` 和数据库，但**不要同时运行两者处理同一消息**。

### 关键约束

- 平台 Cookie/代理只来自 `data/config/platform_config.yaml`（`core/platform_config.py`），Worker 不新增挑战检测或 Cookie 尝试。
- Worker 日志不得包含 URL、Cookie、代理凭据、异常原文；用 `reason=xxx` 结构化字段。
- Worker 不臆造 ParseHub 没有的元数据（作者、发布时间等）。
- 数据库迁移放在 `alembic/versions/`，启动时自动升级。

## 计划文档

`plans/<topic>-<date>/plan.md` 记录各轮设计与验收；`openspec/changes/` 为 Worker 规范变更。新功能先在此落计划再实现。
