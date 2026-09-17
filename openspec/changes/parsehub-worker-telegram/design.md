# 设计

Worker 使用 ParseHub 实际平台注册表，不设七平台白名单；接口版本 1。gptbot 是唯一 update 接收者及最终 Rich sender，Worker 仅解析/下载/处理和 MTProto UploadMedia。两者必须使用同一 Bot Token，健康握手核验 Bot ID。

## 接口
Bearer 认证，默认 loopback 8080。health、capabilities、PUT config、POST/GET/DELETE jobs、PUT/DELETE leases。JSON 使用 camelCase；平台快照代理字段采用 parser_proxies/downloader_proxies 数组，空数组表示直连。
任务字段 text/accountId/mode/outputMode/refresh/requestId/idempotencyKey。results 每项为结构化正文与媒体 fileId/botId 或稳定 error。

## 配置与生命周期
Admin 为唯一凭据配置源，保存重启后生效。按版本同步到 Worker；不输出凭据和原始上游诊断。固定 TTL 172800 秒、10 GiB、本地 SQLite 索引；单次 waiter 取消隔离，发送期间租约保护，Worker 重启中断未完成任务。

## 工作树边界
gptbot 原有 manage-parsehub-platforms 未提交配置/代理代码保留并按需扩展；parse_hub_bot 初始干净。保持非 Telegram 和其他账号行为。

## 用户追加边界
删除 gptbot 原七平台 reader、签名、社交本地下载缓存及交付准备模块，不保留旧后端。非目标 Telegram 账号及微信/企业微信社交解析明确暂不支持，其他聊天工具保持原行为。旧 Cookie/代理键仅作为 Worker 配置兼容输入。

## Compose部署补充
Worker采用独立compose.worker.yaml复用原Dockerfile，覆盖命令python -m worker；原交互Bot入口不变。默认loopback，容器仅显式WORKER_ALLOW_CONTAINER_BIND=true时允许0.0.0.0，并仅向宿主127.0.0.1发布端口。gptbot容器通过共享网络访问parsehub-worker:8080。平台配置仍由gptbot Admin单向同步，原data/config/platform_config.yaml不迁移也不被Worker读取。

## 最终配置归属（用户修正，覆盖前文）
Worker沿用原.env、DATA_PATH/config/platform_config.yaml、DATABASE_URL、DOWNLOAD_DIR与DATA_PATH/sessions/bot_ID.session。gptbot仅配置连接，不推送平台快照；Admin代理GET/PUT原YAML并在Worker重启后生效。Worker表用worker_前缀共用原SQLite文件，保留所有原表；只清理downloads中自身worker-前缀目录。不允许与原交互Bot同时占用session。

## 原项目文件布局

Worker不再生成worker-任务目录、original层、media-001标题或media.tar.gz。原ParseResult.download(DOWNLOAD_DIR)自行选择标题目录和冲突后缀，文件名沿用原provider；processed和归档名称复用原组件。持久化登记新建路径的所有权，发布后关联缓存项；取消、启动恢复和TTL清理只能删除已登记路径，不扫描前缀清理历史文件。
