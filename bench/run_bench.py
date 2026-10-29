"""Benchmark Ladder policies on bench/tasks.jsonl, scored on hidden tests.

    python -m bench.run_bench                       # all policies, all tasks
    python -m bench.run_bench -p ladder ultra-only  # selected policies
    python -m bench.run_bench --difficulty hard --limit 4 -c 6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.agent import POLICIES, Agent, Task
from app.llm import TokenFactory
from app.sandbox import run_tests

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"


def load_tasks(difficulty: str | None, ids: list[str] | None, limit: int | None) -> list[dict]:
    tasks = [json.loads(l) for l in (HERE / "tasks.jsonl").read_text().splitlines() if l.strip()]
    if difficulty:
        tasks = [t for t in tasks if t["difficulty"] == difficulty]
    if ids:
        tasks = [t for t in tasks if t["id"] in ids]
    return tasks[:limit] if limit else tasks


async def run_one(agent: Agent, task: dict, policy: str, sem: asyncio.Semaphore, log) -> dict:
    async with sem:
        done, n_escalations = None, 0
        t = Task(spec=task["spec"], starter=task.get("starter", ""), id=task["id"])
        async for ev in agent.solve(t, POLICIES[policy]):
            if ev["type"] == "escalate":
                n_escalations += 1
            if ev["type"] == "done":
                done = ev
        hidden = await run_tests(done["code"], task["hidden_tests"]) if done and done["code"] else None
        rec = {
            "task": task["id"], "difficulty": task["difficulty"], "policy": policy,
            "agent_status": done["status"] if done else "error",
            "solved": bool(hidden and hidden.ok),
            "hidden": f"{hidden.passed}/{hidden.passed + hidden.failed}" if hidden and not hidden.fatal else (hidden.fatal[:80] if hidden else "-"),
            "solved_by": done.get("solved_by") if done else None,
            "attempts": done["attempts"] if done else 0, "escalations": n_escalations,
            "cost": done["cost"] if done else 0.0, "tokens": (done["tokens_in"] + done["tokens_out"]) if done else 0,
            "wall_s": done["wall_seconds"] if done else 0.0, "by_tier": done["by_tier"] if done else {},
        }
        log.write(json.dumps(rec) + "\n")
        log.flush()
        mark = "PASS" if rec["solved"] else "fail"
        print(f"  {policy:12s} {task['id']:28s} {mark}  hidden={rec['hidden']:6s} by={rec['solved_by']}  "
              f"${rec['cost']:.4f}  {rec['wall_s']:.0f}s", flush=True)
        return rec


def summarize(records: list[dict], tasks: list[dict], policies: list[str]) -> dict:
    diff_counts = Counter(t["difficulty"] for t in tasks)
    order = [d for d in ("easy", "medium", "hard") if d in diff_counts]
    out = []
    for p in policies:
        rs = [r for r in records if r["policy"] == p]
        solved = [r for r in rs if r["solved"]]
        cost = sum(r["cost"] for r in rs)
        out.append({
            "name": p, "n": len(rs), "solved": len(solved),
            "pass_rate": len(solved) / len(rs) if rs else 0.0,
            "cost": cost, "cost_per_solve": cost / len(solved) if solved else 0.0,
            "avg_wall": sum(r["wall_s"] for r in rs) / len(rs) if rs else 0.0,
            "tokens": sum(r["tokens"] for r in rs),
            "by_difficulty": {d: {"n": sum(r["difficulty"] == d for r in rs),
                                  "solved": sum(r["difficulty"] == d and r["solved"] for r in rs)} for d in order},
            "solved_by": dict(Counter(r["solved_by"] for r in solved if r["solved_by"])),
        })
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "n_tasks": len(tasks), "difficulty_counts": {d: diff_counts[d] for d in order},
        "policies": out, "records": records,
    }


def markdown_table(summary: dict) -> str:
    diffs = list(summary["difficulty_counts"])
    head = "| Policy | Solved (hidden tests) | " + " | ".join(diffs) + " | Total cost | $ / solve | Avg time | Solves finished on |"
    sep = "|" + "---|" * (len(diffs) + 6)
    rows = [head, sep]
    for p in summary["policies"]:
        cells = [f"**{p['name']}**", f"{p['solved']}/{p['n']} ({p['pass_rate']:.0%})"]
        cells += [f"{p['by_difficulty'][d]['solved']}/{p['by_difficulty'][d]['n']}" for d in diffs]
        cells += [f"${p['cost']:.4f}", f"${p['cost_per_solve']:.4f}", f"{p['avg_wall']:.1f}s",
                  ", ".join(f"{k} {v}" for k, v in p["solved_by"].items()) or "-"]
        rows.append("| " + " | ".join(cells) + " |")
    return f"_{summary['n_tasks']} tasks, run {summary['generated_at']}_\n\n" + "\n".join(rows)


def update_readme(summary: dict) -> None:
    readme = HERE.parent / "README.md"
    start, end = "<!-- BENCH_RESULTS_START -->", "<!-- BENCH_RESULTS_END -->"
    text = readme.read_text()
    if start in text and end in text:
        pre, rest = text.split(start, 1)
        _, post = rest.split(end, 1)
        readme.write_text(f"{pre}{start}\n{markdown_table(summary)}\n{end}{post}")


def print_table(summary: dict) -> None:
    print(f"\n{'policy':14s} {'solved':>8s} {'cost $':>9s} {'$/solve':>9s} {'avg s':>7s}  solved_by")
    for p in summary["policies"]:
        print(f"{p['name']:14s} {p['solved']:>3d}/{p['n']:<4d} {p['cost']:9.4f} {p['cost_per_solve']:9.4f} "
              f"{p['avg_wall']:7.1f}  {p['solved_by']}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--policies", nargs="+", default=["ladder", "ladder-wide", "ladder-auto", "nano-only", "super-only", "ultra-only"])
    ap.add_argument("--difficulty", choices=["easy", "medium", "hard"])
    ap.add_argument("--ids", nargs="+")
    ap.add_argument("--limit", type=int)
    ap.add_argument("-c", "--concurrency", type=int, default=6)
    ap.add_argument("--no-latest", action="store_true", help="don't overwrite results/latest.json")
    args = ap.parse_args()

    tasks = load_tasks(args.difficulty, args.ids, args.limit)
    RESULTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    llm = TokenFactory()
    agent = Agent(llm)
    sem = asyncio.Semaphore(args.concurrency)
    print(f"{len(tasks)} tasks x {len(args.policies)} policies")
    with open(RESULTS / f"run-{stamp}.jsonl", "w") as log:
        jobs = [run_one(agent, t, p, sem, log) for p in args.policies for t in tasks]
        records = await asyncio.gather(*jobs)
    await llm.aclose()
    summary = summarize(list(records), tasks, args.policies)
    (RESULTS / f"summary-{stamp}.json").write_text(json.dumps(summary, indent=1))
    if not args.no_latest:
        (RESULTS / "latest.json").write_text(json.dumps(summary, indent=1))
        update_readme(summary)
    print_table(summary)


if __name__ == "__main__":
    asyncio.run(main())
