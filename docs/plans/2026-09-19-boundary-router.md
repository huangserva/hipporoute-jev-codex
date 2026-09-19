# Codex + Jev Boundary Router Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a zero-dependency local Responses proxy that pins model/effort per Codex thread and consults Jev only at the four authorized decision points.

**Architecture:** Pure policy and parsing functions feed a stateful `RouterEngine`; an injectable Jev client supplies candidate routes; a standard-library HTTP handler forwards forced-stream requests and either relays SSE chunks or assembles JSON. Thread state and decision logs persist locally with atomic writes.

**Tech Stack:** Python 3.11+ standard library, `unittest`, `http.server`, `http.client`, `urllib.request`, TOML configuration.

---

### Task 1: Configuration, request parsing, and policy

**Files:**
- Create: `config.example.toml`
- Create: `codex_jev_router/config.py`
- Create: `codex_jev_router/policy.py`
- Create: `tests/test_policy.py`

1. Write fixtures and failing tests for identifier precedence/conflict, step classification, first/new/tool/compaction/subagent events, confidence fallback, and switch-cost gates.
2. Run `python -m unittest -v tests.test_policy` and confirm missing imports/functions fail.
3. Implement dataclasses and pure functions only.
4. Run the policy tests to green.
5. Commit with a Chinese message.

### Task 2: Jev client and fail-open engine

**Files:**
- Create: `codex_jev_router/jev.py`
- Create: `codex_jev_router/state.py`
- Create: `codex_jev_router/engine.py`
- Create: `tests/test_engine.py`

1. Write failing tests for no-key fail-open, confidence below 0.5 to sol, three total attempts with exponential delays, state pinning, free reroute, shadow, kill switch, and atomic reload.
2. Run the focused test module and confirm expected failures.
3. Implement key loading, questions/state construction, answer validation, retry, state persistence, and routing orchestration.
4. Run both focused and full unit suites.
5. Commit with a Chinese message.

### Task 3: SSE, upstreams, and HTTP endpoints

**Files:**
- Create: `codex_jev_router/sse.py`
- Create: `codex_jev_router/upstream.py`
- Create: `codex_jev_router/server.py`
- Create: `codex_jev_router/__init__.py`
- Create: `codex_jev_router/__main__.py`
- Create: `tests/test_server.py`

1. Write failing tests for `assemble_sse`, usage extraction, streamed byte relay/chunking, non-stream assembly, forced upstream stream, health/models, and HTTPS proxy CONNECT construction.
2. Run focused tests and confirm failures.
3. Implement minimal forwarding and endpoints; leave SummaryMarker disabled by default.
4. Run all tests and compile the package.
5. Commit with a Chinese message.

### Task 4: Documentation and real direct-mode verification

**Files:**
- Create: `README.md`
- Create: `STATUS.md`
- Create: `.gitignore`
- Temporarily modify and restore: `~/.codex/config.toml`

1. Document direct and Codex Router provider setup, flags, config, state and log schema.
2. Back up `~/.codex/config.toml`, start direct mode, and temporarily select the local provider.
3. Run a real first turn with tools/subagent, then resume the same thread for a second user turn.
4. Restore config byte-for-byte and stop the server.
5. Verify decision log contains `no_key` for main first/new-turn and child first decisions while tool continuations reuse state.
6. Add a redacted log excerpt and actual limitations to README/STATUS.
7. Run full tests, secret scan, repository diff review, and commit in Chinese.
