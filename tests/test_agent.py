"""Offline tests: a scripted fake LLM drives the real agent loop and the real sandbox."""
import asyncio

from app.agent import POLICIES, Agent, Task, extract_code, extract_json
from app.llm import LLMResult, split_reasoning
from app.sandbox import run_tests

TESTS = '''```python
def test_small():
    assert add(1, 2) == 3
def test_neg():
    assert add(-1, -1) == -2
```'''
WRONG = "```python\ndef add(a, b):\n    return a - b\n```"
RIGHT = "Root cause: subtraction.\n```python\ndef add(a, b):\n    return a + b\n```"


class FakeLLM:
    """Answers by role; `code_answers[tier]` is consumed in order."""

    def __init__(self, code_answers, triage='{"difficulty": "easy", "why": "x"}'):
        self.code_answers = {k: list(v) for k, v in code_answers.items()}
        self.triage = triage
        self.calls = []

    async def chat(self, tier, messages, **kw):
        sys = messages[0]["content"]
        if "dispatcher" in sys:
            text = self.triage
        elif "unit tests" in sys:
            text = TESTS
        else:
            text = self.code_answers[tier.name].pop(0)
        self.calls.append(tier.name)
        return LLMResult(tier.name, tier.model, text, "thinking...", 100, 50, 0.01, tier.cost(100, 50))


def collect(agent, task, policy):
    async def go():
        return [ev async for ev in agent.solve(task, POLICIES[policy])]
    return asyncio.run(go())


def test_escalates_from_nano_to_super():
    llm = FakeLLM({"nano": [WRONG, WRONG], "super": [RIGHT]})
    evs = collect(Agent(llm), Task(spec="Write add(a, b) returning the sum."), "ladder")
    kinds = [e["type"] for e in evs]
    assert "escalate" in kinds
    esc = next(e for e in evs if e["type"] == "escalate")
    assert (esc["from"], esc["to"]) == ("nano", "super")
    done = evs[-1]
    assert done["type"] == "done" and done["status"] == "solved" and done["solved_by"] == "super"
    assert done["attempts"] == 3
    assert done["cost"] < done["ultra_equivalent_cost"]
    assert llm.calls == ["super", "nano", "nano", "super"]  # tests by super, then the climb


def test_nano_solves_without_escalation():
    llm = FakeLLM({"nano": [RIGHT]})
    evs = collect(Agent(llm), Task(spec="Write add(a, b) returning the sum."), "ladder")
    assert not any(e["type"] == "escalate" for e in evs)
    assert evs[-1]["solved_by"] == "nano"


def test_triage_picks_start_rung():
    llm = FakeLLM({"ultra": [RIGHT]}, triage='{"difficulty": "hard", "why": "dp"}')
    evs = collect(Agent(llm), Task(spec="Write add(a, b) returning the sum."), "ladder-auto")
    tri = next(e for e in evs if e["type"] == "triage")
    assert tri["start"] == "ultra"
    assert evs[-1]["solved_by"] == "ultra"


def test_user_tests_are_never_replaced_and_failure_reported():
    user_tests = "def test_x():\n    assert add(2, 2) == 4\n"
    sneaky = WRONG + "\n```python\n# TESTS\ndef test_x():\n    assert True\n```\nTESTFIX: test is wrong"
    llm = FakeLLM({"super": [sneaky] * 4})
    evs = collect(Agent(llm), Task(spec="Write add(a, b) returning the sum.", tests=user_tests), "super-only")
    assert not any(e["type"] == "tests" and e["source"].startswith("revised") for e in evs)
    assert evs[-1]["status"] == "failed" and evs[-1]["attempts"] == 4


def test_parsers():
    code, tests = extract_code("x\n```python\ndef f(): pass\n```\n```python\n# TESTS\ndef test_a(): pass\n```")
    assert code == "def f(): pass" and tests.startswith("# TESTS")
    assert extract_json('sure: {"difficulty": "medium"} ok') == {"difficulty": "medium"}
    assert split_reasoning("<think>hmm</think>answer") == ("answer", "hmm")
    assert split_reasoning("hmm</think>answer") == ("answer", "hmm")


def test_sandbox_blocks_env_and_reports():
    rep = asyncio.run(run_tests("import os\ndef k(): return os.environ.get('NEBIUS_API_KEY')",
                                "def test_k():\n    assert k() is None\ndef test_fail():\n    assert 1 == 2"))
    assert rep.passed == 1 and rep.failed == 1 and not rep.ok
    rep = asyncio.run(run_tests("while True: pass", "def test_a(): pass", timeout=2))
    assert rep.fatal.startswith("Timeout")


class StreamingFakeLLM(FakeLLM):
    """Also streams its answer through on_delta, and can fail a given draft."""

    def __init__(self, *a, fail_temps=(), **kw):
        super().__init__(*a, **kw)
        self.fail_temps = set(fail_temps)

    async def chat(self, tier, messages, on_delta=None, temperature=None, **kw):
        if temperature in self.fail_temps:
            from app.llm import LLMError
            raise LLMError("boom")
        r = await super().chat(tier, messages)
        if on_delta:
            on_delta("reasoning", "thinking...")
            for i in range(0, len(r.content), 7):
                on_delta("content", r.content[i:i + 7])
        return r


def test_wide_policy_picks_passing_draft_and_streams():
    llm = StreamingFakeLLM({"nano": [WRONG, RIGHT, WRONG]})
    evs = collect(Agent(llm), Task(spec="Write add(a, b) returning the sum."), "ladder-wide")
    pick = next(e for e in evs if e["type"] == "pick")
    assert pick["ok"] and pick["candidate"] == 1 and pick["width"] == 3
    assert [s["passed"] for s in pick["scores"]] == [0, 2, 0]
    assert evs[-1]["status"] == "solved" and evs[-1]["solved_by"] == "nano" and evs[-1]["rounds"] == 1
    deltas = [e for e in evs if e["type"] == "delta"]
    assert deltas, "deltas must be forwarded"
    code_text = "".join(e["text"] for e in deltas if e["sid"] == "code-2" and e["kind"] == "content")
    assert code_text == RIGHT  # coalescing never loses or reorders text
    assert {e["sid"] for e in evs if e["type"] == "run"} == {"code-1", "code-2", "code-3"}


def test_one_failed_draft_does_not_kill_the_round():
    # draft index 2 runs at temperature 0.8 on nano and raises
    llm = StreamingFakeLLM({"nano": [RIGHT, WRONG]}, fail_temps={0.8})
    evs = collect(Agent(llm), Task(spec="Write add(a, b) returning the sum."), "ladder-wide")
    assert any(e["type"] == "error" and e.get("recoverable") for e in evs)
    assert evs[-1]["status"] == "solved"


def test_salvages_reasoning_cut_off_by_max_tokens():
    class CutOffLLM(FakeLLM):
        async def chat(self, tier, messages, **kw):
            sys = messages[0]["content"]
            if "unit tests" in sys:
                return await super().chat(tier, messages)
            self.calls.append(f"{tier.name}:{'salvage' if 'Stop analysing' in messages[-1]['content'] else 'code'}")
            if "Stop analysing" in messages[-1]["content"]:
                assert kw.get("thinking") is False and "long analysis" in messages[-1]["content"]
                return LLMResult(tier.name, tier.model, RIGHT, "", 50, 20, 0.01, 0.0)
            return LLMResult(tier.name, tier.model, "", "long analysis " * 50, 100, 12000, 0.01, 0.0,
                             meta={"finish_reason": "length"})

    llm = CutOffLLM({})
    evs = collect(Agent(llm), Task(spec="Write add(a, b) returning the sum."), "super-only")
    assert evs[-1]["status"] == "solved" and evs[-1]["attempts"] == 1
    assert llm.calls == ["super", "super:code", "super:salvage"]
    assert any(e["type"] == "call" and e["role"] == "salvage" for e in evs)
