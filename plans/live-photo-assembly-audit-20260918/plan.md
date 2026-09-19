# Live Photo 单消息组装与封面方向修复计划

> 历史方案，已由 `plans/unified-delivery-20260918/plan.md` 取代。以下勾选仅记录当时工作，不代表当前产品规则；普通聊天强制 Rich、Live 拆为两个块及阅读页降级均已撤销，方向预处理保留并收敛。

- [x] ✅ 核对 ParseHub 下载与处理产物，确认 Live Photo 静态图、视频、尺寸、时长和方向元数据如何进入 Worker 契约。
- [x] ✅ 逐层检查 Worker 普通消息、Inline/Guest、失败降级与旧 Bot 发送路径，确认独立原生 Live Photo 帧会把一条结果稳定拆成多条消息。
- [x] ✅ 用最小构造样例复现消息规划：正文加一个 Live Photo 为三帧，无正文仍为两帧，两个 Live Photo 为四帧。
- [x] ✅ 用 EXIF Orientation=6 的 JPEG/WebP 样例验证：现有缩放和格式转换会丢失方向，且媒体尺寸读取使用未转正的像素宽高。
- [x] ✅ 将普通消息、Inline 和 Guest 统一为单个 Rich Message；Live Photo 在同一消息内降级为封面图和实况视频两个块，不再调用独立 `sendLivePhoto`。
- [x] ✅ 禁止普通消息因媒体数、正文或 block 上限拆帧；单条超限时走现有受控失败/阅读版路径。
- [x] ✅ 在共享图片处理入口规范化 EXIF 方向，再执行格式转换、比例判断、缩放、填充或切图，并补齐回归测试。
- [x] ✅ 更新 Worker 文档/OpenSpec；118 项测试、Ruff、改动源文件 mypy、OpenSpec 严格校验、离线链接和 `git diff --check` 均通过。
