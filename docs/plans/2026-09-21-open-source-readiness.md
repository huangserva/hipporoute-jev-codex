# Open-source Readiness Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the repository safe, portable, documented, and testable for a public GitHub release without touching running services or user configuration.

**Architecture:** Keep runtime state outside Git, make credentials path-based, expose deterministic report filtering as pure functions, and separate public operational docs from sanitized experiment records. Treat reachable Git history as a distinct audit surface that is reported rather than rewritten.

**Tech Stack:** Python 3.11+ standard library, TOML, zsh, Git, unittest.

---

### Task 1: Audit and repository boundary

**Files:** Modify `.gitignore`; create `config.service.example.toml`; untrack `config.service.toml`; move dated reports to `docs/experiments/`.

1. Scan the current tree, ignored files, all reachable commits, exact local secret values, commit metadata, machine names, paths, emails, and thread identifiers without printing secret values.
2. Verify the compaction fixture contains only synthetic identifiers and the captured protocol shape.
3. Expand ignore rules and replace tracked service-local configuration with an example.
4. Re-run scans and record any history-only findings without rewriting history.

### Task 2: Key-file configuration

**Files:** Modify `codex_jev_router/config.py`, `codex_jev_router/jev.py`, `codex_jev_router/server.py`, `tests/test_config.py`, `tests/test_jev.py`, `config.example.toml`; create `.env.example`.

1. Write failing tests for environment → configured key file → default `~/.jev.env` precedence and rejection of literal `[jev].key`.
2. Run focused tests and confirm the expected failures.
3. Add `jev_key_file` to configuration and bind `build_app` to `load_key` with that path.
4. Run focused and related engine/server tests.

### Task 3: Time-window shadow reports

**Files:** Modify `scripts/shadow-report.py`, `tests/test_service_tools.py`.

1. Write failing tests for ISO parsing, `--since`, `--hours`, and invalid/conflicting options.
2. Implement timezone-aware row filtering before aggregation with an injectable `now` in the pure helper.
3. Run focused tests, then smoke-test `--hours 3` against the current ignored decision log.

### Task 4: Public documentation and project metadata

**Files:** Rewrite `README.md`; modify `STATUS.md`, scripts and experiment docs; create `LICENSE`, `CONTRIBUTING.md`, `CHANGELOG.md`, and `docs/2026-09-21-开源前审计.md`.

1. Remove machine-specific paths, proxy defaults, backup hashes, and private identity from the current tree.
2. Document direct and Codex Router deployment, key choices, modes, configuration, logs, benchmark limits, risks, and rollback.
3. Attribute the MIT-licensed upstream ideas precisely without claiming code identity.
4. Add release and contribution metadata with a copyright-holder placeholder.

### Task 5: Final verification

1. Run `python3 -m unittest` and `python3 -m compileall -q codex_jev_router scripts tests`.
2. Run `git diff --check`, fixture validation, ignore checks, current-tree secret scans, exact-secret comparisons, and full-history scans.
3. Confirm no process, service, or file under `~/.codex` was changed.
4. Commit in Chinese and print `开源整理完成`.
