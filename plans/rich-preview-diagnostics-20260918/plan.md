# Worker Rich 预览与失败诊断修复计划

- [x] ✅ 1. 核实普通聊天从 Rich Message 回退为原生消息的调用链，并界定 preview 与 raw/zip 行为。
- [x] ✅ 2. 在统一投递核心恢复普通 preview 的 Rich Message 组装，补回平台脚注、可点击“查看原文”和真实视频元数据。
- [x] ✅ 3. 为上传准备失败增加脱敏的阶段、媒体序号、类型、大小和异常类别日志，同时保持用户回执稳定。
- [x] ✅ 4. 更新 Worker/OpenSpec 契约和回归测试，完成最小必要 pytest、Ruff、mypy 与差异检查。

验证：Worker/交付定向测试 113 项通过（1 个既有 Pyrogram 弃用警告）；修改范围 Ruff、mypy、OpenSpec 严格校验和 `git diff --check` 通过。未执行真实 Telegram 发送、部署、Docker、提交或推送。

边界：保留当前未提交的统一投递、Live Photo、缓存、幂等与原路径改动；日志禁止输出来源 URL、凭据、聊天正文、本地完整路径、文件名和 Telegram 原始异常文本。
