"""Ladder: an adaptive-compute coding agent.

Write code -> run tests in a sandbox -> repair. Every attempt starts on the cheapest Nemotron
rung that is likely to succeed and climbs (Nano -> Super -> Ultra) only when the tests keep failing.
The whole run is an async stream of events, so the UI and the benchmark see the same thing.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from . import config
from .llm import LLMError, LLMResult, TokenFactory
from .sandbox import RunReport, run_tests


@dataclass
class Task:
    spec: str
    tests: str = ""              # user-provided tests (immutable); generated when empty
    starter: str = ""            # optional starter code / signature
    id: str = ""


@dataclass
class Policy:
    name: str
    ladder: list[str]            # rungs available, in order
    start: str = "nano"          # "auto" = let Nano triage pick the first rung
    attempts_per_tier: int = 2
    test_tier: str = "super"     # who writes tests when the user gave none

    def to_dict(self) -> dict:
        return self.__dict__.copy()


POLICIES: dict[str, Policy] = {
    "ladder": Policy("ladder", ["nano", "super", "ultra"], start="nano"),
    "ladder-auto": Policy("ladder-auto", ["nano", "super", "ultra"], start="auto"),
    "nano-only": Policy("nano-only", ["nano"], attempts_per_tier=4, test_tier="nano"),
    "super-only": Policy("super-only", ["super"], attempts_per_tier=4),
    "ultra-only": Policy("ultra-only", ["ultra"], attempts_per_tier=4, test_tier="ultra"),
}


# --------------------------------------------------------------------------- prompts

TRIAGE_SYS = """You are the dispatcher of a coding agent. Read the programming task and rate how hard it is
for a small, fast code model to solve correctly on the first try.
- easy: direct implementation, standard library, few edge cases
- medium: needs a known algorithm or careful edge-case handling
- hard: tricky algorithm, subtle invariants, parsing, DP/graph/geometry, or long multi-part spec
Reply with ONLY a JSON object: {"difficulty": "easy|medium|hard", "why": "<12 words>"}"""

TESTS_SYS = """You write Python unit tests for a function that does not exist yet.
Rules:
- Output ONE ```python code block containing only test functions named test_* using plain assert.
- Do NOT import or define the function under test; it is already in scope. Standard-library imports are fine.
- 6-10 tests. Cover every example in the spec, normal cases, and edge cases the spec clearly defines.
- Never guess behaviour the spec leaves undefined. Every expected value must follow unambiguously from the spec;
  compute expected values carefully by hand. A wrong test is worse than a missing one.
- Give each assert a short message, e.g. assert f(2) == 4, "f(2)"."""

CODER_SYS = """You are an expert Python engineer inside an automated write-run-test loop.
Output the COMPLETE solution module in ONE ```python code block: imports, helpers and the required function(s).
Standard library only. No prints, no input(), no example usage, no tests in the solution block.
Make the code correct for every case in the specification, not just the visible tests."""

CODER_TESTFIX = """
You are a senior rung of the ladder. If, and only if, you are certain that a generated test contradicts the
specification, you may also output a corrected full test file in a second code block that starts with the line
`# TESTS` and explain in one line beginning with `TESTFIX:`. Never weaken a test just to make code pass."""


# --------------------------------------------------------------------------- helpers

_BLOCK_RE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.S)


def extract_blocks(text: str) -> list[str]:
    blocks = [b.strip() for b in _BLOCK_RE.findall(text)]
    if not blocks and ("def " in text or "import " in text):
        blocks = [text.strip()]
    return blocks


def extract_code(text: str) -> tuple[str, str | None]:
    """Return (solution_code, replacement_tests_or_None)."""
    code, tests = "", None
    for b in extract_blocks(text):
        if b.lstrip().startswith("# TESTS"):
            tests = b
        elif not code:
            code = b
    return code, tests


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n // 2] + "\n…\n" + s[-n // 2 :]


@dataclass
class Ledger:
    """Running totals for one solve."""
    cost: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_seconds: float = 0.0
    calls: list[dict] = field(default_factory=list)
    by_tier: dict = field(default_factory=dict)

    def add(self, role: str, r: LLMResult) -> dict:
        self.cost += r.cost
        self.tokens_in += r.tokens_in
        self.tokens_out += r.tokens_out
        self.llm_seconds += r.latency_s
        t = self.by_tier.setdefault(r.tier, {"calls": 0, "cost": 0.0, "tokens": 0})
        t["calls"] += 1
        t["cost"] += r.cost
        t["tokens"] += r.tokens_in + r.tokens_out
        rec = {"role": role, "tier": r.tier, "model": r.model, "tokens_in": r.tokens_in,
               "tokens_out": r.tokens_out, "cost": r.cost, "latency_s": round(r.latency_s, 2)}
        self.calls.append(rec)
        return rec

    def ultra_equivalent(self) -> float:
        """Same tokens priced at Ultra rates (a lower bound: Ultra also thinks longer)."""
        return config.TIERS["ultra"].cost(self.tokens_in, self.tokens_out)


# --------------------------------------------------------------------------- agent

class Agent:
    def __init__(self, llm: TokenFactory):
        self.llm = llm

    async def _call(self, ledger: Ledger, role: str, tier: str, messages: list[dict], **kw):
        r = await self.llm.chat(config.TIERS[tier], messages, **kw)
        rec = ledger.add(role, r)
        return r, {"type": "llm", **rec, "content": _clip(r.content, 6000), "reasoning": _clip(r.reasoning, 4000),
                   "finish_reason": r.meta.get("finish_reason")}

    async def solve(self, task: Task, policy: Policy) -> AsyncIterator[dict]:
        t0 = time.perf_counter()
        ledger = Ledger()
        yield {"type": "start", "task_id": task.id, "policy": policy.to_dict(),
               "models": {k: config.TIERS[k].model for k in policy.ladder}}
        try:
            # 1. Triage: which rung do we start on?
            start = policy.start if policy.start in policy.ladder else policy.ladder[0]
            if policy.start == "auto":
                yield {"type": "call", "role": "triage", "tier": "nano", "model": config.TIERS["nano"].model}
                r, ev = await self._call(ledger, "triage", "nano", [
                    {"role": "system", "content": TRIAGE_SYS},
                    {"role": "user", "content": task.spec},
                ], thinking=False, max_tokens=200)
                yield ev
                diff = str(extract_json(r.content).get("difficulty", "medium")).lower()
                start = {"easy": "nano", "medium": "super", "hard": "ultra"}.get(diff, "super")
                if start not in policy.ladder:
                    start = policy.ladder[0]
                yield {"type": "triage", "difficulty": diff, "start": start, "why": extract_json(r.content).get("why", "")}

            # 2. Tests: user-provided ones are law; otherwise a stronger rung writes them.
            tests, tests_locked = task.tests.strip(), bool(task.tests.strip())
            if not tests:
                tt = policy.test_tier if policy.test_tier in config.TIERS else "super"
                yield {"type": "call", "role": "tests", "tier": tt, "model": config.TIERS[tt].model}
                r, ev = await self._call(ledger, "tests", tt, [
                    {"role": "system", "content": TESTS_SYS},
                    {"role": "user", "content": self._spec_block(task)},
                ], thinking=False)
                yield ev
                blocks = extract_blocks(r.content)
                tests = blocks[0] if blocks else ""
                if "def test" not in tests:
                    raise LLMError("test writer returned no test functions")
            yield {"type": "tests", "tests": tests, "source": "user" if tests_locked else "generated"}

            # 3. Write -> run -> repair, climbing the ladder on repeated failure.
            code, report = "", None
            history: list[str] = []
            rungs = policy.ladder[policy.ladder.index(start):]
            attempt_no = 0
            for ti, tier in enumerate(rungs):
                if ti > 0:
                    yield {"type": "escalate", "from": rungs[ti - 1], "to": tier,
                           "reason": report.summary(2).splitlines()[0] if report else ""}
                for k in range(policy.attempts_per_tier):
                    attempt_no += 1
                    messages = self._coder_messages(task, tests, code, report, history, tier, tests_locked)
                    yield {"type": "call", "role": "code", "tier": tier, "model": config.TIERS[tier].model,
                           "attempt": attempt_no}
                    r, ev = await self._call(ledger, "code", tier, messages)
                    yield ev
                    new_code, new_tests = extract_code(r.content)
                    if new_tests and not tests_locked and tier != "nano" and "def test" in new_tests:
                        why = next((ln for ln in r.content.splitlines() if ln.startswith("TESTFIX:")), "TESTFIX:")
                        tests = new_tests
                        yield {"type": "tests", "tests": tests, "source": f"revised by {tier}", "why": why[8:].strip()}
                    if not new_code:
                        report = RunReport(fatal="Model returned no code block")
                    else:
                        code = new_code
                        report = await run_tests(code, tests)
                    yield {"type": "run", "tier": tier, "attempt": attempt_no, "code": code, "report": report.to_dict(),
                           "ok": report.ok, "summary": report.summary()}
                    if report.ok:
                        yield self._done("solved", tier, code, tests, ledger, attempt_no, t0)
                        return
                    history.append(f"attempt {attempt_no} ({tier}): {report.summary(3).splitlines()[0]}")
            yield self._done("failed", rungs[-1], code, tests, ledger, attempt_no, t0)
        except LLMError as e:
            yield {"type": "error", "message": str(e)}
            yield self._done("error", "", "", "", ledger, 0, t0)

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _spec_block(task: Task) -> str:
        s = f"## Specification\n{task.spec.strip()}"
        if task.starter.strip():
            s += f"\n\n## Required signature / starter code\n```python\n{task.starter.strip()}\n```"
        return s

    def _coder_messages(self, task: Task, tests: str, code: str, report: RunReport | None,
                        history: list[str], tier: str, tests_locked: bool) -> list[dict]:
        sys_prompt = CODER_SYS + ("" if tests_locked or tier == "nano" else CODER_TESTFIX)
        user = self._spec_block(task) + f"\n\n## Tests your code must pass\n```python\n{tests}\n```"
        if code and report:
            user += (f"\n\n## Previous attempt\n```python\n{code}\n```\n\n## Result of running the tests\n"
                     f"{_clip(report.summary(), 2500)}")
            if len(history) > 1:
                user += "\n\n## Earlier attempts (all failed)\n" + "\n".join(history[:-1][-5:])
            user += ("\n\nDiagnose the root cause of the failure first, then write a corrected, complete solution. "
                     "Do not repeat an approach that already failed.")
        return [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]

    @staticmethod
    def _done(status: str, tier: str, code: str, tests: str, ledger: Ledger, attempts: int, t0: float) -> dict:
        return {
            "type": "done", "status": status, "solved_by": tier if status == "solved" else None,
            "code": code, "tests": tests, "attempts": attempts,
            "cost": ledger.cost, "ultra_equivalent_cost": ledger.ultra_equivalent(),
            "tokens_in": ledger.tokens_in, "tokens_out": ledger.tokens_out,
            "llm_seconds": round(ledger.llm_seconds, 2), "wall_seconds": round(time.perf_counter() - t0, 2),
            "by_tier": ledger.by_tier, "calls": ledger.calls,
        }
