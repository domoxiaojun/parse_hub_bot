# Worker 薄适配器收敛计划

- [x] ✅ 抽出并复用原版 URL 归一化、持久缓存、解析缓存和 `ParsePipeline`，Worker 不再实现解析或媒体处理流程。
- [x] ✅ 将原版 `PipelineResult` 和 `CacheEntry` 无损适配为发送契约，保留 preview/raw/zip 与 Live Photo 语义。
- [x] ✅ 让 Worker 只负责 HTTP、幂等、租约、消息规划和 Telegram 发送。
- [x] ✅ 修正 Compose 数据目录说明，确保原版缓存与 Worker 可配置为同一数据卷。
- [x] ✅ 更新测试，证明缓存命中不调用解析、缓存绕过和媒体准备均由原版组件决定。
- [ ] 完成 Worker 全量测试、Ruff、mypy、锁文件校验并提交 push。
