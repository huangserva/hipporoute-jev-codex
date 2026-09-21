# Code Review P0/P1 Fixes Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复代码评审指定的 P0/P1 与可顺手完成的 P2，并用真实 Codex 0.155.1 压缩请求固定外部协议契约。

**Architecture:** 保持现有标准库 HTTP 路由器结构不变：协议判定留在 `policy.py`，路由状态与 Jev 故障状态留在 `engine.py`，HTTP 响应生命周期留在 `server.py`。每个评审编号独立走 RED→GREEN→全量回归→中文提交；真实抓包只保存脱敏 fixture，原始含凭据数据留在 gitignored runtime。

**Tech Stack:** Python 3.11+ 标准库、`unittest`、Codex CLI 0.155.1、JSONL/SSE。

---

### Task 1: A1 真实压缩协议与判定

**Files:**
- Create: `tests/fixtures/codex-0.155.1-compaction-request.json`
- Modify: `tests/test_policy.py`, `tests/test_engine.py`
- Modify: `hipporoute/policy.py`

1. 备份 `~/.codex/config.toml` 并记录 SHA-256；用 gitignored runtime 目录捕获真实请求。
2. 以低 `auto_compact_token_limit` 驱动真实 `codex exec`，确认压缩请求 header metadata 与任务开头。
3. 从原始请求生成只含判定字段与截断任务文本的脱敏 fixture，并确认没有认证信息。
4. 改测试为读取 fixture，先运行并观察旧判定失败。
5. 实现 metadata 优先、真实前缀兜底；运行定向与全量测试。
6. 恢复配置并校验 SHA-256，提交 `修复A1：压缩判定改用请求元数据`。

### Task 2: A3 SSE 已发头后的异常边界

**Files:**
- Modify: `tests/test_server.py`
- Modify: `hipporoute/server.py`

1. 增加上游中途抛 `IncompleteRead`、状态/日志写失败的 socket 级测试，断言响应字节只出现一个 HTTP 状态行；先确认失败。
2. 增加 `headers_sent` 状态；发头后异常只记捕获事件、关闭连接，禁止二次 `_json`。
3. 将 `finish_record` 的 usage/state/log I/O 隔离为不影响响应的 best-effort 记录。
4. 运行定向与全量测试，提交 `修复A3：已发响应头后禁止二次响应`。

### Task 3: A2 同模型 effort 成本

**Files:**
- Modify: `tests/test_policy.py`
- Modify: `hipporoute/policy.py`

1. 把原错误断言改为高上下文同模型 effort 变化允许切换；确认失败。
2. 同模型时返回允许且零重建成本；确认不同模型分支不变。
3. 运行测试，提交 `修复A2：同模型effort变化跳过缓存重建成本`。

### Task 4: A5 缺 usage 的上下文估算

**Files:**
- Modify: `tests/test_server.py`
- Modify: `hipporoute/server.py`

1. 增加无 completed/usage 流的状态与日志断言；确认旧代码不更新且缺 `context_source`。
2. 以本请求字符数和配置比例估算，回填线程状态；日志记录 `usage` 或 `estimate`。
3. 运行测试，提交 `修复A5：缺usage时回填上下文估算`。

### Task 5: B1 Jev 重试与熔断

**Files:**
- Modify: `tests/test_jev.py`, `tests/test_engine.py`, `tests/test_config.py`
- Modify: `hipporoute/jev.py`, `hipporoute/engine.py`, `hipporoute/config.py`, `config.example.toml`

1. 测试 400/401 不重试、429 按封顶后的 Retry-After 重试、5xx/网络异常仍指数退避。
2. 测试连续 N 次 Jev 失败后 M 秒内不调用 key/Jev，gate 为 `jev_circuit_open`，到期后半开重试。
3. 加入最小配置项与线程安全熔断状态；成功调用重置失败计数。
4. 运行测试，提交 `修复B1：区分Jev重试并增加熔断`。

### Task 6: A4 非流式缺 completed 返回 502

**Files:**
- Modify: `tests/test_server.py`
- Modify: `hipporoute/server.py`

1. 增加非流式请求收到无 completed SSE 的集成测试，断言 502 JSON；确认旧代码失败。
2. `assemble_sse` 返回 `None` 时生成诊断明确的 502 JSON，保持记录真实上游状态。
3. 运行测试，提交 `修复A4：非流式缺completed返回502`。

### Task 7: P2 防御性修复

**Files:**
- Modify: `tests/test_engine.py`, `tests/test_state.py`, `tests/test_upstream.py`, `tests/test_server.py`
- Modify: `hipporoute/engine.py`, `hipporoute/state.py`, `hipporoute/upstream.py`, `hipporoute/server.py`

1. 分别为 B2 key loader 异常、B5 锁 GC、B9 getresponse 失败关连接、B8 非法 Content-Length 写失败测试。
2. 逐项最小修复并运行定向测试。
3. 运行全量测试，提交 `修复P2：补强密钥加载资源回收与请求校验`。

### Task 8: 文档与最终验证

**Files:**
- Create: `docs/experiments/2026-09-20-评审修复.md`
- Modify: `STATUS.md`

1. 记录每条修法、红绿证据、真实压缩 fixture 来源、剩余已知问题。
2. 更新 STATUS 的已修/未修/准确测试总数。
3. 运行完整 unittest、compileall、secret 扫描、配置 SHA 校验、进程/哨兵清理检查。
4. 提交 `记录评审修复结果与验证证据`，终端输出 `评审修复完成`。
