#!/usr/bin/env python3
"""Benchmark fan-out after a parent accumulates a controlled long context."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
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
    _wait_for_health,
    _write_router_config,
    evaluate_checks,
    read_jsonl_since,
    temporary_codex_config,
)
from bench.run_subagent import (
    _infrastructure_failure,
    codex_thread_id,
    filter_thread_tree,
    thread_metrics,
)


TASKS_PATH = ROOT / "bench" / "tasks-long-parent.json"
FIXTURE_ROOT = ROOT / "runtime" / "bench-fixtures"
MODES = ("shadow_all", "live_all", "live_none")
CHARS_PER_FIXTURE = 24_000


def long_parent_schedule(
    levels: list[dict[str, Any]], repeats: int
) -> list[dict[str, Any]]:
    schedule = []
    for repeat in range(1, repeats + 1):
        for index, level in enumerate(levels):
            rotation = (repeat - 1 + index) % len(MODES)
            modes = MODES[rotation:] + MODES[:rotation]
            for mode in modes:
                schedule.append({"level": level, "mode": mode, "repeat": repeat})
    return schedule


def generate_fixtures(directory: Path, *, count: int, chars_per_file: int) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(1, count + 1):
        path = directory / f"context-{index:02d}.txt"
        lines = []
        sequence = 1
        while sum(len(line) for line in lines) < chars_per_file:
            lines.append(
                f"fixture={index:02d} record={sequence:05d} fact=amber-{index:02d}-{sequence:05d} "
                "This deterministic sentence exists only to build reproducible parent context.\n"
            )
            sequence += 1
        path.write_text("".join(lines), encoding="utf-8")
        paths.append(path)
    return paths


HOOK_SCRIPT = '''#!/usr/bin/env python3
import json
import sys

event = json.load(sys.stdin)
tool_input = dict(event.get("tool_input") or {})
tool_input["fork_turns"] = "none"
json.dump(
    {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": tool_input,
        }
    },
    sys.stdout,
    separators=(",", ":"),
)
'''


def install_fork_none_hook(workspace: Path) -> tuple[Path, Path]:
    directory = workspace / ".codex"
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "fork-none-hook.py"
    script.write_text(HOOK_SCRIPT, encoding="utf-8")
    script.chmod(0o700)
    hooks = directory / "hooks.json"
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": ".*spawn_agent",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 .codex/fork-none-hook.py",
                                    "timeout": 30,
                                }
                            ],
                        }
                    ]
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return hooks, script


def parent_context_before_children(rows: list[dict[str, Any]], root_thread_id: str) -> int | None:
    child_starts = [
        row.get("request_started_monotonic_ns")
        for row in rows
        if row.get("parent_thread_id") == root_thread_id
        and row.get("event") == "subagent_first"
        and isinstance(row.get("request_started_monotonic_ns"), int)
    ]
    if not child_starts:
        return None
    first_child = min(child_starts)
    candidates = [
        row
        for row in rows
        if row.get("thread_id") == root_thread_id
        and row.get("response_completed")
        and isinstance(row.get("response_finished_monotonic_ns"), int)
        and row["response_finished_monotonic_ns"] <= first_child
        and isinstance((row.get("usage") or {}).get("input_tokens"), int)
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda row: row["response_finished_monotonic_ns"])
    return latest["usage"]["input_tokens"]


def _stats(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "median": statistics.median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def aggregate_long_parent(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cells = {}
    for level in sorted({row["level_id"] for row in rows}):
        cells[level] = {}
        for mode in MODES:
            selected = [row for row in rows if row["level_id"] == level and row["mode"] == mode]
            child_threads = [
                thread for row in selected for thread in row["threads"] if thread["role"] == "subagent"
            ]
            parent_threads = [
                thread for row in selected for thread in row["threads"] if thread["role"] == "parent"
            ]
            cells[level][mode] = {
                "sessions": len(selected),
                "pass_rate": (
                    sum(bool(row["passed"]) for row in selected) / len(selected) if selected else None
                ),
                "parent_context_tokens": _stats(
                    [float(row["parent_context_tokens"]) for row in selected if row["parent_context_tokens"]]
                ),
                "child_first_input_tokens": _stats(
                    [float(t["first_input_tokens"]) for t in child_threads if t["first_input_tokens"]]
                ),
                "child_first_cached_tokens": _stats(
                    [float(t["first_cached_tokens"]) for t in child_threads if t["first_cached_tokens"] is not None]
                ),
                "child_first_cache_hit_ratio": _stats(
                    [float(t["first_cache_hit_ratio"]) for t in child_threads if t["first_cache_hit_ratio"] is not None]
                ),
                "parent_cost_usd": _stats(
                    [sum(t["cost_usd"] for t in row["threads"] if t["role"] == "parent") for row in selected]
                ),
                "subagent_cost_usd": _stats(
                    [sum(t["cost_usd"] for t in row["threads"] if t["role"] == "subagent") for row in selected]
                ),
                "total_cost_usd": _stats(
                    [sum(t["cost_usd"] for t in row["threads"]) for row in selected]
                ),
                "wall_ms": _stats([float(row["wall_ms"]) for row in selected]),
                "missing_completed": sum(
                    t["missing_completed"] for t in parent_threads + child_threads
                ),
            }
    return {"cells": cells}


def _load_levels() -> list[dict[str, Any]]:
    levels = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    if not isinstance(levels, list) or len(levels) != 3:
        raise ValueError("tasks-long-parent.json must contain exactly three levels")
    return levels


def _prompt(level: dict[str, Any], fixture_paths: list[Path]) -> str:
    names = [f"runtime/bench-fixtures/{path.name}" for path in fixture_paths[: level["fixture_count"]]]
    numbered = "\n".join(f"{index}. `{name}`" for index, name in enumerate(names, 1))
    return f"""先完成前置上下文累积，再做 fan-out。必须严格按下面顺序逐个文件读取：每个文件单独调用一次命令 `sed -n '1,99999p' <文件>`，看到输出后只做一句内部确认再读取下一个；不得合并命令、不得跳过、不得在全部读完前 spawn。

{numbered}

全部读完后，使用原生 collaboration spawn_agent 在同一轮并行启动恰好三个子 agent，调用时显式写 `fork_turns=all`，task_name 分别为 docstring_policy、docstring_sse、docstring_upstream。分别只让它们给 hipporoute/policy.py 的 inspect_request、hipporoute/sse.py 的 assemble_sse、hipporoute/upstream.py 的 UpstreamClient.open_response 补一条准确简短的 docstring。等待全部完成后运行 `python3 -m unittest -q tests.test_policy tests.test_sse tests.test_upstream`。父线程不要亲自修改这三个文件。"""


CHECKS = [
    {"type": "function_docstring", "path": "hipporoute/policy.py", "function": "inspect_request"},
    {"type": "function_docstring", "path": "hipporoute/sse.py", "function": "assemble_sse"},
    {"type": "function_docstring", "path": "hipporoute/upstream.py", "function": "open_response"},
    {
        "type": "command",
        "argv": [
            "python3", "-m", "unittest", "-q",
            "tests.test_policy", "tests.test_sse", "tests.test_upstream",
        ],
    },
]


def _run_attempt(
    item: dict[str, Any],
    sequence: int,
    attempt: int,
    run_dir: Path,
    work_root: Path,
    decision_log: Path,
    shadow_path: Path,
    fixtures: list[Path],
) -> dict[str, Any]:
    level = item["level"]
    mode = item["mode"]
    if mode == "shadow_all":
        shadow_path.touch()
    else:
        shadow_path.unlink(missing_ok=True)
    key = f"{sequence:03d}-{level['id']}-{mode}-r{item['repeat']}-a{attempt}"
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
        target_fixtures = workspace / "runtime" / "bench-fixtures"
        target_fixtures.mkdir(parents=True)
        for path in fixtures[: level["fixture_count"]]:
            shutil.copy2(path, target_fixtures / path.name)
        if mode == "live_none":
            install_fork_none_hook(workspace)
        command = [
            "codex", "-a", "never", "-s", "workspace-write", "exec", "--json",
            "--skip-git-repo-check",
        ]
        if mode == "live_none":
            command.append("--dangerously-bypass-hook-trust")
        command.append(_prompt(level, fixtures))
        environment = dict(os.environ)
        environment.pop("TYPESAFE_API_KEY", None)
        with output_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                completed = subprocess.run(
                    command,
                    cwd=workspace,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=2400,
                    check=False,
                )
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
                timed_out = True
        wall_ms = int(round((time.monotonic() - started) * 1000))
        time.sleep(0.1)
        window_rows = read_jsonl_since(decision_log, offset)
        root_thread_id = codex_thread_id(output_path)
        rows = filter_thread_tree(window_rows, root_thread_id) if root_thread_id else []
        checks_passed, check_details = evaluate_checks(workspace, CHECKS, "")
        metric_mode = "shadow" if mode == "shadow_all" else "live"
        threads = thread_metrics(rows, metric_mode, BACKTEST_PRICES)
        children = [thread for thread in threads if thread["role"] == "subagent"]
        infra = _infrastructure_failure(exit_code, rows)
        return {
            "level_id": level["id"],
            "target_context_tokens": level["target_context_tokens"],
            "fixture_count": level["fixture_count"],
            "mode": mode,
            "repeat": item["repeat"],
            "sequence": sequence,
            "attempt": attempt,
            "root_thread_id": root_thread_id,
            "observed_children": len(children),
            "child_count_ok": len(children) == 3,
            "parent_context_tokens": (
                parent_context_before_children(rows, root_thread_id) if root_thread_id else None
            ),
            "threads": threads,
            "request_count": len(rows),
            "wall_ms": wall_ms,
            "codex_exit_code": exit_code,
            "timed_out": timed_out,
            "checks": check_details,
            "infrastructure_failure": infra,
            "passed": exit_code == 0 and checks_passed and len(children) == 3 and not infra,
            "output_path": output_path.relative_to(ROOT).as_posix(),
            "stderr_path": stderr_path.relative_to(ROOT).as_posix(),
        }
    finally:
        if workspace.exists():
            shutil.rmtree(workspace)


def run_benchmark(args: argparse.Namespace) -> Path:
    levels = _load_levels()
    if args.levels:
        wanted = set(args.levels.split(","))
        levels = [level for level in levels if level["id"] in wanted]
    modes = tuple(part for part in args.modes.split(",") if part)
    if not modes or any(mode not in MODES for mode in modes):
        raise ValueError(f"modes must be a subset of {MODES}")
    schedule = [item for item in long_parent_schedule(levels, args.repeats) if item["mode"] in modes]
    run_id = args.run_id or time.strftime("long-parent-%Y%m%d-%H%M%S")
    run_dir = ROOT / "runtime" / "bench" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    work_root = ROOT / "runtime" / "bench-work" / run_id
    fixtures = generate_fixtures(
        FIXTURE_ROOT,
        count=max(int(level["fixture_count"]) for level in levels),
        chars_per_file=args.chars_per_fixture,
    )
    router_config = run_dir / "router.toml"
    decision_log = run_dir / "decisions.jsonl"
    shadow_path = run_dir / "router.shadow"
    off_path = run_dir / "router.off"
    results_path = run_dir / "results.jsonl"
    attempts_path = run_dir / "attempts.jsonl"
    summary_path = run_dir / "summary.json"
    backup_path = run_dir / "config.toml.backup"
    _write_router_config(router_config, run_dir, args.port)
    existing = read_jsonl_since(results_path, 0)
    completed_keys = {(r["level_id"], r["mode"], r["repeat"]) for r in existing}
    server_log = (run_dir / "server.log").open("ab")
    process = None
    try:
        with temporary_codex_config(
            Path(args.codex_config).expanduser(), backup_path, f"http://127.0.0.1:{args.port}/v1"
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
                cell = (item["level"]["id"], item["mode"], item["repeat"])
                if cell in completed_keys:
                    continue
                final = None
                for attempt in range(1, args.max_retries + 2):
                    final = _run_attempt(
                        item, sequence, attempt, run_dir, work_root, decision_log,
                        shadow_path, fixtures,
                    )
                    _append_jsonl(attempts_path, final)
                    cost = sum(thread["cost_usd"] for thread in final["threads"])
                    print(
                        f"[{sequence}/{len(schedule)}] {cell} a{attempt} "
                        f"ctx={final['parent_context_tokens']} children={final['observed_children']}/3 "
                        f"pass={final['passed']} infra={final['infrastructure_failure']} "
                        f"cost=${cost:.6f} wall={final['wall_ms']/1000:.1f}s",
                        flush=True,
                    )
                    if not final["infrastructure_failure"]:
                        break
                _append_jsonl(results_path, final)
                completed_keys.add(cell)
            results = read_jsonl_since(results_path, 0)
            summary = aggregate_long_parent(results)
            summary.update(run_id=run_id, expected_sessions=len(schedule), recorded_sessions=len(results))
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        shadow_path.unlink(missing_ok=True)
        off_path.unlink(missing_ok=True)
        (run_dir / "stream.debug").unlink(missing_ok=True)
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
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--levels", default="")
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--run-id")
    parser.add_argument("--port", type=int, default=4320)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--chars-per-fixture", type=int, default=CHARS_PER_FIXTURE)
    parser.add_argument("--codex-config", default=str(DEFAULT_CODEX_CONFIG))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.repeats < 1 or args.max_retries < 0 or args.chars_per_fixture < 100:
        raise SystemExit("invalid repeat, retry, or fixture size")
    run_dir = run_benchmark(args)
    print(f"results={run_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
