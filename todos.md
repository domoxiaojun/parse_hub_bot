# Worker 交付链路审查修复 (2026-09-18)

- [x] 1. 发送失败完全无日志：`worker/sender.py` 捕获异常后只写回执，不记录错误类型/RPC 代码/帧类型；`worker/jobs.py` 通用异常同样静默。补结构化日志（不含 URL/凭据/异常原文）。
- [x] 2. 实况照片原生发送被 Telegram 拒绝（RPCError，确认未发出）时，同一帧降级为 Rich 帧（照片 + “实况视频”），避免整条笔记只发出 1/6。
- [x] 3. kurigram `send_live_photo` 在 FilePartMissing 分支引用未赋值的 `file`，会抛 AttributeError；Worker 侧按非 RPC 错误处理并记录，不再静默。
- [x] 4. 补测试：失败日志、实况降级、降级后仍失败的回执。
- [x] 5. ruff / mypy / pytest 通过后提交。

# 全仓审查修复 (2026-09-18, 第二轮)

## Worker 控制面
- [x] A. `Store.__init__` 无条件执行中断恢复；`clean_cache` 不持 Worker 文件锁，`--stats` 也会破坏运行中的任务。恢复改为显式 `recover()`，CLI 获取同一把锁，被占用时拒绝。
- [x] B. `save_job` 新行 `idem=""` 撞 UNIQUE，第二个任务 IntegrityError。空值改为 NULL。
- [x] C. 中间件 500 无日志；`compare_digest` 遇非 ASCII 抛 TypeError；`invalid_config_update` 返回 409 而非 400；`DELETE /jobs/{id}` 不存在返回 200；`read_media` 文件缺失 500。
- [x] D. `content_restricted` 用裸 ValueError 无 `.code`，落入 `prepare_failed`。
- [x] E. 非复用结果 `expires=now`，cleanup 可能在租约创建前删除。cleanup 跳过刚发布的行。
- [x] F. `validate_url` 放行 `127.1` / `0x7f000001` 等数字写法。
- [x] G. 下载超时 `timeout` 无对应错误码，落入 `upstream_contract`。新增 `upstream_timeout`。

## Worker 引擎与配置
- [x] H. `proxy_url` 不接受 `socks5h://`；`string_list` 限 32 条；`platforms:` 空节判非法。与原版对齐。
- [x] I. `clean_cache --url` 按 sha256 key 精确匹配，永远命中 0 条。按 `Jobs.key` 派生候选键。
- [x] J. `clean_cache` 全量清理 `DELETE FROM worker_jobs`，删掉持久 sent 回执导致重复投递。不再删任务表。
- [x] K. `.cooldown.json` 损坏时 Worker 永久 `senderState=error`。损坏视为无冷却并记录。
- [x] L. preview 模式 `gif_only_skip_download_count_threshold=5`，纯 GIF >5 时返回无媒体"成功"。Worker 改为 0。

## 消息组装与发送
- [x] M. 实况降级不应在 FloodWait/SlowmodeWait 上触发；kurigram `file.id` AttributeError 亦未发出消息，可降级。
- [x] N. `LivePhotoFrame` 不带时长，降级视频块 duration=0。
- [x] O. inline 路径 `media_expired` 被记为 `delivery_limits`。
- [x] P. 测试、ruff、mypy、提交。

## 原版 Bot
- [x] Q. `services/media.py` 未注册 HEIF opener，Worker 入口 HEIC 走 ffmpeg 兜底得到 512x512 缩略图。
- [x] R. `MessageSender._send` 吞掉 Forbidden 后抛 RuntimeError，reporter 的 `on_forbidden` 永不触发。改 `SendFailed` 保留 `__cause__`。
- [x] S. DEBUG 日志原文输出带凭据的代理 URL（parser.py、core/platform_config.py）。新增 `mask_proxy`。
- [x] T. `ParseService.__init__` 每次重建 ParseHub。
- [x] U. `split_video` 段时长 ≤ keep_sec 时死循环。
- [x] V. 单文件 mime 未识别抛 ValueError 导致整篇媒体处理失败。改为原样透传。
- [ ] 未处理（低危/需产品决策）：预览模式实况只发视频不发静态图（sender.py:521）；多媒体降级 caption 位置；论坛匿名发言回落 Group 设置；多链接 set 去重乱序；`remux_to_mp4` 不检查返回码；`parser.py` 抹掉 ParseError 类型；inline `mediaCount` 语义；markdown footer 内嵌 HTML。
