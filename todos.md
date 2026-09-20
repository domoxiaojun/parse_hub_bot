# 上游目录零改动重构 (2026-09-20)

计划：`plans/upstream-isolation-20260920/plan.md`。

- [x] 1. Fork 自有文件 `ruff format`（并入本轮迁移提交，git 仍识别为重命名）
- [x] 2. `worker/`、`delivery/`、`services/douyin_quality.py` 迁入 `ext/`；Fork 测试迁入 `test/ext/`；改写 import、compose、systemd、clean_cache 提示
- [x] 3. 上游目录恢复为 `upstream/main` 原样（含 Dockerfile、README；Worker 改用 `deploy/Dockerfile`）
- [x] 4. 新增 `ext/media/`、`ext/cache.py`、`ext/hooks.py`，Worker 与 delivery 改用 ext 实现；Worker 启动校验 DOWNLOAD_DIR 一致
- [x] 5. 新增 `ext/bot/`（app/handlers/inline/sender/reporters/captions/registry/__main__），handle_parse 替换与 inline handler 排除
- [x] 6. 更新测试路径，新增 `test/ext/test_upstream_hooks.py`（挂钩目标存在性、patcher 脱敏、上游目录 pristine、inline 覆盖完整性）
- [x] 7. ruff / mypy(ext) / pytest 131 项全绿；文档（CLAUDE.md、WORKER.md、COMPOSE_WORKER.md、ext/README.md）同步
- [x] 8. 提交；记录上游本地检出路径到记忆

未挂钩的上游差异（有意放弃，见 `ext/README.md`）：`db/init.py` 新库 stamp、`plugins/filters.py` caption-only 频道转发 None 保护。
