"""Repo mode: point Ladder at a real Python project whose test suite fails.

The agent copies the repo to a temp dir, runs the suite in the sandbox, shows the model the failing tests and
the source files they touch, applies its full-file replacements and climbs Nano -> Super -> Ultra until the
suite passes. The original repo is written (changed files only) only with --apply and a green suite.

    python -m app.repo_agent PATH [--policy ladder] [--test-cmd CMD] [--apply] [--allow-test-edits]
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import difflib
import os
import posixpath
import re
import shlex
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from . import config
from .agent import POLICIES, Agent, Ledger, Policy, _clip, _drive
from .llm import LLMError, TokenFactory
from .sandbox import run_command

DEFAULT_TEST_CMD = "python -m pytest -q -p no:cacheprovider --tb=short"
IGNORED = (".git", ".venv", "venv", "node_modules", "__pycache__", "*.pyc", ".pytest_cache")
MAX_FILES_PER_ATTEMPT = 8
SUMMARY_CAP = 4000

REPO_SYS = """You are a careful senior Python engineer fixing a real repository whose test suite fails.
Rules:
- First diagnose the root cause in one or two sentences.
- Then make minimal, targeted changes to the source code so the failing tests pass without breaking the others.
- Never weaken, skip or delete tests, and never special-case test inputs: the tests define the required behaviour.
- Keep public APIs (names, signatures, return types) unchanged.
- Output ONLY the files you change, each as a COMPLETE file in its own ```python block whose first line is
  `# FILE: relative/path.py` (relative to the repository root). No partial snippets, no unchanged files.
- At most 8 files per answer, .py files only. Do not add third-party dependencies."""


# --------------------------------------------------------------------------- test results

_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")
_FINAL_RE = re.compile(r"(?:passed|failed|errors?|skipped|no tests ran|deselected|xfailed|xpassed).* in [\d.]+s")
_RESULT_RE = re.compile(r"^(FAILED|ERROR) (.+?)(?: - (.*))?$", re.M)
_SECTION_RE = re.compile(r"^_{3,} (.+?) _{3,}$", re.M)
_BANNER_RE = re.compile(r"^={3,} (.+?) ={3,}$", re.M)
_TB_FILE_RE = re.compile(r'^\s*([^\s:"]+\.py):\d+|File "([^"]+\.py)", line \d+', re.M)


@dataclass
class SuiteReport:
    """Outcome of one run of the repo's test command (pytest output parsed when present)."""
    exit_code: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    failing: list[tuple[str, str]] = field(default_factory=list)   # (test id, short message)
    output: str = ""
    fatal: str = ""          # timeout, nothing collected, unparseable crash
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.fatal and self.exit_code == 0 and not self.failed and not self.errors

    @property
    def score(self) -> tuple:
        return (self.ok, not self.fatal, self.passed, -(self.failed + self.errors))

    @property
    def headline(self) -> str:
        if self.fatal:
            return f"FATAL: {self.fatal}"
        return f"{self.passed} passed, {self.failed} failed" + (f", {self.errors} errors" if self.errors else "")

    def summary(self, cap: int = SUMMARY_CAP) -> str:
        """Compact report for the model: counts, failing ids, then as many tracebacks as fit in `cap`."""
        head = [self.headline] + [f"- {tid}" + (f": {msg}" if msg else "") for tid, msg in self.failing[:20]]
        text = "\n".join(head)
        sections = _sections(self.output)
        if not sections:
            return _clip(text + "\n\n" + self.output.strip(), cap).strip() if self.output.strip() else text
        room = cap - len(text) - 2
        each = max(600, room // len(sections))
        for title, body in sections:
            chunk = f"\n\n--- {title}\n{_clip(body.strip(), each)}"
            if len(chunk) > room:
                break
            text += chunk
            room -= len(chunk)
        return text

    def to_dict(self) -> dict:
        return {"passed": self.passed, "failed": self.failed + self.errors, "fatal": self.fatal,
                "duration_s": round(self.duration_s, 3), "exit_code": self.exit_code,
                "tests": [{"name": tid, "ok": False, "error": msg, "where": ""} for tid, msg in self.failing]}


def _sections(out: str) -> list[tuple[str, str]]:
    """Per-test traceback sections of the ERRORS / FAILURES blocks; collection errors first."""
    banners = list(_BANNER_RE.finditer(out))
    found: dict[str, list] = {"ERRORS": [], "FAILURES": []}
    for i, b in enumerate(banners):
        if b.group(1) in found:
            block = out[b.end(): banners[i + 1].start() if i + 1 < len(banners) else len(out)]
            parts = _SECTION_RE.split(block)
            found[b.group(1)] += [(parts[j], parts[j + 1]) for j in range(1, len(parts) - 1, 2)]
    return found["ERRORS"] + found["FAILURES"]


def parse_pytest(exit_code: int, out: str, err: str, timed_out: bool, duration: float, timeout: float) -> SuiteReport:
    rep = SuiteReport(exit_code=exit_code, output=out + ("\n" + err if err.strip() else ""), duration_s=duration)
    final = next((ln for ln in reversed(out.splitlines()) if _FINAL_RE.search(ln)), "")
    for n, kind in _COUNT_RE.findall(final):
        if kind == "passed":
            rep.passed = int(n)
        elif kind == "failed":
            rep.failed = int(n)
        elif kind.startswith("error"):
            rep.errors = int(n)
    rep.failing = [(m.group(2).strip(), (m.group(3) or "")[:300]) for m in _RESULT_RE.finditer(out)]
    if timed_out:
        rep.fatal = f"Timeout: the test suite did not finish within {timeout:.0f}s (infinite loop or too slow?)"
    elif exit_code == 5:
        rep.fatal = "No tests were collected"
    elif exit_code != 0 and not final and not rep.failing:
        tail = (err.strip() or out.strip())[-600:]
        rep.fatal = f"Test command exited with code {exit_code}" + (f": {tail}" if tail else "")
    return rep


def command_argv(cmd: str) -> list[str]:
    """Split the test command; a leading `python` means this interpreter (so the venv's pytest is used)."""
    argv = shlex.split(cmd)
    if argv and argv[0] in ("python", "python3"):
        argv[0] = sys.executable
    return argv


# --------------------------------------------------------------------------- edits

_OPEN_RE = re.compile(r"^( *)(`{3,})[ \t]*([^`\s]*)[^`]*$")
_FILE_RE = re.compile(r"^#\s*FILE:\s*(.+?)\s*$")
# Modules the test runner imports: a repo file with one of these top-level names would replace it (the repo root
# is first on sys.path under `python -m`), so a model could "pass" the suite by faking pytest.
RUNNER_MODULES = frozenset({"pytest", "_pytest", "pluggy", "py", "iniconfig", "packaging", "pygments", "conftest",
                            "sitecustomize", "usercustomize"})


def protected_modules(argv: list[str]) -> frozenset[str]:
    """Top-level names a model may not create: runner modules, the stdlib, the `-m` module and the program."""
    names = set(RUNNER_MODULES) | set(sys.stdlib_module_names)
    if "-m" in argv[:-1]:
        names.add(argv[argv.index("-m") + 1].split(".")[0])
    if argv:
        names.add(Path(argv[0]).stem)
    return frozenset(names)


def shadows(rel: str, root: Path, protected: frozenset[str]) -> str:
    """The module name `rel` would shadow, or ''. Existing repo files keep their names."""
    parts = rel.split("/")
    if parts[-1] in ("sitecustomize.py", "usercustomize.py"):
        return parts[-1][:-3]
    top = parts[0].removesuffix(".py") if len(parts) == 1 else parts[0]
    return top if top in protected and not (root / parts[0]).exists() else ""


DEFAULT_PROTECTED = protected_modules(command_argv(DEFAULT_TEST_CMD))


def code_blocks(text: str) -> list[tuple[str, list[str], bool]]:
    """(language, lines, closed) of each fenced block, paired line by line.

    A block closes only on a bare fence with the opening's indentation, so an indented ``` inside a docstring
    does not end a column-0 block, and ```text / ```bash blocks cannot shift the pairing of later blocks."""
    lines = text.splitlines()
    out: list[tuple[str, list[str], bool]] = []
    i = 0
    while i < len(lines):
        m = _OPEN_RE.match(lines[i])
        i += 1
        if not m:
            continue
        indent, ticks, lang = m.groups()
        close = re.compile(rf"{indent}{ticks}`*[ \t]*")
        start = i
        while i < len(lines) and not close.fullmatch(lines[i]):
            i += 1
        body = [ln[len(indent):] if ln.startswith(indent) else ln for ln in lines[start:i]]
        out.append((lang.lower(), body, i < len(lines)))
        i += 1
    return out


def is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return (any(p in ("tests", "test") for p in parts[:-1]) or name.startswith("test_")
            or name.endswith("_test.py") or name == "conftest.py")


def safe_rel(path: str, root: Path) -> str:
    """Normalise a model-supplied path; raise ValueError unless it is a .py file inside `root`."""
    p = path.strip().strip("`'\"").replace("\\", "/")
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or p.startswith("~"):
        raise ValueError("absolute paths are not allowed")
    norm = posixpath.normpath(p)
    if norm in (".", "..") or norm.startswith("../"):
        raise ValueError("path escapes the repository")
    if not norm.endswith(".py"):
        raise ValueError("only .py files may be edited")
    if any(part in IGNORED or part.startswith(".") for part in norm.split("/")):
        raise ValueError("hidden or ignored directory")
    base = root.resolve()
    if not (base / norm).resolve().is_relative_to(base):
        raise ValueError("path resolves outside the repository (symlink?)")
    return norm


def parse_edits(text: str, root: Path, allow_test_edits: bool = False, max_files: int = MAX_FILES_PER_ATTEMPT,
                protected: frozenset[str] | None = None) -> tuple[dict[str, str], list[str]]:
    """Full-file replacements from a model answer -> ({rel_path: content}, [rejection reasons])."""
    protected = DEFAULT_PROTECTED if protected is None else protected
    files: dict[str, str] = {}
    rejected: list[str] = []
    for lang, lines, closed in code_blocks(text):
        while lines and not lines[0].strip():
            lines = lines[1:]
        m = _FILE_RE.match(lines[0].strip()) if lines else None
        if not closed:
            if m:
                rejected.append(f"{m.group(1).strip()}: the code block was never closed (answer cut off?)")
            continue
        if not m:
            if lang in ("", "python", "py", "python3"):
                rejected.append("a code block without a '# FILE: path' first line was ignored")
            continue
        try:
            rel = safe_rel(m.group(1), root)
        except ValueError as e:
            rejected.append(f"{m.group(1).strip()}: {e}")
            continue
        if mod := shadows(rel, root, protected):
            rejected.append(f"{rel}: would shadow the `{mod}` module (test runner or standard library)")
        elif is_test_path(rel) and not allow_test_edits:
            rejected.append(f"{rel}: test files are read-only, fix the code instead")
        elif rel not in files and len(files) >= max_files:
            rejected.append(f"{rel}: more than {max_files} files in one answer")
        else:
            files[rel] = "\n".join(lines[1:]).rstrip() + "\n"
    return files, rejected


def read_inside(root: Path, rel: str) -> str | None:
    """Text of root/rel if it is a file resolving inside root. The suite may run as another uid and plant
    symlinks, so Ladder (possibly root) never reads through a link that leaves the work copy."""
    p = root / rel
    try:
        if not p.resolve().is_relative_to(root.resolve()) or not p.is_file():
            return None
        return p.read_text(errors="replace")
    except OSError:
        return None


def _target(root: Path, rel: str) -> Path:
    """root/rel for writing or unlinking: its directory must resolve inside root; a symlink there is removed."""
    p = root / rel
    if not p.parent.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"{rel}: path resolves outside the work copy")
    if p.is_symlink():
        p.unlink()
    return p


class WorkCopy:
    """The temp copy of the repo; remembers original contents of every file it touches."""

    def __init__(self, root: Path):
        self.root = root
        self.original: dict[str, str | None] = {}

    def _read(self, rel: str) -> str | None:
        return read_inside(self.root, rel)

    def orig(self, rel: str) -> str | None:
        """Original content; first read happens before any write, while the file is still pristine."""
        if rel not in self.original:
            self.original[rel] = self._read(rel)
        return self.original[rel]

    def write(self, files: dict[str, str]) -> None:
        for rel, body in files.items():
            self.orig(rel)
            p = _target(self.root, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)

    def reset(self, keep: dict[str, str]) -> None:
        """Restore every touched file to `keep` (the best state so far) or to its original."""
        for rel, orig in self.original.items():
            body = keep.get(rel, orig)
            if body is None:
                _target(self.root, rel).unlink(missing_ok=True)
            elif self._read(rel) != body:
                _target(self.root, rel).write_text(body)

    def changed(self, files: dict[str, str]) -> list[str]:
        return sorted(r for r, b in files.items() if b != self.orig(r))

    def diff(self, files: dict[str, str], base: dict[str, str] | None = None) -> str:
        """Unified diff of `files` against `base` (default: the originals)."""
        out = []
        for rel in sorted(files):
            old = (base or {}).get(rel) if rel in (base or {}) else self.orig(rel)
            new = files[rel]
            if old == new:
                continue
            a = [ln if ln.endswith("\n") else ln + "\n" for ln in (old or "").splitlines(True)]
            b = [ln if ln.endswith("\n") else ln + "\n" for ln in new.splitlines(True)]
            out += difflib.unified_diff(a, b, f"a/{rel}" if old is not None else "/dev/null", f"b/{rel}")
        return "".join(out)


_IGNORE_NAMES = shutil.ignore_patterns(*IGNORED)


def _copy_ignore(d: str, names: list[str]) -> set[str]:
    """IGNORED plus anything that is not a file, dir or symlink (copying a FIFO would block forever)."""
    out = set(_IGNORE_NAMES(d, names))
    for n in names:
        try:
            mode = os.lstat(os.path.join(d, n)).st_mode
        except OSError:
            out.add(n)
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            out.add(n)
    return out


def apply_changes(repo: Path, files: dict[str, str]) -> list[str]:
    """Write changed files back into the original repo (paths re-validated against it)."""
    written = []
    for rel, body in sorted(files.items()):
        rel = safe_rel(rel, repo)
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        written.append(rel)
    return written


# --------------------------------------------------------------------------- context

def file_tree(root: Path, cap: int = 3000) -> str:
    rels = sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file() and p.suffix in (".py", ".toml", ".cfg", ".md")
                  and not any(part in IGNORED or part.startswith(".") for part in p.relative_to(root).parts))
    return _clip("\n".join(rels), cap) if rels else "(no files)"


def _imports(root: Path, rel: str) -> list[str]:
    """Repo files imported by `rel` (absolute imports from the root, src/ or the file's dir; relative ones)."""
    try:
        tree = ast.parse(read_inside(root, rel) or "")
    except (SyntaxError, ValueError):
        return []
    pkg = rel.split("/")[:-1]
    mods: list[list[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name.split(".") for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level - 1 > len(pkg):
                    continue
                base = pkg[: len(pkg) - (node.level - 1)] + (node.module.split(".") if node.module else [])
                mods += [["." + "/".join(base)]] + [["." + "/".join(base + [a.name])] for a in node.names]
            elif node.module:
                mods += [node.module.split(".")] + [node.module.split(".") + [a.name] for a in node.names]
    out = []
    for m in mods:
        if m[0].startswith("."):          # already resolved relative import (marked with a leading dot)
            stems = [m[0][1:]]
        else:
            path = "/".join(m)
            stems = [path, f"src/{path}"] + ([f"{'/'.join(pkg)}/{path}"] if pkg else [])
        for stem in stems:
            for cand in (f"{stem}.py", f"{stem}/__init__.py"):
                if stem and (root / cand).is_file() and cand not in out:
                    out.append(cand)
    return out


def _traceback_files(root: Path, report: SuiteReport) -> list[str]:
    base = root.resolve()
    out = []
    for m in _TB_FILE_RE.finditer(report.output):
        raw = m.group(1) or m.group(2)
        p = Path(raw) if Path(raw).is_absolute() else root / raw
        try:
            rel = str(p.resolve().relative_to(base))
        except (ValueError, OSError):
            continue
        if p.is_file() and rel not in out and not any(x in IGNORED for x in Path(rel).parts):
            out.append(rel)
    return out


def build_context(root: Path, report: SuiteReport, changed: list[str], max_chars: int,
                  allow_test_edits: bool = False) -> str:
    """Repo tree + the files that matter, most relevant first, within `max_chars`."""
    tb = _traceback_files(root, report)
    failing_tests = [tid.split("::")[0] for tid, _ in report.failing]
    failing_tests = [t for t in dict.fromkeys(failing_tests) if (root / t).is_file()]
    imported, frontier = [], list(failing_tests)
    for _ in range(3):                    # test -> package __init__ -> modules -> their imports
        nxt = []
        for rel in frontier:
            for dep in _imports(root, rel):
                if dep not in imported and dep not in failing_tests:
                    imported.append(dep)
                    nxt.append(dep)
        frontier = nxt
    order = [f for f in tb if not is_test_path(f)] + changed + failing_tests + [f for f in tb if is_test_path(f)] + imported
    tree = file_tree(root)
    text = f"## Repository files\n{tree}\n"
    budget = max_chars - len(text)
    omitted = []
    for rel in dict.fromkeys(order):
        body = read_inside(root, rel)
        if body is None:
            continue
        ro = " (read-only test file)" if is_test_path(rel) and not allow_test_edits else ""
        block = f"\n### {rel}{ro}\n```python\n{body.rstrip()}\n```\n"
        if len(block) > budget:
            omitted.append(rel)
            continue
        text += block
        budget -= len(block)
    if omitted:
        text += "\nNot shown (context budget): " + ", ".join(omitted) + "\n"
    return text


# --------------------------------------------------------------------------- agent

@dataclass
class Attempt:
    number: int
    tier: str
    diff: str
    rejected: list[str]
    report: SuiteReport
    kept: bool


class RepoAgent:
    def __init__(self, llm: TokenFactory, test_cmd: str = DEFAULT_TEST_CMD, timeout: float = 60,
                 allow_test_edits: bool = False, max_context: int = 40000, apply: bool = False):
        self.agent = Agent(llm)           # reused for its streamed, ledgered model call
        self.test_cmd = test_cmd
        self.timeout = timeout
        self.allow_test_edits = allow_test_edits
        self.max_context = max_context
        self.apply = apply

    async def _run(self, work: Path, argv: list[str]) -> SuiteReport:
        t = time.perf_counter()
        code, out, err, timed_out = await run_command(argv, str(work), timeout=self.timeout)
        return parse_pytest(code, out, err, timed_out, time.perf_counter() - t, self.timeout)

    def _run_event(self, sid: str, tier: str | None, attempt: int, report: SuiteReport, diff: str = "",
                   changed: list[str] | None = None, rejected: list[str] | None = None, kept: bool = True) -> dict:
        return {"type": "run", "sid": sid, "tier": tier, "attempt": attempt, "candidate": 0, "width": 1,
                "code": diff, "ok": report.ok, "summary": report.summary(), "tests_revised": False,
                "report": report.to_dict(), "changed_files": changed or [], "rejected": rejected or [], "kept": kept}

    async def solve(self, repo: str | Path, policy: Policy) -> AsyncIterator[dict]:
        """Same event protocol as Agent.solve (docs/events.md); `done` carries `diff` and `changed_files`."""
        t0 = time.perf_counter()
        ledger = Ledger()
        repo = Path(repo).resolve()
        yield {"type": "start", "task_id": repo.name, "repo": str(repo), "test_cmd": self.test_cmd,
               "policy": {**policy.to_dict(), "width": {}},
               "models": {k: config.TIERS[k].model for k in policy.ladder}}
        if not repo.is_dir():
            yield {"type": "error", "message": f"not a directory: {repo}"}
            yield self._done("error", None, "", [], ledger, 0, 0, t0, SuiteReport(), False)
            return
        argv = command_argv(self.test_cmd)
        protected = protected_modules(argv)
        wc: WorkCopy | None = None
        best: dict[str, str] = {}             # rel path -> content: the best state reached so far
        best_report = SuiteReport()
        attempt_no = 0
        try:
            with tempfile.TemporaryDirectory(prefix="ladder-repo-") as tmp:
                os.chmod(tmp, 0o711)          # a uid-dropped suite (root + LADDER_SANDBOX_UID) must reach its cwd
                work = Path(tmp) / repo.name
                shutil.copytree(repo, work, symlinks=True, ignore=_copy_ignore)
                wc = WorkCopy(work)
                best_report = await self._run(work, argv)
                yield self._run_event("baseline", None, 0, best_report)
                if best_report.ok:
                    yield self._done("solved", None, "", [], ledger, 0, 0, t0, best_report, False)
                    return
                if best_report.exit_code == 5:
                    yield {"type": "error", "message": "the test command collected no tests"}
                    yield self._done("error", None, "", [], ledger, 0, 0, t0, best_report, False)
                    return
                last: Attempt | None = None
                history: list[str] = []
                # Width (parallel drafts) is not used in repo mode: every round is one draft.
                start = policy.start if policy.start in policy.ladder else policy.ladder[0]
                rungs = policy.ladder[policy.ladder.index(start):]
                for ti, tier in enumerate(rungs):
                    if ti:
                        yield {"type": "escalate", "from": rungs[ti - 1], "to": tier, "reason": best_report.headline}
                    for _ in range(policy.attempts_per_tier):
                        attempt_no += 1
                        sid = f"code-{attempt_no}"
                        extra = {"attempt": attempt_no, "candidate": 0, "width": 1}
                        messages = self._messages(work, wc, best, best_report, last, history)
                        out: list = []
                        async for ev in _drive(lambda emit: self.agent._llm(emit, ledger, "code", tier, messages, sid,
                                                                            extra=extra), out):
                            yield ev
                        r = out[0]
                        files, rejected = parse_edits(r.content, work, self.allow_test_edits, protected=protected)
                        wc.reset(best)
                        diff = wc.diff(files, best)
                        touched = sorted(f for f in files if files[f] != best.get(f, wc.orig(f)))
                        if files and diff:
                            wc.write(files)
                            report = await self._run(work, argv)
                        else:
                            hint = " (hit max_tokens)" if r.meta.get("finish_reason") == "length" else ""
                            report = SuiteReport(exit_code=1, fatal="the answer contained no valid file changes" + hint)
                        kept = bool(diff) and report.score >= best_report.score
                        if kept:
                            best, best_report = {**best, **files}, report
                        else:
                            wc.reset(best)
                        yield self._run_event(sid, tier, attempt_no, report, diff, touched, rejected, kept)
                        history.append(f"attempt {attempt_no} ({tier}): {report.headline}" + ("" if kept else ", reverted"))
                        last = Attempt(attempt_no, tier, diff, rejected, report, kept)
                        if report.ok:
                            yield await self._finish("solved", tier, repo, wc, best, ledger, attempt_no, t0, report)
                            return
                yield await self._finish("failed", None, repo, wc, best, ledger, attempt_no, t0, best_report)
        except Exception as e:  # noqa: BLE001 - the protocol promises `done` as the last event
            msg = str(e) if isinstance(e, LLMError) else f"{type(e).__name__}: {_clip(str(e), 600)}"
            yield {"type": "error", "message": msg}
            # diff/changed only use cached originals, so they work after the temp dir is gone
            yield self._done("error", None, wc.diff(best) if wc else "", wc.changed(best) if wc else [], ledger,
                             attempt_no, attempt_no, t0, best_report, False)

    async def _finish(self, status: str, tier: str | None, repo: Path, wc: WorkCopy, best: dict[str, str],
                      ledger: Ledger, attempts: int, t0: float, report: SuiteReport) -> dict:
        changed = wc.changed(best)
        applied = False
        if status == "solved" and self.apply and changed:
            apply_changes(repo, {r: best[r] for r in changed})
            applied = True
        return self._done(status, tier, wc.diff(best), changed, ledger, attempts, attempts, t0, report, applied)

    def _messages(self, work: Path, wc: WorkCopy, best: dict[str, str], best_report: SuiteReport,
                  last: Attempt | None, history: list[str]) -> list[dict]:
        user = (f"## Test command\n`{self.test_cmd}`\n\n## Current test results\n{best_report.summary()}\n\n"
                + build_context(work, best_report, wc.changed(best), self.max_context, self.allow_test_edits))
        if last:
            user += f"\n## Your previous attempt ({last.tier})\n"
            if last.diff:
                user += f"```diff\n{_clip(last.diff, 3000)}```\n"
            if last.kept:
                user += "These changes are applied; the files above include them.\n"
            else:
                user += (f"Result: {_clip(last.report.summary(), 1500)}\nThese changes were REVERTED because they "
                         "did not improve the results; the files above show the current state.\n")
            if last.rejected:
                user += "Rejected edits:\n" + "\n".join(f"- {x}" for x in last.rejected) + "\n"
            if len(history) > 1:
                user += "\n## Earlier attempts\n" + "\n".join(history[-6:]) + "\n"
            user += "\nDo not repeat an approach that already failed.\n"
        user += ("\nDiagnose the root cause in one or two sentences, then output every file you change in full, "
                 "each in its own ```python block starting with `# FILE: relative/path.py`.")
        return [{"role": "system", "content": REPO_SYS}, {"role": "user", "content": user}]

    def _done(self, status: str, tier: str | None, diff: str, changed: list[str], ledger: Ledger, attempts: int,
              rounds: int, t0: float, report: SuiteReport, applied: bool) -> dict:
        return {
            "type": "done", "status": status, "solved_by": tier if status == "solved" else None,
            "diff": diff, "changed_files": changed, "applied": applied, "test_cmd": self.test_cmd,
            "report": report.to_dict(), "attempts": attempts, "rounds": rounds,
            "cost": ledger.cost, "ultra_equivalent_cost": ledger.ultra_equivalent(),
            "tokens_in": ledger.tokens_in, "tokens_out": ledger.tokens_out,
            "llm_seconds": round(ledger.llm_seconds, 2), "wall_seconds": round(time.perf_counter() - t0, 2),
            "by_tier": ledger.by_tier, "calls": ledger.calls,
        }


# --------------------------------------------------------------------------- CLI

C = {"nano": "\033[92m", "super": "\033[96m", "ultra": "\033[95m", "ok": "\033[92m", "fail": "\033[91m",
     "warn": "\033[93m", "dim": "\033[2m", "end": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def _tag(tier: str | None) -> str:
    return f"{C.get(tier or '', '')}[{tier or 'base'}]{C['end']}"


def _color_diff(diff: str) -> str:
    col = {"+": C["ok"], "-": C["fail"], "@": C["warn"]}
    return "\n".join(ln if ln.startswith(("+++", "---")) else col.get(ln[:1], "") + ln + (C["end"] if ln[:1] in col else "")
                     for ln in diff.splitlines())


async def main() -> int:
    ap = argparse.ArgumentParser(description="Ladder repo mode: fix a Python project until its test suite passes")
    ap.add_argument("path", help="the project directory (it is copied; the original is untouched without --apply)")
    ap.add_argument("--policy", default="ladder", choices=sorted(POLICIES))
    ap.add_argument("--test-cmd", default=DEFAULT_TEST_CMD, help="run from the project root; 'python' = this interpreter")
    ap.add_argument("--apply", action="store_true", help="write the fix back if the whole suite passes")
    ap.add_argument("--allow-test-edits", action="store_true", help="let the model edit test files too")
    ap.add_argument("--max-context", type=int, default=40000, help="max characters of source shown to the model")
    ap.add_argument("--timeout", type=float, default=60, help="seconds per test-suite run")
    a = ap.parse_args()

    llm = TokenFactory()
    final = None
    agent = RepoAgent(llm, a.test_cmd, a.timeout, a.allow_test_edits, a.max_context, a.apply)
    try:
        async for ev in agent.solve(a.path, POLICIES[a.policy]):
            t = ev["type"]
            if t == "start":
                print(f"{C['dim']}repo {ev['repo']}  tests: {ev['test_cmd']}{C['end']}")
            elif t == "call":
                print(f"{_tag(ev['tier'])} fix #{ev['attempt']} …", flush=True)
            elif t == "llm":
                print(f"{C['dim']}      {ev['tokens_in']}→{ev['tokens_out']} tok  ${ev['cost']:.5f}  {ev['latency_s']}s{C['end']}")
            elif t == "run":
                col = C["ok"] if ev["ok"] else C["fail"]
                what = "baseline" if ev["sid"] == "baseline" else ", ".join(ev["changed_files"]) or "no changes"
                note = "" if ev["kept"] else f" {C['warn']}(reverted){C['end']}"
                print(f"{col}tests{C['end']} {ev['summary'].splitlines()[0]}  {C['dim']}{what}{C['end']}{note}")
                for x in ev["rejected"]:
                    print(f"{C['warn']}      rejected{C['end']} {x}")
                if not ev["ok"]:
                    print(C["dim"] + "\n".join("      " + ln for ln in ev["summary"].splitlines()[1:6] if ln.strip()) + C["end"])
            elif t == "escalate":
                print(f"{C['warn']}escalate{C['end']} {_tag(ev['from'])} → {_tag(ev['to'])}")
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
    if final["diff"]:
        print("\n" + _color_diff(final["diff"]))
    if final["applied"]:
        print(f"\napplied to {', '.join(final['changed_files'])}")
    elif final["status"] == "solved" and final["changed_files"]:
        print("\n(dry run: re-run with --apply to write these changes)")
    return 0 if final["status"] == "solved" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
