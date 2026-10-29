"""Run store: finished /api/solve runs saved as JSON, for the replay gallery.

Recent runs live in `LADDER_RUNS_DIR` (default <repo>/runs); hand-picked, read-only runs live in
<repo>/runs/featured/. Only agent events are stored (no request headers, no API keys), minus the
token-level `delta` events: replays re-synthesise streaming from each call's final text.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEATURED_DIR = ROOT / "runs" / "featured"
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
SLUG = re.compile(r"^[a-z0-9-]{1,64}$")
RECENT = 30          # runs listed in the gallery
KEEP = 500           # runs kept on disk; older ones are pruned on save

log = logging.getLogger("ladder.runstore")
_summaries: dict[Path, tuple[float, dict]] = {}   # path -> (mtime, summary)


def enabled() -> bool:
    return os.getenv("LADDER_SAVE_RUNS", "1").strip().lower() not in ("0", "false", "no", "off")


def runs_dir() -> Path:
    return Path(os.getenv("LADDER_RUNS_DIR", "").strip() or ROOT / "runs")


def writable() -> bool:
    """True if runs can be saved (enabled and the runs dir exists or can be made, and is writable)."""
    if not enabled():
        return False
    folder = runs_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("runs dir %s unusable, not recording runs: %s", folder, e)
        return False
    return os.access(folder, os.W_OK | os.X_OK)


def _first_line(spec: str, n: int = 140) -> str:
    line = next((s.strip() for s in spec.splitlines() if s.strip()), "")
    return line if len(line) <= n else line[: n - 1] + "…"


def summary(run: dict, featured: bool = False) -> dict:
    return {"id": run.get("id"), "created": run.get("created"), "policy": run.get("policy"),
            "status": run.get("status"), "solved_by": run.get("solved_by"), "cost": run.get("cost", 0.0),
            "wall_seconds": run.get("wall_seconds", 0.0), "title": run.get("title") or _first_line(run.get("spec", "")),
            "featured": featured}


class Recorder:
    """Collects one solve's events (with `t` = seconds since the first event) and saves them once done."""

    def __init__(self, spec: str, policy: str):
        self.id = secrets.token_hex(6)
        self.spec, self.policy = spec, policy
        self.created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.events: list[dict] = []
        self.done: dict | None = None
        self._t0: float | None = None

    def add(self, ev: dict) -> None:
        if ev.get("type") == "delta":
            return
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        self.events.append({**ev, "t": round(now - self._t0, 3)})
        if ev.get("type") == "done":
            self.done = ev

    def to_dict(self) -> dict:
        d = self.done or {}
        return {"id": self.id, "created": self.created, "spec": self.spec, "policy": self.policy,
                "status": d.get("status"), "solved_by": d.get("solved_by"), "cost": d.get("cost", 0.0),
                "wall_seconds": d.get("wall_seconds", 0.0), "events": self.events}

    def save(self) -> Path | None:
        """Write the run if it reached `done`; never raises (a full disk must not break a solve)."""
        if self.done is None or not enabled():
            return None
        try:
            folder = runs_dir()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{self.id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_dict(), separators=(",", ":")))
            os.replace(tmp, path)
            _prune(folder)
            return path
        except OSError as e:
            log.warning("could not save run %s: %s", self.id, e)
            return None


def _recent_paths(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    paths = [p for p in folder.glob("*.json") if RUN_ID.match(p.stem)]
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def _prune(folder: Path) -> None:
    for p in _recent_paths(folder)[KEEP:]:
        p.unlink(missing_ok=True)
        _summaries.pop(p, None)


def _summary_of(path: Path, featured: bool) -> dict | None:
    try:
        mtime = path.stat().st_mtime
        hit = _summaries.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        run = json.loads(path.read_text())
        if not isinstance(run, dict):
            raise ValueError("not a JSON object")
        s = summary(run, featured)
        if featured:
            s["id"] = path.stem   # featured runs are addressed by their file slug
        _summaries[path] = (mtime, s)
        return s
    except (OSError, ValueError, AttributeError) as e:
        log.warning("skipping unreadable run %s: %s", path.name, e)
        return None


def list_runs() -> dict:
    featured = sorted(p for p in FEATURED_DIR.glob("*.json") if SLUG.match(p.stem)) if FEATURED_DIR.is_dir() else []
    return {"featured": [s for p in featured if (s := _summary_of(p, True))],
            "recent": [s for p in _recent_paths(runs_dir())[:RECENT] if (s := _summary_of(p, False))]}


def load(run_id: str) -> dict | None:
    """Full run by id (recent: 12 hex chars) or featured slug; None for unknown or malformed ids."""
    candidates = []
    if RUN_ID.match(run_id):
        candidates.append((runs_dir() / f"{run_id}.json", False))
    if SLUG.match(run_id):
        candidates.append((FEATURED_DIR / f"{run_id}.json", True))
    for path, featured in candidates:
        if path.is_file():
            try:
                run = json.loads(path.read_text())
            except (OSError, ValueError):
                return None
            if not isinstance(run, dict):
                return None
            if featured:
                run["id"], run["featured"] = run_id, True
            return run
    return None
