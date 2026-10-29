"""Runtime configuration: Token Factory endpoint, the model ladder, prices."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    v = os.getenv(name, "").strip()
    return v or default


@dataclass(frozen=True)
class Tier:
    name: str            # "nano" | "super" | "ultra"
    model: str           # Token Factory model id
    price_in: float      # USD per 1M input tokens
    price_out: float     # USD per 1M output tokens
    thinking: bool       # enable Nemotron reasoning mode on this rung
    max_tokens: int

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000


BASE_URL = _env("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1").rstrip("/")
API_KEY = os.getenv("NEBIUS_API_KEY", "")
REQUEST_TIMEOUT = float(_env("LADDER_REQUEST_TIMEOUT", "240"))

TIERS: dict[str, Tier] = {
    "nano": Tier(
        "nano",
        _env("LADDER_NANO_MODEL", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"),
        0.06, 0.24, thinking=False, max_tokens=4096,
    ),
    "super": Tier(
        "super",
        _env("LADDER_SUPER_MODEL", "nvidia/nemotron-3-super-120b-a12b"),
        0.30, 0.90, thinking=True, max_tokens=12000,
    ),
    "ultra": Tier(
        "ultra",
        _env("LADDER_ULTRA_MODEL", "nvidia/Nemotron-3-Ultra-550b-a55b"),
        1.00, 3.00, thinking=True, max_tokens=16000,
    ),
}
LADDER = ["nano", "super", "ultra"]

# Sandbox limits for executing model-written code.
SANDBOX_TIMEOUT_S = float(_env("LADDER_SANDBOX_TIMEOUT", "10"))
SANDBOX_MEM_MB = int(_env("LADDER_SANDBOX_MEM_MB", "512"))
