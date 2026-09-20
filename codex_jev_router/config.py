"""TOML configuration for the boundary router."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .policy import ASTRA, EFFORTS, LUNA, SOL, Price


DEFAULT_STATE_DIR = Path("~/.codex/codex-jev-router").expanduser()


@dataclass(frozen=True)
class RouterConfig:
    listen_host: str
    listen_port: int
    max_request_body_bytes: int
    client_socket_timeout_seconds: float
    max_concurrent_requests: int
    upstream_mode: str
    direct_url: str
    caller_edge_url: str
    caller_secret_path: Path
    upstream_timeout_seconds: float
    state_path: Path
    decision_log_path: Path
    off_path: Path
    shadow_path: Path
    stream_debug_path: Path
    raw_stream_dir: Path
    jev_url: str
    jev_model: str
    jev_timeout_seconds: float
    jev_retries: int
    jev_backoff_seconds: float
    jev_retry_after_cap_seconds: float
    jev_circuit_failure_threshold: int
    jev_circuit_open_seconds: float
    confidence_gate: float
    downgrade_max_context_tokens: int
    switch_budget_usd: float
    chars_per_token: float
    luna_effort: str
    state_ttl_seconds: int
    state_gc_interval_seconds: int
    prices: dict[str, Price]
    summary_marker: bool


DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 4319,
        "max_request_body_bytes": 16 * 1024 * 1024,
        "client_socket_timeout_seconds": 30.0,
        "max_concurrent_requests": 64,
    },
    "upstream": {
        "mode": "direct",
        "direct_url": "https://chatgpt.com/backend-api/codex",
        "caller_edge_url": "http://127.0.0.1:4202",
        "caller_secret_path": "~/.codex/codex-router/caller-secret",
        "timeout_seconds": 900.0,
    },
    "paths": {
        "state": str(DEFAULT_STATE_DIR / "threads.json"),
        "decision_log": str(DEFAULT_STATE_DIR / "decisions.jsonl"),
        "off": str(DEFAULT_STATE_DIR / "router.off"),
        "shadow": str(DEFAULT_STATE_DIR / "router.shadow"),
        "stream_debug": str(DEFAULT_STATE_DIR / "stream.debug"),
        "raw_stream_dir": str(DEFAULT_STATE_DIR / "raw-streams"),
    },
    "jev": {
        "url": "https://api.typesafe.ai/v1/systemone",
        "model": "jev-latest",
        "timeout_seconds": 4.0,
        "retries": 2,
        "backoff_seconds": 0.25,
        "retry_after_cap_seconds": 4.0,
        "circuit_failure_threshold": 3,
        "circuit_open_seconds": 60.0,
        "confidence_gate": 0.5,
    },
    "routing": {
        "downgrade_max_context_tokens": 20_000,
        "switch_budget_usd": 0.25,
        "chars_per_token": 2.8,
        "luna_effort": "low",
        "state_ttl_seconds": 86_400,
        "state_gc_interval_seconds": 300,
        "summary_marker": False,
    },
    "prices": {
        ASTRA: {"input": 10.0, "cached_input": 1.0, "output": 50.0, "cache_write": 12.5},
        SOL: {"input": 4.0, "cached_input": 0.4, "output": 20.0, "cache_write": 5.0},
        LUNA: {"input": 0.2, "cached_input": 0.02, "output": 1.2, "cache_write": 0.25},
    },
}


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _value(data: dict[str, Any], section: str, key: str) -> Any:
    return _section(data, section).get(key, DEFAULTS[section][key])


def _path(value: Any) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def load_config(path: str | Path | None) -> RouterConfig:
    data: dict[str, Any] = {}
    if path is not None:
        with open(path, "rb") as handle:
            decoded = tomllib.load(handle)
        if not isinstance(decoded, dict):
            raise ValueError("configuration root must be a TOML table")
        data = decoded

    price_data = _section(data, "prices")
    prices = {}
    for model, defaults in DEFAULTS["prices"].items():
        raw = price_data.get(model)
        raw = raw if isinstance(raw, dict) else {}
        prices[model] = Price(
            cache_read=float(raw.get("cached_input", defaults["cached_input"])),
            cache_write=float(raw.get("cache_write", defaults["cache_write"])),
            input=float(raw.get("input", defaults["input"])),
            output=float(raw.get("output", defaults["output"])),
        )

    upstream_mode = str(_value(data, "upstream", "mode"))
    if upstream_mode not in ("direct", "caller_edge"):
        raise ValueError("upstream.mode must be direct or caller_edge")
    luna_effort = str(_value(data, "routing", "luna_effort"))
    if luna_effort not in EFFORTS:
        raise ValueError(f"routing.luna_effort must be one of {EFFORTS}")
    max_request_body_bytes = int(_value(data, "server", "max_request_body_bytes"))
    client_socket_timeout_seconds = float(
        _value(data, "server", "client_socket_timeout_seconds")
    )
    max_concurrent_requests = int(_value(data, "server", "max_concurrent_requests"))
    for key, value in (
        ("max_request_body_bytes", max_request_body_bytes),
        ("client_socket_timeout_seconds", client_socket_timeout_seconds),
        ("max_concurrent_requests", max_concurrent_requests),
    ):
        if value <= 0:
            raise ValueError(f"server.{key} must be positive")
    return RouterConfig(
        listen_host=str(_value(data, "server", "host")),
        listen_port=int(_value(data, "server", "port")),
        max_request_body_bytes=max_request_body_bytes,
        client_socket_timeout_seconds=client_socket_timeout_seconds,
        max_concurrent_requests=max_concurrent_requests,
        upstream_mode=upstream_mode,
        direct_url=str(_value(data, "upstream", "direct_url")),
        caller_edge_url=str(_value(data, "upstream", "caller_edge_url")),
        caller_secret_path=_path(_value(data, "upstream", "caller_secret_path")),
        upstream_timeout_seconds=float(_value(data, "upstream", "timeout_seconds")),
        state_path=_path(_value(data, "paths", "state")),
        decision_log_path=_path(_value(data, "paths", "decision_log")),
        off_path=_path(_value(data, "paths", "off")),
        shadow_path=_path(_value(data, "paths", "shadow")),
        stream_debug_path=_path(_value(data, "paths", "stream_debug")),
        raw_stream_dir=_path(_value(data, "paths", "raw_stream_dir")),
        jev_url=str(_value(data, "jev", "url")),
        jev_model=str(_value(data, "jev", "model")),
        jev_timeout_seconds=float(_value(data, "jev", "timeout_seconds")),
        jev_retries=int(_value(data, "jev", "retries")),
        jev_backoff_seconds=float(_value(data, "jev", "backoff_seconds")),
        jev_retry_after_cap_seconds=float(_value(data, "jev", "retry_after_cap_seconds")),
        jev_circuit_failure_threshold=int(_value(data, "jev", "circuit_failure_threshold")),
        jev_circuit_open_seconds=float(_value(data, "jev", "circuit_open_seconds")),
        confidence_gate=float(_value(data, "jev", "confidence_gate")),
        downgrade_max_context_tokens=int(_value(data, "routing", "downgrade_max_context_tokens")),
        switch_budget_usd=float(_value(data, "routing", "switch_budget_usd")),
        chars_per_token=float(_value(data, "routing", "chars_per_token")),
        luna_effort=luna_effort,
        state_ttl_seconds=int(_value(data, "routing", "state_ttl_seconds")),
        state_gc_interval_seconds=int(_value(data, "routing", "state_gc_interval_seconds")),
        prices=prices,
        summary_marker=bool(_value(data, "routing", "summary_marker")),
    )
