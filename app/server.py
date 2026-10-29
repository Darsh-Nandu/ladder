"""FastAPI server: streams agent events to the browser over Server-Sent Events."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .agent import POLICIES, Agent, Task
from .llm import TokenFactory

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
BENCH = ROOT / "bench"

app = FastAPI(title="Ladder", version="1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_slots = asyncio.Semaphore(int(__import__("os").getenv("LADDER_MAX_CONCURRENT", "4")))
_shared_llm: TokenFactory | None = None


def shared_llm() -> TokenFactory:
    global _shared_llm
    if _shared_llm is None:
        _shared_llm = TokenFactory()
    return _shared_llm


class SolveRequest(BaseModel):
    spec: str = Field(min_length=10, max_length=8000)
    tests: str = Field(default="", max_length=12000)
    starter: str = Field(default="", max_length=4000)
    policy: str = "ladder"


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def get_config():
    return {
        "tiers": {k: {"model": t.model, "price_in": t.price_in, "price_out": t.price_out, "thinking": t.thinking}
                  for k, t in config.TIERS.items()},
        "policies": {k: p.to_dict() for k, p in POLICIES.items()},
        "server_key": bool(config.API_KEY),
        "base_url": config.BASE_URL,
    }


@app.get("/api/examples")
async def examples():
    path = BENCH / "tasks.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            t = json.loads(line)
            if t.get("demo"):
                out.append({"id": t["id"], "title": t.get("title", t["id"]), "spec": t["spec"],
                            "starter": t.get("starter", ""), "difficulty": t.get("difficulty", "")})
    return out


@app.get("/api/bench")
async def bench_results():
    path = BENCH / "results" / "latest.json"
    if not path.exists():
        return JSONResponse({"available": False})
    return JSONResponse({"available": True, **json.loads(path.read_text())})


@app.post("/api/solve")
async def solve(req: SolveRequest, request: Request):
    if req.policy not in POLICIES:
        raise HTTPException(400, f"unknown policy {req.policy}")
    user_key = request.headers.get("x-nebius-key", "").strip()
    if not (config.API_KEY or user_key):
        raise HTTPException(401, "No Token Factory key on the server: paste your Nebius API key in Settings.")

    async def stream():
        llm = TokenFactory(api_key=user_key) if user_key else shared_llm()
        try:
            async with _slots:
                agent = Agent(llm)
                task = Task(spec=req.spec, tests=req.tests, starter=req.starter)
                async for ev in agent.solve(task, POLICIES[req.policy]):
                    if await request.is_disconnected():
                        break
                    yield f"data: {json.dumps(ev)}\n\n"
        finally:
            if user_key:
                await llm.aclose()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/healthz")
async def healthz():
    return {"ok": True}
