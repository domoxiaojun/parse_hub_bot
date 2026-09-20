# YouTube 1080p：mweb 播放器客户端挂钩 (2026-09-21)

## 背景

`data/config/platform_config.yaml` 中的 YouTube Cookie 属于 Premium 账号。yt-dlp 2026.8.19 对 Premium 会话默认使用
`web_creator` / `tv_downgraded` / `web` 客户端，而 YouTube 已对这些客户端启用 SABR-only 串流：https 格式全部因缺少 URL
被跳过，info.json 里只剩渐进式 360p（format 18）。parsehub 2.2.4 的下载阶段用 `--load-info-json` 复用解析阶段的格式
列表，排序参数 `-S +codec:h264,filesize~500M` 本身正确，因此拿不到 1080p 是解析阶段的播放器客户端选择问题。

实测（同一 Cookie、同一 yt-dlp 参数、`--js-runtimes node`）：

| player_client | 可用视频格式 | parsehub 排序后选中 |
| --- | --- | --- |
| 默认（Premium） | 1 个，仅 360p | 18，640x360 |
| `mweb` | 32 个，最高 2160p | 137+140，1920x1080 h264 + m4a，`--test` 下载合并成功 |
| `web_safari` | 7 个 HLS 预合并流 | 96，1920x1080 h264 |
| `tv` / `tv_downgraded` 单独 | 报 "The page needs to be reloaded" | — |
| `ios` / `android_vr` | 不支持 Cookie，无格式 | — |

## 方案

- [x] 1. `ext/youtube_quality.py`：`YOUTUBE_PLAYER_CLIENT = "mweb"` 与幂等的 `extend_cli_args()`（已有 `youtube:` 参数时合并，不覆盖）；
      同时把下载阶段的 `-S +codec:h264,filesize~500M` 也加到解析阶段，否则 info.json 顶层宽高描述的是 4K AV1 选流而实际下载 1080p h264
- [x] 2. `ext/hooks.py`：`install_youtube_client()` 覆盖 `parsehub.parsers.parser.youtube.YtbParse.cli_args`，只影响解析阶段；
      下载阶段 `YtbVideoParseResult.cli_args` 不动；Facebook / Snapchat 等其他 `YtParser` 子类不受影响
- [x] 3. `MEDIA_VERSION` 3→4、Worker 缓存键追加 `v11-youtube-mweb`，让已缓存的 360p file_id 与 Worker 结果失效
- [x] 4. 测试：挂钩目标存在与幂等、只影响 YouTube、yt-dlp 能把参数解析为 `extractor_args`
- [x] 5. 文档：`ext/README.md`、`CLAUDE.md`、`WORKER.md`、`TELEGRAM_MEDIA_LIMITS.md`
- [x] 6. 用真实 URL 端到端验证（挂钩后解析 + 下载 + ffprobe 得到 1920x1080）

## 约束与前提

- mweb 的 https/DASH 格式对 Premium 账号免 PO Token（yt-dlp `not_required_for_premium`）；换成非 Premium Cookie 需重新验证。
- 运行主机必须有 yt-dlp 支持的 JS 运行时：Docker 镜像已装 deno；本机没有 deno 时需通过 yt-dlp 配置 `--js-runtimes node`，与本改动无关。
- 该样片的 1080p h264 流为 SDR；HDR 只存在于 vp9 / av01，按 parsehub 优先 h264 的排序仍得到 SDR 1080p。
