"""Check bench/tasks.jsonl: schema, reference passes hidden tests, a stub fails most of them.

    python bench/validate_tasks.py
"""
from __future__ import annotations

import ast
import json
import sys
import threading
from collections import Counter
from pathlib import Path

REQUIRED = {"id": str, "title": str, "difficulty": str, "demo": bool, "spec": str,
            "starter": str, "reference": str, "hidden_tests": str}


def stub_from_starter(starter: str) -> str:
    """Same names as the starter, every function/method returning None."""
    tree = ast.parse(starter.strip() or "pass")

    class Stub(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            node.body = [ast.Return(ast.Constant(None))]
            return node

    return ast.unparse(ast.fix_missing_locations(Stub().visit(tree)))


def run_suite(module_src: str, tests_src: str, timeout: float = 5.0) -> tuple[int, int, list[str]]:
    ns: dict = {}
    result = {"passed": 0, "failed": 0, "errors": []}

    def go():
        exec(compile(module_src, "module", "exec"), ns)
        exec(compile(tests_src, "tests", "exec"), ns)
        for name, fn in list(ns.items()):
            if name.startswith("test") and callable(fn):
                try:
                    fn()
                    result["passed"] += 1
                except BaseException as e:  # noqa: BLE001
                    result["failed"] += 1
                    result["errors"].append(f"{name}: {type(e).__name__}: {str(e)[:120]}")

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return result["passed"], result["failed"] + 1, ["TIMEOUT"]
    return result["passed"], result["failed"], result["errors"]


def main() -> int:
    path = Path(__file__).resolve().parent / "tasks.jsonl"
    tasks = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    ok = True
    for t in tasks:
        problems = [f"field {k} missing/not {ty.__name__}" for k, ty in REQUIRED.items() if not isinstance(t.get(k), ty)]
        if problems:
            print(f"{t.get('id', '?'):28s} FAIL  {problems}")
            ok = False
            continue
        try:
            p, f, errs = run_suite(t["reference"], t["hidden_tests"])
        except BaseException as e:  # noqa: BLE001
            p, f, errs = 0, 1, [f"load error: {e}"]
        n = p + f
        try:
            sp, sf, _ = run_suite(stub_from_starter(t["starter"]), t["hidden_tests"])
        except BaseException:  # noqa: BLE001
            sp, sf = 0, n
        stub_fail_rate = sf / (sp + sf) if sp + sf else 0
        good = f == 0 and 8 <= n <= 15 and stub_fail_rate >= 0.7
        ok &= good
        why = "" if good else f"  ref_failed={f} n={n} stub_fail_rate={stub_fail_rate:.0%} {errs[:3]}"
        print(f"{t['id']:28s} {t['difficulty']:7s} n={n:2d} {'PASS' if good else 'FAIL'}{why}")

    ids = Counter(t.get("id") for t in tasks)
    dups = [i for i, c in ids.items() if c > 1]
    diff = Counter(t.get("difficulty") for t in tasks)
    demos = Counter(t.get("difficulty") for t in tasks if t.get("demo"))
    print(f"\n{len(tasks)} tasks  difficulty={dict(diff)}  demo={dict(demos)}  duplicate_ids={dups}")
    if dups or diff != Counter({"easy": 8, "medium": 8, "hard": 8}) or sum(demos.values()) != 6:
        ok = False
    print("ALL OK" if ok else "VALIDATION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
