# YouTube 1080p：mweb 播放器客户端挂钩 (2026-09-21)

计划：`plans/youtube-mweb-20260921/plan.md`。

- [x] 0. mypy 排除 `scripts/`（`pyproject.toml` `[tool.mypy] exclude`），恢复全量 mypy 可运行
- [x] 1. `ext/youtube_quality.py`：mweb 客户端常量与幂等的 `extend_cli_args()`
- [x] 2. `ext/hooks.py`：`install_youtube_client()` 覆盖 `YtbParse.cli_args`（仅解析阶段），纳入 `install()`
- [x] 3. `MEDIA_VERSION` 3→4、Worker 缓存键追加 `v11-youtube-mweb`
- [x] 4. 测试：`test/ext/test_upstream_hooks.py`、`test/ext/test_youtube_quality.py`
- [x] 5. 文档同步：`ext/README.md`、`CLAUDE.md`、`WORKER.md`、`TELEGRAM_MEDIA_LIMITS.md`
- [x] 6. ruff / mypy(ext) / pytest 140 项全绿；真实 URL 端到端验证：解析 3.5s、下载 13.5s、ffprobe h264 1920x1080 + aac
- [x] 7. 提交

备注：全量 `uv run mypy` 仍有 250 个历史错误，全部在 `test/ext/` 的既有测试文件中（本次改动前因 `scripts/` 模块名冲突根本跑不到这一步）；`ext/` 本身 0 错误。
