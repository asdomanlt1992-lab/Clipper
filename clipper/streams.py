"""A watched stream = recorder + optional chat listener + hype detector."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque

from .chat import chat_source_for
from .clips import ClipLibrary
from .config import Settings
from .hype import HypeDetector
from .recorder import Recorder

log = logging.getLogger(__name__)


class WatchedStream:
    def __init__(self, url: str, quality: str, auto: bool, settings: Settings,
                 library: ClipLibrary, multiplier: float | None = None):
        self.id = uuid.uuid4().hex[:8]
        self.url = url
        self.quality = quality
        self.auto = auto
        self.settings = settings
        self.library = library
        self.chat = chat_source_for(url)
        self.platform = self.chat.platform if self.chat else ""
        self.channel = self.chat.channel if self.chat else ""
        self.name = self.channel or url
        self.created_at = time.time()
        self.chat_status = "n/a" if not self.channel else "idle"
        self.messages_total = 0
        self.rate_history: deque[tuple[float, float, float]] = deque(maxlen=180)  # (ts, rate, baseline)
        self.hype_events: deque[dict] = deque(maxlen=50)
        self.detector = HypeDetector(
            window=settings.hype_window,
            baseline=settings.hype_baseline,
            multiplier=multiplier or settings.hype_multiplier,
            min_messages=settings.hype_min_messages,
            cooldown=settings.hype_cooldown,
            warmup=settings.hype_warmup,
            keywords=settings.hype_keywords,
        )
        self.recorder = Recorder(
            url, quality, settings.buffer_dir / self.id,
            settings.segment_seconds, settings.buffer_seconds, settings.reconnect_seconds,
        )
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self.recorder.start()
        if self.chat:
            self._tasks.append(asyncio.create_task(
                self.chat.run(self._on_message, self._on_chat_status)))
            self._tasks.append(asyncio.create_task(self._hype_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.recorder.stop()

    def _on_chat_status(self, status: str) -> None:
        self.chat_status = status

    def _on_message(self, user: str, text: str) -> None:
        self.messages_total += 1
        self.detector.feed(time.time(), text)

    async def _hype_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            now = time.time()
            rate, baseline, _ = self.detector.stats(now)
            self.rate_history.append((now, round(rate, 2), round(baseline, 2)))
            event = self.detector.check(now)
            if not event:
                continue
            words = ", ".join(event.top_words)
            info = {"at": now, "score": round(event.score, 1), "rate": round(event.rate, 1),
                    "baseline": round(event.baseline, 2), "words": event.top_words, "clip_id": None}
            log.info("hype on %s: x%.1f (%s)", self.name, event.score, words)
            if self.auto and self.recorder.status == "live":
                # The spike is detected a few seconds after the moment; pre-roll covers it.
                clip = self.library.request(
                    self.recorder, stream_id=self.id, channel=self.name,
                    title=f"{self.name}: chat x{event.score:.1f}" + (f" ({words})" if words else ""),
                    reason="auto", detail=f"chat spike x{event.score:.1f}, {event.messages} msgs/{self.detector.window:.0f}s",
                    pre=self.settings.auto_pre, post=self.settings.auto_post, at=now,
                    meta=info,
                )
                info["clip_id"] = clip.id
            self.hype_events.appendleft(info)

    def clip(self, pre: float, post: float, title: str = ""):
        return self.library.request(
            self.recorder, stream_id=self.id, channel=self.name,
            title=title or f"{self.name}: manual clip", reason="manual", detail="",
            pre=pre, post=post,
        )

    def to_dict(self) -> dict:
        rate, baseline, _ = self.detector.stats(time.time()) if self.channel else (0, 0, 0)
        return {
            "id": self.id, "url": self.url, "name": self.name, "channel": self.channel,
            "platform": self.platform,
            "quality": self.quality, "auto": self.auto,
            "multiplier": self.detector.multiplier,
            "status": self.recorder.status, "error": self.recorder.last_error,
            "buffered": round(self.recorder.buffered_seconds(), 1),
            "chat_status": self.chat_status, "messages": self.messages_total,
            "rate": round(rate, 2), "baseline": round(baseline, 2),
            "history": [[round(r, 2), round(b, 2)] for _, r, b in self.rate_history],
            "hype_events": list(self.hype_events)[:10],
        }


class StreamManager:
    def __init__(self, settings: Settings, library: ClipLibrary):
        self.settings = settings
        self.library = library
        self.streams: dict[str, WatchedStream] = {}

    def add(self, url: str, quality: str | None, auto: bool, multiplier: float | None) -> WatchedStream:
        s = WatchedStream(url, quality or self.settings.default_quality, auto,
                          self.settings, self.library, multiplier)
        self.streams[s.id] = s
        s.start()
        return s

    async def remove(self, stream_id: str) -> bool:
        s = self.streams.pop(stream_id, None)
        if not s:
            return False
        await s.stop()
        return True

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.remove(i) for i in list(self.streams)), return_exceptions=True)
