"""Runtime configuration: Token Factory endpoint, the model ladder, prices."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader (no dependency); real environment variables win."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


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

# Same Nemotron 3 weights, different catalog ids. Token Factory is the default; NVIDIA's API is
# supported as a fallback for development (set NEBIUS_BASE_URL=https://integrate.api.nvidia.com/v1).
PROVIDER = "nvidia" if "api.nvidia.com" in BASE_URL else "nebius"
_DEFAULT_IDS = {
    "nebius": {"nano": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
               "super": "nvidia/nemotron-3-super-120b-a12b",
               "ultra": "nvidia/Nemotron-3-Ultra-550b-a55b"},
    # Free NVIDIA API (dev): Nano 3 / Super 3 return 404 there, so use their same-size 3.5 siblings.
    "nvidia": {"nano": "nvidia/nemotron-3.5-lightning-30b-a3b",
               "super": "nvidia/nemotron-3.5-super-vl-120b-a12b",
               "ultra": "nvidia/nemotron-3-ultra-550b-a55b"},
}[PROVIDER]

TIERS: dict[str, Tier] = {
    "nano": Tier(
        "nano",
        _env("LADDER_NANO_MODEL", _DEFAULT_IDS["nano"]),
        0.06, 0.24, thinking=False, max_tokens=4096,
    ),
    "super": Tier(
        "super",
        _env("LADDER_SUPER_MODEL", _DEFAULT_IDS["super"]),
        0.30, 0.90, thinking=True, max_tokens=12000,
    ),
    "ultra": Tier(
        "ultra",
        _env("LADDER_ULTRA_MODEL", _DEFAULT_IDS["ultra"]),
        1.00, 3.00, thinking=True, max_tokens=16000,
    ),
}
LADDER = ["nano", "super", "ultra"]

# Sandbox limits for executing model-written code.
SANDBOX_TIMEOUT_S = float(_env("LADDER_SANDBOX_TIMEOUT", "10"))
SANDBOX_MEM_MB = int(_env("LADDER_SANDBOX_MEM_MB", "512"))
# When the server runs as root (Docker), tests run as this unprivileged uid so they cannot read
# the server's memory or /proc/<pid>/environ (where the API key lives).
SANDBOX_UID = int(_env("LADDER_SANDBOX_UID", "0"))
