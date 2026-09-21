#!/usr/bin/env python3
"""Summarize shadow decision JSONL by local log date."""

from __future__ import annotations

import argparse
import glob
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


PRICES = {
    "gpt-6-astra": {"input": 10.0, "cached_input": 1.0, "output": 50.0},
    "gpt-5.6-sol": {"input": 4.0, "cached_input": 0.4, "output": 20.0},
    "gpt-5.6-luna": {"input": 0.2, "cached_input": 0.02, "output": 1.2},
}
ROUTING_EVENTS = {"first_request", "new_user_turn", "compaction", "subagent_first"}


def _tier(model: Any) -> str | None:
    if not isinstance(model, str):
        return None
    for name in ("astra", "sol", "luna"):
        if model.endswith(name):
            return name
    return model


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _stats(values: list[float]) -> dict[str, float | None]:
    return {
        "median": statistics.median(values) if values else None,
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def _confidence_bin(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "missing"
    if value < 0.35:
        return "0.00-0.35"
    if value < 0.50:
        return "0.35-0.50"
    if value < 0.75:
        return "0.50-0.75"
    return "0.75-1.00"


def _cost(row: dict[str, Any], model: Any) -> float | None:
    usage = row.get("usage")
    if not row.get("response_completed") or model not in PRICES or not isinstance(usage, dict):
        return None
    values = [usage.get(name) for name in ("input_tokens", "cached_tokens", "output_tokens")]
    if not all(isinstance(value, int) and value >= 0 for value in values):
        return None
    input_tokens, cached_tokens, output_tokens = values
    rate = PRICES[model]
    uncached = max(0, input_tokens - cached_tokens)
    return (
        uncached * rate["input"]
        + cached_tokens * rate["cached_input"]
        + output_tokens * rate["output"]
    ) / 1_000_000


def _read_rows(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    malformed = 0
    for path in paths:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    malformed += 1
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    malformed += 1
    return rows, malformed


def _root_thread(thread_id: str, parents: dict[str, str]) -> str:
    seen: set[str] = set()
    current = thread_id
    while current in parents and current not in seen:
        seen.add(current)
        current = parents[current]
    return current


def parse_iso_timestamp(text: str) -> datetime:
    """Parse an ISO-8601 instant, accepting ``Z`` and local naive values."""
    normalized = text.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    value = datetime.fromisoformat(normalized)
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return value


def resolve_filters(
    *,
    since_text: str | None,
    hours: float | None,
    days: int | None,
    now: datetime | None = None,
) -> tuple[datetime | None, str | None]:
    selected = sum(value is not None for value in (since_text, hours, days))
    if selected > 1:
        raise ValueError("use only one of --since, --hours, or --days")
    if hours is not None and hours <= 0:
        raise ValueError("--hours must be positive")
    if days is not None and days <= 0:
        raise ValueError("--days must be positive")
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.now().astimezone().tzinfo)
    if since_text is not None:
        return parse_iso_timestamp(since_text), None
    if hours is not None:
        return current - timedelta(hours=hours), None
    if days is not None:
        return None, (current.date() - timedelta(days=days - 1)).isoformat()
    return None, None


def _at_or_none(row: dict[str, Any]) -> datetime | None:
    at = row.get("at")
    if not isinstance(at, str):
        return None
    try:
        return parse_iso_timestamp(at)
    except ValueError:
        return None


def build_report(paths: Iterable[Path], since: datetime | None = None) -> dict[str, Any]:
    paths = list(paths)
    rows, malformed = _read_rows(paths)
    if since is not None:
        rows = [row for row in rows if (at := _at_or_none(row)) is not None and at >= since]
    parents = {
        str(row["thread_id"]): str(row["parent_thread_id"])
        for row in rows
        if row.get("thread_id") and row.get("parent_thread_id")
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        at = row.get("at")
        if isinstance(at, str) and len(at) >= 10:
            grouped[at[:10]].append(row)

    days: dict[str, Any] = {}
    for day, selected in sorted(grouped.items()):
        thread_ids = {str(row["thread_id"]) for row in selected if row.get("thread_id")}
        roots = {_root_thread(thread_id, parents) for thread_id in thread_ids}
        decisions = [
            row
            for row in selected
            if row.get("event") in ROUTING_EVENTS
            or row.get("consulted_jev") is True
            or row.get("gate") == "jev_circuit_open"
        ]
        would_tiers: Counter[str] = Counter()
        raw_tiers: Counter[str] = Counter()
        confidence_bins: Counter[str] = Counter()
        confidence_values: list[float] = []
        latencies: list[float] = []
        low_confidence = 0
        jev_errors = 0
        for row in decisions:
            would = row.get("would") if isinstance(row.get("would"), dict) else {}
            effective = _tier(would.get("model") or row.get("model"))
            if effective:
                would_tiers[effective] += 1
            jev = row.get("jev") if isinstance(row.get("jev"), dict) else {}
            tier = jev.get("tier") if isinstance(jev.get("tier"), dict) else {}
            raw = _tier(tier.get("choice"))
            if raw:
                raw_tiers[raw] += 1
            confidence = tier.get("confidence")
            confidence_bins[_confidence_bin(confidence)] += 1
            if isinstance(confidence, (int, float)):
                confidence_values.append(float(confidence))
            latency = row.get("jev_ms")
            if isinstance(latency, (int, float)):
                latencies.append(float(latency))
            if row.get("reason") == "low_confidence":
                low_confidence += 1
            gate = str(row.get("gate") or "")
            if gate.startswith("jev_error") or gate == "jev_circuit_open":
                jev_errors += 1

        actual_cost = 0.0
        would_cost = 0.0
        usage_rows = 0
        missing_usage = 0
        for row in selected:
            would = row.get("would") if isinstance(row.get("would"), dict) else {}
            actual = _cost(row, row.get("upstream_model") or row.get("model"))
            projected = _cost(row, would.get("model") or row.get("model"))
            if actual is None or projected is None:
                missing_usage += 1
                continue
            usage_rows += 1
            actual_cost += actual
            would_cost += projected
        savings = actual_cost - would_cost
        days[day] = {
            "sessions": len(roots),
            "threads": len(thread_ids),
            "requests": len(selected),
            "decisions": len(decisions),
            "would_tiers": dict(sorted(would_tiers.items())),
            "raw_jev_tiers": dict(sorted(raw_tiers.items())),
            "confidence_bins": {
                name: confidence_bins.get(name, 0)
                for name in ("0.00-0.35", "0.35-0.50", "0.50-0.75", "0.75-1.00", "missing")
            },
            "confidence": _stats(confidence_values),
            "low_confidence_fallbacks": low_confidence,
            "jev_errors": jev_errors,
            "jev_error_rate": jev_errors / len(decisions) if decisions else 0.0,
            "jev_latency_ms": _stats(latencies),
            "usage_rows_priced": usage_rows,
            "usage_rows_missing": missing_usage,
            "actual_cost_usd": actual_cost,
            "would_cost_usd": would_cost,
            "projected_savings_usd": savings,
            "projected_savings_percent": savings / actual_cost * 100 if actual_cost else None,
        }
    return {"files": len(paths), "rows": len(rows), "malformed_rows": malformed, "days": days}


def _format_number(value: Any, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "| 日期 | 会话/线程 | 决策/请求 | would 档位 | 置信度分桶 | 低置信回退 | Jev 错误率 | Jev 延迟 p50/p95/max ms | 实际/估算费用 | 估算节省 | 缺 usage |",
        "|---|---:|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for day, item in report["days"].items():
        tiers = ", ".join(f"{key}:{value}" for key, value in item["would_tiers"].items()) or "-"
        bins = ", ".join(f"{key}:{value}" for key, value in item["confidence_bins"].items())
        latency = item["jev_latency_ms"]
        savings_percent = item["projected_savings_percent"]
        lines.append(
            f"| {day} | {item['sessions']}/{item['threads']} | {item['decisions']}/{item['requests']} "
            f"| {tiers} | {bins} | {item['low_confidence_fallbacks']} "
            f"| {item['jev_error_rate']:.1%} ({item['jev_errors']}/{item['decisions']}) "
            f"| {_format_number(latency['median'])}/{_format_number(latency['p95'])}/{_format_number(latency['max'])} "
            f"| ${item['actual_cost_usd']:.6f}/${item['would_cost_usd']:.6f} "
            f"| ${item['projected_savings_usd']:.6f} ({_format_number(savings_percent)}%) "
            f"| {item['usage_rows_missing']} |"
        )
    lines.extend(
        [
            "",
            f"读取 {report['files']} 个文件、{report['rows']} 条记录；畸形行 {report['malformed_rows']} 条。",
            "费用是对相同已完成 token usage 的固定轨迹重定价；缺 usage 的请求未计价，且不预测真路由导致的步数或 token 变化。",
        ]
    )
    return "\n".join(lines)


def _expand_inputs(values: list[str]) -> list[Path]:
    found: list[Path] = []
    for value in values:
        matches = [Path(item) for item in glob.glob(str(Path(value).expanduser()), recursive=True)]
        if not matches and Path(value).expanduser().exists():
            matches = [Path(value).expanduser()]
        for match in matches:
            if match.is_dir():
                found.extend(sorted(match.rglob("*.jsonl")))
            elif match.is_file():
                found.append(match)
    return sorted(set(found))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        default=[str(Path("~/.codex/codex-jev-router/decisions.jsonl").expanduser())],
    )
    parser.add_argument("--days", type=int, help="keep only the most recent N local calendar days")
    parser.add_argument("--hours", type=float, help="keep records from the most recent N hours")
    parser.add_argument("--since", help="keep records at or after this ISO-8601 timestamp")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()
    paths = _expand_inputs(args.paths)
    if not paths:
        parser.error("no decision JSONL files found")
    try:
        since, cutoff = resolve_filters(
            since_text=args.since,
            hours=args.hours,
            days=args.days,
        )
    except ValueError as exc:
        parser.error(str(exc))
    report = build_report(paths, since=since)
    if cutoff is not None:
        report["days"] = {day: value for day, value in report["days"].items() if day >= cutoff}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))


if __name__ == "__main__":
    main()
