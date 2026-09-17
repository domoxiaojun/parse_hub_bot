# 设计

Worker 使用 ParseHub 实际平台注册表，不设平台白名单；HTTP 协议版本为 3。gptbot 是唯一 update 接收者，Worker 通过独立 `no_updates` 客户端完成最终 Rich Message 组装与发送。两端使用同一 Bot Token，健康握手核验 Bot ID。

## 薄适配器边界

Worker 按原版 `handle_parse()` 的顺序复用 `ParseService.get_raw_url()`、`persistent_cache`、`parse_cache` 和 `ParsePipeline`。URL 归一化、Cookie/代理选择、解析重试、下载重试、媒体处理和目录命名都由原组件决定。Worker 不直接调用 `ParseResult.download()`，也不维护另一套转码器。

Worker 只把原 `CacheEntry` 或 `PipelineResult` 适配为 HTTP/发送描述，管理幂等、租约、路径所有权、消息规划与 Telegram 回执。直出 preview 可以直接使用原持久缓存中同 Bot 的 `file_id`；文件交接请求不使用该缓存，并与直出使用不同 Worker 缓存键。`refresh=true` 绕过原持久缓存和解析缓存。

## 结果和发送

preview 根据原 `ProcessedMedia` 映射图片、视频和动画；Live Photo 在 10 秒及 10 MiB 范围内作为一个原生实况消息，其他情况显式拆为图片和视频。raw 将原下载文件作为 document，Live Photo 保留静态图和视频；zip 打包原流水线生成的目录和 metadata；read_only 不下载。

普通消息由 Worker 发送，Guest/inline 使用单次 Rich Message 编辑并在限制内降级 Live Photo。直出 HTTP 查询清空内部 results，只返回交付回执与有界证据；`file_id` 不进入 gptbot 的文件交接契约。

## 接口

Bearer 认证，默认 loopback 8080。提供 health、capabilities、GET/PUT config、POST/GET/DELETE jobs、媒体读取及 PUT/DELETE leases。JSON 使用 camelCase。任务字段为 text/accountId/mode/outputMode/refresh/requestId/idempotencyKey 和可选 delivery。

## 配置与存储

Worker 沿用原 `.env`、`DATA_PATH/config/platform_config.yaml`、`DATABASE_URL` 和 `DOWNLOAD_DIR`。gptbot 只配置连接；Admin 代理读写原 YAML，重启 Worker 后生效。Worker 启动时运行原数据库初始化，再在同一 SQLite 文件使用 `worker_` 前缀表；不安装新数据库。

基础 `compose.worker.yaml` 使用宿主 `./data`。原 Bot 使用默认 Compose 命名卷时，可通过 `compose.worker.shared-data.yaml` 显式挂载现有 `parse_hub_bot_data`，不静默迁移数据。

## 生命周期

固定文件缓存 TTL 为 172800 秒、默认容量 10 GiB。原流水线成功输出后登记目录，zip 归档在创建前登记；取消、启动恢复和 TTL 清理只处理已登记路径。单个 waiter 取消不影响共享准备；发送租约保护文件，Worker 重启将未完成任务标为 interrupted，无法确认的发送标为 unknown。

## 工作树边界

parse_hub_bot 的 Worker 改动在 `worker` 分支；gptbot 协议版本 3 和 Rich Message 支持已在其 `main` 分支。两仓分别提交和部署，保留其他聊天渠道行为。
