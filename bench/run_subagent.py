#!/usr/bin/env python3
"""Run the native-Codex fan-out benchmark and split parent/subagent metrics."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from bench.run import (
    BACKTEST_PRICES,
    DEFAULT_CODEX_CONFIG,
    ROOT,
    _append_jsonl,
    _copy_workspace,
    _final_agent_output,
    _wait_for_health,
    _write_router_config,
    apply_preparation,
    balanced_schedule,
    calculate_usage_cost,
    evaluate_checks,
    read_jsonl_since,
    temporary_codex_config,
)


TASKS_PATH = ROOT / "bench" / "tasks-subagent.json"


def selected_schedule(
    tasks: list[dict[str, Any]], repeats: int, modes: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Return the balanced schedule restricted to requested modes."""

    return [item for item in balanced_schedule(tasks, repeats) if item["mode"] in modes]


def _stats(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "median": statistics.median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def codex_thread_id(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


def filter_thread_tree(rows: list[dict[str, Any]], root_thread_id: str) -> list[dict[str, Any]]:
    included = {root_thread_id}
    changed = True
    while changed:
        changed = False
        for row in rows:
            thread_id = row.get("thread_id")
            if row.get("parent_thread_id") in included and isinstance(thread_id, str):
                if thread_id not in included:
                    included.add(thread_id)
                    changed = True
    return [row for row in rows if row.get("thread_id") in included]


def thread_metrics(
    rows: list[dict[str, Any]],
    mode: str,
    prices: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        thread_id = row.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            grouped[thread_id].append(row)
    metrics = []
    for thread_id, thread_rows in grouped.items():
        first = next(
            (
                row
                for row in thread_rows
                if row.get("event") in ("first_request", "subagent_first")
            ),
            thread_rows[0],
        )
        policy = first.get("would") if mode == "shadow" else first
        policy = policy if isinstance(policy, dict) else first
        usage = calculate_usage_cost(thread_rows, prices)
        jev = first.get("jev") if isinstance(first.get("jev"), dict) else {}
        tier = jev.get("tier") if isinstance(jev.get("tier"), dict) else {}
        depth = jev.get("depth") if isinstance(jev.get("depth"), dict) else {}
        first_usage = first.get("usage") if isinstance(first.get("usage"), dict) else {}
        first_input = first_usage.get("input_tokens")
        first_cached = first_usage.get("cached_tokens")
        metrics.append(
            {
                "thread_id": thread_id,
                "parent_thread_id": first.get("parent_thread_id"),
                "role": "subagent" if first.get("parent_thread_id") else "parent",
                "agent_name": first.get("agent_name"),
                "subagent_kind": first.get("subagent_kind"),
                "delegation_source": first.get("delegation_source"),
                "parent_tier": first.get("parent_tier"),
                "policy_model": policy.get("model"),
                "policy_effort": policy.get("effort"),
                "jev_tier_choice": tier.get("choice"),
                "jev_tier_confidence": tier.get("confidence"),
                "jev_depth_choice": depth.get("choice"),
                "jev_depth_confidence": depth.get("confidence"),
                "jev_ms": first.get("jev_ms"),
                "gate": first.get("gate"),
                "first_input_tokens": first_input,
                "first_cached_tokens": first_cached,
                "first_output_tokens": first_usage.get("output_tokens"),
                "first_cache_hit_ratio": (
                    first_cached / first_input
                    if isinstance(first_input, int) and first_input > 0 and isinstance(first_cached, int)
                    else None
                ),
                "request_count": len(thread_rows),
                "completed_request_count": len(thread_rows) - usage["missing_completed"],
                **usage,
            }
        )
    return sorted(metrics, key=lambda item: (item["role"] != "parent", item["agent_name"] or ""))


def _infrastructure_failure(exit_code: int, rows: list[dict[str, Any]]) -> bool:
    if exit_code != 0 or not rows:
        return True
    parent = next((row for row in rows if row.get("event") == "first_request"), None)
    if parent is None:
        return True
    for row in rows:
        if row.get("event") not in ("first_request", "subagent_first"):
            continue
        gate = str(row.get("gate") or "")
        if gate in ("no_key", "jev_circuit_open") or gate.startswith("jev_error"):
            return True
    return any(not isinstance(row.get("status"), int) or row["status"] != 200 for row in rows)


def aggregate_subagent_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if not row.get("infrastructure_failure")]
    modes = {}
    for mode in ("shadow", "live"):
        selected = [row for row in valid if row["mode"] == mode]
        parents = [thread for row in selected for thread in row["threads"] if thread["role"] == "parent"]
        children = [thread for row in selected for thread in row["threads"] if thread["role"] == "subagent"]
        modes[mode] = {
            "sessions": len(selected),
            "pass_rate": sum(bool(row.get("passed")) for row in selected) / len(selected) if selected else None,
            "parent_threads": len(parents),
            "subagent_threads": len(children),
            "parent_cost_usd": sum(float(thread["cost_usd"]) for thread in parents),
            "subagent_cost_usd": sum(float(thread["cost_usd"]) for thread in children),
            "total_cost_usd": sum(float(thread["cost_usd"]) for thread in parents + children),
            "session_cost": _stats(
                [sum(float(thread["cost_usd"]) for thread in row["threads"]) for row in selected]
            ),
            "wall_ms": _stats([float(row["wall_ms"]) for row in selected]),
            "missing_completed": sum(
                int(thread.get("missing_completed") or 0) for thread in parents + children
            ),
        }
    tasks = {}
    for task_id in sorted({row["task_id"] for row in valid}):
        tasks[task_id] = {}
        for mode in ("shadow", "live"):
            selected = [row for row in valid if row["task_id"] == task_id and row["mode"] == mode]
            total_costs = [sum(float(thread["cost_usd"]) for thread in row["threads"]) for row in selected]
            parent_costs = [
                sum(float(thread["cost_usd"]) for thread in row["threads"] if thread["role"] == "parent")
                for row in selected
            ]
            child_costs = [
                sum(float(thread["cost_usd"]) for thread in row["threads"] if thread["role"] == "subagent")
                for row in selected
            ]
            tasks[task_id][mode] = {
                "total_cost": _stats(total_costs),
                "parent_cost": _stats(parent_costs),
                "subagent_cost": _stats(child_costs),
                "pass_rate": sum(bool(row.get("passed")) for row in selected) / len(selected) if selected else None,
            }
    shadow_child = modes["shadow"]["subagent_cost_usd"]
    live_child = modes["live"]["subagent_cost_usd"]
    shadow_total = modes["shadow"]["total_cost_usd"]
    live_total = modes["live"]["total_cost_usd"]
    return {
        "modes": modes,
        "tasks": tasks,
        "subagent_savings_percent": (1 - live_child / shadow_child) * 100 if shadow_child else None,
        "overall_savings_percent": (1 - live_total / shadow_total) * 100 if shadow_total else None,
    }


def _load_tasks() -> list[dict[str, Any]]:
    value = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("tasks-subagent.json must contain exactly four tasks")
    return value


def _run_attempt(
    item: dict[str, Any],
    sequence: int,
    attempt: int,
    run_dir: Path,
    work_root: Path,
    decision_log: Path,
    shadow_path: Path,
) -> dict[str, Any]:
    task = item["task"]
    mode = item["mode"]
    if mode == "shadow":
        shadow_path.touch()
    else:
        shadow_path.unlink(missing_ok=True)
    key = f"{sequence:03d}-{task['id']}-{mode}-r{item['repeat']}-a{attempt}"
    workspace = work_root / key
    output_path = run_dir / "sessions" / f"{key}.jsonl"
    stderr_path = run_dir / "sessions" / f"{key}.stderr.log"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    offset = decision_log.stat().st_size if decision_log.exists() else 0
    started = time.monotonic()
    exit_code = 1
    timed_out = False
    try:
        _copy_workspace(workspace)
        apply_preparation(workspace, task.get("prepare", []))
        environment = dict(os.environ)
        environment.pop("TYPESAFE_API_KEY", None)
        with output_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                completed = subprocess.run(
                    [
                        "codex",
                        "-a",
                        "never",
                        "-s",
                        "workspace-write",
                        "exec",
                        "--json",
                        "--skip-git-repo-check",
                        task["prompt"],
                    ],
                    cwd=workspace,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=1200,
                    check=False,
                )
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = 124
        wall_ms = int(round((time.monotonic() - started) * 1000))
        time.sleep(0.1)
        window_rows = read_jsonl_since(decision_log, offset)
        root_thread_id = codex_thread_id(output_path)
        rows = filter_thread_tree(window_rows, root_thread_id) if root_thread_id else []
        final_output = _final_agent_output(output_path)
        checks_passed, check_details = evaluate_checks(workspace, task["checks"], final_output)
        infra = _infrastructure_failure(exit_code, rows)
        threads = thread_metrics(rows, mode, BACKTEST_PRICES)
        children = [thread for thread in threads if thread["role"] == "subagent"]
        child_count_ok = len(children) == int(task["expected_children"])
        return {
            "task_id": task["id"],
            "mode": mode,
            "repeat": item["repeat"],
            "sequence": sequence,
            "attempt": attempt,
            "expected_children": task["expected_children"],
            "root_thread_id": root_thread_id,
            "observed_children": len(children),
            "child_count_ok": child_count_ok,
            "threads": threads,
            "request_count": len(rows),
            "wall_ms": wall_ms,
            "codex_exit_code": exit_code,
            "timed_out": timed_out,
            "passed": exit_code == 0 and checks_passed and child_count_ok and not infra,
            "checks": check_details,
            "infrastructure_failure": infra,
            "output_path": output_path.relative_to(ROOT).as_posix(),
            "stderr_path": stderr_path.relative_to(ROOT).as_posix(),
        }
    finally:
        if workspace.exists():
            shutil.rmtree(workspace)


def run_benchmark(args: argparse.Namespace) -> Path:
    tasks = _load_tasks()
    if args.task_ids:
        wanted = set(args.task_ids.split(","))
        tasks = [task for task in tasks if task["id"] in wanted]
        missing = wanted - {task["id"] for task in tasks}
        if missing:
            raise ValueError(f"unknown task ids: {sorted(missing)}")
    repeats = 1 if args.dry_run else args.repeats
    if args.dry_run:
        tasks = tasks[:1]
    modes = tuple(part.strip() for part in args.modes.split(",") if part.strip())
    if not modes or any(mode not in ("shadow", "live") for mode in modes):
        raise ValueError("modes must contain shadow and/or live")
    schedule = selected_schedule(tasks, repeats, modes)
    run_id = args.run_id or time.strftime(("subagent-dry" if args.dry_run else "subagent") + "-%Y%m%d-%H%M%S")
    run_dir = ROOT / "runtime" / "bench" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    work_root = ROOT / "runtime" / "bench-work" / run_id
    router_config = run_dir / "router.toml"
    decision_log = run_dir / "decisions.jsonl"
    shadow_path = run_dir / "router.shadow"
    off_path = run_dir / "router.off"
    results_path = run_dir / "results.jsonl"
    attempts_path = run_dir / "attempts.jsonl"
    summary_path = run_dir / "summary.json"
    backup_path = run_dir / "config.toml.backup"
    server_log = (run_dir / "server.log").open("ab")
    _write_router_config(router_config, run_dir, args.port)
    existing = read_jsonl_since(results_path, 0)
    completed_keys = {(row.get("task_id"), row.get("mode"), row.get("repeat")) for row in existing}
    process = None
    try:
        with temporary_codex_config(
            Path(args.codex_config).expanduser(),
            backup_path,
            f"http://127.0.0.1:{args.port}/v1",
        ):
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "hipporoute", "--config", str(router_config)],
                cwd=ROOT,
                stdout=server_log,
                stderr=subprocess.STDOUT,
            )
            health = _wait_for_health(args.port, process)
            if health.get("jev_key") is not True:
                raise RuntimeError("router health reports jev_key=false")
            print(f"run_id={run_id} sessions={len(schedule)} jev_key=true", flush=True)
            for sequence, item in enumerate(schedule, 1):
                task = item["task"]
                cell = (task["id"], item["mode"], item["repeat"])
                if cell in completed_keys:
                    print(f"[{sequence}/{len(schedule)}] skip completed {cell}", flush=True)
                    continue
                final = None
                for attempt in range(1, args.max_retries + 2):
                    result = _run_attempt(
                        item,
                        sequence,
                        attempt,
                        run_dir,
                        work_root,
                        decision_log,
                        shadow_path,
                    )
                    _append_jsonl(attempts_path, result)
                    total_cost = sum(thread["cost_usd"] for thread in result["threads"])
                    print(
                        f"[{sequence}/{len(schedule)}] {task['id']} {item['mode']} r{item['repeat']} "
                        f"a{attempt} children={result['observed_children']}/{task['expected_children']} "
                        f"pass={result['passed']} infra={result['infrastructure_failure']} "
                        f"cost=${total_cost:.6f} wall={result['wall_ms']/1000:.1f}s",
                        flush=True,
                    )
                    final = result
                    if not result["infrastructure_failure"]:
                        break
                _append_jsonl(results_path, final)
                completed_keys.add(cell)
            all_results = read_jsonl_since(results_path, 0)
            summary = aggregate_subagent_results(all_results)
            summary.update(
                run_id=run_id,
                expected_sessions=len(schedule),
                recorded_sessions=len(all_results),
            )
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    finally:
        shadow_path.unlink(missing_ok=True)
        off_path.unlink(missing_ok=True)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server_log.close()
        if work_root.exists():
            shutil.rmtree(work_root)
    return run_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--task-ids")
    parser.add_argument("--modes", default="shadow,live")
    parser.add_argument("--run-id")
    parser.add_argument("--port", type=int, default=4320)
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
