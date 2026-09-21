#!/usr/bin/env python3
"""Safely enable or restore the Codex provider configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any


PROVIDER = "hipporoute"
PROVIDER_HEADER = f"[model_providers.{PROVIDER}]"
PROVIDER_BLOCK = f'''{PROVIDER_HEADER}
name = "HippoRoute (Jev)"
base_url = "http://127.0.0.1:4319/v1"
wire_api = "responses"
requires_openai_auth = true
'''


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def render_enabled_config(original: str) -> str:
    lines = original.splitlines(keepends=True)
    output: list[str] = []
    in_target = False
    in_table = False
    wrote_provider = False
    section_pattern = re.compile(r"^\s*\[.*]\s*(?:#.*)?$")
    provider_pattern = re.compile(r"^\s*model_provider\s*=")

    for line in lines:
        stripped = line.strip()
        if section_pattern.match(line):
            in_target = stripped.split("#", 1)[0].strip() == PROVIDER_HEADER
            in_table = True
            if in_target:
                continue
        if in_target:
            continue
        if not in_table and provider_pattern.match(line):
            if not wrote_provider:
                output.append(f'model_provider = "{PROVIDER}"\n')
                wrote_provider = True
            continue
        output.append(line)

    if not wrote_provider:
        output.insert(0, f'model_provider = "{PROVIDER}"\n')
    rendered = "".join(output).rstrip() + "\n\n" + PROVIDER_BLOCK
    return rendered


def enable_config(
    config_path: Path,
    state_path: Path,
    *,
    backup_dir: Path | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    config_path = Path(config_path).expanduser()
    state_path = Path(state_path).expanduser()
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        current = config_path.read_bytes() if config_path.exists() else b""
        if _sha256(current) != state.get("enabled_sha256"):
            raise RuntimeError("Codex config changed since enable; refusing to overwrite it")
        return state

    existed = config_path.exists()
    original = config_path.read_bytes() if existed else b""
    stamp = timestamp or time.strftime("%Y%m%d-%H%M%S")
    destination = (backup_dir or config_path.parent).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    backup_path = destination / f"{config_path.name}.backup-hipporoute-{stamp}"
    if backup_path.exists():
        raise FileExistsError(backup_path)
    _atomic_write(backup_path, original)

    enabled = render_enabled_config(original.decode("utf-8")).encode("utf-8")
    mode = (config_path.stat().st_mode & 0o777) if existed else 0o600
    _atomic_write(config_path, enabled, mode)
    state = {
        "version": 1,
        "config_path": str(config_path),
        "config_existed": existed,
        "backup_path": str(backup_path),
        "original_sha256": _sha256(original),
        "enabled_sha256": _sha256(enabled),
        "enabled_at": stamp,
    }
    _atomic_write(
        state_path,
        (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return state


def restore_config(config_path: Path, state_path: Path, *, force: bool = False) -> dict[str, Any]:
    config_path = Path(config_path).expanduser()
    state_path = Path(state_path).expanduser()
    if not state_path.exists():
        return {"restored": False, "reason": "not_enabled"}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    current = config_path.read_bytes() if config_path.exists() else b""
    if not force and _sha256(current) != state.get("enabled_sha256"):
        raise RuntimeError(
            "Codex config changed since enable; refusing to overwrite it. "
            f"Original backup: {state.get('backup_path')}"
        )
    backup = Path(state["backup_path"])
    original = backup.read_bytes()
    if _sha256(original) != state.get("original_sha256"):
        raise RuntimeError("Codex config backup checksum mismatch")
    if state.get("config_existed", True):
        mode = (config_path.stat().st_mode & 0o777) if config_path.exists() else 0o600
        _atomic_write(config_path, original, mode)
    else:
        config_path.unlink(missing_ok=True)
    state_path.unlink()
    return {"restored": True, "backup_path": str(backup)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("enable", "restore"))
    parser.add_argument("--config", type=Path, default=Path("~/.codex/config.toml").expanduser())
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.action == "enable":
        result = enable_config(args.config, args.state, backup_dir=args.backup_dir)
    else:
        result = restore_config(args.config, args.state, force=args.force)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
