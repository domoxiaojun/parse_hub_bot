## ADDED Requirements

### Requirement: 同 Bot 全平台准备接口
系统 SHALL 以与 gptbot 相同的 Bot 身份提供 ParseHub 全平台注册能力，不监听消息。

#### Scenario: 普通与 inline 调用
- **WHEN** 匹配账号请求公开分享内容
- **THEN** Worker 准备并注册媒体，gptbot 使用 file_id Rich 发送或编辑
- **AND** LLM 终稿不覆盖已交付媒体

### Requirement: 模式与资源生命周期
系统 SHALL 支持 preview/raw/zip/read_only、批量和强制刷新，并固定缓存 48 小时。

#### Scenario: 并发取消与过期
- **WHEN** 一个等待者取消或缓存过期
- **THEN** 其他等待者不受影响，发送租约释放后才删除文件

### Requirement: 动态配置和故障隔离
系统 SHALL 通过 Admin 管理实际注册平台配置并将当前生效快照同步到 Worker。

#### Scenario: Worker 不可用
- **WHEN** Worker 身份不符或暂不可用
- **THEN** 返回受控错误且不隐式回退，普通聊天继续可用
