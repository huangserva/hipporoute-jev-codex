#!/usr/bin/env python3
"""Run the isolated mechanical-vs-reasoning routing benchmark."""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "bench" / "tasks.json"
DEFAULT_CODEX_CONFIG = Path("~/.codex/config.toml").expanduser()
PROVIDER_NAME = "codex-jev-router-bench"
BACKTEST_PRICES = {
    "gpt-6-astra": {"input": 10.0, "cached_input": 1.0, "output": 50.0},
    "gpt-5.6-sol": {"input": 4.0, "cached_input": 0.4, "output": 20.0},
    "gpt-5.6-luna": {"input": 0.2, "cached_input": 0.02, "output": 1.2},
}


def balanced_schedule(tasks: list[dict[str, Any]], repeats: int) -> list[dict[str, Any]]:
    schedule = []
    for repeat in range(1, repeats + 1):
        for index, task in enumerate(tasks):
            modes = ("shadow", "live") if (repeat - 1 + index) % 2 == 0 else ("live", "shadow")
            for mode in modes:
                schedule.append({"task": task, "mode": mode, "repeat": repeat})
    return schedule


def calculate_usage_cost(
    rows: Iterable[dict[str, Any]], prices: dict[str, dict[str, float]]
) -> dict[str, Any]:
    totals = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
    model_counts: Counter[str] = Counter()
    cost = 0.0
    missing = 0
    for row in rows:
        model = row.get("upstream_model")
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        if not row.get("response_completed") or model not in prices:
            missing += 1
            continue
        values = {
            name: value if isinstance(value := usage.get(name), int) else 0
            for name in totals
        }
        for name, value in values.items():
            totals[name] += value
        model_counts[model] += 1
        rate = prices[model]
        uncached = max(0, values["input_tokens"] - values["cached_tokens"])
        cost += (
            uncached * rate["input"]
            + values["cached_tokens"] * rate["cached_input"]
            + values["output_tokens"] * rate["output"]
        ) / 1_000_000
    return {
        **totals,
        "cost_usd": cost,
        "missing_completed": missing,
        "cost_is_lower_bound": missing > 0,
        "upstream_model_counts": dict(sorted(model_counts.items())),
    }


def apply_preparation(root: Path, operations: Iterable[dict[str, Any]]) -> None:
    for operation in operations:
        if operation.get("type") != "replace":
            raise ValueError(f"unsupported preparation: {operation.get('type')}")
        path = root / operation["path"]
        text = path.read_text(encoding="utf-8")
        needle = operation["find"]
        expected = int(operation.get("count", 1))
        if text.count(needle) < expected:
            raise ValueError(f"prepare text not found {expected} times: {operation['path']}")
        path.write_text(text.replace(needle, operation["replace"], expected), encoding="utf-8")


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _expected_manifest(root: Path, directories: Iterable[str]) -> str:
    files = []
    for directory in directories:
        files.extend(path for path in (root / directory).rglob("*.py") if "__pycache__" not in path.parts)
    lines = []
    for path in sorted(set(files), key=lambda item: item.relative_to(root).as_posix()):
        count = len(path.read_text(encoding="utf-8").splitlines())
        lines.append(f"{count} {path.relative_to(root).as_posix()}")
    return "\n".join(lines) + "\n"


def evaluate_checks(
    root: Path, checks: Iterable[dict[str, Any]], final_output: str
) -> tuple[bool, list[dict[str, Any]]]:
    details = []
    for check in checks:
        kind = check.get("type")
        passed = False
        message = ""
        try:
            if kind in ("file_contains_all", "file_not_contains_all"):
                text = (root / check["path"]).read_text(encoding="utf-8")
                presence = [value in text for value in check["values"]]
                passed = all(presence) if kind == "file_contains_all" else not any(presence)
                message = f"presence={presence}"
            elif kind == "file_regex_all":
                text = (root / check["path"]).read_text(encoding="utf-8")
                matches = [re.search(pattern, text) is not None for pattern in check["patterns"]]
                passed = all(matches)
                message = f"matches={matches}"
            elif kind == "function_docstring":
                tree = ast.parse((root / check["path"]).read_text(encoding="utf-8"))
                function = _find_function(tree, check["function"])
                passed = function is not None and bool(ast.get_docstring(function))
                message = "docstring present" if passed else "docstring missing"
            elif kind == "command":
                completed = subprocess.run(
                    check["argv"],
                    cwd=root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=float(check.get("timeout_seconds", 180)),
                    check=False,
                )
                passed = completed.returncode == 0
                message = f"exit={completed.returncode}"
            elif kind == "output_regex":
                passed = re.search(check["pattern"], final_output) is not None
                message = "matched" if passed else "not matched"
            elif kind == "python_line_manifest":
                actual = (root / check.get("path", "python-lines.txt")).read_text(encoding="utf-8")
                expected = _expected_manifest(root, check["paths"])
                passed = actual == expected
                message = "exact match" if passed else "manifest differs"
            else:
                raise ValueError(f"unsupported check: {kind}")
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            passed = False
        details.append({"type": kind, "passed": passed, "message": message})
    return all(item["passed"] for item in details), details


def read_jsonl_since(path: Path, offset: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("rb") as handle:
        handle.seek(offset)
        for raw in handle:
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _bench_config_text(original: str, base_url: str) -> str:
    if f"[model_providers.{PROVIDER_NAME}]" in original:
        raise ValueError(f"config already contains {PROVIDER_NAME}")
    lines = original.splitlines(keepends=True)
    table_at = next((index for index, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    prefix = [line for line in lines[:table_at] if not re.match(r"^\s*model_provider\s*=", line)]
    prefix.insert(0, f'model_provider = "{PROVIDER_NAME}"\n')
    changed = "".join(prefix + lines[table_at:]).rstrip() + "\n\n"
    changed += (
        f"[model_providers.{PROVIDER_NAME}]\n"
        'name = "Codex + Jev Benchmark"\n'
        f'base_url = "{base_url}"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )
    return changed


@contextlib.contextmanager
def temporary_codex_config(path: Path, backup: Path, base_url: str):
    original = path.read_bytes()
    backup.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(backup, original)
    changed = _bench_config_text(original.decode("utf-8"), base_url).encode("utf-8")
    _atomic_write(path, changed)
    try:
        yield
    finally:
        _atomic_write(path, original)
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(original).digest():
            raise RuntimeError("Codex config restoration hash mismatch")


def is_infrastructure_failure(exit_code: int, rows: list[dict[str, Any]]) -> bool:
    if exit_code != 0 or not rows:
        return True
    decisions = [row for row in rows if row.get("event") in ("first_request", "subagent_first")]
    if not decisions:
        return True
    gate = str(decisions[0].get("gate") or "")
    if gate in ("no_key", "jev_circuit_open") or gate.startswith("jev_error"):
        return True
    return any(not isinstance(row.get("status"), int) or row["status"] != 200 for row in rows)


def _stats(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "median": statistics.median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def aggregate_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if not row.get("infrastructure_failure")]
    modes = {}
    for mode in ("shadow", "live"):
        selected = [row for row in valid if row["mode"] == mode]
        modes[mode] = {
            "sessions": len(selected),
            "pass_rate": sum(bool(row.get("passed")) for row in selected) / len(selected) if selected else None,
            "cost": _stats([float(row["cost_usd"]) for row in selected]),
            "cost_total_usd": sum(float(row["cost_usd"]) for row in selected),
            "usage": {
                name: sum(int(row.get(name) or 0) for row in selected)
                for name in ("input_tokens", "cached_tokens", "output_tokens")
            },
            "requests": sum(int(row.get("request_count") or 0) for row in selected),
            "completed_requests": sum(int(row.get("completed_request_count") or 0) for row in selected),
            "missing_completed": sum(int(row.get("missing_completed") or 0) for row in selected),
            "lower_bound_sessions": sum(bool(row.get("cost_is_lower_bound")) for row in selected),
            "wall_ms": _stats([float(row["wall_ms"]) for row in selected]),
            "jev_ms": _stats([float(row["jev_ms"]) for row in selected if row.get("jev_ms") is not None]),
        }
    tasks = {}
    for task_id in sorted({row["task_id"] for row in valid}):
        tasks[task_id] = {}
        for mode in ("shadow", "live"):
            selected = [row for row in valid if row["task_id"] == task_id and row["mode"] == mode]
            tasks[task_id][mode] = {
                "cost": _stats([float(row["cost_usd"]) for row in selected]),
                "pass_rate": sum(bool(row.get("passed")) for row in selected) / len(selected) if selected else None,
            }
    groups = {}
    for category in ("mechanical", "reasoning"):
        selected = [row for row in valid if row["category"] == category]
        shadow = sum(float(row["cost_usd"]) for row in selected if row["mode"] == "shadow")
        live = sum(float(row["cost_usd"]) for row in selected if row["mode"] == "live")
        groups[category] = {
            "shadow_cost_usd": shadow,
            "live_cost_usd": live,
            "savings_percent": (1 - live / shadow) * 100 if shadow else None,
            "shadow_pass_rate": (
                sum(bool(row.get("passed")) for row in selected if row["mode"] == "shadow")
                / sum(row["mode"] == "shadow" for row in selected)
                if any(row["mode"] == "shadow" for row in selected)
                else None
            ),
            "live_pass_rate": (
                sum(bool(row.get("passed")) for row in selected if row["mode"] == "live")
                / sum(row["mode"] == "live" for row in selected)
                if any(row["mode"] == "live" for row in selected)
                else None
            ),
        }
    mismatches = sorted(
        {
            row["task_id"]
            for row in valid
            if row.get("jev_tier_choice") and row.get("jev_tier_choice") != row.get("expected_model")
        }
    )
    policy_mismatches = sorted(
        {
            row["task_id"]
            for row in valid
            if row.get("policy_model") and row.get("policy_model") != row.get("expected_model")
        }
    )
    shadow_total = modes["shadow"]["cost_total_usd"]
    live_total = modes["live"]["cost_total_usd"]
    return {
        "modes": modes,
        "tasks": tasks,
        "groups": groups,
        "overall_savings_percent": (1 - live_total / shadow_total) * 100 if shadow_total else None,
        "jev_mismatches": mismatches,
        "policy_mismatches": policy_mismatches,
    }


def _load_tasks(path: Path = TASKS_PATH) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ValueError("tasks.json must contain a non-empty array")
    return value


def copy_workspace(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".codex",
            "runtime",
            "__pycache__",
            "*.pyc",
            "config.e2e.toml",
            "config.toml.backup-*",
        ),
    )


def _copy_workspace(destination: Path) -> None:
    copy_workspace(ROOT, destination)


def _write_router_config(
    path: Path,
    run_dir: Path,
    port: int,
    *,
    luna_effort: str = "max",
    confidence_gate: float = 0.5,
) -> None:
    text = f'''[server]
host = "127.0.0.1"
port = {port}

[upstream]
mode = "direct"
direct_url = "https://chatgpt.com/backend-api/codex"
timeout_seconds = 900

[paths]
state = "{(run_dir / 'threads.json').as_posix()}"
decision_log = "{(run_dir / 'decisions.jsonl').as_posix()}"
off = "{(run_dir / 'router.off').as_posix()}"
shadow = "{(run_dir / 'router.shadow').as_posix()}"
stream_debug = "{(run_dir / 'stream.debug').as_posix()}"
raw_stream_dir = "{(run_dir / 'raw-streams').as_posix()}"

[jev]
timeout_seconds = 4
retries = 2
backoff_seconds = 0.25
confidence_gate = {confidence_gate}

[routing]
downgrade_max_context_tokens = 20000
switch_budget_usd = 0.25
chars_per_token = 2.8
luna_effort = "{luna_effort}"
summary_marker = false
'''
    path.write_text(text, encoding="utf-8")


def _wait_for_health(port: int, process: subprocess.Popen, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"router exited early: {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                health = json.loads(response.read().decode("utf-8"))
            if health.get("ok"):
                return health
        except Exception:
            time.sleep(0.1)
    raise TimeoutError("router health timeout")


def _final_agent_output(path: Path) -> str:
    messages = []
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            messages.append(item["text"])
    return messages[-1] if messages else ""


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def _run_one_attempt(
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
    workspace.parent.mkdir(parents=True, exist_ok=True)
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
                    timeout=900,
                    check=False,
                )
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = 124
        wall_ms = int(round((time.monotonic() - started) * 1000))
        time.sleep(0.1)
        rows = read_jsonl_since(decision_log, offset)
        final_output = _final_agent_output(output_path)
        check_started = time.monotonic()
        checks_passed, check_details = evaluate_checks(workspace, task["checks"], final_output)
        check_ms = int(round((time.monotonic() - check_started) * 1000))
        infra = is_infrastructure_failure(exit_code, rows)
        usage = calculate_usage_cost(rows, BACKTEST_PRICES)
        decision = next((row for row in rows if row.get("event") == "first_request"), {})
        jev = decision.get("jev") if isinstance(decision.get("jev"), dict) else {}
        tier = jev.get("tier") if isinstance(jev.get("tier"), dict) else {}
        depth = jev.get("depth") if isinstance(jev.get("depth"), dict) else {}
        policy = decision.get("would") if mode == "shadow" else decision
        policy = policy if isinstance(policy, dict) else {}
        return {
            "task_id": task["id"],
            "category": task["category"],
            "expected_model": task["expected_model"],
            "mode": mode,
            "repeat": item["repeat"],
            "sequence": sequence,
            "attempt": attempt,
            "thread_id": decision.get("thread_id"),
            "jev_tier_choice": tier.get("choice"),
            "jev_tier_confidence": tier.get("confidence"),
            "jev_depth_choice": depth.get("choice"),
            "jev_depth_confidence": depth.get("confidence"),
            "policy_model": policy.get("model"),
            "policy_effort": policy.get("effort"),
            "gate": decision.get("gate"),
            "reason": decision.get("reason"),
            "jev_ms": decision.get("jev_ms"),
            "request_count": len(rows),
            "completed_request_count": len(rows) - usage["missing_completed"],
            **usage,
            "wall_ms": wall_ms,
            "check_ms": check_ms,
            "codex_exit_code": exit_code,
            "timed_out": timed_out,
            "passed": exit_code == 0 and checks_passed and not infra,
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
    if args.dry_run:
        mechanical = next(task for task in tasks if task["category"] == "mechanical")
        reasoning = next(task for task in tasks if task["category"] == "reasoning")
        tasks = [mechanical, reasoning]
        repeats = 1
    else:
        repeats = args.repeats
    schedule = balanced_schedule(tasks, repeats)
    run_id = args.run_id or time.strftime(("dry" if args.dry_run else "full") + "-%Y%m%d-%H%M%S")
    run_dir = ROOT / "runtime" / "bench" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    work_root = ROOT / "runtime" / "bench-work" / run_id
    router_config = run_dir / "router.toml"
    decision_log = run_dir / "decisions.jsonl"
    shadow_path = run_dir / "router.shadow"
    off_path = run_dir / "router.off"
    server_log_path = run_dir / "server.log"
    results_path = run_dir / "results.jsonl"
    attempts_path = run_dir / "attempts.jsonl"
    summary_path = run_dir / "summary.json"
    backup_path = run_dir / "config.toml.backup"
    _write_router_config(router_config, run_dir, args.port)
    completed_keys = set()
    existing_results = read_jsonl_since(results_path, 0)
    for row in existing_results:
        completed_keys.add((row.get("task_id"), row.get("mode"), row.get("repeat")))

    server_log = server_log_path.open("ab")
    process = None
    try:
        with temporary_codex_config(
            Path(args.codex_config).expanduser(),
            backup_path,
            f"http://127.0.0.1:{args.port}/v1",
        ):
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "codex_jev_router", "--config", str(router_config)],
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
                    result = _run_one_attempt(
                        item,
                        sequence,
                        attempt,
                        run_dir,
                        work_root,
                        decision_log,
                        shadow_path,
                    )
                    _append_jsonl(attempts_path, result)
                    print(
                        f"[{sequence}/{len(schedule)}] {task['id']} {item['mode']} r{item['repeat']} "
                        f"a{attempt} model={result['policy_model']} pass={result['passed']} "
                        f"infra={result['infrastructure_failure']} cost=${result['cost_usd']:.6f} "
                        f"wall={result['wall_ms']/1000:.1f}s",
                        flush=True,
                    )
                    final = result
                    if not result["infrastructure_failure"]:
                        break
                _append_jsonl(results_path, final)
                completed_keys.add(cell)
            all_results = read_jsonl_since(results_path, 0)
            summary = aggregate_results(all_results)
            summary["run_id"] = run_id
            summary["expected_sessions"] = len(schedule)
            summary["recorded_sessions"] = len(all_results)
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
    parser.add_argument("--dry-run", action="store_true", help="Run one mechanical and one reasoning task in both modes")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--task-ids", help="Comma-separated task ids")
    parser.add_argument("--run-id")
    parser.add_argument("--port", type=int, default=4319)
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
