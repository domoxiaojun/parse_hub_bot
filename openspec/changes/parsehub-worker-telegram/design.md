# 设计

## 统一投递规则

一个解析结果是一项投递任务：preview 的普通聊天、Inline/Guest 统一由 Worker 组装一条 Rich Message；raw/zip 保留原文件 Document 语义。Rich 视频使用最终文件的真实宽高和时长，平台脚注与来源超链接进入页脚。媒体方向和格式只在共享预处理阶段处理。

共享 DeliveryEnvelope／MediaAsset／SendResult 定义业务输入输出；唯一 send_envelope 选择 preview Rich 或 raw/zip 原生传输。已确认批次不重发，未知结果不盲目补发。文字超过容量只保留摘要和来源／已有阅读链接，不自动发布或补发全文。旧 Bot 独立运行时复用相同核心。

Worker 使用 ParseHub 实际平台注册表，不设平台白名单；HTTP 协议版本为 3。gptbot 是唯一 update 接收者，Worker 通过独立 `no_updates` Kurigram 客户端完成 raw/zip Document 与 preview Rich 组装发送。两端使用同一 Bot Token，健康握手核验 Bot ID。

## 薄适配器边界

Worker 按原版 `handle_parse()` 的顺序复用 `ParseService.get_raw_url()`、`persistent_cache`、`parse_cache` 和 `ParsePipeline`。URL 归一化、Cookie/代理选择、解析重试、下载重试、媒体处理和目录命名都由原组件决定。Worker 不直接调用 `ParseResult.download()`，也不维护另一套转码器。

Worker 只把原 `CacheEntry` 或 `PipelineResult` 适配为 HTTP/发送描述，管理幂等、租约、路径所有权、消息规划与 Telegram 回执。直出 preview 可以直接使用原持久缓存中同 Bot 的 `file_id`；文件交接请求不使用该缓存，并与直出使用不同 Worker 缓存键。`refresh=true` 绕过原持久缓存和解析缓存。

## 结果和发送

preview 根据原 `ProcessedMedia` 生成最终资产并组装 Rich blocks；视频传递最终文件真实宽高和时长，来源页脚使用 `RichTextUrl`。图片在共享预处理层规范化 EXIF 方向；Live 视频检查 H.264/yuv420p、实际尺寸和时长，原生超限不裁切。raw 将原下载文件作为 document；zip 打包原目录与 metadata；read_only 不下载。

普通消息每个来源结果是一项 Rich 任务；Inline/Guest 在 Rich 单条限制内完成一次编辑，超限不丢媒体或补发。长正文摘要加来源或已有阅读链接，不自动发布阅读页。raw/zip 的文件发送保持原生 Document。直出查询清空 results 并过滤内部随机 ID，仅返回回执与有界证据；Telegram 引用仅进入内部缓存。

## 接口

Bearer 认证，默认 loopback 8080。提供 health、capabilities、GET/PUT config、POST/GET/DELETE jobs、媒体读取及 PUT/DELETE leases。JSON 使用 camelCase。任务字段为 text/accountId/mode/outputMode/refresh/requestId/idempotencyKey 和可选 delivery。

## 配置与存储

Worker 沿用原 `.env`、`DATA_PATH/config/platform_config.yaml`、`DATABASE_URL` 和 `DOWNLOAD_DIR`。gptbot 只配置连接；Admin 代理读写原 YAML，重启 Worker 后生效。Worker 启动时运行原数据库初始化，再在同一 SQLite 文件使用 `worker_` 前缀表；不安装新数据库。

基础 `compose.worker.yaml` 使用宿主 `./data`。原 Bot 使用默认 Compose 命名卷时，可通过 `compose.worker.shared-data.yaml` 显式挂载现有 `parse_hub_bot_data`，不静默迁移数据。

## 生命周期

固定文件缓存 TTL 为 172800 秒、默认容量 10 GiB。原流水线成功输出后登记目录，zip 归档在创建前登记；取消、启动恢复和 TTL 清理只处理已登记路径。单个 waiter 取消不影响共享准备；发送租约保护文件，Worker 重启将未完成任务标为 interrupted，无法确认的发送标为 unknown。

## 工作树边界

parse_hub_bot 的 Worker 改动在 `worker` 分支；gptbot 协议版本 3 和 Rich Message 支持已在其 `main` 分支。两仓分别提交和部署，保留其他聊天渠道行为。
