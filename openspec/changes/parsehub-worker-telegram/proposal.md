# Change: ParseHub 全平台 Worker 与 Telegram Rich 接入

## Why
七平台 TypeScript 移植与上游分叉，改为复用 ParseHub 全量解析及媒体处理。

## What Changes
- parse_hub_bot 提供无消息监听 HTTP Worker，并使用同一 BOT_TOKEN 完成最终 Rich Message 发送。
- Worker 直接复用原 URL 归一化、持久缓存、解析缓存、ParseService、ParsePipeline、下载与媒体处理。
- 支持 preview/raw/zip/read_only、批量、刷新、48 小时文件缓存与租约。
- gptbot 负责调用、交互和回执消费，普通消息与 Guest/inline 均支持 Rich 交付。

## Impact
两仓独立部署，先接管一个 Telegram 账号；保留其他渠道实现。无真实发送或本机部署。

## 用户追加边界
删除 gptbot 原七平台 reader、签名、社交本地下载缓存及交付准备模块，不保留旧后端。非目标 Telegram 账号及微信/企业微信社交解析明确暂不支持，其他聊天工具保持原行为。旧 Cookie/代理键仅作为 Worker 配置兼容输入。
