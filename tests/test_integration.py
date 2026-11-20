"""End-to-end: synthetic ffmpeg stream + injected chat burst -> auto clip on disk."""
import asyncio
import shutil
import time

import pytest

from clipper.clips import ClipLibrary
from clipper.config import Settings
from clipper.streams import WatchedStream

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_chat_spike_creates_auto_clip(tmp_path):
    async def scenario():
        s = Settings(data_dir=tmp_path, segment_seconds=1, buffer_seconds=30,
                     auto_pre=4, auto_post=1, hype_window=2, hype_warmup=3,
                     hype_min_messages=5, hype_cooldown=60)
        lib = ClipLibrary(s.clips_dir)
        ws = WatchedStream("lavfi:testsrc=size=320x180:rate=30", "best", True, s, lib)
        ws.channel = "fake"  # pretend there's chat; we inject messages instead of connecting
        ws.recorder.start()
        hype = asyncio.create_task(ws._hype_loop())
        try:
            for _ in range(8):  # quiet chat for warmup / baseline
                ws._on_message("u", "hello")
                await asyncio.sleep(0.5)
            for _ in range(40):  # burst
                ws._on_message("u", "POG no way")
                await asyncio.sleep(0.03)
            deadline = time.time() + 20
            while time.time() < deadline:
                clips = lib.list()
                if clips and clips[0].status != "pending":
                    break
                await asyncio.sleep(0.5)
            clips = lib.list()
            assert clips, "no clip was triggered"
            c = clips[0]
            assert c.reason == "auto" and c.status == "ready", c.error
            assert (s.clips_dir / c.file).stat().st_size > 0
            assert c.duration >= 3
            assert ws.hype_events and ws.hype_events[0]["clip_id"] == c.id
        finally:
            hype.cancel()
            await ws.stop()

    asyncio.run(scenario())
