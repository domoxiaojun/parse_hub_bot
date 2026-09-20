# 当前 Worker 质量与 Telegram 规格记录计划

- [x] 1. 接入抖音显式质量探测与最高候选选择 ✅
- [x] 2. 提升 Worker 媒体版本与任务缓存键，避免复用旧低清结果 ✅
- [x] 3. 核对 Telegram Bot API、MTProto、Kurigram 和当前上传调用链 ✅
- [x] 4. 在当前 Worker checkout 记录 Telegram 全部媒体格式与最高规格 ✅
- [x] 5. 增加不依赖相邻源码的 Worker 抖音质量兜底，并验证运行时生效 ✅
- [ ] 6. 在生产镜像验收 1080p/1440p/2160p 探测请求
- [ ] 7. 审计其他平台的候选质量选择逻辑并补回归测试

说明：Worker 现在可仅依赖 PyPI 的 ParseHub 2.2.x；如果安装的 ParseHub 尚未包含
上游选流修复，`services/douyin_quality.py` 会在解析结果上执行显式质量探测。生产
部署只需当前 Worker checkout，不再要求 VPS 存在相邻 `../ParseHub`。

规格明细见 [`TELEGRAM_MEDIA_LIMITS.md`](../../TELEGRAM_MEDIA_LIMITS.md)。
