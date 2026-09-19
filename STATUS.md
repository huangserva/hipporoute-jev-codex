# 阶段一状态

## 已完成

- 建立 Python 3.11+、零第三方依赖的本地 Responses 路由服务。
- 实现 `/health`、`/v1/models`、`/v1/responses`，支持 SSE、chunked 与非流式组装。
- 实现 header/metadata 多来源线程识别、冲突 fail-open、父子线程识别。
- 实现四个授权决策时机；工具续跑和同轮复用保持线程钉住状态。
- 实现压缩检查点 `sol@high` 与下一轮免费重选。
- 实现 Jev 的 tier/depth 问题、step 分类、0.5 置信度门槛、4 秒超时、2 次指数退避重试和所有失败的 fail-open。
- 实现切换成本预算与 20000 token 降级硬阈值；价格、预算和阈值可配置。
- 实现线程状态内存存储与原子 JSON 落盘、kill switch、shadow 文件哨兵、0600 JSONL 决策日志。
- 实现 Codex Router caller edge 与 ChatGPT Codex 直连两种上游；HTTPS 直连遵守环境代理。
- 完成 44 项离线单元测试。
- 完成 Codex CLI 0.155.1 真实端到端：两个用户轮次、至少两次工具续跑和一个原生子 agent；无 Jev key 时决策点均正确记为 `no_key` 并走 `astra@medium`。
- 端到端实验后已恢复 `~/.codex/config.toml`，恢复文件与实验前备份的 SHA-256 一致。

## 本阶段未做

- 未实现 Codex-dry 备用梯队。
- 未实现旧路由器的调试抓包、用量汇总、launchd/watchdog 安装器和 UI reasoning summary 标记。
- `SummaryMarker` 仅保留了关闭的配置位，尚未向 SSE 插入可见路由标记。
- 未在真实 Jev key 下验证分类质量；单测使用假 Jev，真实验证覆盖的是 `no_key` fail-open 路径。
- 未在本机安装 Codex Router，因此 caller edge 仅有协议/路径单测，真实端到端使用直连模式。

## 已知问题

- Codex 的私有 header 和 `x-codex-turn-metadata` 不是稳定公开协议；升级 Codex CLI 后应重跑线程标识与子 agent fixture 验证。
- 同一线程如果并发到达两个请求，当前存储写入本身是线程安全和原子的，但“读状态—决策—写状态”不是每线程事务；正常 Codex 串行续跑不受影响，阶段二应加入 per-thread lock。
- 内存状态不会自动淘汰；长期运行需要按最后活动时间做保守 GC。
- 直连模式会把 Codex 登录态认证 header 转发给 `chatgpt.com`，仅适合本机回环开发；不得把监听地址改为外网接口。
- `/v1/models` 为兼容 Codex CLI 0.155.1 返回双结构；未来 CLI 目录协议改变时需要适配。
- 字符估算只是 usage 缺失时的兜底，中文/工具 schema 很大时误差可能显著。

## 阶段二：子 agent 分档所需工作

1. 用真实 Jev key 对主线程和多类子 agent 任务建立带人工标签的评测集，校准 tier/depth 问题、0.5 门槛及各档 effort。
2. 明确 Codex 原生子 agent 与 `~/.codex/agents/` 自定义 agent 的稳定标识，至少覆盖 collab spawn、fork、嵌套子 agent 和重启恢复。
3. 为子 agent 增加专属特征：父线程已钉档位、委托文本、agent role/name、只读/写入权限、预期 fan-out；不要把父线程模型无条件继承给子线程。
4. 加 per-thread 锁、状态 TTL/GC、并发压力测试和崩溃恢复测试。
5. 在安装了 Codex Router 的机器上做 caller edge 真实验收，验证 caller secret、shared session、目录刷新和服务托管。
6. 在真实流量的 shadow 模式比较 `would` 与现有路由的质量、成本和切换率，确认阈值后再启用实际分档。

