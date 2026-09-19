# 审查修复落地 (2026-09-19)

回滚点: `rollback-before-audit-fixes` (e81ddb0)。每批: ruff check + mypy(非 test) + 全量 pytest 后单独提交。

## 第一批：小而明确
- [x] 1-1 `clean_cache --online` UnboundLocalError；补 --online 测试
- [x] 1-5 Worker 取消时 `partial` 语义恢复；补测试
- [x] 1-4 `send_raw`/`send_zip` 恢复 report_error；打包纳入 try
- [x] 1-8 `custom_content` 追加而非替换；补测试
- [x] 2-7 死代码：BotRejected 分支、未用参数、gif 阈值常量、KEY_VERSIONS 单一来源；重复 plan() 保留以维持交付错误映射
- [x] 3-a filters.py caption-only 转发传 None
- [x] 3-b parser.py 保留异常类型
- [x] 3-c reporters/sender fire-and-forget 任务持引用+异常保护
- [x] 3-d Dockerfile Deno 固定版本 + pipefail + dockerignore
- [x] 3-e db/init 新库 create_all + stamp head
- [x] 4-2/4-3 E501、clean_cache 容器名提示

## 第二批：约束与容量
- [ ] pipeline.py:214 下载代理 mask_proxy（1-2 的唯一保留项）
- [ ] 1-6 Bot 不写持久回执；Worker 回执 7d TTL；recover 只扫未完成；ON CONFLICT upsert；文档同步
- [ ] 2-4 Bot.stop 不清空 Worker 占用的 downloads
- [ ] 2-5 asset_key 哈希到线程；Store synchronous=NORMAL；ack checkpoint 失败不误判
- [ ] 2-1 transport.refresh origin 失败回退本地重传

## 第三批：功能与健壮性
- [ ] 1-3 文章 markdown Rich 全量交付（下载内嵌图、tg://photo 嵌入、超限按段拆分、删 Telegraph/rich_mode、恢复 video_cover）
- [ ] 全局媒体规则：仅超 Telegram 限制/不支持格式才转码；去 2560 缩放；视频 remux 优先；实况 remux 优先 + 损坏降级静图；MEDIA_VERSION+1
- [ ] 1-7 内联：语言、hide_*、异常兜底、缓存命中快路径、enable_inline_raw_url、文档 feedback 100%
- [ ] 2-3 媒体处理超时 + run_cmd 返回码 + 两套 ffmpeg 封装合一 + Worker prepare 总超时
- [ ] 2-6 错误分类：类型优先 + 规则表 + 合约测试 + 进度字符串守护
