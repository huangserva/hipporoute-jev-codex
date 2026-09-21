# Codex Router 接入与 caller edge 验收

一句话结论：Codex Router 0.6.0 已安装并接管 Codex 的本机入口，`Codex + Jev Router` 已作为可见的 `jev/auto` generic provider 发布；真实 CLI 请求经 4202 → 4319 → caller edge 完整返回，Jev 决策为 `apply`、shadow 实际模型为 `gpt-6-astra`，GUI 只剩用户 `⌘Q` 重开后的列表与新任务验收。

## 安装结果

| 项目 | 实测结果 |
|---|---|
| Codex Router | `0.6.0`，LaunchAgent 正常，`127.0.0.1:4202/health` 返回 `ok=true` |
| Codex 配置 | 安装前备份 `~/.codex/config.toml.backup-before-codex-router-<timestamp>`，SHA-256 `<redacted-sha256>`；当前为 Codex Router managed 配置 |
| ChatGPT session | `sharing=enabled`、`session=usable`；caller secret 为 `~/.codex/codex-router/caller-secret`，权限 0600 |
| Jev 服务 | `com.jev.codex-jev-router`，`127.0.0.1:4319`，health 为 `jev_key=true` |
| Jev 上游 | `caller_edge`，配置只保存 4202 地址与 caller-secret 文件路径，不保存 secret 内容 |
| generic provider | id `jev`，name `Codex + Jev Router`，adapter `openai-responses`，base URL `http://127.0.0.1:4319/v1`，enabled |
| 目录 | `jev/auto` → `jev-auto` → upstream `auto`；effort 为 low/medium/high/xhigh/max；picker visible |
| 模式 | `~/.codex/codex-jev-router/router.shadow` 保持存在 |

安装前先用旧版 `scripts/disable.sh` 恢复了原配置，SHA-256 与既有恢复点 `<redacted-sha256>` 完全一致；随后才执行 Codex Router 安装。安装器在 `~/.codex/config.toml` 写入了带 `BEGIN/END codex-router-managed` 的 4202 入口和 `[model_providers.codex-router]`，没有把 Jev key 或 caller secret 写入配置。

本机需要 `HTTPS_PROXY=http://127.0.0.1:7897` 才能稳定访问外网。Codex Router 服务已启用 Node 的代理环境；`scripts/install-service.sh` 还安装 `com.jev.codex-router-loopback-env.plist`，在 GUI launchd 域持久设置 `NO_PROXY=localhost,127.0.0.1,::1`，避免 ChatGPT.app 把本机 4202/4319 请求送进出网代理。

## 七步安装与验证记录

1. 撤销旧直连：旧 `disable.sh` 恢复后，`shasum -a 256 ~/.codex/config.toml` 为 `<redacted-sha256>`。
2. 安装 Codex Router：从参考 checkout 运行官方 `install.sh`。首次使用 `--no-discovery` 得到空目录，因此用支持的 `codex-router disable` 清理 managed 块后重新安装并发现 11 个 native models。原生 `codex exec 'Say OK'` 经 4202 正常返回 `OK`。
3. 开 shared session：`chatgpt-session status --json` 返回 `sharing=enabled, session=usable`；secret 文件非空且权限 0600，任何命令输出和仓库文件均未记录其内容。
4. 切 Jev 上游：`config.service.toml` 设置 `mode=caller_edge`、URL 只到 `http://127.0.0.1:4202`，secret 由 `caller_secret_path` 运行时读取。launchd 重启后 health 仍是：

   ```json
   {"ok":true,"service":"codex-jev-router","version":"0.1.0","jev_key":true}
   ```

5. 注册 provider：脱敏后的 `providers generic list --json` 核心字段为：

   ```json
   {"id":"jev","displayName":"Codex + Jev Router","baseUrl":"http://127.0.0.1:4319/v1","adapter":"openai-responses","allowPrivate":true,"enabled":true}
   ```

6. 发布目录：`user-models.json` 由幂等 helper 合并，保留其他用户条目；`refresh-catalog` 显示 12 个模型，其中 11 native、1 routed。picker 状态为 `visible=["jev/auto"]`。
7. caller edge 端到端：直接 curl `/_codex-router/<redacted>/v1/responses` 得到完整 `response.completed`；真实 `codex exec -m jev/auto` 返回 `CLI_CALLER_EDGE_OK`。CLI 样本的脱敏决策如下：

   ```json
   {"event":"first_request","gate":"apply","consulted_jev":true,"jev_ms":11908,"shadow":true,"model":"gpt-6-astra","would":{"model":"gpt-5.6-luna","effort":"low"},"status":200,"upstream_model":"gpt-6-astra","response_completed":true}
   ```

   这条记录同时给出两层硬证据：Jev 确实参与并想选 luna；shadow 覆盖后上游 `response.completed.model` 确实是 astra。该次 Jev 约 11.9 秒，符合本机代理下冷调用可能跨过 4 秒并重试的已知现象。另一条 curl 曾出现 `jev_error:URLError`，仍以 astra 完整完成；随后 CLI 重试恢复为 `apply`，因此错误被保留为真实的 fail-open 样本而未掩盖。

ChatGPT.app 包内的 Codex 0.155.0-alpha.9.2 也以 `-m jev/auto` 返回 `APP_CALLER_EDGE_OK`；对应日志为 `gate=apply`、`shadow=true`、would luna@low、Jev 1282 ms、HTTP 200、completed model astra。这证明 App 携带的二进制能读取新目录并走完整 caller-edge 链路，但仍不能代替 GUI 宿主中点击模型并发送任务。

## 接入时发现并修复的兼容问题

Codex Router 的 native 路径有 `thread-id`、`x-codex-turn-metadata` 等转发白名单，但 generic/routed 路径会固定重建 headers，并显式删除 `client_metadata`。真实脱敏入站抓包只剩 `prompt_cache_key`、message ids、input 与常规 Responses 字段。未修前请求虽然 HTTP 200，却被本服务记录为 `missing_thread_id`，Jev 根本没有参与。

兼容修复只在正式 thread 三来源全部缺失时启用：根线程以 `prompt_cache_key` 标识；子线程必须追加稳定的 `NEW_TASK` message id；轮次使用最新 user message id。`prompt_cache_key` 仍不被单独用作子线程键，避免阶段一已证实的主/子碰撞。单测覆盖根线程和子线程组合键，真实 CLI 则证明首请求恢复为 `gate=apply`。这是 Codex Router 0.6.0 的兼容层，不是公开协议保证，升级时必须重验。

## 日常启用、停用与应急

启用（幂等）：

```bash
cd <repo>
scripts/install-service.sh
scripts/enable.sh
```

`enable.sh` 先检查 4202 和 4319/key，再建立 shadow，随后启用 shared session、注册/启用 provider、合并目录、refresh 并 picker show。它不再直接改 `~/.codex/config.toml`。实测 disable → enable 后 provider 从 disabled 恢复 enabled，picker 从 hidden 恢复 visible；重复运行没有重复目录条目。

普通停用：

```bash
scripts/disable.sh
scripts/disable.sh --stop-service  # 还要停 4319 时
```

这会 hide `jev/auto` 并 disable provider，Codex Router 与其 managed config 保持正常，shadow 哨兵也保留。路由器挂掉时先运行这个脚本，选择 native 模型即可继续工作。

临时保留转发但不问 Jev：

```bash
touch ~/.codex/codex-jev-router/router.off
```

恢复判断用 `rm -f ~/.codex/codex-jev-router/router.off`。一周后真开路由时保持 off 不存在，并删除 shadow：

```bash
rm -f ~/.codex/codex-jev-router/router.shadow
```

## GUI 验收清单

本会话不能代替用户操作 GUI。请完成以下四步：

1. `⌘Q` 完全退出 ChatGPT.app，再重新打开；模型目录只在启动边界可靠刷新。
2. 在 Codex 模型列表选最后的 **Codex + Jev Router**。
3. 新建任务，例如“只回复 `GUI_CALLER_EDGE_OK`”。
4. 运行下面的只读检查，确认出现同一时间的新行，且 `gate=apply/hold`、`shadow=true`、`upstream_model=gpt-6-astra`、`response_completed=true`：

   ```bash
   tail -1 ~/.codex/codex-jev-router/decisions.jsonl \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); print({k:d.get(k) for k in ("at","event","gate","shadow","would","upstream_model","response_completed","status")})'
   ```

若列表没有该项，先运行 `scripts/enable.sh`，再 `⌘Q` 重开；若有 401，运行 `codex-router chatgpt-session enable`，不要配置 API key。若 GUI 没新增日志但 `codex exec -m jev/auto` 能新增，说明 GUI 宿主未使用同一路径，应保留为未通过并继续使用 CLI。

## Codex Router 升级后的四项核查

1. `codex-router doctor`：确认 Codex routing config、shared session、background service、router health 均为 OK。
2. `providers generic list --json`：确认 `jev` 仍 enabled、adapter/base URL 未变。
3. `refresh-catalog` 后 `control picker set jev/auto show`：确认 merged catalog 仍有 1 routed entry，picker visible。
4. 重跑 caller-edge curl和 `codex exec -m jev/auto`：重点检查不再出现 `missing_thread_id`，决策为 `apply/hold/sticky` 且 completed model 与 shadow 预期一致。

## 完整回滚到安装前

先隐藏自定义项并停本服务：

```bash
cd <repo>
scripts/disable.sh --stop-service
cd <path-to-codex-router>
./bin/codex-router chatgpt-session disable
./bin/codex-router uninstall
```

官方 uninstall 会移除 Codex managed 配置和 Codex Router 后台服务，但保留 checkout、日志和恢复资产。随后核对：

```bash
shasum -a 256 ~/.codex/config.toml
# 期望安装前值：<redacted-sha256>
```

若官方 uninstall 未恢复到该哈希，先保留当前文件作故障证据，再从 `~/.codex/config.toml.backup-before-codex-router-<timestamp>` 恢复。最后可 bootout 并删除 `com.jev.codex-jev-router.plist` 与 `com.jev.codex-router-loopback-env.plist`；不要删除 `~/.codex/hooks.json`。本次没有实际执行卸载，因为目标是保持一周 shadow 运行。

## 当前边界

已证明 CLI、generic provider、目录、shared session、caller edge、Jev 决策、shadow 覆盖和 completed model 全链路工作。尚未证明 GUI 宿主实际发过一条任务，必须由用户完成上面的手工步骤。generic 路径的身份兼容回退依赖 0.6.0 的保留字段；此外 shared ChatGPT session 会过期，约每几天需重新 enable 或由健康检查提醒。
