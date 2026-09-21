# Changelog

All notable changes to HippoRoute-Jev-Codex are documented here.

## Unreleased

- Renamed the project from `codex-jev-router` to **HippoRoute-Jev-Codex**:
  repository and directory `hipporoute-jev-codex`, Python package `hipporoute`,
  launchd label `com.hippo.hipporoute`, model-picker entry `HippoRoute (Jev)`,
  and default state directory `~/.codex/hipporoute`. The Codex Router provider id
  remains `jev`, so no provider re-registration is needed. See the migration
  section in [README.md](README.md).

## 0.1.0 - 2026-09-21

- Thread-boundary model routing for Codex Responses traffic.
- Independent sub-agent routing, sticky tool continuations, compaction handling,
  and cost-aware switch gates.
- Direct ChatGPT and Codex Router caller-edge upstream modes.
- Shadow/off sentinels, JSONL decisions, state persistence, SSE passthrough,
  bounded retries/circuit breaking, resource limits, and launchd helpers.
- Reproducible benchmark drivers, shadow reporting, and sanitized fixtures.
