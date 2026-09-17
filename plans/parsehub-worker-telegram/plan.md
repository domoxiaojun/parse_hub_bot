# ParseHub Worker Telegram 实施

同一 BOT_TOKEN；Worker 不监听消息，gptbot 最终 Rich 交付。全部平台注册能力，保留其他渠道。

- [x] ✅ 方案、契约及混合工作树边界
- [x] ✅ Worker HTTP/任务/配置/缓存/租约
- [x] ✅ ParseHub 全平台及媒体上传准备
- [x] ✅ Telegram 普通/Guest/inline 集成
- [x] ✅ Admin 动态平台管理与配置同步
- [x] ✅ 测试、构建和部署说明

不执行真实平台凭据请求、Telegram 发送、部署、重启、Docker 或数据库服务器安装。

用户追加：彻底删除 gptbot 移植七平台实现，不保留其他渠道旧解析或自动回退；通用媒体转换和 Rich 渲染保留。

## 验证记录（2026-09-16）

- ✅ gptbot 当前工作树定向回归34文件341项；Admin动态平台/请求头粘贴/草稿保护3项通过。最初误扫描两个 .claude/worktrees 副本，在并发构建时3项超时；限定 --exclude '.claude/**' --maxWorkers=4 后全部通过。
- ✅ Worker最终33项通过，包含真实本地FFmpeg样本、全平台分派、模式、封面、无消息UploadMedia、HTTP安全、yt-dlp限制、48小时缓存、取消、租约、幂等与重启恢复。
- ✅ 实际跨进程HTTP联调：真实TS客户端→aiohttp+Jobs+Store，外部平台与Telegram替身；身份/配置/两项批量/file_id封面/二次缓存/4独立租约释放/read_only均通过，无真实消息。
- ✅ TypeScript、受影响文件只读ESLint、Worker mypy与ruff、两仓diff检查通过。
- ✅ 本地server/Admin构建、runtime dependencies（74 chunks/35 roots）、Admin预算（initial gzip279096 <307200，max chunk74164 <102400）通过。
- ✅ 两仓OpenSpec strict、Taplo配置检查及gptbot文档离线链接检查通过。
- 全仓门禁未全绿：原 guest_management.ts 893>880 行架构例外；原 scripts/check-runtime-deps.ts、storage/task_handlers.ts lint，以及本轮开始前已有 outbound_proxy/safe_fetch 未提交草稿lint仍存在。没有将其掩盖成全部通过。
- 脱敏扫描：Worker新增源码无泄漏；gptbot完整差异检测到2项已删除 bilibili_wbi.test.ts 的公开签名测试常量，新增内容单独复核。
- 当前配套源码基线：gptbot 67ccfb1、parse_hub_bot b4da525；本轮工作树改动尚未提交或推送。协议版本1，不代表已部署。
- 本轮没有真实平台请求、真实Telegram上传发送、部署或重启；真实大文件吞吐及各平台Cookie可用性尚未验收。公开性不明确时失败关闭。
- ✅ 首次短链与正式URL在匿名展开后共享内容准备；保留Bilibili分P，小红书xsec_token仅移出身份键、保留原请求。缓存命中不请求源站。
- ✅ 新增内容脱敏扫描无泄漏；旧差异中的两项命中均为已删除的Bilibili测试常量。
- Admin保留旧Cookie键作为兼容配置，旧解析reader/签名/下载缓存/准备目录已完全删除。

## Compose 源码构建交付

用户追加：只准备部署机器源码构建Compose，本机不得构建或运行Docker。

- [x] ✅ 核对原交互入口未改及Worker配置来源
- [x] ✅ 独立Worker Compose、凭据示例、数据挂载、容器绑定与健康检查
- [x] ✅ gptbot共享网络覆盖文件及两种部署说明
- [x] ✅ 静态配置与定向测试验证：3项pytest、ruff、mypy、YAML网络拓扑断言、OpenSpec与文档离线链接通过；没有运行任何Docker命令或镜像构建。

## 用户追加：沿用原配置与存储路径

- [x] ✅ 原.env、data/config/platform_config.yaml、DATABASE_URL、downloads与bot_ID.session复用
- [x] ✅ Admin远程读取/编辑原YAML，移除gptbot平台快照覆盖
- [x] ✅ 原数据库表与历史下载保留、Compose路径与定向验证

此决定覆盖此前独立.env.worker、data/worker及gptbot配置同步设计。不得运行Docker或在本机构建。

## Telegraph 模块归属修正

- [x] ✅ 原聊天TelegraphSender未迁移；将旧社交阅读页发布/正文转换/降级工具从parsehub_worker抽到gptbot src/utils/telegraph共享层，Worker仅调用。
- [x] ✅ 共享发布及聊天长文发送回归与类型检查

原路径调整验证：Python42项、mypy14模块、ruff通过；共享Telegraph/Worker客户端/Admin/旧聊天发送回归8文件36项与TypeScript通过。Admin跨进程验证读取0写入、显式编辑YAML、CAS409、重启Worker生效已通过。没有本机构建、Docker执行、部署或真实发送。

## 原项目目录和文件名对齐

- [x] ✅ 原ParseHub.download命名、原processed布局及同名tar.gz打包
- [x] ✅ 缓存登记实际目录/归档，取消及过期只清理已登记路径，保留历史下载
- [x] ✅ 原命名碰撞/并发/租约/原数据库回归及文档

当前工作树已改为Worker返回本地媒体下载引用，保留该最新架构。本轮不修改Telegram职责，不构建、不运行Docker、不部署。

目录修正验证：52项测试通过（原生命名、_2碰撞、LivePhoto、同名归档、实际FFmpeg、取消、目录登记、历史文件保护和租约），mypy14模块、只读ruff、OpenSpec和文档链接通过。复用原库整批下载失败行为，不保留逐项改名下载；转换阶段仍逐项降级。未构建、未运行Docker、未部署、未提交。
