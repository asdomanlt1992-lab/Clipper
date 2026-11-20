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


def _csv(name: str, default: str) -> list[str]:
    return [k.strip().lower() for k in _env(name, default).split(",") if k.strip()]


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
    min_clip_seconds: float = field(default_factory=lambda: _env("MIN_CLIP_SECONDS", 31.0, float))
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
                "clip,clip it,pog,poggers,pogchamp,lul,lol,kekw,omegalul,omg,holy,insane,no way,w,lmao,goat,clutch,hype",
            ).split(",")
            if k.strip()
        ]
    )

    hype_negative_keywords: list[str] = field(
        default_factory=lambda: _csv(
            "HYPE_NEGATIVE_KEYWORDS",
            "yikes,cringe,l,ratio,ban,banned,mods,report,reported,toxic,chill,calm down,relax,"
            "stop yelling,bro chill,awkward,sad,rip,f,not okay,cancelled,weirdchamp,monkas,unsub",
        )
    )

    # Scoring and the decision gate (0-100 score; see clipper/scoring.py)
    min_score: float = field(default_factory=lambda: _env("MIN_SCORE", 30.0, float))
    approve_score: float = field(default_factory=lambda: _env("APPROVE_SCORE", 50.0, float))
    mood_skip: float = field(default_factory=lambda: _env("MOOD_SKIP", -0.5, float))
    review_uncertain: bool = field(default_factory=lambda: _env("REVIEW_UNCERTAIN", False, bool))
    rejected_keep_hours: float = field(default_factory=lambda: _env("REJECTED_KEEP_HOURS", 24.0, float))

    # Audio
    audio: bool = field(default_factory=lambda: _env("AUDIO", True, bool))
    audio_trigger_db: float = field(default_factory=lambda: _env("AUDIO_TRIGGER_DB", 10.0, float))
    audio_only: str = field(default_factory=lambda: _env("AUDIO_ONLY", "auto"))  # auto | on | off

    # Transcription (faster-whisper, local)
    transcribe: str = field(default_factory=lambda: _env("TRANSCRIBE", "auto"))  # auto | on | off
    whisper_model: str = field(default_factory=lambda: _env("WHISPER_MODEL", "base"))
    whisper_device: str = field(default_factory=lambda: _env("WHISPER_DEVICE", "auto"))

    # Claude classifier
    classifier: str = field(default_factory=lambda: _env("CLASSIFIER", "auto"))  # auto | on | off
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-opus-5-5"))
    claude_effort: str = field(default_factory=lambda: _env("CLAUDE_EFFORT", "low"))
    reject_categories: list[str] = field(
        default_factory=lambda: _csv("REJECT_CATEGORIES", "rage,argument,technical,offtopic,sensitive")
    )

    @property
    def classifier_enabled(self) -> bool:
        if self.classifier == "off":
            return False
        if self.classifier == "on":
            return True
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    @property
    def audio_only_enabled(self) -> bool:
        if not self.audio or self.audio_only == "off":
            return False
        return self.audio_only == "on" or self.classifier_enabled

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def buffer_dir(self) -> Path:
        return self.data_dir / "buffer"
