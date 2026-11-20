"""FastAPI app: REST API + single-page UI."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Literal
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analysis
from .clips import ClipLibrary
from .config import Settings
from .streams import StreamManager

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class AddStream(BaseModel):
    url: str = Field(min_length=3)
    quality: str | None = None
    auto: bool = True
    multiplier: float | None = Field(default=None, gt=1.0, le=20.0)


class UpdateStream(BaseModel):
    auto: bool | None = None
    multiplier: float | None = Field(default=None, gt=1.0, le=20.0)


class VerdictRequest(BaseModel):
    verdict: Literal["approved", "rejected"]


class ClipRequest(BaseModel):
    pre: float | None = Field(default=None, ge=1, le=600)
    post: float | None = Field(default=None, ge=0, le=120)
    title: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    library = ClipLibrary(settings.clips_dir, settings.min_clip_seconds)
    manager = StreamManager(settings, library)
    streams_file = settings.data_dir / "streams.json"

    def persist_streams() -> None:
        streams_file.write_text(json.dumps([
            {"url": s.url, "quality": s.quality, "auto": s.auto, "multiplier": s.detector.multiplier}
            for s in manager.streams.values()
        ], indent=2))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        shutil.rmtree(settings.buffer_dir, ignore_errors=True)  # stale buffers from a previous run
        if streams_file.exists():
            try:
                for s in json.loads(streams_file.read_text()):
                    manager.add(s["url"], s.get("quality"), s.get("auto", True), s.get("multiplier"))
            except Exception:  # noqa: BLE001
                log.exception("could not restore streams")
        async def purge_loop():
            while True:
                try:
                    n = library.purge_rejected(settings.rejected_keep_hours)
                    if n:
                        log.info("deleted video for %d rejected clips", n)
                except Exception:  # noqa: BLE001
                    log.exception("purge failed")
                await asyncio.sleep(60)

        purge = asyncio.create_task(purge_loop())
        log.info("classifier: %s, transcription: %s, audio-only triggers: %s",
                 "on" if settings.classifier_enabled else "off (no Anthropic credentials)",
                 "on" if settings.transcribe != "off" and analysis.whisper_available() else "off",
                 "on" if settings.audio_only_enabled else "off")
        yield
        purge.cancel()
        await manager.shutdown()

    app = FastAPI(title="Clipper", lifespan=lifespan)
    app.state.settings, app.state.library, app.state.manager = settings, library, manager

    def get_stream(stream_id: str):
        s = manager.streams.get(stream_id)
        if not s:
            raise HTTPException(404, "stream not found")
        return s

    @app.get("/api/config")
    def config():
        return {"buffer_seconds": settings.buffer_seconds,
                "manual_pre": settings.manual_pre, "manual_post": settings.manual_post,
                "auto_pre": settings.auto_pre, "auto_post": settings.auto_post,
                "multiplier": settings.hype_multiplier,
                "min_clip_seconds": settings.min_clip_seconds,
                "classifier": settings.classifier_enabled,
                "classifier_model": settings.claude_model if settings.classifier_enabled else "",
                "transcribe": settings.transcribe != "off" and analysis.whisper_available(),
                "audio": settings.audio, "audio_only": settings.audio_only_enabled,
                "approve_score": settings.approve_score, "min_score": settings.min_score,
                "reject_categories": settings.reject_categories,
                "rejected_keep_hours": settings.rejected_keep_hours}

    @app.get("/api/streams")
    def list_streams():
        return [s.to_dict() for s in manager.streams.values()]

    @app.post("/api/streams", status_code=201)
    async def add_stream(body: AddStream):
        if any(s.url == body.url for s in manager.streams.values()):
            raise HTTPException(409, "already watching that stream")
        s = manager.add(body.url.strip(), body.quality, body.auto, body.multiplier)
        persist_streams()
        return s.to_dict()

    @app.patch("/api/streams/{stream_id}")
    def update_stream(stream_id: str, body: UpdateStream):
        s = get_stream(stream_id)
        if body.auto is not None:
            s.auto = body.auto
        if body.multiplier is not None:
            s.detector.multiplier = body.multiplier
        persist_streams()
        return s.to_dict()

    @app.delete("/api/streams/{stream_id}", status_code=204)
    async def delete_stream(stream_id: str):
        if not await manager.remove(stream_id):
            raise HTTPException(404, "stream not found")
        persist_streams()

    @app.post("/api/streams/{stream_id}/clip", status_code=202)
    async def clip_stream(stream_id: str, body: ClipRequest):
        s = get_stream(stream_id)
        pre = body.pre if body.pre is not None else settings.manual_pre
        post = body.post if body.post is not None else settings.manual_post
        pre = max(pre, settings.min_clip_seconds - post)
        if pre + post > settings.buffer_seconds:
            raise HTTPException(400, f"clip longer than the {settings.buffer_seconds:.0f}s buffer")
        return asdict(s.clip(pre, post, body.title))

    @app.get("/api/clips")
    def list_clips():
        return [asdict(c) for c in library.list()]

    @app.post("/api/clips/{clip_id}/verdict")
    def set_verdict(clip_id: str, body: VerdictRequest):
        if clip_id not in library.clips:
            raise HTTPException(404, "clip not found")
        try:
            return asdict(library.set_verdict(clip_id, body.verdict))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/clips/{clip_id}", status_code=204)
    def delete_clip(clip_id: str):
        if not library.delete(clip_id):
            raise HTTPException(404, "clip not found")

    app.mount("/media", StaticFiles(directory=settings.clips_dir), name="media")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app
