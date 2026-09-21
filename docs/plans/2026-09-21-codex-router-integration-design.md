# Codex Router caller edge 接入设计

## 目标

在不改参考仓库源码、不复制任何凭据的前提下，把 Codex Router 安装为本机 4202 网关，把 HippoRoute-Jev-Codex 注册为 `openai-responses` generic provider，并发布 `jev/auto` 到 Codex/ChatGPT App 模型目录。Jev 路由器继续 shadow，真实上游改由 caller edge 使用现有 ChatGPT 登录态。

## 安装与数据边界

Codex Router 使用无 provider、无 discovery 的 idle 安装，避免探测第三方凭据；安装器仍负责 Node/Python 依赖、launchd 服务、caller capability、原生目录和 `~/.codex/config.toml` 的两个 managed block。managed block、`generic-providers.json` 和 caller secret 只通过 Router CLI 操作，不手改。`user-models.json` 是文档允许的手工状态文件，更新时保留所有既有条目并原子写入。

HippoRoute-Jev-Codex 增加一份无 secret 的常驻配置，`upstream.mode=caller_edge`、base URL 为 `http://127.0.0.1:4202`、secret 路径为 `~/.codex/codex-router/caller-secret`。现有代码在运行时拼出 `/_codex-router/<secret>/v1`；plist 只增加 `--config`，不会包含 capability。shadow 哨兵继续位于 `~/.codex/hipporoute/router.shadow`。

## 启停语义

新的 `enable.sh` 不再改 `model_provider`。它依次确认 Codex Router 与 Jev 服务健康、确认 shared ChatGPT session、注册/启用 generic provider、确保 `jev/auto` 目录条目存在、刷新目录、显示 picker 条目并建立 shadow。`disable.sh` 只隐藏 picker 并 disable provider，保留两套服务和 Codex Router managed config；可选参数才停止 Jev 服务。彻底卸载由文档明确分成隐藏/移除 Jev、自身服务卸载、Codex Router uninstall 和原配置哈希核对。

## 验证

先验证 4202 原生模型请求，再启用 shared session。随后用 caller edge 对 `jev/auto` 发 SSE 请求，并从新增决策记录验证 `shadow=true`、`gate=apply`、`upstream_model=gpt-6-astra`。最后以 `codex exec -m jev/auto` 验证客户端目录/dispatcher/provider 全链路。自动测试锁定脚本不再编辑 Codex 根 provider、catalog JSON 合并幂等、caller config 不含 secret，以及 enable/disable 的 CLI 行为。
