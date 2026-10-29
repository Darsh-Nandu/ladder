"""Offline tests for repo mode: a scripted fake LLM fixes examples/buggy_inventory in a temp copy."""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from app.agent import POLICIES
from app.llm import LLMResult
import app.repo_agent as repo_agent
from app.repo_agent import (RepoAgent, SuiteReport, WorkCopy, command_argv, parse_edits, parse_pytest,
                            protected_modules)
from app.sandbox import run_command

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "buggy_inventory"
PRICING = (EXAMPLE / "inventory" / "pricing.py").read_text()
REPORT = (EXAMPLE / "inventory" / "report.py").read_text()


def _fix(src: str, *pairs: tuple[str, str]) -> str:
    for old, new in pairs:
        assert old in src, old
        src = src.replace(old, new)
    return src


FIXED_PRICING = _fix(
    PRICING,
    ("from decimal import Decimal\n", "from decimal import ROUND_HALF_UP, Decimal\n"),
    ("    return Decimal(str(round(float(amount), 2)))",
     '    return Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)'),
    ("quantity > BULK_THRESHOLD", "quantity >= BULK_THRESHOLD"))
FIXED_REPORT = _fix(REPORT, ("rows.sort(key=lambda r: (r[0], r[1]), reverse=True)", "rows.sort(key=lambda r: (-r[0], r[1]))"))
HALF_PRICING = _fix(PRICING, ("quantity > BULK_THRESHOLD", "quantity >= BULK_THRESHOLD"))  # fixes 1 of 3 bugs


def answer(*files: tuple[str, str], why: str = "Root cause: rounding through float.") -> str:
    return why + "\n" + "\n".join(f"```python\n# FILE: {p}\n{body}```" for p, body in files)


RIGHT = answer(("inventory/pricing.py", FIXED_PRICING), ("inventory/report.py", FIXED_REPORT))
PARTIAL = answer(("inventory/pricing.py", HALF_PRICING))
BROKEN = answer(("inventory/pricing.py", "def oops(:\n"))
EVIL = answer(("../evil.py", "x = 1\n"), ("/etc/x.py", "x = 1\n"), ("tests/test_pricing.py", "def test_ok(): pass\n"),
              ("inventory/notes.txt", "hi\n"))


class FakeLLM:
    """Answers code calls per tier, in order; records the prompts it saw."""

    def __init__(self, answers: dict[str, list[str]]):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def chat(self, tier, messages, **kw):
        self.calls.append(tier.name)
        self.prompts.append(messages[-1]["content"])
        text = self.answers[tier.name].pop(0)
        return LLMResult(tier.name, tier.model, text, "", 1000, 400, 0.01, tier.cost(1000, 400))


@pytest.fixture
def repo(tmp_path):
    dst = tmp_path / "proj" / "buggy_inventory"
    shutil.copytree(EXAMPLE, dst)
    return dst


def solve(llm, path, policy="ladder", **kw):
    async def go():
        return [ev async for ev in RepoAgent(llm, timeout=30, **kw).solve(path, POLICIES[policy])]
    return asyncio.run(go())


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}


def test_escalates_and_fixes_without_touching_original(repo):
    before = snapshot(repo)
    llm = FakeLLM({"nano": [PARTIAL, BROKEN], "super": [RIGHT]})
    evs = solve(llm, repo)
    runs = [e for e in evs if e["type"] == "run"]
    assert runs[0]["sid"] == "baseline" and runs[0]["report"]["passed"] == 8 and runs[0]["report"]["failed"] == 5
    assert runs[1]["kept"] and runs[1]["report"]["passed"] == 9            # partial fix is kept
    assert not runs[2]["kept"] and runs[2]["report"]["fatal"] == ""          # syntax error -> collection errors, reverted
    assert runs[3]["ok"] and runs[3]["report"]["passed"] == 13
    esc = next(e for e in evs if e["type"] == "escalate")
    assert (esc["from"], esc["to"]) == ("nano", "super")
    done = evs[-1]
    assert done["type"] == "done" and done["status"] == "solved" and done["solved_by"] == "super"
    assert done["attempts"] == 3 and llm.calls == ["nano", "nano", "super"]
    assert done["changed_files"] == ["inventory/pricing.py", "inventory/report.py"]
    assert "+++ b/inventory/report.py" in done["diff"] and "-    if quantity > BULK_THRESHOLD:" in done["diff"]
    assert not done["applied"] and snapshot(repo) == before
    # the model saw the source it needed, and the revert was reported back to it
    assert "### inventory/pricing.py" in llm.prompts[0] and "### inventory/report.py" in llm.prompts[0]
    assert "REVERTED" in llm.prompts[2]


def test_apply_writes_back_only_on_success(repo):
    evs = solve(FakeLLM({"nano": [RIGHT]}), repo, apply=True)
    assert evs[-1]["applied"] and evs[-1]["solved_by"] == "nano"
    assert (repo / "inventory" / "pricing.py").read_text() == FIXED_PRICING
    assert (repo / "inventory" / "report.py").read_text() == FIXED_REPORT

    fresh = repo.parent / "again"
    shutil.copytree(EXAMPLE, fresh)
    evs = solve(FakeLLM({"nano": [PARTIAL] * 4}), fresh, policy="nano-only", apply=True)
    assert evs[-1]["status"] == "failed" and not evs[-1]["applied"]
    assert (fresh / "inventory" / "pricing.py").read_text() == PRICING


def test_unsafe_and_test_edits_are_rejected(repo):
    files, rejected = parse_edits(EVIL, repo)
    assert files == {}
    assert len(rejected) == 4 and any("../evil.py" in r for r in rejected) and any("/etc/x.py" in r for r in rejected)
    assert any("read-only" in r for r in rejected)
    files, _ = parse_edits(EVIL, repo, allow_test_edits=True)
    assert list(files) == ["tests/test_pricing.py"]
    files, rejected = parse_edits("".join(f"```python\n# FILE: m{i}.py\nx = {i}\n```\n" for i in range(10)), repo)
    assert len(files) == 8 and len(rejected) == 2
    # through the full loop: nothing escapes the work copy and rejections are fed back to the model
    llm = FakeLLM({"nano": [EVIL, RIGHT]})
    evs = solve(llm, repo)
    assert not (repo.parent / "evil.py").exists() and not (repo.parent.parent / "evil.py").exists()
    first = next(e for e in evs if e["type"] == "run" and e["sid"] == "code-1")
    assert not first["kept"] and len(first["rejected"]) == 4
    assert "Rejected edits" in llm.prompts[1] and evs[-1]["status"] == "solved"


def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "link.py").symlink_to(tmp_path / "outside.py")
    files, rejected = parse_edits("```python\n# FILE: link.py\nx = 1\n```", root)
    assert not files and "outside" in rejected[0]


def test_run_command_timeout_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "sk-test-leak")
    monkeypatch.setenv("LADDER_LEAK_CHECK", "1")
    code, out, err, timed_out = asyncio.run(run_command(
        [sys.executable, "-c", "import os; e = os.environ; print(e.get('NEBIUS_API_KEY'), e.get('LADDER_LEAK_CHECK'), "
         "e['HOME'])"], str(tmp_path)))
    assert code == 0 and not timed_out and out.split() == ["None", "None", str(tmp_path)]
    code, out, err, timed_out = asyncio.run(run_command(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"], str(tmp_path), timeout=1))
    assert timed_out and code != 0 and "started" in out
    rep = parse_pytest(code, out, err, timed_out, 1.0, 1)
    assert rep.fatal.startswith("Timeout") and not rep.ok


def test_parse_pytest_and_argv():
    out = ("FAILED tests/test_a.py::test_x[1 2] - AssertionError: boom\nERROR tests/test_b.py - ImportError: no\n"
           "1 failed, 3 passed, 1 error in 0.12s\n")
    rep = parse_pytest(1, out, "", False, 0.1, 60)
    assert (rep.passed, rep.failed, rep.errors) == (3, 1, 1) and not rep.ok and not rep.fatal
    assert rep.failing == [("tests/test_a.py::test_x[1 2]", "AssertionError: boom"), ("tests/test_b.py", "ImportError: no")]
    assert parse_pytest(5, "no tests ran in 0.01s\n", "", False, 0.1, 60).fatal == "No tests were collected"
    assert parse_pytest(0, "4 passed in 0.1s\n", "", False, 0.1, 60).ok
    assert SuiteReport(passed=2, failed=0).ok
    assert command_argv("python -m pytest -q")[0] == sys.executable
    assert command_argv("pytest -q") == ["pytest", "-q"]


def test_run_command_does_not_wait_for_leftover_children(tmp_path):
    t = time.perf_counter()
    code, out, _, timed_out = asyncio.run(run_command(
        [sys.executable, "-c", "import subprocess; subprocess.Popen(['sleep', '30']); print('1 passed in 0.1s')"],
        str(tmp_path), timeout=20))
    assert code == 0 and not timed_out and "1 passed" in out and time.perf_counter() - t < 8


def test_runner_shadowing_rejected(repo):
    for path in ("pytest.py", "_pytest/main.py", "pluggy/__init__.py", "sitecustomize.py", "inventory/usercustomize.py",
                 "json.py", "conftest.py"):
        files, rejected = parse_edits(f"```python\n# FILE: {path}\nprint('13 passed in 0.01s')\n```", repo,
                                      allow_test_edits=True)
        assert not files and "shadow" in rejected[0], path
    assert "mytool" in protected_modules(command_argv("python -m mytool.cli -q"))
    # existing modules keep their names; ordinary new modules are fine
    (repo / "json.py").write_text("x = 1\n")
    files, _ = parse_edits("```python\n# FILE: json.py\nx = 2\n```\n```python\n# FILE: inventory/util.py\ny = 1\n```", repo)
    assert list(files) == ["json.py", "inventory/util.py"]
    (repo / "json.py").unlink()
    # through the loop with --apply: the fake runner never lands, in the copy or the original
    evs = solve(FakeLLM({"nano": [answer(("pytest.py", "print('13 passed in 0.01s')\n"))] * 4}), repo,
                policy="nano-only", apply=True)
    assert evs[-1]["status"] == "failed" and not evs[-1]["applied"] and not (repo / "pytest.py").exists()


def test_fence_pairing():
    root = Path(".")
    files, rejected = parse_edits("Root cause...\n```text\nAssertionError\n```\nFix:\n```python\n"
                                  "# FILE: inventory/pricing.py\nx = 1\n```\n", root)
    assert files == {"inventory/pricing.py": "x = 1\n"} and rejected == []
    files, _ = parse_edits("```python3\n# FILE: a.py\na = 1\n```\n```Python\n# FILE: b.py\nb = 1\n```\n", root)
    assert files == {"a.py": "a = 1\n", "b.py": "b = 1\n"}
    doc = 'def f():\n    """Example:\n\n    ```\n    f()\n    ```\n    """\n    return 1\n'
    files, _ = parse_edits(f"```python\n# FILE: a.py\n{doc}```", root)
    assert files == {"a.py": doc}
    files, rejected = parse_edits("```python\n# FILE: a.py\ndef f(:", root)
    assert not files and "never closed" in rejected[0]


def test_workcopy_never_follows_planted_symlinks(tmp_path):
    root, outside = tmp_path / "w", tmp_path / "secret.py"
    root.mkdir()
    outside.write_text("SECRET = 1\n")
    (root / "a.py").symlink_to(outside)
    wc = WorkCopy(root)
    assert wc.orig("a.py") is None and repo_agent.read_inside(root, "a.py") is None
    wc.write({"a.py": "x = 1\n"})
    assert outside.read_text() == "SECRET = 1\n" and not (root / "a.py").is_symlink()


def test_unexpected_errors_still_end_with_done(repo, monkeypatch):
    os.mkfifo(repo / "pipe")                     # skipped by the copy instead of blocking it
    def boom(*a, **k):
        raise ValueError("cannot write back")
    monkeypatch.setattr(repo_agent, "apply_changes", boom)
    evs = solve(FakeLLM({"nano": [RIGHT]}), repo, apply=True)
    assert evs[-2] == {"type": "error", "message": "ValueError: cannot write back"}
    assert evs[-1]["type"] == "done" and evs[-1]["status"] == "error" and not evs[-1]["applied"]
    assert evs[-1]["changed_files"] == ["inventory/pricing.py", "inventory/report.py"]
