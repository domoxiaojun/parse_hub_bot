# Live Photo 统一投递实施计划

一个解析结果是一项投递任务。普通聊天使用原生 Live／单媒体／相册；Inline/Guest 使用 Rich，Live 表示为带自身 `video_cover` 的一个视频块。

- [x] ✅ 核对现有入口、SDK 原生 Live 相册缺口及 Rich 封面序列化边界，保留两仓无关工作树改动。
- [x] ✅ 建立 MediaAsset、DeliveryEnvelope、SendResult 与纯投递规划、文字预算、相册分组。
- [x] ✅ 收敛共享预处理：EXIF、视频真实属性、H.264/yuv420p、配对完整性。
- [x] ✅ 实现原生 MTProto 与 Rich Bot API 薄适配器、引用缓存、唯一 send_envelope 及逐批持久回执；重启发现 inFlight 时先标记 unknown，过期文件不影响已保存回执。
- [x] ✅ Worker 与旧 Bot 发送／缓存入口迁移；停止拆对和失败后媒体语义降级。
- [x] ✅ 同步 gptbot 契约、文档和 OpenSpec。
- [x] ✅ 离线 Python 112 项测试、Ruff、27 个源文件 mypy；gptbot 53 项契约／直出测试及 TypeScript 类型检查通过。
- [x] ✅ OpenSpec 严格校验、变更文档离线链接检查及两仓 `git diff --check` 通过。
- [ ] gptbot ESLint：现有 TypeScript 7.0.2 与 typescript-eslint 8.61.0 加载时报 `Cannot read properties of undefined (reading 'Cjs')`，未为此修改项目依赖。
- [ ] 发布前真实 Telegram 验收由用户自行执行：原生 Live、混排相册、Inline 冷热缓存指定封面。本轮测试脚本在本地素材生成阶段失败，未连接 Telegram、未发出测试消息；用户随后撤回代测要求。

约束：上传无可见副作用，发送前完成整项准备；每个可见请求预登记随机 ID；不重发已确认批次，发送结果不明保留 unknown。相册 11 项分 9+2，21 项分 10+9+2。超长文本摘要加来源链接；不自动发布第三方阅读页。

实机检查重点：普通聊天 1／2／10／11／21 张 Live 及混排；Inline 每张 Live 仅一个视频块，冷／热缓存都使用对应封面；旋转／镜像素材方向一致。指定封面的参数和离线序列化验证不代表客户端已正确显示。代码尚未提交、推送或部署。
