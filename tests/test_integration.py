"""End-to-end: synthetic ffmpeg stream + fake chat -> auto clips with verdicts."""
import asyncio
import shutil
import time

import pytest

from clipper import analysis
from clipper.chat import ChatSource
from clipper.clips import ClipLibrary
from clipper.config import Settings
from clipper.streams import WatchedStream

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
SOURCE = "lavfi:testsrc=size=320x180:rate=30[out0];sine=f=440[out1]"


def settings(tmp_path, **kw):
    base = dict(data_dir=tmp_path, segment_seconds=1, buffer_seconds=30, auto_pre=4, auto_post=1,
                hype_window=2, hype_warmup=3, hype_min_messages=5, hype_cooldown=60,
                transcribe="off", classifier="off", min_clip_seconds=0)
    base.update(kw)
    return Settings(**base)


def fake_chat(burst_text):
    async def run(on_message, on_status):
        on_status("connected")
        for _ in range(8):  # quiet chat for warmup / baseline
            on_message("u", "hello")
            await asyncio.sleep(0.5)
        for _ in range(40):  # burst
            on_message("u", burst_text)
            await asyncio.sleep(0.03)
        await asyncio.Event().wait()
    return ChatSource("fake", "fake", run)


async def run_stream(s, burst_text, wait_for_clip=True):
    lib = ClipLibrary(s.clips_dir, s.min_clip_seconds)
    ws = WatchedStream(SOURCE, "best", True, s, lib)
    ws.chat = fake_chat(burst_text)
    ws.start()
    try:
        deadline = time.time() + 25
        while time.time() < deadline:
            clips = lib.list()
            if clips and clips[0].status not in ("pending", "analyzing"):
                break
            if not wait_for_clip and ws.hype_events:
                break
            await asyncio.sleep(0.5)
        return lib, ws
    finally:
        await ws.stop()


def test_hype_burst_is_clipped_and_approved(tmp_path):
    lib, ws = asyncio.run(run_stream(settings(tmp_path), "POG no way"))
    clips = lib.list()
    assert clips, "no clip was triggered"
    c = clips[0]
    assert c.reason == "auto" and c.status == "ready", c.error
    assert (lib.dir / c.file).stat().st_size > 0 and c.duration >= 3
    assert c.verdict == "approved" and c.score >= 50, (c.score, c.verdict_reason)
    assert ws.audio.has_audio is True


def test_minimum_length_is_enforced(tmp_path):
    # pre 4 + post 1 asks for 5s; the stream has only a few seconds buffered at the moment,
    # so the clip has to keep recording after the moment to reach the 12s minimum.
    lib, _ = asyncio.run(run_stream(settings(tmp_path, min_clip_seconds=12), "POG no way"))
    c = lib.list()[0]
    assert c.status == "ready", c.error
    assert c.duration >= 12 - 0.1, c.duration


def test_negative_chat_is_skipped(tmp_path):
    lib, ws = asyncio.run(run_stream(settings(tmp_path), "bro chill, cringe", wait_for_clip=False))
    assert ws.hype_events, "spike was not detected"
    assert "negative" in ws.hype_events[0]["skipped"]
    assert lib.list() == []


def test_classifier_rejects_rage(tmp_path, monkeypatch):
    seen = {}

    async def fake_classify(context, transcript, frames, s):
        seen["frames"], seen["chat"] = len(frames), context["chat"]
        return {"category": "rage", "keep": False, "confidence": 0.9, "title": "Streamer loses it",
                "summary": "Yelling at chat.", "reason": "Angry outburst."}

    monkeypatch.setattr(analysis, "classify", fake_classify)
    lib, _ = asyncio.run(run_stream(settings(tmp_path, classifier="on"), "POG no way"))
    c = lib.list()[0]
    assert c.verdict == "rejected" and c.category == "rage"
    assert c.title == "Streamer loses it"
    assert seen["frames"] == 3 and seen["chat"]
