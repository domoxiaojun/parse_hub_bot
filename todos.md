# Worker 交付链路审查修复 (2026-09-18)

- [x] 1. 发送失败完全无日志：`worker/sender.py` 捕获异常后只写回执，不记录错误类型/RPC 代码/帧类型；`worker/jobs.py` 通用异常同样静默。补结构化日志（不含 URL/凭据/异常原文）。
- [x] 2. 实况照片原生发送被 Telegram 拒绝（RPCError，确认未发出）时，同一帧降级为 Rich 帧（照片 + “实况视频”），避免整条笔记只发出 1/6。
- [x] 3. kurigram `send_live_photo` 在 FilePartMissing 分支引用未赋值的 `file`，会抛 AttributeError；Worker 侧按非 RPC 错误处理并记录，不再静默。
- [x] 4. 补测试：失败日志、实况降级、降级后仍失败的回执。
- [x] 5. ruff / mypy / pytest 通过后提交。
