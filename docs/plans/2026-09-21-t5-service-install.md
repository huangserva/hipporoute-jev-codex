# T5 Service Installation Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Install codex-jev-router as a persistent launchd shadow service with safe Codex configuration enable/disable scripts and a one-week shadow report.

**Architecture:** launchd owns the router process and explicit proxy environment. A small standard-library Python helper performs reversible, hash-guarded config changes, while shell entrypoints orchestrate health-first enable and reverse-order disable. A separate Python analyzer aggregates redacted decision JSONL without contacting external services.

**Tech Stack:** zsh/POSIX shell, Python 3.11+ standard library, launchd plist, unittest, JSONL/TOML text transformation.

---

### Task 1: Reversible Codex configuration

**Files:**
- Create: `scripts/configure-codex.py`
- Create: `tests/test_service_tools.py`

1. Write failing tests for first enable, repeated enable, exact restore, and changed-config refusal.
2. Run `python3 -m unittest tests.test_service_tools -v` and confirm the helper import fails.
3. Implement atomic provider insertion plus timestamped backup/state metadata and guarded restore.
4. Re-run the focused tests and confirm they pass.
5. Commit with a Chinese message.

### Task 2: launchd and shell lifecycle

**Files:**
- Create: `scripts/install-service.sh`
- Create: `scripts/enable.sh`
- Create: `scripts/disable.sh`
- Create: `scripts/watchdog.sh`
- Modify: `tests/test_service_tools.py`

1. Add failing static/behavior tests for plist label, proxy, KeepAlive, log paths, health-before-config ordering, shadow and idempotent disable.
2. Run the focused test and confirm expected failures.
3. Implement the scripts with explicit paths, health polling and no credential serialization.
4. Re-run focused tests; then run `zsh -n scripts/*.sh`.
5. Commit with a Chinese message.

### Task 3: Shadow report

**Files:**
- Create: `scripts/shadow-report.py`
- Modify: `tests/test_service_tools.py`

1. Add a synthetic JSONL test for daily root-session counts, decision/would/confidence/error/latency metrics, repriced savings and missing usage.
2. Run it and confirm the missing module/function failure.
3. Implement JSON and Markdown output using only the standard library.
4. Run the focused test and run the script against existing benchmark logs.
5. Commit with a Chinese message.

### Task 4: Documentation and real installation

**Files:**
- Modify: `README.md`
- Modify: `STATUS.md`
- Create: `docs/2026-09-21-T5-安装.md`
- Create at runtime: `~/Library/LaunchAgents/com.jev.codex-jev-router.plist`
- Modify with backup retained: `~/.codex/config.toml`

1. Document install, GUI verification, emergency operations, reporting semantics and limitations.
2. Run the full unit suite.
3. Run `scripts/install-service.sh`, verify launchd and `/health` with `jev_key=true`, then run `scripts/enable.sh` and verify shadow/config state.
4. Run one real CLI request and prove a new shadow decision has `gate=apply|hold`, `shadow=true`, and unchanged upstream astra.
5. Re-run the full suite, inspect git diff/secrets/status, commit documentation and record exact verification evidence.
