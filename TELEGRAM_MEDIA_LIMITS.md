# Telegram 媒体格式与规格基线

核查日期：2026-09-20  
官方 Bot API 版本：10.3

本文记录 Telegram 官方公开限制、当前 Worker 实际上传路径，以及两者之间的边界。
它不把经验性兼容建议误写成 Telegram 的硬限制。

## 结论摘要

- 普通 `sendVideo` 没有官方写死的 1080p、2K 或 4K 分辨率上限，也没有写死
  码率、帧率或时长上限。只要文件大小和上传通道允许，Worker 不应仅因为分辨率高于
  1080p 就主动降清晰度。
- 官方只明确说 Telegram 客户端支持 MPEG4 视频；其他格式可以作为 Document 发送。
  MP4 + H.264/AVC + AAC + `yuv420p` + `faststart` 是跨客户端兼容策略，不是普通
  视频的全部官方硬限制。
- 默认云端 Bot API 的新文件上传限制通常是照片 10 MB、其他文件 50 MB；当前 Worker
  实际使用 Kurigram/Pyrogram 的 MTProto 上传，不受这个 50 MB 上传入口限制。
- 当前 Worker 对视频采用 `2,000,000,000` 字节（约 1907.35 MiB）安全阈值；锁定的
  Kurigram 2.2.26 对非 Premium 会话允许 2000 MiB，对 Premium 会话允许 4000 MiB。
- Telegram 只明确保证大频道可能异步生成多质量、多格式的 `alt_documents`。不能把
  这项服务端转码能力当作普通私聊、群聊或所有频道的通用保证。

## Bot API 发送规格

以下“上传上限”指通过默认 `api.telegram.org` 新上传文件，不包含复用已经在 Telegram
服务器上的 `file_id`。

| 类型 | 官方接受的格式或语义 | 默认 Bot API 新上传上限 | 其他最高规格 |
| --- | --- | ---: | --- |
| 照片 `sendPhoto` | 官方未在该方法中枚举扩展名 | 10 MB | 宽+高不超过 10000；宽高比不超过 20:1 |
| Live Photo `sendLivePhoto` | 一张静态照片 + 一段 Live Photo 视频 | 视频 10 MB；静态图同时受通用照片上限约束 | 视频最长 10 秒；照片和视频均不支持 URL 发送 |
| 视频 `sendVideo` | Telegram 客户端支持 MPEG4；其他格式可作为 Document | 50 MB | 官方未声明普通视频的固定分辨率、码率、帧率或时长上限 |
| 动画 `sendAnimation` | GIF，或无音频的 H.264/MPEG-4 AVC 视频 | 50 MB | 官方未声明固定分辨率、帧率或时长上限 |
| 音乐 `sendAudio` | `.MP3` 或 `.M4A` | 50 MB | 在音乐播放器中展示；时长没有固定上限声明 |
| 语音 `sendVoice` | OGG/Opus、MP3 或 M4A | 50 MB | 其他音频应作为 Audio 或 Document |
| 视频留言 `sendVideoNote` | 圆形/圆角方形 MPEG4 | 通用“其他文件”上限 50 MB | 最长 1 分钟；`length` 是视频宽高/直径，官方未声明固定像素上限 |
| 文档 `sendDocument` | 任意文件类型 | 50 MB | 不兼容的图片、音频和视频可保留原文件作为 Document 发送 |
| 贴纸 `sendSticker` | 静态 WEBP、动画 TGS、视频 WEBM | 见“贴纸与自定义表情” | 直接 HTTP URL 只支持 WEBP；TGS/WEBM 不能通过 URL 新传 |
| 付费媒体 `sendPaidMedia` | 照片或视频 | 遵循对应媒体/通用上传限制 | 一条消息最多 10 项 |
| Rich Message 媒体 | Animation、Audio、Document、Photo、Video、Voice Note | 遵循对应 `InputMedia` | `tg://photo`、`tg://video`、`tg://document`、`tg://audio` 可引用已声明媒体 |

补充限制：

- `sendMediaGroup` 支持 Photo、Live Photo、Video、Document、Audio，每组 2–10 项；
  Document 和 Audio 只能与相同类型组成一组。Animation、Voice、Video Note、Sticker
  不属于该相册接口的成员类型。
- 缩略图必须是 JPEG、小于 200 kB，宽和高均不超过 320 像素。
- 媒体 caption 在上述普通媒体方法中通常为解析后 0–1024 字符。

## 默认 Bot API 的三种文件来源

| 文件来源 | 限制 |
| --- | --- |
| `file_id` | 官方写明“以这种方式发送文件没有限制”；这是复用 Telegram 已存文件，不代表可以经默认 HTTP API 无限制上传新文件 |
| HTTP URL | 照片最大 5 MB，其他内容最大 20 MB；`sendDocument` 的 URL 当前只保证 PDF/ZIP；`sendVoice` URL 必须是 `audio/ogg` 且不超过 1 MB，1–20 MB 会作为普通文件发送 |
| `multipart/form-data` | 照片最大 10 MB，其他文件最大 50 MB |

默认 Bot API 的 `getFile` 下载上限是 20 MB。自行运行官方 Local Bot API Server 后，
官方列出的能力是：下载无大小限制，新文件上传最高 2000 MB。

## Stories

Stories 只适用于有 `can_manage_stories` 权限的托管 Business 账号，不是当前 Worker
普通解析结果发送路径。

| 类型 | 硬规格 |
| --- | --- |
| Story 照片 | 必须为 1080×1920；不超过 10 MB |
| Story 视频 | 必须为 720×1280、可流式播放、MPEG4 容器、H.265 编码、每秒有关键帧；不超过 30 MB；时长 0–60 秒 |

## 贴纸与自定义表情

| 类型 | 格式 | 大小 | 尺寸、时长和帧率 |
| --- | --- | ---: | --- |
| 静态贴纸 | PNG 或 WEBP，透明层 | 最大 512 KB | 一边必须为 512 px，另一边不超过 512 px |
| TGS 动画贴纸 | TGS/Lottie | 最大 64 KB | 512×512；最长 3 秒；创建指南要求循环、60 FPS；导入页列为 30–60 FPS |
| WEBM 视频贴纸 | WEBM + VP9，无音频 | 最大 256 KB | 一边必须为 512 px，另一边不超过 512 px；最长 3 秒；最高 30 FPS |
| 静态自定义表情 | PNG 或 WEBP | 官方创建页未单列不同大小上限 | 必须为 100×100 px |
| 视频自定义表情 | WEBM + VP9，无音频 | 最大 256 KB | 必须为 100×100 px；最长 3 秒；最高 30 FPS |

Bot API 的 `sendSticker` 直接上传文案只列出 WEBP/TGS/WEBM；创建或添加贴纸集时，
`InputSticker` 的静态格式明确允许 PNG 或 WEBP。

## MTProto 官方边界

当前 Worker 本地文件发送走 MTProto，而不是默认 Bot API 的 multipart 上传：

1. 文件按分片上传，最大且推荐的分片大小为 512 KB。
2. 大于 10 MB 使用 `upload.saveBigFilePart`，较小文件使用 `upload.saveFilePart`。
3. 总文件大小由动态配置 `upload_max_fileparts_default` 和
   `upload_max_fileparts_premium` 乘以实际分片大小决定；官方文件页没有把总大小固定
   写死在协议说明里。
4. 大频道的视频可能由 Telegram 自动生成多质量、多格式的 `alt_documents`，处理期间
   消息可能暂时进入计划队列。这不是普通聊天视频的普遍转码承诺。
5. MTProto 相册最多也是 10 项。

## 当前 Worker 实际行为

当前 Worker 使用 Kurigram 2.2.26，视频上传调用链是：

```text
TelegramTransport.upload
  -> TelegramTransport._document
  -> client.save_file（512 KiB 分片）
  -> messages.UploadMedia
  -> InputMediaUploadedDocument + DocumentAttributeVideo
```

项目现状：

- `MediaProcessingUnit.TG_MAX_VIDEO_SIZE = 2_000_000_000`，超过后才分段。这比
  Kurigram 非 Premium 的 2000 MiB 上限更保守。
- `DocumentAttributeVideo(supports_streaming=True)` 只是告诉 Telegram 此文件适合流式
  播放，不会自动把低清源变成高清，也不会替 Worker 选择 1080p/2K/4K 源流。
- 普通处理路径只在容器不是 MP4 时尝试 `-c copy` 重封装为 MP4，并加 `faststart`；
  它不会改变分辨率。
- `ext/media/processing.py` 的 `ensure_h264()` 虽然存在，但当前没有调用者。若未来接入，其现有规则会把超过
  30 分钟且高度大于 720 的视频缩到 720p，因此接入前必须先修改质量策略。
- 当前常规路径没有完整验证或强制 H.264、AAC、`yuv420p`。单纯把 VP9/AV1/HEVC
  码流 `-c copy` 到 MP4 不等于跨客户端兼容。
- 当前 Worker 已将 `MEDIA_VERSION` 提升到 3，并将任务缓存键提升到
  `v10-douyin-quality`，避免旧的低清媒体结果继续复用。

## 对最高画质选择的约束

后续平台质量审计应按以下顺序判断，不能把 Telegram 兼容性提前等同于“只选 1080p”：

1. 先从源站候选中选择真实最高分辨率；同分辨率再比较码率、帧率和文件大小。
2. 若最高质量本身是兼容 MP4 视频，应原样上传，不因 2K/4K 主动降为 1080p。
3. 若最高质量使用 AV1、VP9、HEVC 或非 MP4 容器，应区分：
   - 原始最高画质：作为 Document 保留；
   - 客户端内联播放版本：必要时转为 MP4/H.264/AAC/`yuv420p`/`faststart`。
4. 转码版本不能悄悄替代原始最高画质；如果只能发送一个版本，产品层需要明确选择
   “最高原画”还是“最大兼容”。
5. Telegram 上传成功只证明文件已送达，不证明 Telegram 客户端一定按原分辨率播放，
   也不证明服务端为普通聊天生成了其他质量版本。

## 官方来源

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Sending files](https://core.telegram.org/bots/api#sending-files)
- [Using a Local Bot API Server](https://core.telegram.org/bots/api#using-a-local-bot-api-server)
- [Uploading and Downloading Files（MTProto）](https://core.telegram.org/api/files)
- [Telegram Stickers](https://core.telegram.org/stickers)
- [Sticker Formats](https://core.telegram.org/import-stickers#sticker-formats)

