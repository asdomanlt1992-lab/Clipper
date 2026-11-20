"""Loudness tracking on the rolling buffer: one volumedetect pass per finished segment."""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
from collections import deque

from .recorder import Recorder

log = logging.getLogger(__name__)
_MEAN = re.compile(r"mean_volume:\s*(-?[\d.]+) dB")


async def measure_db(path: str) -> float | None:
    """Mean loudness (dB) of a file's first audio track, or None if it has no audio."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0?",
        "-af", "volumedetect", "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    m = _MEAN.search(err.decode("utf-8", "replace"))
    return float(m.group(1)) if m else None


class AudioMonitor:
    def __init__(self, recorder: Recorder, history_seconds: float = 300.0):
        self.recorder = recorder
        n = max(30, int(history_seconds / max(recorder.segment_seconds, 0.5)))
        self.history: deque[tuple[float, float]] = deque(maxlen=n)  # (segment end, mean dB)
        self.has_audio: bool | None = None
        self._seen: set[str] = set()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, self.recorder.segment_seconds))
            try:
                segs = self.recorder.segments()[:-1]  # skip the one still being written
                names = {s.path.name for s in segs}
                self._seen &= names
                for s in segs:
                    if s.path.name in self._seen:
                        continue
                    self._seen.add(s.path.name)
                    db = await measure_db(str(s.path))
                    if db is None:
                        if self.has_audio is None:
                            self.has_audio = False
                        continue
                    self.has_audio = True
                    self.history.append((s.end, db))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("audio monitor error")

    def baseline(self) -> float | None:
        """Median loudness, ignoring the newest few segments so a spike doesn't raise its own bar."""
        vals = [db for _, db in list(self.history)[:-3]]
        return statistics.median(vals) if len(vals) >= 10 else None

    def boost(self, start: float, end: float) -> float:
        """Loudest segment in [start, end] relative to baseline, in dB (>= 0)."""
        base = self.baseline()
        if base is None:
            return 0.0
        vals = [db for t, db in self.history if start <= t <= end + self.recorder.segment_seconds]
        return max(0.0, max(vals) - base) if vals else 0.0

    def latest_boost(self) -> float:
        base = self.baseline()
        if base is None or not self.history:
            return 0.0
        return max(0.0, self.history[-1][1] - base)
