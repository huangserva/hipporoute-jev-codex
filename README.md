# HippoRoute-Jev-Codex

Boundary-only model routing for OpenAI Codex, driven by TypeSafe Jev.

This project is not affiliated with OpenAI or TypeSafe.

HippoRoute-Jev-Codex is a local, standard-library-only Python service that chooses
an appropriate Codex model at **thread boundaries**, then pins that choice for
the thread's lifetime. It asks TypeSafe Jev only for a new thread, a new user
turn, a compaction checkpoint, or a sub-agent's first request. Tool continuations
stay sticky. Model changes pass a cache-rewrite cost gate, and every Jev or
protocol failure fails open to `gpt-6-astra@medium`.

The HTTP surface is OpenAI Responses-compatible: `GET /health`, `GET /v1/models`,
and `POST /v1/responses`. The server binds to loopback by default, streams SSE
without buffering, supports non-stream callers, and can sit directly in front of
ChatGPT Codex or behind [Codex Router](https://github.com/duolahypercho/codex-router).
Repository: <https://github.com/huangserva/hipporoute-jev-codex>.

## Why boundary routing

Per-request routing makes an agent repeatedly reconsider its model and can force
expensive prompt-cache rewrites. This router instead maintains per-thread state:

- first request: ask Jev and pin model + effort;
- new user turn: ask Jev, but switch only when the cost gate allows it;
- compaction checkpoint: use `gpt-5.6-sol@high`, then allow one free reroute;
- sub-agent first request: make an independent decision for that child thread;
- tool continuation or same-turn reuse: never ask Jev, keep the pinned route.

Thread identity prefers `thread-id`, then `client_metadata.thread_id`, then
`x-codex-turn-metadata.thread_id`. Conflicting identities fail open. Codex
Router's generic-provider compatibility path has a narrower fallback based on
the cache key and stable message IDs when it strips all three official signals.

## Relationship to `jev-codex-router`

This is an independent boundary-routing implementation. It was informed by the
MIT-licensed `jev-codex-router`, especially its SSE forwarding, caller-edge
integration details, and dry-fallback concept. This project differs by pinning
thread state, limiting Jev calls to explicit boundaries, applying a cache-cost
gate, and routing native sub-agents independently. See [LICENSE](LICENSE); retain
the upstream project's MIT notice in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Requirements

- macOS or another system capable of running a local Python service;
- Python 3.11 or newer; no third-party Python packages;
- Codex CLI 0.155.1 is the tested protocol baseline;
- a TypeSafe API key for Jev, obtainable from your TypeSafe account;
- an authenticated Codex ChatGPT session;
- optional: Codex Router for a model-picker entry and shared caller edge.

Protocol headers used here are private implementation details. Revalidate the
fixtures and smoke tests after upgrading Codex CLI or Codex Router.

## Configure the Jev key

Use exactly one of these mechanisms. A literal key in `config.toml` is rejected.

1. Environment variable:

   ```bash
   export TYPESAFE_API_KEY='your-key'
   ```

2. Default file `~/.jev.env`:

   ```bash
   printf '%s\n' 'TYPESAFE_API_KEY=your-key' > ~/.jev.env
   chmod 600 ~/.jev.env
   ```

3. A custom key file configured in TOML:

   ```toml
   [jev]
   key_file = "~/.config/hipporoute/jev.env"
   ```

   The referenced file has the same single-line `TYPESAFE_API_KEY=...` format
   and should have mode `0600`.

Lookup order is environment variable, configured `key_file`, then `~/.jev.env`.
Start from [.env.example](.env.example) and [config.example.toml](config.example.toml).

## A. Direct mode (without Codex Router)

This path uses Codex's existing ChatGPT subscription authorization. Back up the
Codex config before changing it.

```bash
git clone https://github.com/huangserva/hipporoute-jev-codex.git
cd hipporoute-jev-codex
cp config.example.toml config.local.toml
python3 -m hipporoute --config config.local.toml
curl -fsS http://127.0.0.1:4319/health
```

Keep this in `config.local.toml`:

```toml
[upstream]
mode = "direct"
direct_url = "https://chatgpt.com/backend-api/codex"
```

Then back up and edit `~/.codex/config.toml`:

```toml
model_provider = "hipporoute"

[model_providers.hipporoute]
name = "HippoRoute (Jev)"
base_url = "http://127.0.0.1:4319/v1"
wire_api = "responses"
requires_openai_auth = true
```

Verify with a new thread:

```bash
codex exec 'Say OK'
tail -n 1 ~/.codex/hipporoute/decisions.jsonl
```

Restore your backed-up `~/.codex/config.toml` before stopping the service.

## B. Codex Router mode (model picker)

This mode exposes **HippoRoute (Jev)** as `jev/auto` in compatible model
pickers. Install Codex Router first and verify its health endpoint.

```bash
export CODEX_ROUTER_HOME=/path/to/codex-router
"$CODEX_ROUTER_HOME/bin/codex-router" start
"$CODEX_ROUTER_HOME/bin/codex-router" chatgpt-session enable
curl -fsS http://127.0.0.1:4202/health
```

Prepare the local service config and start the service manually:

```bash
cp config.service.example.toml config.service.toml
python3 -m hipporoute --config config.service.toml
curl -fsS http://127.0.0.1:4319/health
```

Or on macOS, install the `RunAtLoad + KeepAlive` launchd service:

```bash
# Optional if direct outbound HTTPS needs a proxy:
export JEV_ROUTER_HTTPS_PROXY='http://127.0.0.1:PORT'
scripts/install-service.sh
```

The installer embeds only the proxy URL, never the Jev key. It reads
`config.service.toml`, creating it from the ignored example if absent.

Register and show the provider (the script is idempotent):

```bash
CODEX_ROUTER_HOME=/path/to/codex-router scripts/enable.sh
```

Equivalent provider commands are:

```bash
"$CODEX_ROUTER_HOME/bin/codex-router" providers generic add jev \
  --name "HippoRoute (Jev)" \
  --base-url http://127.0.0.1:4319/v1 \
  --adapter openai-responses \
  --allow-private
python3 scripts/codex_router_catalog.py ~/.codex/codex-router/user-models.json
"$CODEX_ROUTER_HOME/bin/codex-router" refresh-catalog
"$CODEX_ROUTER_HOME/bin/codex-router" control picker set jev/auto show
```

Verify the CLI path with `codex exec -m jev/auto 'Say OK'`. For ChatGPT.app,
quit it completely, reopen it, choose **HippoRoute (Jev)**, submit a new task,
and check that a new decision line appears.

## Operating modes

Sentinel paths are configurable under `[paths]`:

| Mode | File state | Behavior |
|---|---|---|
| live | neither sentinel exists | Jev decisions are applied |
| shadow | `router.shadow` exists | record `would`, serve Astra |
| off | `router.off` exists | skip Jev, serve Astra fail-open |

```bash
mkdir -p ~/.codex/hipporoute
touch ~/.codex/hipporoute/router.shadow   # shadow
rm -f ~/.codex/hipporoute/router.shadow  # live
touch ~/.codex/hipporoute/router.off      # off
```

The JSONL decision log defaults to
`~/.codex/hipporoute/decisions.jsonl`, is created with mode `0600`, and
contains no authorization header or full prompt. Important fields include:

| Field | Meaning |
|---|---|
| `thread_id`, `turn_id`, `parent_thread_id` | redaction-sensitive route identity |
| `event` | boundary or sticky event |
| `gate`, `reason` | apply/hold/sticky/fail-open outcome |
| `jev`, `jev_ms` | raw judgment and latency |
| `would`, `shadow` | shadow recommendation and mode |
| `model`, `effort`, `upstream_model` | selected and completed model evidence |
| `usage`, `response_completed` | token accounting and SSE completeness |

Summarize recent shadow traffic without editing the log:

```bash
scripts/shadow-report.py --hours 3 ~/.codex/hipporoute/decisions.jsonl
scripts/shadow-report.py --since 2026-09-21T09:00:00+08:00 /path/to/decisions.jsonl
scripts/shadow-report.py --days 7 --json /path/to/log-directory
```

## Configuration reference

| Setting | Default | Purpose |
|---|---:|---|
| Astra / Sol / Luna | `gpt-6-astra` / `gpt-5.6-sol` / `gpt-5.6-luna` | three model tiers |
| `routing.luna_effort` | `low` | Luna reasoning effort; configurable through `max` |
| `jev.confidence_gate` | `0.5` | lower confidence falls back to Sol |
| `routing.downgrade_max_context_tokens` | `20000` | block downgrade above this context |
| `routing.switch_budget_usd` | `0.25` | maximum cache-switch premium |
| `jev.timeout_seconds` / `retries` | `4` / `2` | Jev timeout and retry count |
| `jev.backoff_seconds` | `0.25` | exponential retry base |
| circuit failures / open seconds | `3` / `60` | temporary Jev circuit breaker |
| request body / socket / concurrency | 16 MiB / 30 s / 64 | local resource limits |
| state TTL / flush interval | 24 h / 2 s | memory GC and merged persistence |

The switch gate compares target cache-write cost with current cache-read cost.
A same-model effort change does not charge a cache rewrite. Missing usage falls
back to a character estimate and is labeled `context_source=estimate`.

## Measured results

These are controlled measurements from one machine, one account, and small
automatic-check samples. They are not universal performance or cost guarantees.

| Experiment | Observed result | Decision |
|---|---|---|
| mechanical new threads | 95.36% lower cost, 48/48 sessions passed | route once at task start |
| native sub-agents | 79.74% lower parent+child cost, 24/24 passed | route each child independently |
| 30k–150k parent fan-out | child first request stayed ~22k; no measured decay | keep `fork_turns=all` |
| Luna effort | `low` cut median wall time 44.6% vs `max`, pass rate unchanged | default Luna to `low` |
| confidence gate | 0.35 was cheaper in a tiny sample, but causal evidence was insufficient | keep default `0.5` |

Detailed, environment-specific reports are under [docs/experiments](docs/experiments/README.md).

## Known limitations and risks

- Codex encrypts the concrete delegated payload in observed sub-agent traffic;
  routing may have only the agent/task name plus parent context.
- Thread headers and turn metadata are private, unstable protocols.
- Codex Router's generic path needs a compatibility identity fallback and must
  be revalidated after upgrades.
- Up to the first 500 task characters are sent to TypeSafe Jev for judgment.
- The server has no local authentication; bind it only to loopback.
- A cold Jev call can exceed the nominal 4-second attempt timeout and succeed on
  retry, adding user-visible latency.
- Direct mode forwards Codex's existing authorization to ChatGPT; never expose
  the proxy to another host.
- Decision logs contain thread identifiers and task previews; protect and rotate
  them as sensitive local data.

## Uninstall and rollback

For Codex Router mode:

```bash
CODEX_ROUTER_HOME=/path/to/codex-router scripts/disable.sh
CODEX_ROUTER_HOME=/path/to/codex-router scripts/disable.sh --stop-service
launchctl bootout "gui/$(id -u)/com.hippo.codex-router-loopback-env" 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.hippo.hipporoute.plist
rm -f ~/Library/LaunchAgents/com.hippo.codex-router-loopback-env.plist
```

Then use Codex Router's own uninstall/restore command if you no longer want it.
For direct mode, restore the exact `~/.codex/config.toml` backup made before the
provider edit, verify native `codex exec 'Say OK'`, then stop the router.

## Migrating from `codex-jev-router`

The project was renamed from `codex-jev-router` to **HippoRoute-Jev-Codex**. A
running installation keeps working until you reinstall: the repository carries
the new names, but nothing under `~/.codex` is touched by this repository. The
Codex Router provider id stays `jev`, so no provider re-registration is needed.

| Old | New |
|---|---|
| repository / directory `codex-jev-router` | `hipporoute-jev-codex` |
| Python package `codex_jev_router` | `hipporoute` |
| `python3 -m codex_jev_router` | `python3 -m hipporoute` |
| state directory `~/.codex/codex-jev-router/` | `~/.codex/hipporoute/` |
| launchd label `com.jev.codex-jev-router` | `com.hippo.hipporoute` |
| proxy-env launchd label `com.jev.codex-router-loopback-env` | `com.hippo.codex-router-loopback-env` |
| direct-mode provider id `codex-jev-router` | `hipporoute` |
| model-picker entry `Codex + Jev Router` | `HippoRoute (Jev)` |
| log `~/Library/Logs/codex-jev-router.*.log` | `~/Library/Logs/hipporoute.*.log` |

To migrate an existing install:

```bash
# 1. unload the old launchd labels (the old files stay on disk until you remove them)
launchctl bootout "gui/$(id -u)/com.jev.codex-jev-router" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.jev.codex-router-loopback-env" 2>/dev/null || true

# 2. move the state directory, keeping the decisions log and thread state
mv ~/.codex/codex-jev-router ~/.codex/hipporoute

# 3. reinstall from the renamed checkout, then re-enter shadow mode if you want it
touch ~/.codex/hipporoute/router.shadow
scripts/install-service.sh
```

If your `config.service.toml` or `config.local.toml` sets `[paths]` explicitly,
update those values (or regenerate the file from the example) so the service
reads and writes the new directory. Direct-mode users must also rename the
provider id in `~/.codex/config.toml`: run `scripts/configure_codex.py restore
--state <path>` and then `enable` again, and delete a leftover
`[model_providers.codex-jev-router]` table if an older run left one behind.

## Development

```bash
python3 -m unittest -v
python3 -m compileall -q hipporoute bench tests scripts
```

Tests use fake Jev and upstream services and do not require network access. See
[CONTRIBUTING.md](CONTRIBUTING.md) and [STATUS.md](STATUS.md).

## 中文速览

这是一个本机 Codex 边界路由器：只在线程首请求、新用户轮次、压缩点和
子 agent 首请求问 Jev，工具续跑固定沿用已选模型；切换前再计算缓存重写
成本，任何异常都 fail-open 到 Astra。第一次使用建议先开 shadow：创建
`~/.codex/hipporoute/router.shadow`，观察决策日志，再决定是否进入 live。
密钥只能通过 `TYPESAFE_API_KEY`、权限 600 的 `~/.jev.env`，或 TOML 中的
`jev.key_file` 提供，不能直接写进配置文件。
项目原名 `codex-jev-router`，现已更名为 HippoRoute-Jev-Codex：Python 包名
`hipporoute`，状态目录 `~/.codex/hipporoute/`，launchd 标识
`com.hippo.hipporoute`，模型目录条目 `HippoRoute (Jev)`；Codex Router 的
provider id 仍为 `jev`。老用户迁移步骤见上文 “Migrating from `codex-jev-router`”。
