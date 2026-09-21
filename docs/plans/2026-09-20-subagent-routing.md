# Subagent Tier Routing Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Route every native Codex subagent independently from its visible delegation context, then pin that tier for the subagent lifetime while making state concurrency-safe and expirable.

**Architecture:** Extend identity and request inspection with delegation evidence, construct a subagent-specific Jev state without inheriting the parent model, serialize each thread's decision transaction with a keyed lock, and conservatively garbage-collect persisted state. Add a separate repeatable fan-out benchmark that aggregates parent and child usage independently.

**Tech Stack:** Python 3.11+ standard library, `unittest`, Codex CLI 0.155.1, Responses SSE.

---

### Task 1: Freeze capture evidence and delegation extraction contract

**Files:**
- Modify: `hipporoute/policy.py`
- Modify: `tests/test_policy.py`

**Steps:**
1. Add failing fixtures for plaintext `NEW_TASK Payload`, plaintext spawn arguments, and `gAAAAA…` encrypted arguments.
2. Run `python3 -m unittest -v tests.test_policy` and verify failures are due to missing extraction APIs.
3. Implement `SpawnDelegation` extraction and encrypted-message rejection.
4. Re-run the focused tests and commit.

### Task 2: Add subagent identity/state and dedicated Jev context

**Files:**
- Modify: `hipporoute/policy.py`
- Modify: `hipporoute/state.py`
- Modify: `hipporoute/engine.py`
- Modify: `hipporoute/jev.py`
- Modify: `tests/test_engine.py`
- Modify: `tests/test_jev.py`

**Steps:**
1. Add failing tests asserting `agent_name`, `subagent_kind`, `parent_tier`, independent candidate selection, fallback source, and delegated-task question instructions.
2. Verify focused tests fail for missing fields/behavior.
3. Implement the minimum identity, state, task-resolution, and Jev question changes.
4. Verify focused and full suites, then commit.

### Task 3: Serialize per-thread decisions and add TTL/GC

**Files:**
- Modify: `hipporoute/config.py`
- Modify: `config.example.toml`
- Modify: `hipporoute/state.py`
- Modify: `hipporoute/engine.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_engine.py`

**Steps:**
1. Add a blocking FakeJev concurrency test that observes two simultaneous first requests and expects one Jev call.
2. Add GC tests for expired, active, legacy, and invalid timestamps plus default/override config tests.
3. Verify red failures.
4. Implement keyed locks, activity touch, interval GC, and default 24-hour TTL.
5. Verify focused and full suites, then commit.

### Task 4: Observe spawn calls and enrich decision logs

**Files:**
- Modify: `hipporoute/sse.py`
- Modify: `hipporoute/server.py`
- Modify: `tests/test_sse.py`
- Modify: `tests/test_server.py`

**Steps:**
1. Add failing tests for spawn observation across arbitrary SSE chunks and parent/child decision log fields.
2. Verify failures.
3. Teach the SSE tracker to collect safe spawn delegation metadata and feed it to the engine; log routing task/source and parent chain.
4. Verify focused and full suites, then commit.

### Task 5: Build the fan-out benchmark driver

**Files:**
- Create: `bench/tasks-subagent.json`
- Create: `bench/run_subagent.py`
- Create: `tests/test_bench_subagent.py`

**Steps:**
1. Add failing tests for 24-cell scheduling, parent/child grouping, model/cost aggregation, and required child counts.
2. Verify red failures.
3. Implement four isolated fan-out tasks and a driver reusing safe config, workspace, pricing, retry, and cleanup helpers from `bench/run.py`.
4. Verify tests and dry-run one task in both modes.
5. Fix only evidence-backed failures with a regression test, then commit.

### Task 6: Run the real benchmark and report

**Files:**
- Create: `docs/experiments/2026-09-20-子agent分档基准.md`
- Modify: `README.md`
- Modify: `STATUS.md`

**Steps:**
1. Run `python3 bench/run_subagent.py --repeats 3 --run-id phase2-20260920` without reducing repetitions.
2. Assert 24 unique sessions, expected child counts, completed-model evidence, explicit missing streams, pass rates, and cleanup.
3. Write the report with validation evidence, parent/child cost tables, Jev comparisons, latency, limitations, and cleanup hash.
4. Run the full verification suite, secret scan, Git-ignore/config/port/sentinel assertions.
5. Commit in Chinese and print `阶段二完成`.

