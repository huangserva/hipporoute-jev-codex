# Mechanical Routing Benchmark Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development while implementing each behavior.

**Goal:** Build and run a repeatable 48-session benchmark comparing shadow astra against real Jev routing on six mechanical and two reasoning tasks.

**Architecture:** A standard-library Python driver owns router/config lifecycle, creates one isolated repository copy per session, slices router JSONL by file offset, applies automatic checks, and writes raw plus aggregate JSON. Router decisions expose the Jev Choice values and confidences needed by the benchmark.

**Tech Stack:** Python 3.11+ standard library, `unittest`, Codex CLI 0.155.1, local Responses router, JSON/JSONL.

---

### Task 1: Persist Jev confidence

**Files:** modify `hipporoute/engine.py`, `hipporoute/server.py`; test `tests/test_engine.py`, `tests/test_server.py`.

1. Add a failing test asserting a successful fake Jev response appears in `Decision.jev` with tier/depth choices and confidences.
2. Run the focused test and verify the missing field failure.
3. Add the observation to the decision and JSONL without recording state text or credentials.
4. Run focused and full tests.
5. Commit in Chinese.

### Task 2: Define tasks and pure benchmark helpers

**Files:** create `bench/tasks.json`, `bench/run.py`, `tests/test_bench.py`.

1. Add failing tests for balanced 48-run scheduling, cost calculation, fixture preparation, checks, log slicing, aggregation, config restoration, and infrastructure retry classification.
2. Run `python3 -m unittest -v tests.test_bench` and confirm failures.
3. Implement only pure helpers and lifecycle primitives needed by the tests.
4. Run focused and full tests.
5. Commit in Chinese.

### Task 3: Dry run

1. Back up `~/.codex/config.toml` through the driver.
2. Run one mechanical and one reasoning task once in both modes using `--dry-run` (4 sessions).
3. Verify key health, scheduling, task checks, decision confidence, completed-model evidence, raw output, and cleanup.
4. Fix any reproducible driver/router defect test-first and rerun dry run.

### Task 4: Full benchmark

1. Run `python3 bench/run.py --repeats 3` without reducing repetitions.
2. Poll progress; let the driver retry infrastructure failures at most twice.
3. Verify 48 effective results, 24 per mode, 6 observations per task, and no missing task/mode/repeat cells.
4. Confirm config hash, stopped router, removed sentinel, and deleted work directories.

### Task 5: Analyze and report

**Files:** create `docs/experiments/2026-09-19-机械任务基准.md`; update README/STATUS only if behavior changed.

1. Generate per-task medians/ranges, group savings, pass rates, mismatch list, latency and missing-completed counts from raw JSONL.
2. Write evidence-backed conclusions and limitations.
3. Run all tests, compileall, secret scan, cleanup assertions, and Git cleanliness checks.
4. Commit report and any verified fixes in Chinese.
