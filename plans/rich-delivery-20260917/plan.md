# ParseBot RichMessage 内容完整性优化

- [x] ✅ 1. 对照 ParseHub、Worker 和 Telegram Rich 限制，定位标题与正文丢失路径。
- [x] ✅ 2. 将标题改为普通加粗段落，移除 Worker 与旧 Rich 路径的静默正文截断。
- [x] ✅ 3. 增加正文 Unicode/UTF-8 安全分片、媒体分片和普通消息 reply 链。
- [x] ✅ 4. 接入公开文章/纯文本的 Telegraph 完整分页、阅读链接复用及失败回执。
- [x] ✅ 5. 按用户补充要求取消 inline 阅读版与媒体附件组合，不自动打包媒体。
- [x] ✅ 6. 验证全文还原、超长标题、分片预算、媒体配对、发送回执与旧格式助手。

## 交付边界

- 普通消息完整分片，标题为普通字号加粗；按整帧 UTF-8 字节数、块数及媒体数量检查上限。
- inline/guest 容量内保留现有 Rich 编辑；文章或纯文本超限采用独立阅读版，不附加本地媒体。
- 媒体帖子超限明确失败并提示改用普通消息，不发布半截正文，不自动打包或丢弃部分媒体。
- 原版 parsehubbot 的逐图片/视频 inline 候选列表保持原有逻辑。gptbot 查询入口的候选列表不在本次修改内。
- Telegraph 内容以 literal 文本节点保存；超出页面大小时链接续页，已创建页面 URL 随回执持久化。
- 完整正文交付与提供给模型的 8000 字节证据摘要分离；仅完整确认的结果进入证据，部分已发文本留在回执。
- ParseHub 解析字段保持不变，不执行真实 Telegram 发送、Telegraph 发布、生产部署或 git 提交。

## 验证结果

- `uv run pytest test/test_worker_delivery.py test/test_worker_service.py test/test_worker_sender_runtime.py`：55 项通过。
- 修改范围 Ruff 与 5 个源码文件 mypy 通过，`git diff --check` 通过。
- Pyrogram 导入有既有 event loop 弃用警告；未进行线上客户端显示或发送验证。
