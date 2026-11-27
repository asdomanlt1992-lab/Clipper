"""Rolling-buffer recorder: streamlink -> ffmpeg -> short MPEG-TS segments on disk."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .chat.youtube import resolve_watch_url, youtube_target

log = logging.getLogger(__name__)


@dataclass
class Segment:
    path: Path
    start: float  # wall-clock time (approx.)
    end: float


def list_segments(buffer_dir: Path, segment_seconds: float) -> list[Segment]:
    """Segments in order, with wall-clock start/end estimated from mtimes."""
    files = sorted(buffer_dir.glob("seg_*.ts"))
    segs: list[Segment] = []
    prev_end: float | None = None
    for f in files:
        try:
            end = f.stat().st_mtime
        except FileNotFoundError:
            continue
        start = prev_end if prev_end is not None and prev_end < end else end - segment_seconds
        segs.append(Segment(f, start, end))
        prev_end = end
    return segs


def select_segments(segs: list[Segment], start: float, end: float) -> list[Segment]:
    """Segments overlapping [start, end]."""
    return [s for s in segs if s.end > start and s.start < end]


def build_source_cmds(url: str, quality: str) -> tuple[list[str] | None, list[str]]:
    """Return (streamlink_cmd or None, ffmpeg input args).

    Prefixes for testing / non-streamlink sources:
      lavfi:<graph>   ffmpeg synthetic source, e.g. lavfi:testsrc=size=640x360:rate=30
                      (with audio: lavfi:testsrc=size=640x360:rate=30[out0];sine=f=440[out1])
      direct:<url>    read the URL/file directly with ffmpeg
    """
    if url.startswith("lavfi:"):
        return None, ["-re", "-f", "lavfi", "-i", url[len("lavfi:"):]]
    if url.startswith("direct:"):
        return None, ["-re", "-i", url[len("direct:"):]]
    streamlink = [
        sys.executable, "-m", "streamlink_cli", "--stdout", "--loglevel", "warning",
        "--twitch-disable-ads", url, quality,
    ]
    return streamlink, ["-i", "pipe:0"]


class Recorder:
    def __init__(self, url: str, quality: str, buffer_dir: Path,
                 segment_seconds: float, buffer_seconds: float, reconnect_seconds: float):
        self.url = url
        self.quality = quality
        self.buffer_dir = buffer_dir
        self.segment_seconds = segment_seconds
        self.buffer_seconds = buffer_seconds
        self.reconnect_seconds = reconnect_seconds
        self.status = "idle"
        self.last_error = ""
        self._procs: list[asyncio.subprocess.Process] = []
        self._task: asyncio.Task | None = None
        self._prune_task: asyncio.Task | None = None

    # ---------- lifecycle ----------
    def start(self) -> None:
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._run_forever())
        self._prune_task = asyncio.create_task(self._prune_forever())

    async def stop(self) -> None:
        for t in (self._task, self._prune_task):
            if t:
                t.cancel()
        await self._kill()
        for t in (self._task, self._prune_task):
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        shutil.rmtree(self.buffer_dir, ignore_errors=True)
        self.status = "stopped"

    async def _kill(self) -> None:
        for p in self._procs:
            if p.returncode is None:
                try:
                    p.terminate()
                    await asyncio.wait_for(p.wait(), 5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    try:
                        p.kill()
                    except ProcessLookupError:
                        pass
        self._procs = []

    async def _run_forever(self) -> None:
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.exception("recorder %s failed", self.url)
            await self._kill()
            self.status = "offline (retrying)"
            await asyncio.sleep(self.reconnect_seconds)

    async def _run_once(self) -> None:
        url = self.url
        if not url.startswith(("lavfi:", "direct:")) and youtube_target(url):
            url = await resolve_watch_url(url)  # channel links: find the live video each time
        sl_cmd, ff_input = build_source_cmds(url, self.quality)
        # Continue numbering after any existing segments so names stay sorted.
        existing = sorted(self.buffer_dir.glob("seg_*.ts"))
        start_number = int(existing[-1].stem.split("_")[1]) + 1 if existing else 0
        ff_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            *ff_input,
            "-map", "0:v?", "-map", "0:a?", "-c", "copy",
            "-f", "segment", "-segment_time", str(self.segment_seconds),
            "-segment_format", "mpegts", "-reset_timestamps", "1",
            "-segment_start_number", str(start_number),
            str(self.buffer_dir / "seg_%09d.ts"),
        ]
        if sl_cmd is None and "lavfi" in ff_input:
            # Synthetic sources must be encoded, not stream-copied.
            i = ff_cmd.index("-c")
            ff_cmd[i:i + 2] = ["-c:v", "libx264", "-preset", "veryfast", "-g",
                               str(int(30 * self.segment_seconds)), "-pix_fmt", "yuv420p",
                               "-c:a", "aac"]
        self.status = "starting"
        if sl_cmd:
            r, w = os.pipe()
            sl = await asyncio.create_subprocess_exec(
                *sl_cmd, stdout=w, stderr=asyncio.subprocess.PIPE)
            os.close(w)
            ff = await asyncio.create_subprocess_exec(
                *ff_cmd, stdin=r, stderr=asyncio.subprocess.PIPE)
            os.close(r)
            self._procs = [sl, ff]
        else:
            ff = await asyncio.create_subprocess_exec(
                *ff_cmd, stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            self._procs = [ff]
        self.status = "live"
        self.last_error = ""
        # Wait for any process to exit; then tear the pipeline down.
        waits = [asyncio.create_task(p.wait()) for p in self._procs]
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        errs = []
        for p in self._procs:
            if p.returncode is not None and p.stderr:
                out = (await p.stderr.read()).decode("utf-8", "replace").strip()
                if out:
                    errs.append(out.splitlines()[-1])
        self.last_error = " | ".join(errs) or "stream ended"
        log.info("recorder %s stopped: %s", self.url, self.last_error)

    async def _prune_forever(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, self.segment_seconds))
            cutoff = time.time() - self.buffer_seconds
            segs = list_segments(self.buffer_dir, self.segment_seconds)
            for s in segs[:-1]:  # never delete the segment being written
                if s.end < cutoff:
                    s.path.unlink(missing_ok=True)

    # ---------- queries ----------
    def segments(self) -> list[Segment]:
        return list_segments(self.buffer_dir, self.segment_seconds)

    def buffered_seconds(self) -> float:
        segs = self.segments()
        return segs[-1].end - segs[0].start if segs else 0.0
