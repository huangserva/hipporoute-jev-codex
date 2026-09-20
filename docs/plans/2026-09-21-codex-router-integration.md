# Codex Router Integration Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Install Codex Router, publish `jev/auto`, switch codex-jev-router to caller edge, and preserve shadow mode with reversible operations.

**Architecture:** Codex Router owns user Codex managed blocks, the caller capability, provider registry and merged catalog. codex-jev-router reads a secret path at runtime and is managed by its existing LaunchAgent. Repository scripts orchestrate public CLI commands rather than editing router-owned state.

**Tech Stack:** Codex Router Node/Python service, launchd, Python 3.11+ standard library, zsh, unittest, JSON/TOML.

---

### Task 1: Install and validate Codex Router

1. Confirm the pre-install backup hash.
2. Run the idle/no-discovery installer from the reference checkout.
3. Verify service status, public health, doctor and managed config shape.
4. Send a native `Say OK` through Codex and retain only redacted evidence.

### Task 2: Enable caller edge and switch Jev upstream

1. Enable and verify `chatgpt-session` without printing the secret.
2. Add a caller-edge service config containing only the secret file path.
3. Write a failing config/launchd test, then update the installer/plist arguments.
4. Restart and verify both health endpoints and shadow.

### Task 3: Register and publish `jev/auto`

1. Register `jev` with `providers generic add` and verify list output.
2. Add/merge the documented model object in `user-models.json` with mode 0600.
3. Refresh the catalog, show the picker entry, and run doctor.

### Task 4: End-to-end caller verification

1. Record the Jev decision-log offset.
2. POST an SSE `Say OK` to caller edge with model `jev/auto` without printing capability.
3. Verify completed stream and the new shadow decision fields.
4. Run `codex exec -m jev/auto` and verify another completed decision.

### Task 5: Adapt lifecycle scripts with TDD

1. Write failing tests proving enable/disable no longer modify `model_provider` and use Router CLI.
2. Implement idempotent catalog merge helper and new shell orchestration.
3. Run focused tests and commit.

### Task 6: Document and finalize

1. Write installation evidence, GUI checklist, upgrade checks and full rollback.
2. Update README and STATUS.
3. Run all tests, shell syntax, service/config/catalog and secret-leak checks.
4. Commit and leave both services running with shadow present.
