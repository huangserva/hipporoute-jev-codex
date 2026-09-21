#!/usr/bin/env python3
"""Idempotently publish the Codex + Jev model in Codex Router's user catalog."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


MODEL = {
    "slug": "jev/auto",
    "gatewayModel": "jev-auto",
    "compHash": "jev-auto-user-v1",
    "upstreamModel": "auto",
    "provider": "jev",
    "listed": True,
    "displayName": "Codex + Jev Router",
    "description": "Thread-pinned model and effort routing by Jev.",
    "priority": 95,
    "defaultEffort": "medium",
    "reasoningLevels": [
        {"effort": "low", "description": "Quick reasoning"},
        {"effort": "medium", "description": "Balanced reasoning"},
        {"effort": "high", "description": "Deep reasoning"},
        {"effort": "xhigh", "description": "Extended reasoning"},
        {"effort": "max", "description": "Maximum reasoning"},
    ],
    "contextWindow": 258400,
    "autoCompact": 219640,
    "inputModalities": ["text", "image"],
}


def upsert(path: Path) -> None:
    path = Path(path).expanduser()
    if path.exists():
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("models"), list):
            raise ValueError(f"invalid Codex Router user catalog: {path}")
    else:
        document = {"version": 1, "models": []}
    models = [item for item in document["models"] if item.get("slug") != MODEL["slug"]]
    models.append(MODEL)
    document["version"] = 1
    document["models"] = models
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path.home() / ".codex/codex-router/user-models.json",
    )
    arguments = parser.parse_args()
    upsert(arguments.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
