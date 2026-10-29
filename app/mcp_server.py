"""Ladder as an MCP server: coding assistants delegate "write a verified function" to the Nemotron ladder.

    python -m app.mcp_server                 # stdio (Claude Code, Cursor, ...)
    python -m app.mcp_server --http [--port 8765]   # streamable HTTP at http://127.0.0.1:8765/mcp

Requires the MCP SDK v2 (`pip install -r requirements-mcp.txt`). See docs/mcp.md.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, TypedDict

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .agent import POLICIES, Agent, Task
from .llm import TokenFactory

MAX_SPEC = 8000
MAX_TESTS = 12000
MAX_STARTER = 8000

POLICY_HELP: dict[str, str] = {
    "ladder": "Start on Nano, climb to Super then Ultra after 2 failed rounds per rung.",
    "ladder-wide": "Like ladder, but 3 parallel Nano drafts per round before climbing (default; cheapest).",
    "ladder-auto": "Nano triages the task first and picks the starting rung (easy=Nano, medium=Super, hard=Ultra).",
    "nano-only": "Nano only, 4 rounds. Baseline: cheapest, weakest.",
    "super-only": "Super only, 4 rounds. Baseline.",
    "ultra-only": "Ultra only, 4 rounds. Baseline: strongest, most expensive.",
}


class SolveResult(TypedDict):
    status: str                 # solved | failed | error
    solved_by: str | None       # rung that produced passing code
    attempts: int
    cost_usd: float
    ultra_equivalent_cost_usd: float
    wall_seconds: float
    code: str                   # final (best) solution module
    tests: str                  # test file the code was verified against
    summary: str                # e.g. "nano ✗✗ → super ✓ — solved by super ..."
    error: str | None


class PolicyInfo(TypedDict):
    name: str
    description: str
    ladder: list[str]


OnEvent = Callable[[dict], Awaitable[None]]


def _rung_path(marks: list[tuple[str, bool]]) -> str:
    """[(tier, ok), ...] -> 'nano ✗✗ → super ✓' (one mark per round)."""
    parts: list[str] = []
    last = None
    for tier, ok in marks:
        if tier != last:
            parts.append(f"{tier} ")
            last = tier
        parts[-1] += "✓" if ok else "✗"
    return " → ".join(parts)


def _summary(done: dict, marks: list[tuple[str, bool]], error: str | None) -> str:
    path = _rung_path(marks)
    cost = f"${done['cost']:.4f} (Ultra-only equivalent ${done['ultra_equivalent_cost']:.4f}), {done['wall_seconds']}s"
    if done["status"] == "solved":
        head = f"solved by {done['solved_by']} in {done['attempts']} attempt(s)"
    elif done["status"] == "failed":
        head = f"failed: no draft passed all tests after {done['attempts']} attempt(s)"
    else:
        head = f"error: {error or 'unknown'}"
    return " — ".join(p for p in (path, head, cost) if p)


def validate(spec: str, tests: str, starter: str, policy: str) -> None:
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; choose one of: {', '.join(POLICIES)}")
    if not spec.strip():
        raise ValueError("spec is empty")
    for name, value, limit in (("spec", spec, MAX_SPEC), ("tests", tests, MAX_TESTS), ("starter", starter, MAX_STARTER)):
        if len(value) > limit:
            raise ValueError(f"{name} is {len(value)} chars; the limit is {limit}")


async def run_solve(agent: Agent, spec: str, tests: str = "", starter: str = "", policy: str = "ladder-wide",
                    on_event: OnEvent | None = None) -> SolveResult:
    """Run one Ladder solve to completion and condense its event stream into a SolveResult.
    Raises ValueError on bad input; `on_event` sees every agent event (for progress reporting)."""
    validate(spec, tests, starter, policy)
    marks: list[tuple[str, bool]] = []
    error: str | None = None
    done: dict | None = None
    async for ev in agent.solve(Task(spec=spec, tests=tests, starter=starter, id="mcp"), POLICIES[policy]):
        t = ev["type"]
        if t == "run" and ev.get("width", 1) == 1:
            marks.append((ev["tier"], bool(ev["ok"])))
        elif t == "pick":  # parallel round: one mark for the winning draft
            marks.append((ev["tier"], bool(ev["ok"])))
        elif t == "error" and not ev.get("recoverable"):
            error = ev["message"]
        elif t == "done":
            done = ev
        if on_event is not None and t != "delta":
            await on_event(ev)
    if done is None:  # the contract guarantees a done event; be defensive anyway
        raise RuntimeError("agent stream ended without a done event")
    return SolveResult(
        status=done["status"], solved_by=done["solved_by"], attempts=done["attempts"],
        cost_usd=round(done["cost"], 6), ultra_equivalent_cost_usd=round(done["ultra_equivalent_cost"], 6),
        wall_seconds=done["wall_seconds"], code=done["code"], tests=done["tests"],
        summary=_summary(done, marks, error), error=error,
    )


def _progress_message(ev: dict) -> str | None:
    t = ev["type"]
    if t == "call":
        what = f"draft {ev['attempt']}" if ev["role"] == "code" else ev["role"]
        return f"{ev['tier']}: {what}"
    if t == "run":
        return f"{ev['tier']} draft {ev['attempt']}: {'pass' if ev['ok'] else 'fail'} ({ev['summary'].splitlines()[0]})"
    if t == "escalate":
        return f"escalating {ev['from']} → {ev['to']}: {ev['reason']}"
    if t == "triage":
        return f"triage: {ev['difficulty']}, starting on {ev['start']}"
    return None


# --------------------------------------------------------------------------- server

_llm: TokenFactory | None = None


def _get_llm() -> TokenFactory:
    """Created on first use so listing tools works without an API key or network."""
    global _llm
    if _llm is None:
        _llm = TokenFactory()
    return _llm


@asynccontextmanager
async def _lifespan(_: MCPServer) -> AsyncIterator[None]:
    global _llm
    try:
        yield None
    finally:
        if _llm is not None:
            await _llm.aclose()
            _llm = None


app: MCPServer = MCPServer(
    "ladder",
    instructions="Ladder writes a Python function from a spec, verifies it against unit tests in a sandbox, and "
                 "escalates NVIDIA Nemotron tiers (Nano → Super → Ultra) only when tests keep failing. "
                 "Call solve_function with a precise spec (and tests if you have them); it returns tested code.",
    lifespan=_lifespan,
)


@app.tool()
async def solve_function(spec: str, tests: str = "", starter: str = "", policy: str = "ladder-wide",
                         ctx: Context | None = None) -> SolveResult:
    """Write a Python function that passes unit tests, using the cheapest Nemotron rung that can do it.

    spec: what the function must do (name, signature, behaviour, examples). Max 8000 chars.
    tests: optional pytest-style test functions (plain asserts, the function is already in scope). They are
        never modified. When empty, Ladder generates tests from the spec. Max 12000 chars.
    starter: optional signature / starter code.
    policy: one of list_policies(); default 'ladder-wide'.
    Returns status (solved|failed|error), the code, the tests it passed, cost and a one-line summary.
    Can take a few minutes on hard tasks.
    """
    step = 0

    async def on_event(ev: dict) -> None:
        nonlocal step
        msg = _progress_message(ev)
        if msg is None or ctx is None:
            return
        step += 1
        # Progress only: MCP logging (ctx.info) is deprecated since 2026-07-28 (SEP-2577) and opt-in per request.
        try:  # best-effort: never fail a solve because a notification could not be sent
            await ctx.report_progress(step, None, msg)
        except Exception:
            pass

    try:
        return await run_solve(Agent(_get_llm()), spec, tests, starter, policy, on_event)
    except ValueError as e:
        raise ToolError(str(e)) from e


@app.tool()
def list_policies() -> list[PolicyInfo]:
    """List the escalation policies solve_function accepts, with a one-line description each."""
    return [PolicyInfo(name=n, description=POLICY_HELP.get(n, ""), ladder=list(p.ladder)) for n, p in POLICIES.items()]


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ladder MCP server")
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1",
                    help="HTTP bind address. HTTP mode has NO authentication: anyone who can reach it can run "
                         "generated code and spend your NEBIUS_API_KEY, so keep it on loopback")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allow-remote", action="store_true",
                    help="permit a non-loopback --host (unsafe; only behind your own auth proxy or firewall)")
    args = ap.parse_args()
    if args.http and args.host not in LOOPBACK and not args.allow_remote:
        ap.error(f"refusing to serve unauthenticated HTTP on {args.host!r}; use a loopback host or --allow-remote")
    if args.http:
        app.run("streamable-http", host=args.host, port=args.port)
    else:
        app.run()


if __name__ == "__main__":
    main()
