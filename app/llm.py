"""Thin async client for Nebius Token Factory (OpenAI-compatible chat completions)."""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from . import config
from .config import Tier

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


@dataclass
class LLMResult:
    tier: str
    model: str
    content: str
    reasoning: str
    tokens_in: int
    tokens_out: int
    latency_s: float
    cost: float
    meta: dict = field(default_factory=dict)


class LLMError(RuntimeError):
    pass


class _StreamBroken(Exception):
    """The connection dropped mid-stream; the request is retried from scratch."""


def split_reasoning(text: str) -> tuple[str, str]:
    """Separate inline <think> blocks from the answer (some deployments inline them)."""
    thoughts = " ".join(m.group(0)[7:-8] for m in _THINK_RE.finditer(text))
    answer = _THINK_RE.sub("", text)
    if "</think>" in answer:  # opening tag consumed by the template, closing tag left in output
        head, _, answer = answer.partition("</think>")
        thoughts = (thoughts + " " + head).strip()
    return answer.strip(), thoughts.strip()


class TokenFactory:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, transport=None):
        self.api_key = api_key if api_key is not None else config.API_KEY
        self.base_url = (base_url or config.BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(config.REQUEST_TIMEOUT, connect=20.0), transport=transport)
        # Models that rejected chat_template_kwargs; we fall back to /think system tags for them.
        self._no_template_kwargs: set[str] = set()
        self._alias: dict[str, str] = {}  # configured model id -> id that the live catalog accepts

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_models(self) -> list[str]:
        r = await self._client.get(f"{self.base_url}/models", headers=self._headers())
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []))

    def _headers(self) -> dict:
        if not self.api_key:
            raise LLMError("NEBIUS_API_KEY is not set")
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def chat(
        self,
        tier: Tier,
        messages: list[dict],
        *,
        thinking: bool | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_delta: Callable[[str, str], None] | None = None,
    ) -> LLMResult:
        """One chat completion. With `on_delta(kind, text)` the response is streamed and every
        reasoning/content fragment is reported as it arrives (kind "reset" = a retry restarted it)."""
        thinking = tier.thinking if thinking is None else thinking
        body: dict = {
            "model": self._alias.get(tier.model, tier.model),
            "messages": messages,
            "max_tokens": max_tokens or tier.max_tokens,
            # NVIDIA's recommended sampling: greedy-ish for non-reasoning, 0.6/0.95 when thinking.
            "temperature": temperature if temperature is not None else (0.6 if thinking else 0.2),
            "top_p": 0.95,
        }
        if tier.model in self._no_template_kwargs:
            sys_tag = "/think" if thinking else "/no_think"
            body["messages"] = [{"role": "system", "content": sys_tag}] + messages
        else:
            body["chat_template_kwargs"] = {"enable_thinking": thinking}
        if on_delta:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}

        t0 = time.perf_counter()
        data = await self._post(body, tier, on_delta)
        latency = time.perf_counter() - t0

        msg = data["choices"][0]["message"]
        content, inline_thoughts = split_reasoning(msg.get("content") or "")
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or inline_thoughts or ""
        usage = data.get("usage") or {}
        meta = {"finish_reason": data["choices"][0].get("finish_reason")}
        t_in = int(usage.get("prompt_tokens") or 0)
        t_out = int(usage.get("completion_tokens") or 0)
        if not usage:  # some streaming endpoints omit usage: estimate (~4 chars/token) and say so
            t_in = sum(len(str(m.get("content", ""))) for m in body["messages"]) // 4
            t_out = (len(msg.get("content") or "") + len(reasoning)) // 4
            meta["usage_estimated"] = True
        return LLMResult(
            tier=tier.name, model=body["model"], content=content, reasoning=reasoning,
            tokens_in=t_in, tokens_out=t_out, latency_s=latency, cost=tier.cost(t_in, t_out), meta=meta,
        )

    async def _post(self, body: dict, tier: Tier, on_delta: Callable[[str, str], None] | None = None) -> dict:
        url = f"{self.base_url}/chat/completions"
        last = ""
        for attempt in range(4):
            try:
                if on_delta:
                    async with self._client.stream("POST", url, json=body, headers=self._headers()) as r:
                        if r.status_code == 200:
                            if attempt:
                                on_delta("reset", "")
                            return await self._consume_stream(r, on_delta)
                        status, text = r.status_code, (await r.aread()).decode(errors="replace")
                else:
                    r = await self._client.post(url, json=body, headers=self._headers())
                    if r.status_code == 200:
                        return r.json()
                    status, text = r.status_code, r.text
            except (httpx.TransportError, _StreamBroken) as e:
                last = f"transport error: {e}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            last = f"HTTP {status}: {text[:300]}"
            if status in (400, 404) and "model" in text.lower() and body["model"] == tier.model \
                    and tier.model not in self._alias:
                # Catalog id differs from our default (casing / region): find the closest live id.
                resolved = await self._resolve(tier)
                if resolved:
                    self._alias[tier.model] = resolved
                    body["model"] = resolved
                    continue
            if status == 400 and "chat_template_kwargs" in body:
                # Endpoint doesn't accept template kwargs: switch this model to system-tag mode.
                self._no_template_kwargs.add(tier.model)
                thinking = body.pop("chat_template_kwargs").get("enable_thinking", False)
                body["messages"] = [{"role": "system", "content": "/think" if thinking else "/no_think"}] + body["messages"]
                continue
            if status == 400 and "stream_options" in body:
                body.pop("stream_options")  # older OpenAI-compatible servers reject it
                continue
            if status in (429, 500, 502, 503, 504):
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            break
        raise LLMError(f"{body['model']}: {last}")

    @staticmethod
    async def _consume_stream(r: httpx.Response, on_delta: Callable[[str, str], None]) -> dict:
        """Fold an OpenAI-style SSE stream back into a non-streaming response dict."""
        content, reasoning, usage, finish = [], [], None, None
        try:
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    rc = d.get("reasoning_content") or d.get("reasoning")
                    if rc:
                        reasoning.append(rc)
                        on_delta("reasoning", rc)
                    if d.get("content"):
                        content.append(d["content"])
                        on_delta("content", d["content"])
                    finish = ch.get("finish_reason") or finish
        except httpx.TransportError as e:
            raise _StreamBroken(str(e)) from e
        return {"choices": [{"message": {"content": "".join(content), "reasoning_content": "".join(reasoning)},
                             "finish_reason": finish}], "usage": usage}

    async def _resolve(self, tier: Tier) -> str | None:
        try:
            ids = await self.list_models()
        except Exception:
            return None
        want = tier.model.lower()
        for mid in ids:
            if mid.lower() == want:
                return mid
        keys = ["nemotron", tier.name] + (["3"] if "3" in want else [])
        cands = [m for m in ids if all(k in m.lower() for k in keys) and "vl" not in m.lower().split("-")]
        return min(cands, key=len) if cands else None
