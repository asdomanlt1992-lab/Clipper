"""Cutting clips out of the rolling buffer and keeping a clip library."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .recorder import Recorder, select_segments

log = logging.getLogger(__name__)


@dataclass
class Clip:
    id: str
    stream_id: str
    channel: str
    title: str
    reason: str  # "manual" or "auto"
    detail: str
    created_at: float
    pre: float
    post: float
    status: str = "pending"  # pending | analyzing | ready | failed
    error: str = ""
    duration: float = 0.0
    file: str = ""
    thumb: str = ""
    meta: dict = field(default_factory=dict)
    score: float = 0.0
    verdict: str = ""  # approved | rejected | review
    verdict_reason: str = ""
    category: str = ""
    summary: str = ""
    transcript: str = ""
    purged: bool = False  # video deleted (rejected clips after the retention period)
    platform: str = ""
    status_detail: str = ""
    vertical: str = ""  # 9:16 version for Shorts/TikTok
    posts: dict = field(default_factory=dict)  # platform -> {status, url, id, error, posted_at, ...}


# Called after cutting: (clip, video path) -> None. Sets verdict/category/etc. on the clip.
Analyzer = Callable[["Clip", Path], Awaitable[None]]


class ClipLibrary:
    def __init__(self, clips_dir: Path, min_seconds: float = 0.0):
        self.dir = clips_dir
        self.min_seconds = min_seconds
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        self.clips: dict[str, Clip] = {}
        self.on_approved: Callable[[Clip], None] | None = None  # set by the publisher
        self._load()

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            for raw in json.loads(self.index_path.read_text()):
                clip = Clip(**raw)
                if clip.status in ("pending", "analyzing"):
                    clip.status, clip.error = "failed", "interrupted by restart"
                self.clips[clip.id] = clip
        except Exception:  # noqa: BLE001
            log.exception("could not read clip index; starting empty")

    def save(self) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(c) for c in self.list()], indent=2))
        tmp.replace(self.index_path)

    def list(self) -> list[Clip]:
        return sorted(self.clips.values(), key=lambda c: c.created_at, reverse=True)

    def delete(self, clip_id: str) -> bool:
        clip = self.clips.pop(clip_id, None)
        if not clip:
            return False
        for name in (clip.file, clip.thumb, clip.vertical):
            if name:
                (self.dir / name).unlink(missing_ok=True)
        self.save()
        return True

    def set_verdict(self, clip_id: str, verdict: str) -> Clip:
        clip = self.clips[clip_id]
        if verdict == "approved" and clip.purged:
            raise ValueError("the video was already deleted")
        clip.verdict, clip.verdict_reason = verdict, "set by hand"
        self.save()
        if verdict == "approved" and clip.status == "ready" and self.on_approved:
            self.on_approved(clip)
        return clip

    def purge_rejected(self, keep_hours: float) -> int:
        """Delete video files of rejected clips older than keep_hours; keep their metadata."""
        cutoff = time.time() - keep_hours * 3600
        n = 0
        for clip in self.clips.values():
            if clip.verdict == "rejected" and not clip.purged and clip.created_at <= cutoff:
                for name in (clip.file, clip.thumb, clip.vertical):
                    if name:
                        (self.dir / name).unlink(missing_ok=True)
                clip.file = clip.thumb = clip.vertical = ""
                clip.purged = True
                n += 1
        if n:
            self.save()
        return n

    def request(self, recorder: Recorder, *, stream_id: str, channel: str, title: str,
                reason: str, detail: str, pre: float, post: float,
                at: float | None = None, meta: dict | None = None, score: float = 0.0,
                analyzer: Analyzer | None = None, platform: str = "") -> Clip:
        now = at if at is not None else time.time()
        if pre + post < self.min_seconds:
            pre = self.min_seconds - post  # make up the difference with earlier footage
        clip = Clip(
            id=time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + uuid.uuid4().hex[:6],
            stream_id=stream_id, channel=channel, title=title, reason=reason,
            detail=detail, created_at=now, pre=pre, post=post, meta=meta or {}, score=score,
            platform=platform,
        )
        self.clips[clip.id] = clip
        self.save()
        asyncio.create_task(self._cut(recorder, clip, analyzer))
        return clip

    async def _cut(self, recorder: Recorder, clip: Clip, analyzer: Analyzer | None = None) -> None:
        try:
            start, end = clip.created_at - clip.pre, clip.created_at + clip.post
            segs: list = []
            for _ in range(3):
                # Wait until the post-roll has been written (plus one segment to close).
                wait = end + recorder.segment_seconds + 0.5 - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                segs = select_segments(recorder.segments(), start, end)
                if not segs:
                    raise RuntimeError("no buffered video for that time range (is the stream live?)")
                # Not enough earlier footage (e.g. the stream just started)? Record longer instead.
                covered_from = max(start, segs[0].start)
                if end - covered_from >= self.min_seconds:
                    break
                end = covered_from + self.min_seconds
                clip.post = end - clip.created_at
            out = self.dir / f"{clip.id}.mp4"
            thumb = self.dir / f"{clip.id}.jpg"
            await concat_segments([s.path for s in segs], out)
            await make_thumbnail(out, thumb)
            clip.file = out.name
            clip.thumb = thumb.name if thumb.exists() else ""
            clip.duration = await probe_duration(out)
            if clip.duration and clip.duration < self.min_seconds - 0.1:
                out.unlink(missing_ok=True)
                thumb.unlink(missing_ok=True)
                clip.file = clip.thumb = ""
                raise RuntimeError(f"only {clip.duration:.0f}s of video available "
                                   f"(minimum is {self.min_seconds:.0f}s); the stream probably dropped")
            if analyzer:
                clip.status = "analyzing"
                self.save()
                await analyzer(clip, out)
            if not clip.verdict:
                clip.verdict = "approved" if clip.reason == "manual" else "review"
            clip.status = "ready"
            log.info("clip %s ready (%.1fs, %s)", clip.id, clip.duration, clip.verdict)
            if clip.verdict == "approved" and self.on_approved:
                self.on_approved(clip)
        except Exception as exc:  # noqa: BLE001
            clip.status, clip.error = "failed", str(exc)
            log.warning("clip %s failed: %s", clip.id, exc)
        self.save()


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"{cmd[0]} failed: {msg[-1] if msg else proc.returncode}")
    return out.decode()


async def concat_segments(paths: list[Path], out: Path) -> None:
    # Copy first so the pruner can't delete segments mid-read.
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        lines = []
        for i, p in enumerate(paths):
            dst = tmpdir / f"{i:05d}.ts"
            try:
                shutil.copyfile(p, dst)
            except FileNotFoundError:
                continue
            lines.append(f"file '{dst}'")
        if not lines:
            raise RuntimeError("buffer segments disappeared before they could be copied")
        listfile = tmpdir / "list.txt"
        listfile.write_text("\n".join(lines) + "\n")
        await _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "concat", "-safe", "0", "-i", str(listfile),
                   "-c", "copy", "-movflags", "+faststart", str(out))


async def make_thumbnail(video: Path, thumb: Path) -> None:
    try:
        await _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", "1", "-i", str(video), "-frames:v", "1",
                   "-vf", "scale=480:-2", str(thumb))
    except RuntimeError as exc:
        log.info("thumbnail failed: %s", exc)


async def probe_duration(video: Path) -> float:
    try:
        out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=nw=1:nk=1", str(video))
        return round(float(out.strip()), 2)
    except (RuntimeError, ValueError):
        return 0.0
