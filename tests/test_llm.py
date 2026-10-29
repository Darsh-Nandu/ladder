"""TokenFactory client against a mocked OpenAI-compatible endpoint (no network, no credits)."""
import asyncio
import json

import httpx

from app.config import TIERS
from app.llm import TokenFactory

LIVE_IDS = ["nvidia/nemotron-3-nano-30b-a3b", "nvidia/Nemotron-3.5-Super-VL", "nvidia/nemotron-3-super-120b-a12b"]


def make_transport(log, *, reject_kwargs=False, flaky=0):
    state = {"flaky": flaky}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer k"
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": i} for i in LIVE_IDS]})
        body = json.loads(req.content)
        log.append(body)
        if state["flaky"]:
            state["flaky"] -= 1
            return httpx.Response(429, text="rate limited")
        if body["model"] not in LIVE_IDS:
            return httpx.Response(404, text=f"The model `{body['model']}` does not exist")
        if reject_kwargs and "chat_template_kwargs" in body:
            return httpx.Response(400, text="unexpected field chat_template_kwargs")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "<think>plan</think>```python\nx=1\n```"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        })
    return httpx.MockTransport(handler)


def chat(tf, tier):
    async def go():
        try:
            return await tf.chat(TIERS[tier], [{"role": "user", "content": "hi"}])
        finally:
            await tf.aclose()
    return asyncio.run(go())


def test_resolves_model_id_and_meters_cost():
    log = []
    r = chat(TokenFactory("k", "http://tf/v1", make_transport(log)), "nano")
    assert r.model == "nvidia/nemotron-3-nano-30b-a3b"       # resolved from the live catalog
    assert r.content == "```python\nx=1\n```" and r.reasoning == "plan"
    assert abs(r.cost - (1000 * 0.06 + 500 * 0.24) / 1e6) < 1e-12
    assert log[-1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_super_resolution_skips_vision_model():
    log = []
    r = chat(TokenFactory("k", "http://tf/v1", make_transport(log)), "super")
    assert r.model == "nvidia/nemotron-3-super-120b-a12b"


def test_falls_back_to_system_think_tags_and_retries_429():
    log = []
    r = chat(TokenFactory("k", "http://tf/v1", make_transport(log, reject_kwargs=True, flaky=1)), "super")
    assert r.content
    last = log[-1]
    assert "chat_template_kwargs" not in last
    assert last["messages"][0] == {"role": "system", "content": "/think"}
