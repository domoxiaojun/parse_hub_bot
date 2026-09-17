# Change: ParseHub 全平台 Worker 与 Telegram Rich 接入

## Why
七平台 TypeScript 移植与上游分叉，改为复用 ParseHub 全量解析及媒体处理。

## What Changes
- parse_hub_bot 新增无消息监听 HTTP Worker，同一 BOT_TOKEN 上传注册。
- 全平台注册表、preview/raw/zip/read_only、批量、刷新、48 小时缓存与租约。
- gptbot 动态配置及 Rich 文件引用交付，普通和 Guest/inline 保留交互。

## Impact
两仓独立部署，先接管一个 Telegram 账号；保留其他渠道旧实现。无真实发送、部署或原生依赖安装。

## 用户追加边界
删除 gptbot 原七平台 reader、签名、社交本地下载缓存及交付准备模块，不保留旧后端。非目标 Telegram 账号及微信/企业微信社交解析明确暂不支持，其他聊天工具保持原行为。旧 Cookie/代理键仅作为 Worker 配置兼容输入。
