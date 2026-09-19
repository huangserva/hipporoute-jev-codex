# 阶段二：子 agent 分档设计

## 已验证的协议事实

Codex CLI 0.155.1 的真实 spawn 抓包否定了“边界层能读到委托正文”的假设。子线程首请求的 `agent_message` 明文只有 `Message Type: NEW_TASK`、完整 task name、sender 和空的 `Payload:`；实际 payload 是同一 content 数组里的 `encrypted_content`。父线程下一次请求中的 `function_call(name=spawn_agent)` 虽然明文暴露 `task_name` 和 `fork_turns`，但 `arguments.message` 已是 `gAAAAA…` 密文。进一步抓取上游 SSE 后，`response.output_item.done` 中的 message 从模型输出时就是同一密文；本地 rollout 也不保存明文参数。因此路由器不能解密或恢复委托正文。

当前实现的 `inspect_request()` 会从子线程继承输入中倒序取最后一个 user message，实际得到父任务或其他继承内容。这既不是子任务，也没有标注为回退来源，会误导 Jev。

## 方案比较

1. **推荐：明文优先、语义名称回退。** 支持从未来客户端的明文 `NEW_TASK Payload`、明文 spawn message 取委托；0.155.1 使用 `agent_name/task_name` 作为子任务摘要，再附带明确标注的父任务上下文、父档位和 subagent kind。优点是完全在边界层可实现、不会虚构可见性、兼容未来协议；缺点是分类质量依赖父 agent 使用有语义的 task name。
2. **读取/解密 encrypted_content。** 不采用。边界层没有密钥，猜测格式或读取进程内存都不可靠且越过安全边界。
3. **修改 Codex collaboration 协议或自造 agent。** 不采用。它能增加明文 routing hint，但违反“不造子 agent、不改 Codex/参考仓库”的范围。可在未来向 Codex 提议独立的非敏感 `routing_hint` 字段。

基准提示会明确要求使用语义化 task name；生产日志会记录 `delegation_source`，从而区分真正的委托明文与 `agent_name_fallback`。

## 路由数据流

请求身份增加 `agent_name` 和 `subagent_kind`。父请求和父响应都扫描 `spawn_agent` function call：如果 message 是明文，就按 `(parent_thread_id, canonical_agent_name)` 暂存；密文只记录 task name，不记录密文正文。子首请求的任务解析顺序是：子 `NEW_TASK` 明文 Payload → 父 spawn 明文缓存 → agent name + 父任务上下文回退。当前 CLI 将稳定走第三条。

子线程首请求仍以自己的 thread-id 建立独立状态并调用 Jev，绝不继承父模型。Jev state 包含 `task`、`delegation.is_subagent`、`parent_tier`、`agent_name`、`subagent_kind` 和 `source`。tier/depth 问题不改 choices，但子任务版 instructions 明确要求只判断被委托工作，父档位只是上下文而不是默认值。

子线程状态增加 `parent_tier`、`agent_name`、`subagent_kind`、`delegation_task`、`delegation_source`、`last_active_at`；保留 `parent_thread_id`。决策日志写出这些字段和实际用于 Jev 的 `routing_task`，因此父子链、选择依据和回退路径都可审计。

## 并发与生命周期

RouterEngine 使用 keyed `RLock`，对同一 thread-id 的身份解析之后到读状态、Jev 决策、写状态整个事务串行化；不同 thread-id 不互相阻塞。这样两个并发首请求只有一个调用 Jev，第二个看到已钉状态并按 reuse/sticky 返回。

状态用 `last_active_at` 记录最近请求或 usage 更新。配置新增 `state_ttl_seconds`（默认 86400）与 `state_gc_interval_seconds`（默认 300）。Engine 按间隔触发保守 GC：只有能解析时间且严格早于截止点的状态才删除；旧格式缺少 `last_active_at` 时回退 `decided_at`；无效时间保留。GC 删除后原子落盘。内存 delegation cache 同样按 TTL 清理；未知或仍活跃状态不冒险删除。

## 测试和基准

单测先覆盖：真实脱敏 fixture 的明文/密文委托提取；子 agent 使用独立 Jev 结果而非父模型；同线程并发首请求只调用一次 Jev；TTL 对过期、活跃和无效时间的行为；Jev 子任务 instructions；日志父子字段。

基准使用 4 个 fan-out 任务，每个父会话明确要求并行 spawn 2–3 个语义化命名的子 agent。shadow/live 各 3 遍，共 24 个全新会话。每个样本按父/子 thread 拆出真实模型、请求数、token、费用、Jev choice/confidence、通过和墙钟；缺 completed 单列。所有编辑发生在隔离副本，基础设施失败最多重试 2 次，任务失败不重跑。

