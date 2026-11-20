"""Turns kept clips into vertical videos and posts them to every connected platform.

Runs as one background worker so renders and uploads happen one at a time. Per platform it
respects a daily cap and a minimum gap between posts, retries transient failures, and waits
until tomorrow when a platform says the daily limit is reached.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..clips import Clip, ClipLibrary
from ..config import Settings
from ..vertical import render_vertical
from .tiktok import TikTok
from .youtube import QuotaError, YouTube

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


class Publisher:
    def __init__(self, settings: Settings, library: ClipLibrary, youtube: YouTube, tiktok: TikTok):
        self.settings = settings
        self.library = library
        self.youtube = youtube
        self.tiktok = tiktok
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._timers: list[asyncio.TimerHandle] = []

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._task = asyncio.create_task(self._worker())
        for clip in self.library.list():
            if self._has_unfinished(clip):
                self.enqueue(clip.id)

    async def stop(self) -> None:
        for t in self._timers:
            t.cancel()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def enqueue(self, clip_id: str) -> None:
        self.queue.put_nowait(clip_id)

    def enqueue_later(self, clip_id: str, at: float) -> None:
        loop = asyncio.get_running_loop()
        self._timers = [t for t in self._timers if not t.cancelled()]
        self._timers.append(loop.call_later(max(1.0, at - time.time()), self.enqueue, clip_id))

    def retry(self, clip: Clip) -> None:
        for p in clip.posts.values():
            if p.get("status") in ("failed", "waiting"):
                p.update(status="pending", error="", attempts=0)
        self.library.save()
        self.enqueue(clip.id)

    # ---------------------------------------------------------------- state
    def platforms(self) -> dict[str, YouTube | TikTok]:
        out: dict[str, YouTube | TikTok] = {}
        if self.settings.post_youtube and self.youtube.configured and self.youtube.connected:
            out["youtube"] = self.youtube
        if self.settings.post_tiktok and self.tiktok.configured and self.tiktok.connected:
            out["tiktok"] = self.tiktok
        return out

    def _has_unfinished(self, clip: Clip) -> bool:
        if clip.verdict != "approved" or clip.status != "ready" or clip.purged:
            return False
        return any(clip.posts.get(p, {}).get("status") not in ("posted", "failed")
                   for p in self.platforms())

    def blocked_reason(self, clip: Clip) -> str:
        if clip.verdict != "approved":
            return "not kept"
        if clip.status != "ready" or not clip.file or clip.purged:
            return "video not available"
        if clip.duration and clip.duration < self.settings.min_clip_seconds - 0.1:
            return f"shorter than {self.settings.min_clip_seconds:.0f}s"
        if not self.settings.post_requires_classifier:
            return ""
        if clip.reason == "manual" or clip.verdict_reason == "set by hand":
            return ""
        cls = clip.meta.get("classification")
        if cls and not cls.get("refused"):
            return ""
        return "waiting for Claude's approval or a Keep click"

    def _posted_times(self, platform: str) -> list[float]:
        return [c.posts[platform]["posted_at"] for c in self.library.clips.values()
                if c.posts.get(platform, {}).get("status") == "posted"
                and c.posts[platform].get("posted_at")]

    def next_slot(self, platform: str, now: float) -> float:
        """Earliest time a new post is allowed on this platform (now if allowed)."""
        times = sorted(self._posted_times(platform))
        limit = self.settings.youtube_daily_limit if platform == "youtube" else self.settings.tiktok_daily_limit
        slot = now
        if times:
            slot = max(slot, times[-1] + self.settings.post_interval_minutes * 60)
        recent = [t for t in times if t > now - 86400]
        if limit > 0 and len(recent) >= limit:
            slot = max(slot, recent[-limit] + 86400)
        return slot

    # ---------------------------------------------------------------- work
    async def _worker(self) -> None:
        while True:
            clip_id = await self.queue.get()
            clip = self.library.clips.get(clip_id)
            if not clip:
                continue
            try:
                await self.process(clip)
            except Exception:  # noqa: BLE001
                log.exception("publishing %s failed", clip_id)
            self.library.save()

    async def process(self, clip: Clip) -> None:
        targets = self.platforms()
        if not targets:
            return
        blocked = self.blocked_reason(clip)
        for name in targets:
            post = clip.posts.setdefault(name, {"status": "pending", "attempts": 0})
            if post["status"] in ("posted", "failed"):
                continue
            if blocked:
                post.update(status="waiting", error=blocked)
        if blocked:
            return
        todo = [n for n in targets if clip.posts[n]["status"] not in ("posted", "failed")]
        if not todo:
            return

        video = self.library.dir / clip.file
        if self.settings.vertical:
            if not clip.vertical or not (self.library.dir / clip.vertical).exists():
                out = self.library.dir / f"{clip.id}_vertical.mp4"
                clip.status_detail = "rendering vertical"
                self.library.save()
                await render_vertical(video, out, clip.meta.get("words"),
                                      self.settings.vertical_mode, self.settings.captions)
                clip.vertical = out.name
                clip.status_detail = ""
            video = self.library.dir / clip.vertical

        title, description, tags, caption = self.texts(clip)
        for name in todo:
            post = clip.posts[name]
            now = time.time()
            slot = self.next_slot(name, now)
            if slot > now + 1:
                post.update(status="scheduled", next_try=slot, error="")
                self.enqueue_later(clip.id, slot)
                continue
            post.update(status="uploading", error="")
            self.library.save()
            try:
                if name == "youtube":
                    result = await self.youtube.upload(video, title, description, tags)
                else:
                    result = await self.tiktok.upload(video, caption)
                post.update(status="posted", posted_at=time.time(), next_try=None, **result)
                log.info("posted %s to %s: %s", clip.id, name, result.get("url") or result.get("id"))
            except QuotaError as exc:
                retry_at = time.time() + 6 * 3600
                post.update(status="scheduled", error=str(exc), next_try=retry_at)
                self.enqueue_later(clip.id, retry_at)
            except Exception as exc:  # noqa: BLE001
                post["attempts"] = post.get("attempts", 0) + 1
                if post["attempts"] < MAX_ATTEMPTS:
                    retry_at = time.time() + 600 * post["attempts"]
                    post.update(status="scheduled", error=str(exc), next_try=retry_at)
                    self.enqueue_later(clip.id, retry_at)
                else:
                    post.update(status="failed", error=str(exc))
                log.warning("posting %s to %s failed: %s", clip.id, name, exc)
            self.library.save()

    def texts(self, clip: Clip) -> tuple[str, str, list[str], str]:
        tags = [t.lstrip("#") for t in self.settings.hashtags if t.lstrip("#")]
        hashtags = " ".join(f"#{t}" for t in tags)
        has_cls = bool(clip.meta.get("classification", {}).get("title"))
        base = clip.title if (has_cls or clip.reason == "manual" and ":" not in clip.title) \
            else f"{clip.channel} live moment"
        title = base if "#shorts" in base.lower() or len(base) > 90 else f"{base} #Shorts"
        platform = clip.platform
        source = f"Clipped live from {clip.channel}" + (f" on {platform.title()}" if platform else "")
        description = "\n\n".join(x for x in (clip.summary, source + ".", hashtags) if x)
        caption = f"{base} {hashtags}".strip()
        return title, description, tags + [clip.channel], caption
