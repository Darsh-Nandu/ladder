"""Terminal front-end: python -m app.cli SPEC_FILE [--tests tests.py] [--policy ladder] [-o solution.py]"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .agent import POLICIES, Agent, Task
from .llm import TokenFactory

C = {"nano": "\033[92m", "super": "\033[96m", "ultra": "\033[95m", "ok": "\033[92m", "fail": "\033[91m",
     "warn": "\033[93m", "dim": "\033[2m", "end": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def tag(tier: str) -> str:
    return f"{C.get(tier, '')}[{tier}]{C['end']}"


async def main() -> int:
    ap = argparse.ArgumentParser(description="Ladder: adaptive-compute coding agent on Nebius Token Factory")
    ap.add_argument("spec", help="file with the task specification, or '-' for stdin")
    ap.add_argument("--tests", help="your own test file (test_* functions); otherwise Nemotron writes tests")
    ap.add_argument("--starter", help="file with a required signature / starter code")
    ap.add_argument("--policy", default="ladder-wide", choices=sorted(POLICIES))
    ap.add_argument("-o", "--out", help="write the passing solution here")
    a = ap.parse_args()

    spec = sys.stdin.read() if a.spec == "-" else Path(a.spec).read_text()
    task = Task(spec=spec, tests=Path(a.tests).read_text() if a.tests else "",
                starter=Path(a.starter).read_text() if a.starter else "")
    llm = TokenFactory()
    final = None
    try:
        async for ev in Agent(llm).solve(task, POLICIES[a.policy]):
            t = ev["type"]
            if t == "call":
                draft = f" (draft {ev['candidate'] + 1}/{ev['width']})" if ev.get("width", 1) > 1 else ""
                print(f"{tag(ev['tier'])} {ev['role']}{' #' + str(ev['attempt']) if ev.get('attempt') else ''}{draft} …",
                      flush=True)
            elif t == "llm":
                print(f"{C['dim']}      {ev['tokens_in']}→{ev['tokens_out']} tok  ${ev['cost']:.5f}  {ev['latency_s']}s{C['end']}")
            elif t == "triage":
                print(f"{C['warn']}triage{C['end']} difficulty={ev['difficulty']} → start on {tag(ev['start'])}")
            elif t == "tests":
                print(f"{C['ok']}tests{C['end']} {ev['tests'].count('def test')} ({ev['source']})")
            elif t == "run":
                col = C["ok"] if ev["ok"] else C["fail"]
                print(f"{col}sandbox{C['end']} {ev['summary'].splitlines()[0]}")
                if not ev["ok"]:
                    print(C["dim"] + "\n".join("      " + ln for ln in ev["summary"].splitlines()[1:6]) + C["end"])
            elif t == "pick":
                scores = "  ".join(f"#{x['candidate'] + 1}:{x['passed']}/{x['total']}" for x in ev["scores"])
                col = C["ok"] if ev["ok"] else C["warn"]
                print(f"{col}pick{C['end']} draft {ev['candidate'] + 1} of {ev['width']}  [{scores}]")
            elif t == "escalate":
                print(f"{C['warn']}escalate{C['end']} {tag(ev['from'])} → {tag(ev['to'])}")
            elif t == "error":
                print(f"{C['fail']}error{C['end']} {ev['message']}")
            elif t == "done":
                final = ev
    finally:
        await llm.aclose()

    if not final:
        return 2
    saved = 1 - final["cost"] / final["ultra_equivalent_cost"] if final["ultra_equivalent_cost"] else 0
    print(f"\n{final['status'].upper()}  solved_by={final['solved_by']}  attempts={final['attempts']}  "
          f"cost=${final['cost']:.4f} (same tokens on Ultra ${final['ultra_equivalent_cost']:.4f}, -{saved:.0%})  "
          f"{final['wall_seconds']}s")
    if final["status"] == "solved":
        if a.out:
            Path(a.out).write_text(final["code"] + "\n")
            print(f"wrote {a.out}")
        else:
            print("\n" + final["code"])
    return 0 if final["status"] == "solved" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
