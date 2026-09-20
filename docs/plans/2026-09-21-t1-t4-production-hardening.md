# T1–T4 结论落地与常驻健壮性 Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 T1–T4 的已验证结论变成默认策略，并补齐请求资源边界、上游响应嗅探和异步状态落盘三项常驻服务能力。

**Architecture:** 保留现有四个路由决策点和 fail-open 语义；协调型任务仅作为 Jev 输入信号与分类说明，不在路由器内硬编码模型。HTTP 边界在读取/处理前实施可配置资源限制；上游先读取一块再判 SSE/JSON；状态存储改为内存立即更新、后台合并原子写盘、关闭时强制刷新。

**Tech Stack:** Python 3.11+ 标准库、`unittest`、`ThreadingHTTPServer`、`threading.Condition/Semaphore`、TOML 配置。

---

### Task 1: 默认 Luna effort 与协调信号

**Files:**
- Modify: `tests/test_config.py`, `tests/test_policy.py`, `tests/test_jev.py`, `tests/test_engine.py`
- Modify: `codex_jev_router/config.py`, `codex_jev_router/policy.py`, `codex_jev_router/jev.py`, `codex_jev_router/engine.py`
- Modify: `config.example.toml`, `README.md`

1. 先把默认 Luna 断言改为 `low`，新增 fan-out 协调任务识别和 Jev state/questions 断言。
2. 运行相关测试，确认因默认值与协调信号缺失而失败。
3. 最小实现：默认 `low`；从明确“并行/子 agent/spawn”任务文本生成 `coordination_hint`，只对非子线程传给 Jev；tier instructions 明确纯委托汇总属于 Luna。
4. 运行相关测试并提交中文 commit。

### Task 2: B6 请求资源边界

**Files:**
- Modify: `tests/test_config.py`, `tests/test_server.py`
- Modify: `codex_jev_router/config.py`, `codex_jev_router/server.py`
- Modify: `config.example.toml`, `README.md`

1. 新增失败测试：Content-Length 超限返回 413；chunked 累积超限返回 413；handler socket timeout 使用配置；并发槽耗尽返回 503 且决策日志记录 `server_busy`。
2. 运行测试确认失败。
3. 加 `[server] max_request_body_bytes/client_socket_timeout_seconds/max_concurrent_requests`；有界信号量只包 POST 生命周期并在 `finally` 释放，超限记录最小脱敏日志。
4. 运行测试并提交中文 commit。

### Task 3: B7 首块嗅探

**Files:**
- Modify: `tests/test_server.py`
- Modify: `codex_jev_router/server.py`

1. 新增两个失败测试：200 无 Content-Type + JSON 对 stream 客户端返回 JSON；200 无 Content-Type + SSE 仍流式透传/非流式组装。
2. 运行测试，确认 JSON 被旧 `status == 200` 错当 SSE。
3. 在发下游 headers 前读首块；显式 SSE Content-Type 优先，否则只认 `event:`/`data:` 前缀；relay 支持预读块且不丢 tracker/capture/chunk 边界。
4. 运行测试并提交中文 commit。

### Task 4: B3 脏状态后台合并落盘

**Files:**
- Modify: `tests/test_config.py`, `tests/test_engine.py`（或新建 `tests/test_state.py`）
- Modify: `codex_jev_router/config.py`, `codex_jev_router/state.py`, `codex_jev_router/server.py`, `codex_jev_router/__main__.py`
- Modify: `config.example.toml`, `README.md`

1. 新增失败测试：put/update 不同步写；多个更新在一次 `flush()` 合并；后台间隔落盘；`close()` 强制刷新且幂等；server shutdown/close 触发 store close。
2. 运行测试确认失败。
3. `ThreadStateStore` 增加 dirty generation、条件变量与单后台线程；快照在锁内复制，文件 I/O 在锁外，写完只清理对应 generation；配置默认 2 秒；关闭时唤醒、join、最终同步 flush。
4. 运行测试并提交中文 commit。

### Task 5: s2 真实验证与文档

**Files:**
- Modify: `STATUS.md`
- Create: `docs/2026-09-21-结论落地.md`

1. 备份并记录 `~/.codex/config.toml` SHA-256。
2. 用 `bench/run_subagent.py` 仅运行 `s2_parallel_tests` live 3 遍；不改 0.5 门槛，核对父线程 raw tier/confidence、实际模型、通过率和 completed model。
3. 无论结果是否超过 0.5，都如实写前后对照；清理路由器、哨兵、工作副本并恢复配置哈希。
4. 更新 STATUS 默认参数表、T1–T4 结论、剩余问题与常驻部署差距。
5. 完整运行 `python3 -m unittest`、compileall、secret scan、端口/配置/工作树审计，提交中文 commit，并在终端输出指定完成语。
