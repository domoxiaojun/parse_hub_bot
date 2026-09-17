# Worker 登录恢复

- [x] ✅ 确认内存会话与登录前HTTP未监听导致重启循环。
- [x] ✅ 独立持久session和可跨重启保留的FloodWait截止时间。
- [x] ✅ 健康接口先监听，后台登录并暴露deliveryReady/senderState/retryAfterSeconds。
- [x] ✅ 定向38项pytest、7个源码mypy、修改范围Ruff通过。

验证包含真实SDK本地session保存/重读、原会话不变、2753秒FloodWait和重启继续等待、健康接口继续可用、拒绝新交付且保留旧回执。没有连接真实Telegram或操作服务器。Worker源码本地保留，配套修复包用于手动同步。
