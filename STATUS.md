# 阶段二及评审修复状态

## 已完成

- 建立 Python 3.11+、零第三方依赖的本地 Responses 路由服务。
- 实现 `/health`、`/v1/models`、`/v1/responses`，支持 SSE、chunked 与非流式组装。
- 实现 header/metadata 多来源线程识别、冲突 fail-open、父子线程识别。
- 实现四个授权决策时机；工具续跑和同轮复用保持线程钉住状态。
- 实现并真实验证压缩检查点 `sol@high` 与下一轮免费重选；Codex 0.155.1 以 `request_kind=compaction` 和结构化 `compaction` metadata 标记，真实提示词只作兜底。
- 实现 Jev 的 tier/depth 问题、step 分类、0.5 置信度门槛、4 秒超时、2 次指数退避重试和所有失败的 fail-open；4xx 除 429 外不重试，429 的 Retry-After 有上限，连续失败会临时熔断并记 `jev_circuit_open`。
- 实现切换成本预算与 20000 token 降级硬阈值；价格、预算和阈值可配置。
- 实现线程状态内存存储与原子 JSON 落盘、kill switch、shadow 文件哨兵、0600 JSONL 决策日志。
- 实现 Codex Router caller edge 与 ChatGPT Codex 直连两种上游；HTTPS 直连遵守环境代理。
- 完成子 agent 委托提取、独立 Jev 上下文和父子链日志；子线程不无条件继承父模型。
- 完成 per-thread 决策锁和默认 24 小时状态 TTL/保守 GC；状态过期时同步保守回收空闲锁。
- 完成 T2 原始上游流捕获和缺 completed 根因修复；客户端断开后继续排空上游，同任务 6 次、127 请求缺失率为 0。
- 完成 99 项离线单元测试，覆盖委托提取、子线程独立决策、同线程并发只决策一次、GC、真实压缩 fixture、Jev 重试/熔断、SSE 异常生命周期、原始流捕获、断开后排空和长父线程基准驱动。
- 修复评审 A1–A5、B1、B2、B5、B8、B9：包括已发 SSE 头后禁止二次响应、同模型 effort 成本、更可靠的 usage 兜底、非流式缺 completed 返回 502、key loader fail-open 与上游连接回收。详见 `docs/2026-09-20-评审修复.md`。
- 用真实 Jev key 完成 shadow 与真路由对照；`response.completed.model` 证明 luna/astra 实际切换，详见 `docs/2026-09-19-真实Jev端到端.md`。
- 增加可重复的 8 任务机械/推理基准驱动；48 个正式新会话全部通过，机械组费用降低 95.36%，全任务费用降低 44.75%，详见 `docs/2026-09-19-机械任务基准.md`。
- 增加可重复的 4 任务原生 fan-out 基准；24 个有效会话全部通过，已完成 SSE 的子 agent 费用降低 83.90%，父子合计降低 79.74%，详见 `docs/2026-09-20-子agent分档基准.md`。
- 完成 T1 长父线程 fan-out 的 27 个有效会话；子首请求未随 30k→150k 父上下文增长，`fork_turns=none` 无稳定费用优势，所测档位无 live_all/shadow 交叉点。
- 完成 Codex CLI 0.155.1 真实端到端：两个用户轮次、至少两次工具续跑和一个原生子 agent；无 Jev key 时决策点均正确记为 `no_key` 并走 `astra@medium`。
- 端到端实验后已恢复 `~/.codex/config.toml`，恢复文件与实验前备份的 SHA-256 一致。

## 本阶段未做

- 未实现 Codex-dry 备用梯队。
- 未实现旧路由器的调试抓包、用量汇总、launchd/watchdog 安装器和 UI reasoning summary 标记。
- `SummaryMarker` 仅保留了关闭的配置位，尚未向 SSE 插入可见路由标记。
- 真实 Jev 基准目前只覆盖 8 个单 agent 任务和 4 个 fan-out 任务、每格 3 次；还没有覆盖多仓库、多语言、长任务或人工质量评分。
- 未在本机安装 Codex Router，因此 caller edge 仅有协议/路径单测，真实端到端使用直连模式。

## 已知问题

- Codex 的私有 header 和 `x-codex-turn-metadata` 不是稳定公开协议；升级 Codex CLI 后应重跑线程标识与子 agent fixture 验证。
- Codex CLI 0.155.1 在子线程 `NEW_TASK` 和父线程 `spawn_agent` arguments 中都将具体委托 payload 加密；当前只能以 agent name 加父任务上下文分档，无语义名称会降低准确性。
- 历史 fan-out 基准中 live 的 18 条缺 `response.completed` 已定位为客户端断开后路由器过早关上游；修复后验证缺失率为 0，但历史报告的费用仍应按下界解读。
- T1 的 1,466 个有效请求中仍有 1 条未开 raw debug 时的 completed 缺失；已标记为费用下界，说明仍需保留缺失可观测性。
- 直连模式会把 Codex 登录态认证 header 转发给 `chatgpt.com`，仅适合本机回环开发；不得把监听地址改为外网接口。
- `/v1/models` 为兼容 Codex CLI 0.155.1 返回双结构；未来 CLI 目录协议改变时需要适配。
- 字符估算只是 usage 缺失时的兜底，中文/工具 schema 很大时误差可能显著。
- 状态文件仍在请求关键路径上同步全量重写（评审 B3），尚未做 dirty 合并刷盘。
- 状态文件版本不匹配或条目丢弃仍缺显式告警/迁移（B4）。
- 本机回环服务仍没有请求体上限、客户端 socket 超时和并发上限（B6），也没有可选本地鉴权（B11）。
- 上游 200 响应在缺 Content-Type 时仍以状态码作为 SSE 兜底，尚未完整实现首块嗅探（B7）；客户端 `accept-encoding` 也尚未显式剥离（B10）。

## 阶段二完成后仍需的工作

1. 扩大带人工质量标签的子任务样本，重点校准 0.5 置信度门槛；`luna@max` 的 effort 问题按约定留到本阶段之后处理。
2. 覆盖 fork、嵌套子 agent、`~/.codex/agents/` 自定义 agent 和路由器重启恢复，验证标识和委托缓存的稳定性。
3. 与 Codex 协议层协作，让边界获得可控、脱敏的明文委托摘要；在此之前保留 `agent_name_fallback` 的可观测警告。
4. 为 per-thread lock 增加长时运行压力测试，为状态落盘增加崩溃恢复验收。
5. 在安装了 Codex Router 的机器上做 caller edge 真实验收，验证 caller secret、shared session、目录刷新和服务托管。
6. 在更长时间的真实流量中继续监测 `upstream_error` 和真正的 `upstream_eof + end(false)`，防止上游协议变化。
