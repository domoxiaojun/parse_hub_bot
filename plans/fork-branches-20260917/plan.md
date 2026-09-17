# Fork 分支与上游同步

- [x] ✅ 1. 核实 origin 为 domoxiaojun/parse_hub_bot、upstream 为原作者，获取最新远程状态。
- [x] ✅ 2. 创建 worker 分支并保留当前 Worker 和消息显示定制。
- [x] ✅ 3. 提交定制，合并 upstream/main 并完成必要验证。
- [ ] 4. main 仅快进至上游版本，将两个分支推送到自己的 Fork。
- [ ] 5. Fork 默认分支设为 worker，核对默认分支、远程 SHA 和工作区状态。

分支约定：worker 是定制版默认分支；main 是原作者 upstream/main 的同步分支。合并更新，不强制推送，不向原作者推送，不创建 Release，不部署。

验证：合并上游 60a8da5；锁文件同步后 ParseHub 为 2.2.3。Worker 测试 99 通过、3 个既有跳过；Ruff、mypy、差异检查和暂存区脱敏凭据扫描通过。
