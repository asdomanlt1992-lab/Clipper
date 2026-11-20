"""Runtime settings, read from CLIPPER_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default, cast=str):
    raw = os.environ.get(f"CLIPPER_{name}")
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "data")))
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env("PORT", 8000, int))

    # Rolling buffer
    segment_seconds: float = field(default_factory=lambda: _env("SEGMENT_SECONDS", 2.0, float))
    buffer_seconds: float = field(default_factory=lambda: _env("BUFFER_SECONDS", 180.0, float))
    default_quality: str = field(default_factory=lambda: _env("QUALITY", "best"))
    reconnect_seconds: float = field(default_factory=lambda: _env("RECONNECT_SECONDS", 30.0, float))

    # Clip lengths
    manual_pre: float = field(default_factory=lambda: _env("MANUAL_PRE", 30.0, float))
    manual_post: float = field(default_factory=lambda: _env("MANUAL_POST", 5.0, float))
    auto_pre: float = field(default_factory=lambda: _env("AUTO_PRE", 30.0, float))
    auto_post: float = field(default_factory=lambda: _env("AUTO_POST", 10.0, float))

    # Hype detection
    hype_window: float = field(default_factory=lambda: _env("HYPE_WINDOW", 10.0, float))
    hype_baseline: float = field(default_factory=lambda: _env("HYPE_BASELINE", 300.0, float))
    hype_multiplier: float = field(default_factory=lambda: _env("HYPE_MULTIPLIER", 3.0, float))
    hype_min_messages: int = field(default_factory=lambda: _env("HYPE_MIN_MESSAGES", 15, int))
    hype_cooldown: float = field(default_factory=lambda: _env("HYPE_COOLDOWN", 60.0, float))
    hype_warmup: float = field(default_factory=lambda: _env("HYPE_WARMUP", 60.0, float))
    hype_keywords: list[str] = field(
        default_factory=lambda: [
            k.strip().lower()
            for k in _env(
                "HYPE_KEYWORDS",
                "clip,pog,poggers,pogchamp,lul,lol,kekw,omegalul,wtf,omg,holy,insane,no way,w,lmao",
            ).split(",")
            if k.strip()
        ]
    )

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def buffer_dir(self) -> Path:
        return self.data_dir / "buffer"
