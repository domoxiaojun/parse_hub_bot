# Worker 原生 Rich Message 全量更新计划

- ✅ 核对 ParseHub 图片、Live Photo 下载结构与 Telegram Bot API 10.x / Kurigram 类型。
- ✅ 将 SDK 最低版本升级为当前稳定版 Kurigram 2.2.26（Bot API 10.3）。
- ✅ 补齐 HEIC、HEIF、AVIF 等图片解码及 Telegram 预览格式转换。
- ✅ 将 Live Photo 建模为原子媒体，普通消息使用原生 `sendLivePhoto`，Inline/Guest 明确降级；多图映射为 Rich Collage。
- ✅ 审计并补齐视频、动画、音频、语音、文档、长正文和链接的 Rich Block 映射与限制。
- ✅ 更新媒体缓存版本，避免继续复用旧的部分失败结果。
- ✅ 移除Worker预解析与自定义生产解析入口，生产环境直接调用上游`ParseService.parse()`。
- ✅ 完成针对性测试、Worker 全量测试、Ruff、mypy 与锁文件校验。
