# T5 常驻 shadow 状态

截至 2026-09-20，`com.jev.codex-jev-router` 已作为当前用户 LaunchAgent 运行，Codex CLI 与 ChatGPT.app 包内 Codex 均真实经过本路由器；`~/.codex/config.toml` 有校验备份并长期保持本地 provider，shadow soak 已开始。GUI 宿主仍需用户完全退出并重开 ChatGPT 后发一条任务完成最后确认。

## 当前默认参数

| 项目 | 默认值 | 说明 |
|---|---|---|
| Luna | `gpt-5.6-luna@low`，`service_tier=priority` | 机械任务与纯协调 fan-out；可把 `[routing] luna_effort` 改回 `medium/max` |
| Sol | `gpt-5.6-sol`，effort 取 Jev depth | 标准实现；低置信度回退档 |
| Astra | `gpt-6-astra`，effort 取 Jev depth | 困难/模糊/高风险；失败时 fail-open 为 `medium` |
| Jev 置信度门槛 | `0.5` | T4 证据不足以整体降到 0.35，保持不变 |
| 降级上下文硬阈值 | `20000` token | 超过后拒绝降档 |
| 切换预算 | `$0.25` | `cache_write - stay cache_read` 超预算则 hold |
| 状态持久化 | 每 `2s` 合并刷新 | TTL 24h，GC 300s，退出强制 flush |
| HTTP 资源边界 | 16 MiB / 30s / 64 并发 | 依次为请求体、客户端 socket、POST 并发上限 |

## 已验证结论一览

| 验证 | 结论 | 已落地决定 |
|---|---|---|
| T1 长父线程 | 子首请求约 22k，不随 31k/75k/151k 父上下文增长，均省 96%–97% | 保留 `fork_turns=all` |
| T2 completed | 客户端断开后停读上游是主因；修后 127 请求缺失归零 | 下游断开仍排空上游 |
| T3 Luna effort | low 比 max 步数少 42%、墙钟少 45%、费用少 40%，通过率不变 | 默认改为 `luna@low` |
| T4 门槛 | 整体降到 0.35 证据不足；s2 纯协调父线程系统性低置信 | 门槛保持 0.5，协调任务单独提示 |
| s2 复验 | 父线程 Luna 置信度由 0.26–0.31 升至 0.98–0.99，3/3 通过 | 保留 instructions + `coordination_hint` 双层信号 |

## 已完成

- 建立 Python 3.11+、零第三方依赖的本地 Responses 路由服务。
- 实现 `/health`、`/v1/models`、`/v1/responses`，支持 SSE、chunked 与非流式组装。
- 实现 header/metadata 多来源线程识别、冲突 fail-open、父子线程识别。
- 实现四个授权决策时机；工具续跑和同轮复用保持线程钉住状态。
- 实现并真实验证压缩检查点 `sol@high` 与下一轮免费重选；Codex 0.155.1 以 `request_kind=compaction` 和结构化 `compaction` metadata 标记，真实提示词只作兜底。
- 实现 Jev 的 tier/depth 问题、step 分类、0.5 置信度门槛、4 秒超时、2 次指数退避重试和所有失败的 fail-open；4xx 除 429 外不重试，429 的 Retry-After 有上限，连续失败会临时熔断并记 `jev_circuit_open`。
- 实现切换成本预算与 20000 token 降级硬阈值；价格、预算和阈值可配置。
- 实现线程状态内存存储、脏标记与后台合并原子 JSON 落盘、退出强刷、kill switch、shadow 文件哨兵、0600 JSONL 决策日志。
- 实现 Codex Router caller edge 与 ChatGPT Codex 直连两种上游；HTTPS 直连遵守环境代理。
- 完成子 agent 委托提取、独立 Jev 上下文和父子链日志；子线程不无条件继承父模型。
- 完成 per-thread 决策锁和默认 24 小时状态 TTL/保守 GC；状态过期时同步保守回收空闲锁。
- 完成 T2 原始上游流捕获和缺 completed 根因修复；客户端断开后继续排空上游，同任务 6 次、127 请求缺失率为 0。
- 完成 131 项离线单元测试，覆盖委托提取、协调 hint、子线程独立决策、同线程并发只决策一次、GC、后台状态刷新、HTTP 资源边界、跨块上游首块嗅探、真实压缩 fixture、Jev 重试/熔断、SSE 异常生命周期、原始流捕获、断开后排空、长父线程、调参基准与 T5 配置/服务/周报工具。
- 修复评审 A1–A5、B1、B2、B5、B8、B9：包括已发 SSE 头后禁止二次响应、同模型 effort 成本、更可靠的 usage 兜底、非流式缺 completed 返回 502、key loader fail-open 与上游连接回收。详见 `docs/2026-09-20-评审修复.md`。
- 修复常驻相关 B3、B6、B7：状态后台合并刷新；请求体/socket/并发上限；200 无 Content-Type 的响应按首块区分 SSE 与 JSON。
- 用真实 Jev key 完成 shadow 与真路由对照；`response.completed.model` 证明 luna/astra 实际切换，详见 `docs/2026-09-19-真实Jev端到端.md`。
- 增加可重复的 8 任务机械/推理基准驱动；48 个正式新会话全部通过，机械组费用降低 95.36%，全任务费用降低 44.75%，详见 `docs/2026-09-19-机械任务基准.md`。
- 增加可重复的 4 任务原生 fan-out 基准；24 个有效会话全部通过，已完成 SSE 的子 agent 费用降低 83.90%，父子合计降低 79.74%，详见 `docs/2026-09-20-子agent分档基准.md`。
- 完成 T1 长父线程 fan-out 的 27 个有效会话；子首请求未随 30k→150k 父上下文增长，`fork_turns=none` 无稳定费用优势，所测档位无 live_all/shadow 交叉点。
- 完成 T3 Luna effort 基准：54 个机械任务有效会话全部通过；`low` 相对 `max` 将请求数中位数降低 41.7%、墙钟降低 44.6%、费用中位数降低 40.3%，现已改为默认值。
- 完成 T4 Jev 门槛基准：0.35/0.5 各 12 个 fan-out 会话全部通过；0.35 总费用低 8.46%，但目标置信度带样本只有 3 个，尚不足以直接修改生产默认值。
- 协调任务修正后重跑 s2 live 3 遍：父线程 raw Luna 置信度为 0.99/0.98/0.99，全部实际由 `luna@low` 服务且 3/3 通过；父线程费用中位数相对 T4 降低 96.5%。
- 完成 Codex CLI 0.155.1 真实端到端：两个用户轮次、至少两次工具续跑和一个原生子 agent；无 Jev key 时决策点均正确记为 `no_key` 并走 `astra@medium`。
- 完成 T5 launchd 安装：`RunAtLoad + KeepAlive`、显式 7897 HTTPS 代理、health/key 门禁、幂等 enable/disable、哈希保护的一键还原与 watchdog 回退。
- 完成 T5 CLI 实流量：系统 Codex 0.155.1 和 ChatGPT.app 内置 Codex 0.155.0-alpha.9.2 都识别 `provider=codex-jev-router`，日志 `gate=apply`、`shadow=true`，上游 completed model 为 astra。
- 增加按日 shadow 周报：根会话/线程、决策、would/置信度、低置信回退、Jev 错误/延迟、固定轨迹费用估算和 usage 缺失均可审计。

## 本阶段未做

- 未实现 Codex-dry 备用梯队。
- 未实现 UI reasoning summary 标记。
- `SummaryMarker` 仅保留了关闭的配置位，尚未向 SSE 插入可见路由标记。
- 真实 Jev 基准目前只覆盖 8 个单 agent 任务和 4 个 fan-out 任务、每格 3 次；T3/T4 虽增加了参数重复实验，仍没有覆盖多仓库、多语言、长任务或人工质量评分。
- 未在本机安装 Codex Router，因此 caller edge 仅有协议/路径单测，真实端到端使用直连模式。

## 已知问题

- Codex 的私有 header 和 `x-codex-turn-metadata` 不是稳定公开协议；升级 Codex CLI 后应重跑线程标识与子 agent fixture 验证。
- Codex CLI 0.155.1 在子线程 `NEW_TASK` 和父线程 `spawn_agent` arguments 中都将具体委托 payload 加密；当前只能以 agent name 加父任务上下文分档，无语义名称会降低准确性。
- 历史 fan-out 基准中 live 的 18 条缺 `response.completed` 已定位为客户端断开后路由器过早关上游；修复后验证缺失率为 0，但历史报告的费用仍应按下界解读。
- T1 的 1,466 个有效请求中仍有 1 条未开 raw debug 时的 completed 缺失；已标记为费用下界，说明仍需保留缺失可观测性。
- 直连模式会把 Codex 登录态认证 header 转发给 `chatgpt.com`，仅适合本机回环开发；不得把监听地址改为外网接口。
- `/v1/models` 为兼容 Codex CLI 0.155.1 返回双结构；未来 CLI 目录协议改变时需要适配。
- 字符估算只是 usage 缺失时的兜底，中文/工具 schema 很大时误差可能显著。
- 状态文件版本不匹配或条目丢弃仍缺显式告警/迁移（B4）。
- 本机回环服务已有资源上限，但仍没有可选本地鉴权（B11）；任何本机进程仍可消耗用户的 Codex 登录态额度。
- 客户端 `accept-encoding` 尚未显式剥离（B10）；未来客户端若请求 gzip，上游流解析可能失效。
- 协调 hint 依赖明确的“子 agent + 并行派发 + 等待汇总”措辞；隐式协调任务仍可能漏判。s2 只有 3 个有效样本，尚不能代表所有 fan-out 协调任务。
- 后台状态刷新已覆盖单测与正常退出，但尚未做进程崩溃、磁盘满恢复和数万线程长跑测试。
- 桌面 App 包内 Codex 已验证，但 GUI 宿主是否在完全重启后读取同一 provider 仍待用户发一条实际任务确认；不能用包内 CLI 结果冒充 GUI 验收。
- 决策日志尚无轮转/保留策略；一周 soak 期间需观察体积，报告脚本不会删除原始日志。

## T5 soak 与后续工作

1. 当前先跑一周 shadow soak；结束后运行 `scripts/shadow-report.py --days 7`，再决定是否删除 shadow 哨兵真开路由。
2. 增加状态 schema 版本告警/迁移（B4），并为决策日志与 raw stream 提供轮转和保留策略。
3. 显式剥离或处理 `accept-encoding`（B10），增加可选本地鉴权（B11）。
4. 做进程崩溃、磁盘满恢复、并发峰值、数万线程状态增长和日志轮转的长期故障注入。
5. 在安装 Codex Router 的机器上做 caller edge 真实验收，验证 caller secret、shared session、目录刷新和服务托管。
6. 扩大人工质量样本，覆盖隐式协调、嵌套/fork 子 agent、多仓库、多语言和高风险任务；每次升级 Codex CLI 重放压缩与子线程真实 fixture。
