# Contributing

Issues and focused pull requests for HippoRoute-Jev-Codex are welcome. Include
the Codex CLI and Codex Router versions used, because private protocol fields
may change.

Before submitting:

```bash
python3 -m unittest -v
python3 -m compileall -q hipporoute bench tests scripts
```

Never commit API keys, caller secrets, authorization headers, raw captures,
decision JSONL, thread state, or machine-specific configuration. Tests must use
fake Jev and fake upstream implementations. Add a failing test before changing
routing behavior, and document fail-open behavior.
