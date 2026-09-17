# ParseHub 源结果与消息组装收敛计划

- [x] 以 ParseHub 2.2.3 的四种 `ParseResult` 和媒体引用为唯一源契约，保留准确正文格式与结果类型。
- [x] 让 Worker 直接复用上游 `ParseService.parse()` 和原生下载结果，定制仅从 Telegram 媒体适配开始。
- [x] 将 Live Photo 建模为原子媒体，并为普通消息实现原生发送、为 Inline/Guest 实现明确降级。
- [x] 重构消息规划，覆盖空结果、长正文、多图、多媒体、原文件、归档和部分失败。
- [x] 同步 gptbot 契约和 Worker 直出边界，禁止 Cookie 内容被旧工具路径发布到 Telegraph。
- [x] 完成 ParseHub Worker 与 gptbot 针对性测试、静态检查和类型检查。
- [ ] 精确提交两个仓库的本次文件并 push，核对远端提交 SHA。
