# Codex + Jev 边界路由器（阶段一）

这是一个只监听本机回环地址的 OpenAI Responses 兼容服务。它在 Codex 与真实后端之间维持线程级路由状态，只在边界事件调用 Jev，随后将所选 `model`、`reasoning.effort` 和 `service_tier` 写入上游请求。

阶段一的核心约束是“线程内钉住”：首请求、新用户轮次、压缩检查点和子 agent 首请求之外，不重新决策；同一 `thread-id + turn_id` 的工具续跑绝不调用 Jev。

## 能力

- `GET /health`（只返回 Jev key 是否已加载的布尔值，不返回 key）
- `GET /v1/models`（同时返回 Codex CLI 所需的 `models` 字段与 OpenAI 风格的 `data`）
- `POST /v1/responses`
- 强制上游 `stream: true`，逐字节 SSE 透传并重新声明 `Content-Type`
- 调用方请求非流式时，用 `response.completed` 组装 JSON
- 支持 chunked 请求和 chunked SSE 响应
- 线程状态原子落盘，决策日志以权限 `0600` 的 JSONL 写入
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
| `compaction` | 本次强制 `gpt-5.6-sol@high`，下一轮免费重选 |
| `subagent_first` | 调 Jev，按子线程独立钉住 |

`tool_continuation` 和同轮 `reuse` 都沿用现有状态。Jev 低于 `0.5` 置信度时回退 `gpt-5.6-sol`；缺 key、超时或响应异常时 fail-open 到 `gpt-6-astra@medium`。Jev 单次超时 4 秒，失败后最多重试 2 次，退避为 0.25、0.5 秒。

切换成本按每百万 token 的美元价格计算：

```text
switch_cost = context_tokens × target.cache_write / 1_000_000
stay_cost   = context_tokens × current.cached_input / 1_000_000
```

两者差额超过 `$0.25` 时保持当前路由；上下文超过 `20000` token 时拒绝降级。上下文优先取上一响应 `response.completed.usage.input_tokens`，缺失时按请求字符数估算。价格和阈值都在 TOML 中可改。

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

# 恢复
rm ~/.codex/codex-jev-router/router.off
rm ~/.codex/codex-jev-router/router.shadow
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
python3 -m compileall -q codex_jev_router tests
```

测试使用本地假 Jev 与假上游，不访问网络，覆盖四个决策时机、工具续跑、成本关卡、身份样例、Jev 重试和 fail-open、SSE 透传/组装、代理 CONNECT、状态和 HTTP 端点。
