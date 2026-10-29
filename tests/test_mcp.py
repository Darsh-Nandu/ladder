"""Offline tests for the MCP server: a scripted fake LLM drives run_solve and the registered tools."""
import asyncio

import pytest

pytest.importorskip("mcp")

from app import mcp_server  # noqa: E402
from app.agent import POLICIES, Agent  # noqa: E402
from app.llm import LLMError, LLMResult  # noqa: E402

SPEC = "Write add(a, b) returning the sum."
TESTS = "```python\ndef test_small():\n    assert add(1, 2) == 3\ndef test_neg():\n    assert add(-1, -1) == -2\n```"
WRONG = "```python\ndef add(a, b):\n    return a - b\n```"
RIGHT = "```python\ndef add(a, b):\n    return a + b\n```"


class FakeLLM:
    """Answers by role; `code_answers[tier]` is consumed in order."""

    def __init__(self, code_answers, fail: str | None = None):
        self.code_answers = {k: list(v) for k, v in code_answers.items()}
        self.fail = fail

    async def chat(self, tier, messages, **kw):
        if self.fail:
            raise LLMError(self.fail)
        sys = messages[0]["content"]
        text = TESTS if "unit tests" in sys else self.code_answers[tier.name].pop(0)
        return LLMResult(tier.name, tier.model, text, "", 100, 50, 0.01, tier.cost(100, 50))


def solve(llm, policy="ladder", **kw):
    return asyncio.run(mcp_server.run_solve(Agent(llm), SPEC, policy=policy, **kw))


def test_solved_after_escalation_with_summary():
    events = []

    async def on_event(ev):
        events.append(ev["type"])

    res = solve(FakeLLM({"nano": [WRONG, WRONG], "super": [RIGHT]}), on_event=on_event)
    assert res["status"] == "solved" and res["solved_by"] == "super" and res["attempts"] == 3
    assert "return a + b" in res["code"] and "def test_small" in res["tests"]
    assert 0 < res["cost_usd"] < res["ultra_equivalent_cost_usd"]
    assert res["summary"].startswith("nano ✗✗ → super ✓ — solved by super in 3 attempt(s)")
    assert res["error"] is None
    assert "escalate" in events and events[-1] == "done" and "delta" not in events


def test_wide_policy_marks_one_per_round():
    res = solve(FakeLLM({"nano": [WRONG] * 6, "super": [RIGHT]}), policy="ladder-wide")
    assert res["status"] == "solved" and res["attempts"] == 7
    assert res["summary"].startswith("nano ✗✗ → super ✓")


def test_failed_with_user_tests():
    user_tests = "def test_x():\n    assert add(2, 2) == 4\n"
    res = solve(FakeLLM({"super": [WRONG] * 4}), policy="super-only", tests=user_tests)
    assert res["status"] == "failed" and res["solved_by"] is None
    assert res["tests"] == user_tests.strip()
    assert res["summary"].startswith("super ✗✗✗✗ — failed")


def test_llm_error_is_reported():
    res = solve(FakeLLM({}, fail="NEBIUS_API_KEY is not set"))
    assert res["status"] == "error" and res["error"] == "NEBIUS_API_KEY is not set"
    assert "error: NEBIUS_API_KEY is not set" in res["summary"]


@pytest.mark.parametrize("kw, msg", [
    ({"policy": "bogus"}, "unknown policy 'bogus'"),
    ({"tests": "x" * (mcp_server.MAX_TESTS + 1)}, "tests is"),
])
def test_input_validation(kw, msg):
    with pytest.raises(ValueError, match=msg):
        solve(FakeLLM({}), **kw)
    with pytest.raises(ValueError, match="spec is"):
        asyncio.run(mcp_server.run_solve(Agent(FakeLLM({})), "y" * (mcp_server.MAX_SPEC + 1)))


def test_app_registers_tools():
    names = {t.name for t in asyncio.run(mcp_server.app.list_tools())}
    assert {"solve_function", "list_policies"} <= names


def test_tools_via_app(monkeypatch):
    monkeypatch.setattr(mcp_server, "_get_llm", lambda: FakeLLM({"nano": [RIGHT]}))

    async def go():
        pol = await mcp_server.app.call_tool("list_policies", {})
        res = await mcp_server.app.call_tool("solve_function", {"spec": SPEC, "policy": "ladder"})
        with pytest.raises(Exception, match="unknown policy"):
            await mcp_server.app.call_tool("solve_function", {"spec": SPEC, "policy": "nope"})
        return pol, res

    pol, res = asyncio.run(go())
    assert {p["name"] for p in pol.structured_content["result"]} == set(POLICIES)
    assert res.structured_content["status"] == "solved"
    assert res.structured_content["summary"].startswith("nano ✓")


def test_progress_over_client_session(monkeypatch):
    from mcp import Client

    monkeypatch.setattr(mcp_server, "_get_llm", lambda: FakeLLM({"nano": [WRONG, WRONG], "super": [RIGHT]}))
    progress: list[tuple[float, str | None]] = []

    async def on_progress(p, total, message):
        progress.append((p, message))

    async def go():
        async with Client(mcp_server.app) as client:
            return await client.call_tool("solve_function", {"spec": SPEC, "policy": "ladder"},
                                          progress_callback=on_progress)

    res = asyncio.run(go())
    assert res.structured_content["status"] == "solved"
    msgs = [m for _, m in progress]
    assert any(m and m.startswith("escalating nano → super") for m in msgs)
    assert "nano: draft 1" in msgs and any(m and m.startswith("super draft 3: pass") for m in msgs)
    assert [p for p, _ in progress] == list(range(1, len(progress) + 1))


def test_http_refuses_non_loopback_host(monkeypatch):
    monkeypatch.setattr("sys.argv", ["mcp_server", "--http", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        mcp_server.main()
