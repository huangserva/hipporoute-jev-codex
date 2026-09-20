# T5 常驻安装设计

## 目标与边界

把现有本地路由器交给当前登录用户的 launchd 守护，并让 Codex CLI 与 ChatGPT 桌面 App 内置 Codex 在同一份 `~/.codex/config.toml` 下指向 `127.0.0.1:4319`。初始运行模式固定为 shadow：Jev 正常判断并记录 `would`，上游仍由 astra 服务。安装过程不复制、不打印 `~/.jev.env` 中的 key，也不修改全局 hook。

## 方案选择

服务安装采用 launchd plist，而不是长期后台 shell：它原生提供 `RunAtLoad` 和 `KeepAlive`，用户登录后自动拉起。plist 显式写入 `HOME`、稳定的 Python 可执行路径、`PATH`、`HTTPS_PROXY=http://127.0.0.1:7897` 与本地地址的 `NO_PROXY`；程序仍通过现有 `load_key()` 从 `~/.jev.env` 读取 key。安装脚本在 bootstrap 后轮询 `/health`，同时要求 `ok=true` 和 `jev_key=true`。

配置编辑不在 shell 中拼接 TOML，而由标准库 Python helper 负责。首次 enable 保存带时间戳的完整原文件，并在 git 忽略的 runtime 状态中记录原始哈希、备份路径和启用后哈希。重复 enable 复用同一个恢复点；disable 只在当前文件仍等于启用后哈希时自动恢复，避免覆盖一周内的人工修改，必要时允许显式 `--force`。启动顺序是安装/确认服务、保存恢复点、建立 shadow 哨兵、最后切换 Codex provider，从而没有指向死端口或短暂 live 的窗口。

## 周报与验证

`shadow-report.py` 读取一个或多个 JSONL，按本地日期聚合根会话、线程、Jev 决策、would 档位、置信度分桶、低置信回退、Jev 错误和延迟。费用使用 BACKTEST 每百万 token 价格，对每条已完成请求的同一份 usage 分别按实际上游模型和该线程 `would` 模型重定价；缺 completed/usage 单独计数。它能估算固定 token 轨迹下的价格差，不能预测真路由造成的请求步数和 token 轨迹变化。

自动验收覆盖配置变换/幂等恢复、手工修改保护、日志聚合与缺 usage。实际安装后检查 launchctl、plist、health、shadow、配置哈希和 CLI 实流量。桌面 App 必须由用户 `⌘Q` 后重开并发新任务；报告给出只读日志验证命令，不伪造 GUI 结果。
