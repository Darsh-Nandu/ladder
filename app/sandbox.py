"""Execute model-written code against tests in an isolated, resource-limited subprocess.

Isolation (best effort, layered):
  * fresh temp dir per run, scrubbed environment (no API keys reach the child)
  * CPU / memory / file-size rlimits and a wall-clock timeout
  * no network via `unshare -rn` when the kernel allows unprivileged user namespaces
"""
from __future__ import annotations

import asyncio
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from . import config

_MARK = "@@LADDER_RESULT@@"

# Runs inside the sandbox: import the solution, execute each test_* function, report JSON.
_HARNESS = r'''
import json, sys, traceback, io, contextlib, socket
sys.setrecursionlimit(10000)
def _no_net(*a, **k):
    raise OSError("network access is disabled in the Ladder sandbox")
socket.socket = socket.create_connection = socket.getaddrinfo = _no_net  # second layer when unshare is unavailable
results = []
ns = {}
try:
    import solution
    ns.update({k: getattr(solution, k) for k in dir(solution) if not k.startswith("__")})
    exec(compile(open("tests.py").read(), "tests.py", "exec"), ns)
except BaseException as e:
    print(MARK + json.dumps({"fatal": "".join(traceback.format_exception_only(type(e), e)).strip(),
                              "trace": traceback.format_exc()[-1500:]}))
    sys.exit(0)
for name, fn in list(ns.items()):
    if not (name.startswith("test") and callable(fn)):
        continue
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn()
        results.append({"name": name, "ok": True})
    except BaseException as e:
        tb = traceback.extract_tb(e.__traceback__)
        line = next((f"{fr.filename}:{fr.lineno}: {fr.line}" for fr in reversed(tb) if fr.filename == "tests.py"), "")
        msg = "".join(traceback.format_exception_only(type(e), e)).strip()
        results.append({"name": name, "ok": False, "error": msg[:400], "where": line[:300],
                        "stdout": buf.getvalue()[-300:]})
print(MARK + json.dumps({"tests": results}))
'''.replace("MARK", repr(_MARK))


@dataclass
class TestOutcome:
    name: str
    ok: bool
    error: str = ""
    where: str = ""


@dataclass
class RunReport:
    passed: int = 0
    failed: int = 0
    tests: list[TestOutcome] = field(default_factory=list)
    fatal: str = ""          # import/syntax error, timeout, crash
    stderr: str = ""
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.fatal and self.failed == 0 and self.passed > 0

    def summary(self, limit: int = 6) -> str:
        """Compact failure report suitable for feeding back to a model."""
        if self.fatal:
            return f"FATAL: {self.fatal}\n{self.stderr[-800:]}".strip()
        lines = [f"{self.passed} passed, {self.failed} failed"]
        for t in [t for t in self.tests if not t.ok][:limit]:
            lines.append(f"- {t.name}: {t.error}" + (f"\n    at {t.where}" if t.where else ""))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed, "failed": self.failed, "fatal": self.fatal,
            "duration_s": round(self.duration_s, 3),
            "tests": [t.__dict__ for t in self.tests],
        }


def _can_unshare() -> bool:
    if not shutil.which("unshare"):
        return False
    try:
        return subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


_UNSHARE = _can_unshare()


def _limits() -> None:
    mem = config.SANDBOX_MEM_MB * 1024 * 1024
    cpu = int(config.SANDBOX_TIMEOUT_S) + 1
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
    os.setsid()
    if config.SANDBOX_UID and os.geteuid() == 0:
        os.setgroups([])
        os.setgid(config.SANDBOX_UID)
        os.setuid(config.SANDBOX_UID)


async def run_tests(code: str, tests: str, timeout: float | None = None) -> RunReport:
    timeout = timeout or config.SANDBOX_TIMEOUT_S
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with tempfile.TemporaryDirectory(prefix="ladder-") as d:
        for name, body in (("solution.py", code), ("tests.py", tests), ("harness.py", _HARNESS)):
            with open(os.path.join(d, name), "w") as f:
                f.write(body)
        if config.SANDBOX_UID and os.geteuid() == 0:
            os.chown(d, config.SANDBOX_UID, config.SANDBOX_UID)
        cmd = [sys.executable, "harness.py"]
        if _UNSHARE:
            cmd = ["unshare", "-rn"] + cmd
        env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": d, "HOME": d}
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=d, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL, preexec_fn=_limits,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
            await proc.wait()
            return RunReport(fatal=f"Timeout: tests did not finish within {timeout:.0f}s (infinite loop or too slow?)",
                             duration_s=loop.time() - t0)

    rep = RunReport(stderr=err.decode(errors="replace")[-2000:], duration_s=loop.time() - t0)
    payload = next((ln[len(_MARK):] for ln in out.decode(errors="replace").splitlines() if ln.startswith(_MARK)), None)
    if payload is None:
        rep.fatal = f"Process exited with code {proc.returncode} without reporting (crash / memory limit?)"
        return rep
    data = json.loads(payload)
    if "fatal" in data:
        rep.fatal = data["fatal"]
        rep.stderr = data.get("trace", "")
        return rep
    rep.tests = [TestOutcome(t["name"], t["ok"], t.get("error", ""), t.get("where", "")) for t in data["tests"]]
    rep.passed = sum(t.ok for t in rep.tests)
    rep.failed = len(rep.tests) - rep.passed
    if not rep.tests:
        rep.fatal = "No test functions found (expected functions named test_*)"
    return rep
