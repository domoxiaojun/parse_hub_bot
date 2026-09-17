# Worker 原版解析与请求对齐

- [x] ✅ 1. 对照 ParseHub 原库、ParseService、ParsePipeline 和平台配置，核实请求改写来源。
- [x] ✅ 2. 删除 HTTP/socket/yt-dlp 全局补丁，恢复解析器自己的认证、重定向、下载后端和响应处理。
- [x] ✅ 3. 对齐 Cookie/代理随机选择、三次解析与链接规范化尝试、三次下载及每次下载超时。
- [x] ✅ 4. 去掉额外可见性阻断和 URL 路径猜测，保留原分享参数、文件路径保护及脱敏错误。
- [x] ✅ 5. 更新缓存版本与说明，补全 Worker 回归验证并完成最小必要验证。

边界：修改 Worker 适配层，不修改 ParseHub 库或平台解析算法，不改用户现有配置、数据库和 session，不部署、不提交。HTTP 服务鉴权、任务幂等、缓存/租约、取消、文件边界与 Telegram 交付继续保留。

核实偏差：匿名优先导致内置 Authorization 被删；只选第一个 Cookie/代理；错误时不按原版重试；JSON 响应强制 8 MiB 上限；socket 与 httpx 全局 monkey patch；yt-dlp 被强制 urllib/native 下载并禁止原生外部下载器；凭据解析成功仍可能被 visibility_unknown 拦截；下载只尝试一次且整轮 30 分钟会截断原版逐次下载重试。

## 验证结果

- `uv run pytest test/test_worker_engine.py test/test_worker_delivery.py test/test_worker_service.py`：75 项通过，3 项旧 Worker 网络拦截测试跳过。
- 修改范围 Ruff、mypy、Python 编译检查和 `git diff --check` 通过。
- 未执行真实 Twitter/ParseHub 请求、Telegram 发送、生产部署或 git 提交。
