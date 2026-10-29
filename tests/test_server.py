"""Offline server tests: a fake LLM drives /api/solve; runs are saved to a temp run store."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from app import config, runstore, server
from app.llm import LLMResult

TESTS = "```python\ndef test_add():\n    assert add(1, 2) == 3\n```"
RIGHT = "```python\ndef add(a, b):\n    return a + b\n```"
SPEC = "Write add(a, b) returning the sum."


class FakeLLM:
    """Writes tests, then always the right answer; streams its text through on_delta."""

    async def chat(self, tier, messages, on_delta=None, **kw):
        text = TESTS if "unit tests" in messages[0]["content"] else RIGHT
        if on_delta:
            on_delta("reasoning", "thinking...")
            on_delta("content", text)
        return LLMResult(tier.name, tier.model, text, "thinking...", 100, 50, 0.01, tier.cost(100, 50))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LADDER_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("LADDER_SAVE_RUNS", raising=False)
    monkeypatch.setattr(runstore, "FEATURED_DIR", tmp_path / "featured")
    monkeypatch.setattr(config, "API_KEY", "dummy-key")
    fake = FakeLLM()
    monkeypatch.setattr(server, "shared_llm", lambda: fake)
    return TestClient(server.app)


def solve(client, policy="ladder-wide"):
    r = client.post("/api/solve", json={"spec": SPEC, "policy": policy})
    assert r.status_code == 200
    evs = [json.loads(line[6:]) for line in r.text.split("\n\n") if line.startswith("data: ")]
    return r, evs


def test_config_lists_ladder_wide(client):
    assert "ladder-wide" in client.get("/api/config").json()["policies"]


def test_solve_streams_and_saves_run(client, tmp_path):
    r, evs = solve(client)
    assert evs[0]["type"] == "start" and evs[-1]["type"] == "done" and evs[-1]["status"] == "solved"
    assert any(e["type"] == "delta" for e in evs) and any(e["type"] == "pick" for e in evs)
    run_id = r.headers["x-ladder-run"]
    assert runstore.RUN_ID.match(run_id)

    listing = client.get("/api/runs").json()
    assert listing["featured"] == []
    assert [s["id"] for s in listing["recent"]] == [run_id]
    assert listing["recent"][0]["status"] == "solved" and listing["recent"][0]["policy"] == "ladder-wide"

    run = client.get(f"/api/runs/{run_id}").json()
    assert run["spec"] == SPEC and run["solved_by"] == "nano"
    types = [e["type"] for e in run["events"]]
    assert "delta" not in types and types[0] == "start" and types[-1] == "done"
    ts = [e["t"] for e in run["events"]]
    assert ts == sorted(ts) and ts[0] == 0
    raw = (tmp_path / "runs" / f"{run_id}.json").read_text()
    assert "dummy-key" not in raw


def test_recent_runs_newest_first(client, tmp_path):
    first = solve(client)[0].headers["x-ladder-run"]
    second = solve(client, "ladder")[0].headers["x-ladder-run"]
    os.utime(tmp_path / "runs" / f"{first}.json", (1, 1))  # make the first run clearly older
    ids = [s["id"] for s in client.get("/api/runs").json()["recent"]]
    assert ids == [second, first]


def test_featured_runs_are_listed_and_loadable(client, tmp_path):
    feat = tmp_path / "featured"
    feat.mkdir()
    (feat / "two-sum-demo.json").write_text(json.dumps({"spec": SPEC, "policy": "ladder", "status": "solved",
                                                        "events": [{"type": "start", "t": 0}]}))
    (feat / "Bad_Name.json").write_text("{}")
    listing = client.get("/api/runs").json()
    assert [s["id"] for s in listing["featured"]] == ["two-sum-demo"]
    run = client.get("/api/runs/two-sum-demo").json()
    assert run["featured"] and run["events"][0]["type"] == "start"


@pytest.mark.parametrize("bad", ["../x", "..%2Fx", "ABC", "abc_def", "a" * 65, "%2e%2e"])
def test_bad_ids_rejected(client, bad):
    assert client.get(f"/api/runs/{bad}").status_code in (404, 422)


def test_missing_run_is_404(client):
    assert client.get("/api/runs/0123456789ab").status_code == 404


def test_save_runs_can_be_disabled(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LADDER_SAVE_RUNS", "0")
    r, evs = solve(client)
    assert evs[-1]["type"] == "done" and "x-ladder-run" not in r.headers
    assert client.get("/api/runs").json()["recent"] == []
    assert not (tmp_path / "runs").exists()


def test_recorder_skips_unfinished_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("LADDER_RUNS_DIR", str(tmp_path))
    rec = runstore.Recorder(SPEC, "ladder")
    rec.add({"type": "start"})
    rec.add({"type": "delta", "sid": "x", "kind": "content", "text": "hi"})
    assert rec.save() is None and len(rec.events) == 1


def test_user_key_never_saved(client, monkeypatch, tmp_path):
    seen = {}

    class KeyedFake(FakeLLM):
        def __init__(self, api_key=None):
            seen["key"] = api_key

        async def aclose(self):
            seen["closed"] = True

    monkeypatch.setattr(server, "TokenFactory", KeyedFake)
    r = client.post("/api/solve", json={"spec": SPEC, "policy": "ladder"}, headers={"X-Nebius-Key": "sk-SECRET-123"})
    assert r.status_code == 200 and seen == {"key": "sk-SECRET-123", "closed": True}
    run_id = r.headers["x-ladder-run"]
    assert "SECRET" not in (tmp_path / "runs" / f"{run_id}.json").read_text()
    assert "SECRET" not in client.get(f"/api/runs/{run_id}").text


def test_non_object_run_files_are_404(client, tmp_path):
    feat = tmp_path / "featured"
    feat.mkdir()
    (feat / "demo.json").write_text("[1, 2]")
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "0123456789ab.json").write_text('"text"')
    listing = client.get("/api/runs").json()
    assert listing == {"featured": [], "recent": []}
    assert client.get("/api/runs/demo").status_code == 404
    assert client.get("/api/runs/0123456789ab").status_code == 404


def test_no_replay_id_when_runs_dir_unusable(client, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("LADDER_RUNS_DIR", str(blocker / "runs"))  # parent is a file: mkdir fails
    r, evs = solve(client)
    assert evs[-1]["type"] == "done" and "x-ladder-run" not in r.headers
