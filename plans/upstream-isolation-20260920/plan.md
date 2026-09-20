# 上游目录零改动：Fork 代码全部收敛到 `ext/`

日期：2026-09-20。上游对照：`upstream/main` = `1e1d671`（本地检出 `/Users/kkl/Documents/parse_hub_bot`）。

## 目标

- `bot.py`、`log.py`、`i18n.py`、`core/`、`db/`、`repo/`、`plugins/`、`services/`、`utils/`、`i18n/`、`alembic/`、`Dockerfile`、`README*.md` 与 `upstream/main` 逐字节一致；同步上游只需 ff 合并，不再解决业务冲突。
- Worker、共享投递核心、Rich Message 兼容、媒体准备增强、抖音画质兜底、Bot 侧定制全部位于新顶层包 `ext/`。
- 根目录仅保留不可避免的共享配置差异：`pyproject.toml`（aiohttp、parsehub/kurigram 下限）、`uv.lock`、`.gitignore`、`.dockerignore`。

## 现状盘点（相对 upstream/main）

| 上游文件 | Fork 改动 | 处理方式 |
| --- | --- | --- |
| `bot.py` | stop() 不删 `downloads/` | `ext/bot/app.py` 子类覆写 `stop()` |
| `core/platform_config.py` | 日志脱敏代理凭据 | `ext/hooks.py` loguru patcher 全局脱敏 `scheme://user:pass@` |
| `db/init.py` | 新库 create_all+stamp | 放弃（上游迁移均带 has_table 守卫，现行流程可用） |
| `plugins/filters.py` | caption-only 频道自动转发 None 保护 | 放弃（仅影响频道已禁用平台的极窄边界，建议提 PR 给上游） |
| `plugins/helpers.py` | Rich 文案格式、不截断正文、Telegraph 客户端关闭 | `ext/bot/captions.py` |
| `plugins/parse/handlers.py` | 结果分派改走投递核心 | 保留上游 `parse()` 入口，`ext/bot/app.py` 启动时把 `plugins.parse.handlers.handle_parse` 换成 `ext/bot/handlers.py` |
| `plugins/parse/inline.py` | 整体重写 | 插件 `exclude` 上游两个 handler，注册 `ext/bot/inline.py` |
| `plugins/parse/sender.py` / `delivery.py` / `cache.py` | send_* 改走投递核心 | `ext/bot/sender.py`；上游 `MessageSender` 原样复用 |
| `plugins/parse/reporters.py` | dismiss 容错等 | `ext/bot/reporters.py` 子类只覆写 `dismiss()` |
| `services/cache.py` | Live/音频类型、asset_key、media_version | `ext/cache.py` 自有模型 + `PersistentCache` 子类 |
| `services/media.py` + `utils/media_processing_unit.py` | EXIF 方向、Telegram 限制缩放、Live 封面不切图、实况视频准备、ffmpeg 超时 | `ext/media/processing.py` 子类 + `ext/media/motion.py`；`ext/hooks.py` 把 `services.pipeline.process_media_files` 指向 ext 实现 |
| `services/parser.py` | 抖音画质挂钩、代理脱敏 | `ext/hooks.py` 包装 `ParseService.parse`；脱敏由 patcher 承担 |
| `services/pipeline.py` | `download_dir` 参数、媒体处理超时 | 放弃参数：Worker 启动校验 `DOWNLOAD_DIR` 与 `bs.download_dir` 一致；超时由 ext 的 ffmpeg 封装承担 |
| `utils/helpers.py` / `utils/ph.py` | run_cmd(check)、mask_proxy、Telegraph.close | `ext/media/commands.py`、patcher、`ext/bot/captions.py` |
| `Dockerfile` | pipefail、Deno 固定版本 | 放弃，使用上游镜像定义 |
| `README*.md` | Worker 段落、BotFather 说明 | 移到 `ext/README.md` |

## 目标布局

```
ext/
  __init__.py
  README.md          Fork 说明：入口、同步流程、挂钩清单
  hooks.py           所有对上游模块的运行时覆盖（幂等、目标缺失即启动失败）
  cache.py           Fork 持久缓存模型与 PersistentCache 子类
  douyin_quality.py  抖音显式画质兜底
  media/             commands.py(run_cmd) / motion.py(实况视频) / processing.py(MPU 子类、process_media_files)
  delivery/          models / assets / rich / transport / sender（原 delivery/）
  worker/            原 worker/，入口 `python -m ext.worker`
  bot/               交互 Bot 定制，入口 `python -m ext.bot`
test/ext/            Fork 测试（不放 __init__.py，避免遮蔽顶层 ext 包）
```

## 验收

- `git diff --stat upstream/main -- bot.py log.py i18n.py core db repo plugins services utils i18n alembic Dockerfile README.md README.en.md .github docker-compose.yaml alembic.ini LICENSE` 为空；`test/ext/test_upstream_hooks.py` 在存在 `upstream/main` 引用时自动断言该项。
- `ruff check`、`ruff format --check ext test/ext`、`uv run mypy`（非 test 无新增错误）、`uv run pytest` 全绿。
- 部署入口改为 `python -m ext.worker`、`python -m ext.worker.healthcheck`、`python -m ext.worker.clean_cache`、`python -m ext.bot`，compose/systemd/文档同步。

## 结果（2026-09-20）

- 上游目录与 `upstream/main`（merge-base `60a8da5`）零差异；根目录差异仅 `pyproject.toml`、`uv.lock`、`.gitignore`、`.dockerignore`。
- 131 项测试、`ruff check`、`ruff format --check ext test/ext`、`uv run mypy ext bot.py` 全绿。
- ForkBot 离线冒烟：`plugins.parse.inline` 三个 handler 被卸载，`ext.bot.inline` 三个同名 handler 注册，`plugins.parse.handlers.parse` 保留并调用 Fork `handle_parse`。
- 与计划的偏差：`Dockerfile` 保持上游原样，Worker 改用 `deploy/Dockerfile`（多 pipefail 与固定 Deno 两行，测试断言差异）；`plugins/parse/cache.py` 随上游恢复（Fork 未使用）。
- 未做真实 Telegram 联调；部署机需把启动命令改为 `python -m ext.worker` / `python -m ext.bot`。
