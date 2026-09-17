# Worker RichMessage 直接交付

- [x] ✅ 1. 核对现有 Worker job、缓存、lease 和 Telegram SDK 能力。
- [x] ✅ 2. 实现可信 delivery 输入、RichMessage 组装和 no-updates sender。
- [x] ✅ 3. 实现 delivery 回执、幂等和 lease 终态收口。
- [x] ✅ 4. 补齐 Worker 定向测试和最小必要验证。

验证：定向75项pytest、7个修改源码mypy、修改范围Ruff通过；WORKER.md离线链接检查通过。没有执行真实发送、部署或Docker。

已有Worker及原生路径改动保留；本次不提交或推送parse_hub_bot。发送未知和部分完成返回持久回执，不盲目重复；源文字以literal RichText放入结构化块，样式为标题、元信息、可折叠正文、媒体和来源。
