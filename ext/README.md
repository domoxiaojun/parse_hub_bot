# ext/：Fork 自有代码

本仓库是 [z-mio/parse_hub_bot](https://github.com/z-mio/parse_hub_bot) 的 Fork。所有 Fork 自有实现都放在 `ext/` 与 `test/ext/`，上游目录（`bot.py`、`core/`、`db/`、`repo/`、`plugins/`、`services/`、`utils/`、`i18n/`、`alembic/`、`Dockerfile`、`README*.md`）与 `upstream/main` 逐字节一致，同步上游只需 ff 合并。

`test/ext/test_upstream_hooks.py` 在存在 `upstream/main` 引用时断言上述路径无差异，并检查每个运行时挂钩的目标仍然存在。

## 入口

| 进程 | 命令 | 说明 |
| --- | --- | --- |
| 交互 Bot（Fork 版） | `uv run python -m ext.bot` | 复用上游 `Bot` 类与全部插件；替换 `handle_parse` 与 inline handler，走共享投递核心 |
| HTTP Worker | `uv run python -m ext.worker` | 详见 `WORKER.md`、`COMPOSE_WORKER.md` |
| 缓存清理 | `uv run python -m ext.worker.clean_cache --stats` | |
| 容器健康检查 | `python -m ext.worker.healthcheck` | |

直接运行 `uv run bot.py` 得到的是纯上游 Bot，不包含任何 Fork 行为。

## 布局

```
ext/
  hooks.py          对上游模块的运行时覆盖（幂等；目标缺失即抛 HookTargetMissing）
  cache.py          Fork 持久缓存模型（Live Photo 配对、asset_key、media_version）与 PersistentCache 子类
  douyin_quality.py 抖音显式画质兜底
  youtube_quality.py YouTube 解析保留 mweb；解析和下载统一 1080p 优先，不按约 500MB 选流
  twitter_quality.py X 普通推文、动图和文章视频的最高码率 MP4 选择
  xhs_compat.py      小红书跨编码组选择最高分辨率，兼容缺失和改名的编码组
  instagram_media.py    Ins 候选元数据与共用质量排序
  instagram_quality.py  Ins 候选、媒体 ID、类型、DASH 保留与选源，轮播缺失项恢复
  instagram_download.py  复用 ParseHub/yt-dlp 单项下载，限域 Cookie、回退、落盘核验
  media/            commands.py（严格 run_cmd）、processing.py（MediaProcessingUnit 子类、process_media_files）
  delivery/         Bot 与 Worker 共享的投递核心（models/assets/preparation/rich/transport/sender）
  worker/           HTTP Worker
  bot/              交互 Bot 定制：app（ForkBot）、handlers、inline、sender、reporters、captions、registry
test/ext/           Fork 测试；`test/` 根目录只保留上游测试
```

## 挂钩清单（`ext/hooks.py`）

| 挂钩 | 目标 | 作用 |
| --- | --- | --- |
| `install_log_patcher` | loguru 全局 patcher | 所有日志行脱敏 `scheme://user:pass@`，覆盖上游平台配置的导入期 dump 与代理日志 |
| `install_parsehub_compat` | `ParseService.parse/get_raw_url`、`ParseResult.download` | ParseHub 2.4 作品不可用立即停止重试，其他错误继续轮换配置；仅在 Worker 流水线上限制多文件下载并发 |
| `install_douyin_quality` | `services.parser.ParseService.parse` | 抖音结果替换为显式最高画质播放地址 |
| `install_youtube_client` | `YtbParse.cli_args`、`YtbVideoParseResult.cli_args` | 解析阶段保留 mweb；解析和下载统一 1080p 优先排序，取消约 500MB 体积偏好；其他 yt-dlp 平台不变 |
| `install_twitter_quality` | `Twitter.parse`、`ArticleRenderer._best_mp4_url` | 按 `bitrate` / `bit_rate` 选择最高码率 MP4，保留请求与原结果解析 |
| `install_xhs_compat` | `XHSAPI.__select_stream` | 跨编码组和列表按分辨率优先选择，有 4K 则优先 4K |
| `install_instagram_quality` | Ins provider 媒体映射与字段恢复、`InstagramParser._do_parse`、`InstagramYtParse` | 保留候选/类型/ID，复用现有 API；自有结果类协调原下载器、可选 Cookie、回退与落盘核验 |
| `install_media_processing` | `services.pipeline.process_media_files` / `services.media.process_media_files` | EXIF/HEIF 方向、Telegram 照片限制、Live 封面不切图、实况视频准备、ffmpeg 失败即抛 |
| `install_bot_handlers`（仅 Bot） | `plugins.parse.handlers.handle_parse` | 解析结果分派走共享投递核心；上游 `parse` 消息入口原样保留 |

`ForkBot` 另通过 kurigram 插件 `exclude` 卸载 `plugins.parse.inline`，并显式注册 `ext.bot.inline` 的同名 handler。

## 上游行为差异（未挂钩，按需处理）

- `db/init.py`：上游对已有库先 `create_all` 再 `upgrade`；Fork 不再改写，上游迁移均带 `has_table` 守卫。
- `plugins/filters.py`：频道自动转发且仅有 caption 的消息在“频道已禁用平台”判断中会因 `text=None` 抛错；影响面极窄，建议向上游提 PR。
- `Dockerfile`：保持上游原样；Worker 用 `deploy/Dockerfile`（上游内容加 pipefail 与固定 Deno 版本）构建，`test/ext/test_worker_compose.py` 断言两者只差这两行。
- Worker 的下载根目录必须等于 `DOWNLOAD_DIR`（上游 `ParsePipeline` 无 `download_dir` 参数），启动时校验。

## 同步上游

```sh
git fetch upstream
git switch main && git merge --ff-only upstream/main && git push origin main
git switch worker && git merge main
uv sync --frozen
uv run pytest            # test/ext/test_upstream_hooks.py 会验证上游目录与挂钩目标
git push origin worker
```

冲突只可能出现在 `pyproject.toml`、`uv.lock`、`.gitignore`、`.dockerignore` 与 `CLAUDE.md`。若挂钩测试失败，说明上游改了被覆盖的符号，只需在 `ext/hooks.py` 中调整目标。
