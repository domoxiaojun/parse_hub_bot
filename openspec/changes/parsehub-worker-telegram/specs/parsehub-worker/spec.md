## ADDED Requirements

### Requirement: 同 Bot 全平台准备接口
系统 SHALL 以与 gptbot 相同的 Bot 身份提供 ParseHub 全平台注册能力，不监听消息。

#### Scenario: 普通与 inline 调用
- **WHEN** 匹配账号请求公开分享内容
- **THEN** Worker 复用原解析流水线并完成 Rich 发送或编辑，gptbot 消费回执
- **AND** LLM 终稿不覆盖已交付媒体

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

#### Scenario: Worker 不可用
- **WHEN** Worker 身份不符或暂不可用
- **THEN** 返回受控错误且不隐式回退，普通聊天继续可用
