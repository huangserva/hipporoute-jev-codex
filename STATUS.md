# HippoRoute-Jev-Codex status

Version target: **0.1.0 public preview**. The router, benchmarks, launchd helpers,
and offline test suite are implemented. The author is running a shadow soak;
public users should also begin in shadow mode and inspect their own traffic before
enabling live routing.

## Current defaults

| Area | Default |
|---|---|
| tiers | `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-luna` |
| Luna effort | `low`, `service_tier=priority` |
| confidence gate | `0.5`; low confidence falls back to Sol |
| downgrade context limit | 20,000 tokens |
| switch premium budget | USD 0.25 |
| Jev resilience | 4 s attempt timeout, 2 retries, 0.25 s exponential base; 3 failures open circuit for 60 s |
| state lifecycle | 24 h TTL, 300 s GC, 2 s merged flush |
| HTTP limits | 16 MiB request, 30 s client socket, 64 concurrent POSTs |

All values are configurable in [config.example.toml](config.example.toml).

## Verified behavior

- Boundary-only routing, sticky tool continuations, cost-gated user-turn changes,
  compaction handling, independent sub-agent state, and fail-open behavior.
- Direct ChatGPT and Codex Router caller-edge paths, SSE passthrough, chunked
  requests, non-stream assembly, response-model evidence, and missing-usage
  estimation.
- Per-thread locking, state TTL/GC, merged atomic persistence, Jev retry/circuit
  breaker, body/socket/concurrency limits, kill switch, shadow mode, and raw
  stream diagnostics.
- A real Codex 0.155.1 compaction shape is retained as a sanitized fixture.
- Controlled author experiments observed 95.36% lower mechanical-task cost,
  79.74% lower fan-out parent+child cost, no child-context growth across the
  tested 30k–150k parent range, and 44.6% lower Luna wall time at low effort.
  See [the experiment archive](docs/experiments/README.md); these small samples
  are not universal guarantees.

## Known limitations

- Codex private headers and turn metadata are not stable public APIs.
- Observed sub-agent delegation text is encrypted; agent names and parent
  context may be the only routing signal.
- Codex Router's generic provider currently strips some identity fields, so its
  compatibility fallback must be retested after upgrades.
- Up to 500 task characters are sent to TypeSafe Jev.
- Loopback HTTP has no authentication. Never bind it to a public interface.
- Decisions and thread state are sensitive local data; automatic log rotation is
  not implemented.
- State schema migration/warnings, explicit compressed-response handling, crash
  recovery under disk-full conditions, and large-scale soak tests remain open.
- `SummaryMarker` and a Codex-dry fallback tier are not implemented.

## Before calling it production-ready

1. Complete a multi-day shadow soak and review `shadow-report.py` output.
2. Add log rotation/retention and state schema migration diagnostics.
3. Add optional loopback authentication or an equivalent local trust boundary.
4. Test abrupt termination, disk-full recovery, concurrency saturation, and
   tens of thousands of thread states.
5. Re-run real protocol fixtures after every Codex CLI/Codex Router upgrade.
6. Expand quality evaluation beyond automatic checks and one account/machine.

## Maintainer verification

```bash
python3 -m unittest -v
python3 -m compileall -q hipporoute bench tests scripts
```

The exact passing test count is recorded in the latest release/audit report,
not hard-coded here, because it changes with each regression test.
