"""Chat-activity spike detection.

Compares the weighted message rate in a short recent window against a longer
baseline. When the recent rate jumps well above normal, it's probably a
highlight worth clipping.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass


@dataclass
class HypeEvent:
    at: float
    rate: float
    baseline: float
    score: float  # rate / baseline
    messages: int
    top_words: list[str]
    mood: float = 0.0  # -1 (chat is negative) .. +1 (chat is hyped)


_WORD = re.compile(r"[\w']+", re.UNICODE)


class HypeDetector:
    def __init__(
        self,
        window: float = 10.0,
        baseline: float = 300.0,
        multiplier: float = 3.0,
        min_messages: int = 15,
        cooldown: float = 60.0,
        warmup: float = 60.0,
        keywords: list[str] | None = None,
        negative_keywords: list[str] | None = None,
        keyword_weight: float = 2.0,
        baseline_floor: float = 0.2,
    ):
        self.window = window
        self.baseline = baseline
        self.multiplier = multiplier
        self.min_messages = min_messages
        self.cooldown = cooldown
        self.warmup = warmup
        self.keyword_weight = keyword_weight
        self.baseline_floor = baseline_floor  # msgs/sec; keeps dead chats from triggering on 3 msgs
        self._pos = self._compile(keywords)
        self._neg = self._compile(negative_keywords)
        self._events: deque[tuple[float, float, str]] = deque()  # (ts, weight, text)
        self._started: float | None = None
        self._last_trigger: float = float("-inf")

    @staticmethod
    def _compile(keywords: list[str] | None) -> tuple[set[str], list[str]]:
        kws = [k.lower() for k in (keywords or [])]
        return {k for k in kws if " " not in k}, [k for k in kws if " " in k]

    @staticmethod
    def _matches(text: str, compiled: tuple[set[str], list[str]]) -> bool:
        low = text.lower()
        single, phrases = compiled
        return bool(set(_WORD.findall(low)) & single) or any(p in low for p in phrases)

    def weight(self, text: str) -> float:
        return self.keyword_weight if self._matches(text, self._pos) else 1.0

    def mood(self, now: float) -> float:
        """Positive vs negative keyword balance in the recent window, -1..1 (0 = neutral/unknown)."""
        pos = neg = 0
        for ts, _, text in self._events:
            if ts > now - self.window:
                pos += self._matches(text, self._pos)
                neg += self._matches(text, self._neg)
        return (pos - neg) / (pos + neg) if pos + neg else 0.0

    def feed(self, ts: float, text: str) -> None:
        if self._started is None:
            self._started = ts
        self._events.append((ts, self.weight(text), text))
        self._prune(ts)

    def _prune(self, now: float) -> None:
        cutoff = now - self.baseline - self.window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def stats(self, now: float) -> tuple[float, float, int]:
        """Return (recent_rate, baseline_rate, recent_message_count) in weighted msgs/sec."""
        self._prune(now)
        w_start = now - self.window
        recent_w = 0.0
        recent_n = 0
        base_w = 0.0
        for ts, w, _ in self._events:
            if ts > w_start:
                recent_w += w
                recent_n += 1
            else:
                base_w += w
        started = self._started if self._started is not None else now
        base_span = min(self.baseline, max(0.0, w_start - started))
        baseline = base_w / base_span if base_span > 0 else 0.0
        return recent_w / self.window, baseline, recent_n

    def check(self, now: float) -> HypeEvent | None:
        if self._started is None or now - self._started < self.warmup:
            return None
        if now - self._last_trigger < self.cooldown:
            return None
        rate, baseline, n = self.stats(now)
        effective = max(baseline, self.baseline_floor)
        if n < self.min_messages or rate < self.multiplier * effective:
            return None
        self._last_trigger = now
        return HypeEvent(
            at=now,
            rate=rate,
            baseline=baseline,
            score=rate / effective,
            messages=n,
            top_words=self._top_words(now),
            mood=self.mood(now),
        )

    def _top_words(self, now: float, k: int = 3) -> list[str]:
        counts: dict[str, int] = {}
        for ts, _, text in self._events:
            if ts > now - self.window:
                for word in set(_WORD.findall(text.lower())):
                    if len(word) > 1:
                        counts[word] = counts.get(word, 0) + 1
        return [w for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:k]]
