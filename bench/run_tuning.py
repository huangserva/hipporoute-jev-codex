#!/usr/bin/env python3
"""Run T3 luna-effort or T4 confidence-threshold tuning benchmarks."""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from bench.run import (
    BACKTEST_PRICES,
    DEFAULT_CODEX_CONFIG,
    ROOT,
    _append_jsonl,
    _load_tasks,
    _run_one_attempt,
    _wait_for_health,
    _write_router_config,
    read_jsonl_since,
    temporary_codex_config,
)
from bench.run_subagent import _load_tasks as _load_subagent_tasks
from bench.run_subagent import _run_attempt as _run_subagent_attempt


T3_VARIANTS = ("max", "medium", "low")
T4_VARIANTS = ("0.35", "0.5")


def rotating_schedule(
    tasks: list[dict[str, Any]], variants: tuple[str, ...], repeats: int
) -> list[dict[str, Any]]:
    """Rotate variant order within every task/repeat cell to reduce time-order bias."""

    schedule = []
    for repeat in range(1, repeats + 1):
        for task_index, task in enumerate(tasks):
            start = (repeat - 1 + task_index) % len(variants)
            order = variants[start:] + variants[:start]
            schedule.extend(
                {"task": task, "variant": variant, "repeat": repeat} for variant in order
            )
    return schedule


def _stats(values: Iterable[float | int]) -> dict[str, Any]:
    selected = list(values)
    return {
        "n": len(selected),
        "median": statistics.median(selected) if selected else None,
        "min": min(selected) if selected else None,
        "max": max(selected) if selected else None,
    }


def _repriced_cost(row: dict[str, Any], model: str) -> float | None:
    if model not in BACKTEST_PRICES:
        return None
    input_tokens = row.get("input_tokens")
    cached_tokens = row.get("cached_tokens")
    output_tokens = row.get("output_tokens")
    if not all(isinstance(value, int) for value in (input_tokens, cached_tokens, output_tokens)):
        return None
    rate = BACKTEST_PRICES[model]
    uncached = max(0, input_tokens - cached_tokens)
    return (
        uncached * rate["input"]
        + cached_tokens * rate["cached_input"]
        + output_tokens * rate["output"]
    ) / 1_000_000


def annotate_threshold_threads(
    threads: list[dict[str, Any]], threshold: float
) -> list[dict[str, Any]]:
    """Add explicit threshold outcome and fixed-token fallback cost fields."""

    annotated = []
    for original in threads:
        thread = dict(original)
        choice = thread.get("jev_tier_choice")
        confidence = thread.get("jev_tier_confidence")
        policy_model = thread.get("policy_model")
        below = isinstance(confidence, (int, float)) and confidence < threshold
        fallback = below and isinstance(choice, str) and policy_model != choice
        raw_cost = _repriced_cost(thread, choice) if isinstance(choice, str) else None
        actual_cost = thread.get("cost_usd")
        extra = (
            float(actual_cost) - raw_cost
            if fallback and isinstance(actual_cost, (int, float)) and raw_cost is not None
            else 0.0
        )
        thread.update(
            threshold=threshold,
            below_confidence_gate=below,
            confidence_fallback=fallback,
            luna_confidence_band_035_05=(
                choice == "gpt-5.6-luna"
                and isinstance(confidence, (int, float))
                and 0.35 <= confidence < 0.5
            ),
            fixed_token_raw_choice_cost_usd=raw_cost,
            fallback_extra_cost_usd=max(0.0, extra),
        )
        annotated.append(thread)
    return annotated


def aggregate_t3(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if not row.get("infrastructure_failure")]
    variants = {}
    for variant in T3_VARIANTS:
        selected = [row for row in valid if row.get("variant") == variant]
        request_metrics = [
            metric
            for row in selected
            for metric in row.get("request_metrics", [])
            if isinstance(metric, dict)
        ]
        variants[variant] = {
            "sessions": len(selected),
            "pass_rate": (
                sum(bool(row.get("passed")) for row in selected) / len(selected)
                if selected
                else None
            ),
            "requests": _stats(int(row.get("request_count") or 0) for row in selected),
            "output_tokens_per_request": _stats(
                int(metric["output_tokens"])
                for metric in request_metrics
                if isinstance(metric.get("output_tokens"), int)
            ),
            "request_ms": _stats(
                int(metric["total_ms"])
                for metric in request_metrics
                if isinstance(metric.get("total_ms"), int)
            ),
            "wall_ms": _stats(int(row.get("wall_ms") or 0) for row in selected),
            "cost_usd": _stats(float(row.get("cost_usd") or 0.0) for row in selected),
            "cost_total_usd": sum(float(row.get("cost_usd") or 0.0) for row in selected),
            "completed_requests": sum(
                int(row.get("completed_request_count") or 0) for row in selected
            ),
            "missing_completed": sum(int(row.get("missing_completed") or 0) for row in selected),
        }
    return {"variants": variants}


def aggregate_t4(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if not row.get("infrastructure_failure")]
    variants = {}
    for variant in T4_VARIANTS:
        selected = [row for row in valid if row.get("variant") == variant]
        threads = [
            thread
            for row in selected
            for thread in row.get("threads", [])
            if isinstance(thread, dict)
        ]
        session_costs = [
            sum(float(thread.get("cost_usd") or 0.0) for thread in row.get("threads", []))
            for row in selected
        ]
        variants[variant] = {
            "sessions": len(selected),
            "pass_rate": (
                sum(bool(row.get("passed")) for row in selected) / len(selected)
                if selected
                else None
            ),
            "threads": len(threads),
            "below_gate_threads": sum(bool(thread.get("below_confidence_gate")) for thread in threads),
            "fallback_threads": sum(bool(thread.get("confidence_fallback")) for thread in threads),
            "band_threads": sum(
                bool(thread.get("luna_confidence_band_035_05")) for thread in threads
            ),
            "fallback_extra_cost_usd": sum(
                float(thread.get("fallback_extra_cost_usd") or 0.0) for thread in threads
            ),
            "cost_usd": _stats(session_costs),
            "cost_total_usd": sum(session_costs),
            "wall_ms": _stats(int(row.get("wall_ms") or 0) for row in selected),
            "missing_completed": sum(
                int(thread.get("missing_completed") or 0) for thread in threads
            ),
        }
    return {"variants": variants}


def _stop_router(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _start_router(config_path: Path, port: int, server_log) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "codex_jev_router", "--config", str(config_path)],
        cwd=ROOT,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    health = _wait_for_health(port, process)
    if health.get("jev_key") is not True:
        _stop_router(process)
        raise RuntimeError("router health reports jev_key=false")
    return process


def _request_metrics(rows: list[dict[str, Any]], thread_id: str | None) -> list[dict[str, Any]]:
    selected = [row for row in rows if not thread_id or row.get("thread_id") == thread_id]
    metrics = []
    for row in selected:
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        metrics.append(
            {
                "event": row.get("event"),
                "response_completed": bool(row.get("response_completed")),
                "upstream_model": row.get("upstream_model"),
                "output_tokens": usage.get("output_tokens"),
                "total_ms": row.get("total_ms"),
            }
        )
    return metrics


def _load_experiment(args: argparse.Namespace):
    if args.experiment == "t3":
        tasks = [task for task in _load_tasks() if task.get("category") == "mechanical"]
        variants = T3_VARIANTS
        port = args.port or 4331
    else:
        tasks = _load_subagent_tasks()
        variants = T4_VARIANTS
        port = args.port or 4332
    if args.task_ids:
        wanted = set(args.task_ids.split(","))
        tasks = [task for task in tasks if task["id"] in wanted]
        missing = wanted - {task["id"] for task in tasks}
        if missing:
            raise ValueError(f"unknown task ids: {sorted(missing)}")
    if args.dry_run:
        tasks = tasks[:1]
    repeats = 1 if args.dry_run else args.repeats
    return tasks, variants, repeats, port


def run_benchmark(args: argparse.Namespace) -> Path:
    tasks, variants, repeats, port = _load_experiment(args)
    schedule = rotating_schedule(tasks, variants, repeats)
    run_id = args.run_id or time.strftime(f"{args.experiment}-%Y%m%d-%H%M%S")
    run_dir = ROOT / "runtime" / "bench" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    work_root = ROOT / "runtime" / "bench-work" / run_id
    decision_log = run_dir / "decisions.jsonl"
    shadow_path = run_dir / "router.shadow"
    off_path = run_dir / "router.off"
    results_path = run_dir / "results.jsonl"
    attempts_path = run_dir / "attempts.jsonl"
    summary_path = run_dir / "summary.json"
    backup_path = run_dir / "config.toml.backup"
    server_log = (run_dir / "server.log").open("ab")
    existing = read_jsonl_since(results_path, 0)
    completed = {
        (row.get("task_id"), row.get("variant"), row.get("repeat")) for row in existing
    }
    process = None
    try:
        with temporary_codex_config(
            Path(args.codex_config).expanduser(),
            backup_path,
            f"http://127.0.0.1:{port}/v1",
        ):
            print(
                f"run_id={run_id} experiment={args.experiment} sessions={len(schedule)}",
                flush=True,
            )
            for sequence, item in enumerate(schedule, 1):
                task = item["task"]
                variant = item["variant"]
                cell = (task["id"], variant, item["repeat"])
                if cell in completed:
                    print(f"[{sequence}/{len(schedule)}] skip completed {cell}", flush=True)
                    continue
                final = None
                for attempt in range(1, args.max_retries + 2):
                    _stop_router(process)
                    process = None
                    config_path = run_dir / f"router-{args.experiment}-{variant.replace('.', '_')}.toml"
                    effort = variant if args.experiment == "t3" else "max"
                    threshold = float(variant) if args.experiment == "t4" else 0.5
                    _write_router_config(
                        config_path,
                        run_dir,
                        port,
                        luna_effort=effort,
                        confidence_gate=threshold,
                    )
                    process = _start_router(config_path, port, server_log)
                    offset = decision_log.stat().st_size if decision_log.exists() else 0
                    runner_item = {
                        "task": task,
                        "mode": variant,
                        "repeat": item["repeat"],
                    }
                    if args.experiment == "t3":
                        result = _run_one_attempt(
                            runner_item,
                            sequence,
                            attempt,
                            run_dir,
                            work_root,
                            decision_log,
                            shadow_path,
                        )
                        rows = read_jsonl_since(decision_log, offset)
                        result["request_metrics"] = _request_metrics(
                            rows, result.get("thread_id")
                        )
                        result["configured_luna_effort"] = effort
                    else:
                        result = _run_subagent_attempt(
                            runner_item,
                            sequence,
                            attempt,
                            run_dir,
                            work_root,
                            decision_log,
                            shadow_path,
                        )
                        result["threads"] = annotate_threshold_threads(
                            result["threads"], threshold
                        )
                        result["confidence_gate"] = threshold
                    result["variant"] = variant
                    result["experiment"] = args.experiment
                    _append_jsonl(attempts_path, result)
                    total_cost = (
                        float(result.get("cost_usd") or 0.0)
                        if args.experiment == "t3"
                        else sum(
                            float(thread.get("cost_usd") or 0.0)
                            for thread in result.get("threads", [])
                        )
                    )
                    print(
                        f"[{sequence}/{len(schedule)}] {task['id']} {variant} "
                        f"r{item['repeat']} a{attempt} pass={result['passed']} "
                        f"infra={result['infrastructure_failure']} cost=${total_cost:.6f} "
                        f"wall={result['wall_ms']/1000:.1f}s",
                        flush=True,
                    )
                    final = result
                    _stop_router(process)
                    process = None
                    if not result["infrastructure_failure"]:
                        break
                _append_jsonl(results_path, final)
                completed.add(cell)
            all_results = read_jsonl_since(results_path, 0)
            summary = aggregate_t3(all_results) if args.experiment == "t3" else aggregate_t4(all_results)
            summary.update(
                run_id=run_id,
                experiment=args.experiment,
                expected_sessions=len(schedule),
                recorded_sessions=len(all_results),
            )
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    finally:
        _stop_router(process)
        shadow_path.unlink(missing_ok=True)
        off_path.unlink(missing_ok=True)
        server_log.close()
        if work_root.exists():
            shutil.rmtree(work_root)
    return run_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", choices=("t3", "t4"), required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--task-ids")
    parser.add_argument("--run-id")
    parser.add_argument("--port", type=int)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--codex-config", default=str(DEFAULT_CODEX_CONFIG))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.repeats < 1 or args.max_retries < 0:
        raise SystemExit("repeats must be positive and max-retries non-negative")
    run_dir = run_benchmark(args)
    print(f"results={run_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
