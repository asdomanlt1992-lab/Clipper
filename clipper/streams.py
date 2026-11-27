"""A watched stream = recorder + chat listener + audio monitor + hype detector + auto-clipper."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from pathlib import Path

from . import analysis
from .audio import AudioMonitor
from .chat import chat_source_for
from .clips import Clip, ClipLibrary
from .config import Settings
from .hype import HypeDetector
from .recorder import Recorder
from .scoring import score_moment

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
        self.chat_status = "n/a" if not self.chat else "idle"
        self.messages_total = 0
        self.chat_log: deque[tuple[float, str, str]] = deque(maxlen=2000)  # (ts, user, text)
        self.rate_history: deque[tuple[float, float, float]] = deque(maxlen=180)  # (ts, rate, baseline)
        self.hype_events: deque[dict] = deque(maxlen=50)
        self._last_audio_trigger = float("-inf")
        self.detector = HypeDetector(
            window=settings.hype_window,
            baseline=settings.hype_baseline,
            multiplier=multiplier or settings.hype_multiplier,
            min_messages=settings.hype_min_messages,
            cooldown=settings.hype_cooldown,
            warmup=settings.hype_warmup,
            keywords=settings.hype_keywords,
            negative_keywords=settings.hype_negative_keywords,
            baseline_floor=settings.hype_baseline_floor,
        )
        self.recorder = Recorder(
            url, quality, settings.buffer_dir / self.id,
            settings.segment_seconds, settings.buffer_seconds, settings.reconnect_seconds,
            settings.secrets_dir / "cookies.txt", settings.streamlink_args,
        )
        self.audio = AudioMonitor(self.recorder, settings.hype_baseline)
        self._tasks: list[asyncio.Task] = []

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.recorder.start()
        if self.settings.audio:
            self.audio.start()
        if self.chat:
            self._tasks.append(asyncio.create_task(
                self.chat.run(self._on_message, self._on_chat_status)))
        self._tasks.append(asyncio.create_task(self._hype_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.audio.stop()
        await self.recorder.stop()

    def _on_chat_status(self, status: str) -> None:
        self.chat_status = status

    def _on_message(self, user: str, text: str) -> None:
        now = time.time()
        self.messages_total += 1
        self.chat_log.append((now, user, text))
        self.detector.feed(now, text)

    def chat_between(self, start: float, end: float) -> list[str]:
        return [f"[{ts - start:5.1f}s] {u}: {t}" for ts, u, t in self.chat_log if start <= ts <= end]

    # ---------------------------------------------------------------- detection
    async def _hype_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                self._tick(time.time())
            except Exception:  # noqa: BLE001
                log.exception("hype loop error on %s", self.name)

    def _tick(self, now: float) -> None:
        if self.chat:
            rate, baseline, _ = self.detector.stats(now)
            self.rate_history.append((now, round(rate, 2), round(baseline, 2)))
            event = self.detector.check(now)
            if event:
                audio_db = self.audio.boost(now - self.detector.window, now)
                self._on_moment(now, "chat", event.score, event.mood, audio_db, event.top_words,
                                f"chat spike x{event.score:.1f}, {event.messages} msgs/"
                                f"{self.detector.window:.0f}s")
        elif self.settings.audio_only_enabled:
            boost = self.audio.latest_boost()
            if boost >= self.settings.audio_trigger_db and \
                    now - self._last_audio_trigger >= self.settings.hype_cooldown:
                self._last_audio_trigger = now
                self._on_moment(now, "audio", 0.0, 0.0, boost, [], f"audio spike +{boost:.0f} dB")

    def _on_moment(self, now: float, source: str, spike: float, mood: float, audio_db: float,
                   words: list[str], detail: str) -> None:
        s = score_moment(spike, self.detector.multiplier, mood, audio_db)
        info = {"at": now, "source": source, "score": s["score"], "breakdown": s,
                "words": words, "detail": detail, "clip_id": None, "skipped": ""}
        # Gate before cutting. Audio-only moments skip the score gate: the classifier decides.
        if source == "chat" and mood <= self.settings.mood_skip:
            info["skipped"] = f"chat mood negative ({mood:+.2f})"
        elif source == "chat" and s["score"] < self.settings.min_score:
            info["skipped"] = f"score {s['score']:.0f} < {self.settings.min_score:.0f}"
        elif not self.auto:
            info["skipped"] = "auto-clipping off"
        elif self.recorder.status != "live":
            info["skipped"] = "stream not live"
        log.info("moment on %s: %s score=%.0f %s", self.name, detail, s["score"],
                 f"(skipped: {info['skipped']})" if info["skipped"] else "")
        if not info["skipped"]:
            title_words = f" ({', '.join(words)})" if words else ""
            clip = self.library.request(
                self.recorder, stream_id=self.id, channel=self.name,
                title=f"{self.name}: {detail.split(',')[0]}{title_words}",
                reason="auto", detail=detail,
                pre=self.settings.auto_pre, post=self.settings.auto_post, at=now,
                meta=info, score=s["score"], analyzer=self._analyze, platform=self.platform,
            )
            info["clip_id"] = clip.id
        self.hype_events.appendleft(info)

    # ---------------------------------------------------------------- analysis
    async def _analyze(self, clip: Clip, video: Path) -> None:
        try:
            context = {
                "platform": self.platform, "channel": self.name,
                "trigger": clip.detail or "manual clip by the operator",
                "duration": clip.duration,
                "score": clip.meta.get("breakdown", {}),
                "chat": self.chat_between(clip.created_at - clip.pre, clip.created_at + clip.post),
            }
            result = await analysis.analyze(video, clip.duration, context, self.settings)
            clip.transcript = result.transcript
            if result.words:
                clip.meta["words"] = result.words
            if result.classification:
                cls = result.classification
                clip.category = cls.get("category", "")
                clip.summary = cls.get("summary", "")
                if cls.get("title"):
                    clip.title = cls["title"]
                clip.meta["classification"] = cls
            if result.errors:
                clip.meta["analysis_errors"] = result.errors
            clip.verdict, clip.verdict_reason = analysis.decide(
                clip.reason, clip.score, result, self.settings)
        except Exception as exc:  # noqa: BLE001
            log.exception("analysis failed for %s", clip.id)
            clip.meta["analysis_errors"] = [str(exc)]
            clip.verdict, clip.verdict_reason = analysis.decide(
                clip.reason, clip.score, analysis.Analysis(), self.settings)

    def clip(self, pre: float, post: float, title: str = ""):
        now = time.time()
        audio_db = self.audio.boost(now - 10, now)
        return self.library.request(
            self.recorder, stream_id=self.id, channel=self.name,
            title=title or f"{self.name}: manual clip", reason="manual", detail="",
            pre=pre, post=post, at=now, analyzer=self._analyze, platform=self.platform,
            meta={"breakdown": score_moment(0, self.detector.multiplier,
                                            self.detector.mood(now), audio_db)},
        )

    # ---------------------------------------------------------------- api view
    def to_dict(self) -> dict:
        now = time.time()
        rate, baseline, _ = self.detector.stats(now) if self.chat else (0, 0, 0)
        return {
            "id": self.id, "url": self.url, "name": self.name, "channel": self.channel,
            "platform": self.platform,
            "quality": self.quality, "auto": self.auto,
            "multiplier": self.detector.multiplier,
            "status": self.recorder.status, "error": self.recorder.last_error,
            "buffered": round(self.recorder.buffered_seconds(), 1),
            "chat_status": self.chat_status, "messages": self.messages_total,
            "rate": round(rate, 2), "baseline": round(baseline, 2),
            "mood": round(self.detector.mood(now), 2) if self.chat else 0,
            "has_audio": self.audio.has_audio,
            "audio_db": round(self.audio.latest_boost(), 1),
            "audio_only": not self.chat and self.settings.audio_only_enabled,
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
