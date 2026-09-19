## ADDED Requirements

### Requirement: 同 Bot 全平台准备接口
系统 SHALL 以与 gptbot 相同的 Bot 身份提供 ParseHub 全平台注册能力，不监听消息。

#### Scenario: 普通与 inline 调用
- **WHEN** 匹配账号请求公开分享内容
- **THEN** Worker 复用原解析流水线，按目标能力完成原生发送或 Rich 发送／编辑，gptbot 消费回执
- **AND** LLM 终稿不覆盖已交付媒体

#### Scenario: Preview Rich 内容完整性
- **WHEN** `outputMode=preview` 且结果包含标题、媒体和来源
- **THEN** 普通聊天、Guest 和 inline 均由 Worker 组装 Rich Message
- **AND** 视频块携带最终文件的真实宽度、高度和时长
- **AND** 页脚保留平台名称，并将“查看原文”作为来源超链接

#### Scenario: Live Photo 与多结果组装
- **WHEN** 解析结果包含 Live Photo、其他媒体或多个链接结果
- **THEN** Worker 使用统一投递信封，preview 普通聊天、Guest 与 inline 组装 Rich Message
- **AND** raw/zip 才将原文件或归档作为 Document 发送
- **AND** Rich 超出能力限制时受控失败，不自动改变媒体语义

#### Scenario: 配对缓存与发送确认
- **WHEN** Live 引用缺失、过期或发送结果未知
- **THEN** 系统按资产成对恢复引用，不能使用其他资产的静图或视频
- **AND** 已确认批次记录全部 message ID 和 grouped ID，不重发已确认批次
- **AND** 结果不明确时记录 unknown，文件缓存过期不删除持久投递回执

### Requirement: 原版解析流水线唯一来源
系统 SHALL 复用原 URL 归一化、持久缓存、解析缓存、ParseService、ParsePipeline、下载重试和媒体处理，不在 Worker 中重复实现。

#### Scenario: 原持久缓存命中
- **WHEN** 直出 preview 请求命中原 Bot 的同账号 file_id 缓存
- **THEN** Worker 不重新解析、下载或转码，直接用缓存媒体组装并发送

#### Scenario: 缓存未命中或强制刷新
- **WHEN** 缓存未命中
- **THEN** 原 ParsePipeline 决定下载和媒体处理结果
- **AND** `refresh=true` 同时绕过原持久缓存与解析缓存

### Requirement: 模式与资源生命周期
系统 SHALL 支持 preview/raw/zip/read_only、批量和强制刷新，并固定缓存 48 小时。

#### Scenario: 并发取消与过期
- **WHEN** 一个等待者取消或缓存过期
- **THEN** 其他等待者不受影响，发送租约释放后才删除文件

### Requirement: 动态配置和故障隔离
系统 SHALL 通过 Admin 管理 Worker 直接读取的原平台 YAML，保存后由重启加载。

#### Scenario: 交付失败可定位
- **WHEN** 媒体准备、上传或 Rich 发送失败
- **THEN** Worker 日志记录阶段、批次、媒体序号、媒体类型、大小、表示方式、异常类型和受控 RPC 标识
- **AND** 日志不记录来源 URL、本地路径、文件名、正文、凭据或异常原文

#### Scenario: Worker 不可用
- **WHEN** Worker 身份不符或暂不可用
- **THEN** 返回受控错误且不隐式回退，普通聊天继续可用
