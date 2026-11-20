"""One 0-100 score per moment, from chat spike size, chat mood and loudness.

    chat spike   up to 50 pts   full marks at 2x the trigger multiplier
    chat mood    up to 25 pts   0 = all negative, 12.5 = neutral, 25 = all hype
    audio        up to 25 pts   full marks at +12 dB over the stream's normal level

A spike right at the threshold with neutral chat and no audio scores 37.5.
Negative chat at the threshold scores 25 and is skipped by the default MIN_SCORE of 30.
"""
from __future__ import annotations


def score_moment(spike: float, multiplier: float, mood: float, audio_db: float) -> dict:
    chat = 50 * min(spike / (2 * multiplier), 1.0) if spike > 0 else 0.0
    mood_pts = 25 * (max(-1.0, min(1.0, mood)) + 1) / 2
    audio = 25 * min(max(audio_db, 0.0) / 12, 1.0)
    return {
        "score": round(chat + mood_pts + audio, 1),
        "chat": round(chat, 1),
        "mood": round(mood_pts, 1),
        "audio": round(audio, 1),
        "spike": round(spike, 2),
        "mood_raw": round(mood, 2),
        "audio_db": round(audio_db, 1),
    }
