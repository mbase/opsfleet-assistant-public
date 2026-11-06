"""Load only the settings needed by the local prototype."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name not in {
            "GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", "BQ_LOCATION", "GEMINI_MODEL",
            "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
        }:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


@dataclass(frozen=True)
class Settings:
    project: str | None
    location: str
    model: str
    gemini_api_key: str | None = field(default=None, repr=False)
    openrouter_model: str = "google/gemini-3.8-flash"
    openrouter_api_key: str | None = field(default=None, repr=False)

    history_max_messages: int = 6
    history_max_chars: int = 2400
    input_max_chars: int = 2000
    context_ttl_seconds: int = 1800

    def __post_init__(self):
        if not (2 <= self.history_max_messages <= 20 and 240 <= self.history_max_chars <= 3000
                and 2 <= self.input_max_chars <= 2000 and 1 <= self.context_ttl_seconds <= 1800):
            raise ValueError("invalid_conversation_limits")

    @classmethod
    def load(cls, root: Path = ROOT) -> "Settings":
        values = _read_dotenv(root / ".env")
        values.update(_read_dotenv(root / ".env.local"))
        values.update({key: value for key, value in os.environ.items() if key in {
            "GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", "BQ_LOCATION", "GEMINI_MODEL",
            "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
        }})
        return cls(
            project=values.get("GOOGLE_CLOUD_PROJECT") or None,
            location=values.get("BQ_LOCATION") or "US",
            model=values.get("GEMINI_MODEL") or "gemini-3.8-flash",
            gemini_api_key=values.get("GEMINI_API_KEY") or None,
            openrouter_model=values.get("OPENROUTER_MODEL") or "google/gemini-3.8-flash",
            openrouter_api_key=values.get("OPENROUTER_API_KEY") or None,
        )
