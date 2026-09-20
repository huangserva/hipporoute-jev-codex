# Codex + Jev 边界路由器

这是一个只监听本机回环地址的 OpenAI Responses 兼容服务。它在 Codex 与真实后端之间维持线程级路由状态，只在边界事件调用 Jev，随后将所选 `model`、`reasoning.effort` 和 `service_tier` 写入上游请求。

核心约束是“线程内钉住”：首请求、新用户轮次、压缩检查点和子 agent 首请求之外，不重新决策；同一 `thread-id + turn_id` 的工具续跑绝不调用 Jev。阶段二让每个原生子 agent 在首请求独立分档，不无条件继承父线程模型。

## 能力

- `GET /health`（只返回 Jev key 是否已加载的布尔值，不返回 key）
- `GET /v1/models`（同时返回 Codex CLI 所需的 `models` 字段与 OpenAI 风格的 `data`）
- `POST /v1/responses`
- 强制上游 `stream: true`，逐字节 SSE 透传并重新声明 `Content-Type`
- 调用方请求非流式时，用 `response.completed` 组装 JSON
- 支持 chunked 请求和 chunked SSE 响应
- 线程状态原子落盘，决策日志以权限 `0600` 的 JSONL 写入
- debug 哨兵开启时，每条上游流按原始 chunk 与时间戳写入独立 0600 JSONL
- 直连 ChatGPT Codex 后端，或经 Codex Router caller edge 使用共享登录态
- 遵守系统 `HTTPS_PROXY`；不需要也不读取 OpenAI API key

项目仅使用 Python 3.11+ 标准库。

## 路由规则

线程身份按以下优先级解析：

1. `thread-id` header
2. `client_metadata.thread_id`
3. `x-codex-turn-metadata.thread_id`

多个来源不一致时记为 `identity_conflict`，fail-open 到 `gpt-6-astra@medium`，不污染线程状态。`prompt_cache_key` 不参与线程识别。子 agent 由 `x-openai-subagent: collab_spawn` 或 `x-codex-parent-thread-id` 识别，并以自己的 thread id 独立钉住。

只有以下事件允许改变路由：

| 事件 | 行为 |
|---|---|
| `first_request` | 调 Jev，钉住线程 |
| `new_user_turn` | 调 Jev，再过切换成本与降级上下文关卡 |
| `compaction` | 本次强制 `gpt-5.6-sol@high`，下一轮免费重选；优先识别 `x-codex-turn-metadata.request_kind/compaction`，提示词仅兜底 |
| `subagent_first` | 调 Jev，按子线程独立钉住 |

`tool_continuation` 和同轮 `reuse` 都沿用现有状态。子 agent 的 Jev state 包含 `parent_tier`、`agent_name`、`subagent_kind` 和委托来源；路由 instructions 明确要求只评估子任务。Jev 低于 `0.5` 置信度时回退 `gpt-5.6-sol`；缺 key、超时或响应异常时 fail-open 到 `gpt-6-astra@medium`。Jev 单次超时 4 秒，失败后最多重试 2 次，退避为 0.25、0.5 秒；HTTP 4xx 除 429 外不重试，429 遵守有上限的 `Retry-After`。默认连续 3 次失败后熔断 60 秒，期间以 `gate=jev_circuit_open` 直接 fail-open。参数位于 `[jev]`。

Luna 默认使用 `effort=low` 和 `service_tier=priority`；这是 T3 机械任务基准中通过率不变且步数、墙钟和费用最低的设置。可用 `[routing] luna_effort = "medium"` 或 `"max"` 调高 effort，service tier 不随该配置改变。

Codex CLI 0.155.1 会把具体委托 payload 以 `encrypted_content` 发给后端，所以边界路由器当前以 `agent_name/task_name` 加已标注的父任务上下文作分档输入，并记 `delegation_source=agent_name_fallback`。代码已预留对未来明文 `NEW_TASK Payload` 和明文 spawn `message` 的优先提取；不尝试解密。

同一 thread id 的决策临界区由 per-thread lock 串行化。状态按 `last_active_at` 做保守 GC，默认 TTL 是 86400 秒、GC 间隔是 300 秒，可在 `[routing]` 的 `state_ttl_seconds` 和 `state_gc_interval_seconds` 修改。

常驻服务默认限制单请求体为 16 MiB、客户端 socket 空闲 30 秒、同时处理 64 个 POST；可用 `[server] max_request_body_bytes`、`client_socket_timeout_seconds`、`max_concurrent_requests` 调整。并发超限立即返回 503，并以 `gate=server_busy` 写入决策日志。

切换成本按每百万 token 的美元价格计算：

```text
switch_cost = context_tokens × target.cache_write / 1_000_000
stay_cost   = context_tokens × current.cached_input / 1_000_000
```

两者差额超过 `$0.25` 时保持当前路由；上下文超过 `20000` token 时拒绝降级。同模型只改变 effort 不会重建 prompt cache，因此直接按 cache-read 成本放行。上下文优先取上一响应 `response.completed.usage.input_tokens`，缺失时按本请求字符数估算，决策日志用 `context_source=usage|estimate` 标明来源。价格和阈值都在 TOML 中可改。

## 启动

```bash
cp config.example.toml config.local.toml
python3 -m codex_jev_router --config config.local.toml
curl -s http://127.0.0.1:4319/health
```

Jev key 的读取顺序为环境变量 `TYPESAFE_API_KEY`，然后是 `~/.jev.env` 中的同名变量。不要把 key 写进项目配置。

### 直连模式（开发）

保持 `config.local.toml` 中：

```toml
[upstream]
mode = "direct"
direct_url = "https://chatgpt.com/backend-api/codex"
```

再在 `~/.codex/config.toml` 顶层设置 provider，并增加 provider 表：

```toml
model_provider = "codex-jev-router"

[model_providers.codex-jev-router]
name = "Codex + Jev Router"
base_url = "http://127.0.0.1:4319/v1"
wire_api = "responses"
requires_openai_auth = true
```

这会使用 Codex 的 ChatGPT 订阅登录态。修改前务必备份 `~/.codex/config.toml`，实验结束恢复。

### 通过 Codex Router 部署（生产形态）

先把 `config.local.toml` 的 `upstream.mode` 改为 `caller_edge`，并确认 `caller_edge_url` 与 `caller_secret_path`。随后在 Codex Router checkout 中运行：

```bash
./bin/codex-router chatgpt-session enable
./bin/codex-router providers generic add jev \
  --name "Jev Router" \
  --base-url http://127.0.0.1:4319/v1 \
  --adapter openai-responses \
  --allow-private
```

在 `~/.codex/codex-router/user-models.json` 增加本地模型目录：

```json
{
  "version": 1,
  "models": [
    {
      "slug": "jev/auto",
      "gatewayModel": "jev-auto",
      "compHash": "jev-auto-user-v1",
      "upstreamModel": "auto",
      "provider": "jev",
      "listed": true,
      "displayName": "Codex + Jev Boundary Router",
      "description": "Thread-pinned boundary routing by Jev.",
      "priority": 95,
      "defaultEffort": "medium",
      "reasoningLevels": [
        { "effort": "low", "description": "Quick reasoning" },
        { "effort": "medium", "description": "Balanced reasoning" },
        { "effort": "high", "description": "Deep reasoning" },
        { "effort": "xhigh", "description": "Extended reasoning" },
        { "effort": "max", "description": "Maximum reasoning" }
      ],
      "contextWindow": 258400,
      "autoCompact": 219640,
      "inputModalities": ["text", "image"]
    }
  ]
}
```

```bash
./bin/codex-router refresh-catalog
./bin/control picker set jev/auto show
```

## 运行开关

默认文件路径可在 `[paths]` 修改。

```bash
# kill switch：不问 Jev，所有请求直接走 astra@medium
touch ~/.codex/codex-jev-router/router.off

# shadow：照常决策和记录 would，但实际走 astra@medium
touch ~/.codex/codex-jev-router/router.shadow

# 上游原始流调试：仅排查时开启，文件默认写入 raw-streams/
touch ~/.codex/codex-jev-router/stream.debug

# 恢复
rm ~/.codex/codex-jev-router/router.off
rm ~/.codex/codex-jev-router/router.shadow
rm ~/.codex/codex-jev-router/stream.debug
```

线程状态默认在 `~/.codex/codex-jev-router/threads.json`，决策日志默认在 `~/.codex/codex-jev-router/decisions.jsonl`。日志不保存完整请求正文和认证 header；`task` 仅保留最多 160 字符的脱敏预览。字段包括线程/轮次/父线程、事件、`apply/hold/sticky` 关卡、原因、shadow/would、Jev 耗时、实际路由、SSE completed model、usage 与总耗时。示例：

```json
{"thread_id":"thread-main…","turn_id":"turn-1…","parent_thread_id":null,"event":"first_request","gate":"apply","reason":"jev","consulted_jev":true,"jev_ms":1106,"model":"gpt-5.6-luna","effort":"max","upstream_model":"gpt-5.6-luna","response_completed":true,"status":200,"out":"sse","usage":{"input_tokens":16268,"cached_tokens":0,"output_tokens":824}}
```

## 真实端到端验证（2026-09-19）

在 Codex CLI 0.155.1 上以直连模式完成真实会话：主线程首轮执行了至少两次工具续跑，原生协作产生一个子线程，随后用 `codex exec resume` 发出第二个用户轮次。机器没有 Jev key，因此三个决策点均为 `no_key`，工具续跑均为 `sticky`；所有上游响应为 HTTP 200。脱敏后的关键日志如下：

```jsonl
{"thread_id":"main…","parent_thread_id":null,"event":"first_request","gate":"no_key","model":"gpt-6-astra","effort":"medium","status":200}
{"thread_id":"main…","parent_thread_id":null,"event":"tool_continuation","gate":"sticky","model":"gpt-6-astra","effort":"medium","status":200}
{"thread_id":"child…","parent_thread_id":"main…","event":"subagent_first","gate":"no_key","model":"gpt-6-astra","effort":"medium","status":200}
{"thread_id":"main…","parent_thread_id":null,"event":"new_user_turn","gate":"no_key","model":"gpt-6-astra","effort":"medium","status":200}
```

本次日志共 8 条：`first_request ×1`、`subagent_first ×1`、`new_user_turn ×1`、`tool_continuation ×4`、`reuse ×1`；2 个独立 thread id。真实原始运行文件留在被 `.gitignore` 排除的 `runtime/` 中。

## 测试

```bash
python3 -m unittest -v
python3 -m compileall -q codex_jev_router bench tests
```

测试使用本地假 Jev 与假上游，不访问网络，覆盖四个决策时机、真实 Codex 0.155.1 压缩 fixture、工具续跑、成本关卡、身份样例、Jev 重试/熔断和 fail-open、SSE 异常生命周期与组装、代理 CONNECT、状态和 HTTP 端点。

## 机械任务基准

`bench/run.py` 会启动直连路由器、临时改写并按 SHA-256 核对恢复 `~/.codex/config.toml`，为每个样本复制一份隔离工作区，并在结束时停止服务、删除哨兵和工作副本。需要真实 Jev key；原始结果写入被 Git 忽略的 `runtime/bench/`。

```bash
python3 bench/run.py --dry-run --run-id dry-YYYYMMDD
python3 bench/run.py --repeats 3 --run-id full-YYYYMMDD
```

正式基准使用 8 个任务、shadow/live 各 3 遍，共 48 个独立新会话；基础设施失败最多重试 2 次，任务本身未通过不会重试。2026-09-20 的实测结果是 48/48 通过，机械组节省 95.36%，全任务节省 44.75%，详见 `docs/2026-09-19-机械任务基准.md`。

## 子 agent 分档基准

`bench/run_subagent.py` 用 4 个自动验收的 fan-out 任务验证父/子线程独立分档，同样会负责启动路由器、交替 shadow/live、备份恢复 Codex 配置、重试基础设施失败并清理工作副本。

```bash
python3 bench/run_subagent.py --dry-run --run-id subagent-dry-YYYYMMDD
python3 bench/run_subagent.py --repeats 3 --run-id subagent-full-YYYYMMDD
```

2026-09-20 的 24 个有效会话全部通过；已完成 SSE 的子 agent 费用降低 83.90%，父子合计降低 79.74%，但 live 墙钟高 21.55% 且缺 completed 数更多，必须按单次受控样本解读。详见 `docs/2026-09-20-子agent分档基准.md`。

## T1/T2 后续验证

T2 用 debug 哨兵下的原始上游 chunk 捕获定位了历史 completed 缺失：Codex 客户端提前断开后，路由器过早关闭上游。现在下游断开后会继续排空上游以取得 completed/usage；同任务 6 次、127 请求缺失为 0。详见 `docs/2026-09-20-T2-缺completed排查.md`。

T1 用 `bench/run_long_parent.py` 运行 30k/80k/150k 父上下文 × shadow_all/live_all/live_none × 3 遍。27/27 会话通过；`fork_turns=all` 的子首请求约 22k，未随父上下文增长，所测档位没有出现 live_all 费用不如 shadow 的交叉点。`fork_turns=none` 将子首请求降到约 16.9k，但总费用没有稳定优势，因此当前不建议全局强制 none。详见 `docs/2026-09-20-T1-长父线程fanout.md`。
