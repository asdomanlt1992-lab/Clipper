"""FastAPI app: REST API + single-page UI."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import time
from typing import Literal
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analysis
from .clips import ClipLibrary
from .config import Settings
from .publish.publisher import Publisher
from .publish.tiktok import TikTok
from .publish.tokens import TokenStore
from .publish.youtube import YouTube
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
    tokens = TokenStore(settings.secrets_dir)
    youtube = YouTube(settings.youtube_client_id, settings.youtube_client_secret,
                      f"{settings.public_url}/auth/youtube/callback", tokens,
                      settings.youtube_privacy, settings.youtube_category)
    tiktok = TikTok(settings.tiktok_client_key, settings.tiktok_client_secret,
                    f"{settings.public_url}/auth/tiktok/callback", tokens, settings.tiktok_privacy)
    publisher = Publisher(settings, library, youtube, tiktok)
    library.on_approved = lambda clip: publisher.enqueue(clip.id)
    oauth_states: dict[str, tuple[str, float]] = {}
    clients = {"youtube": youtube, "tiktok": tiktok}
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
        publisher.start()
        log.info("classifier: %s, transcription: %s, audio-only triggers: %s",
                 "on" if settings.classifier_enabled else "off (no Anthropic credentials)",
                 "on" if settings.transcribe != "off" and analysis.whisper_available() else "off",
                 "on" if settings.audio_only_enabled else "off")
        yield
        purge.cancel()
        await publisher.stop()
        await manager.shutdown()

    app = FastAPI(title="Clipper", lifespan=lifespan)
    app.state.settings, app.state.library, app.state.manager = settings, library, manager
    app.state.publisher = publisher

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

    @app.post("/api/clips/{clip_id}/post", status_code=202)
    def post_clip(clip_id: str):
        clip = library.clips.get(clip_id)
        if not clip:
            raise HTTPException(404, "clip not found")
        if not publisher.platforms():
            raise HTTPException(400, "no platform connected")
        if clip.verdict != "approved":
            library.set_verdict(clip_id, "approved")  # posting a clip by hand means keeping it
        clip.verdict_reason = "set by hand"
        publisher.retry(clip)
        return asdict(clip)

    # ---------------------------------------------------------------- platforms
    @app.get("/api/platforms")
    def platforms():
        now = time.time()
        out = {}
        for name, c in clients.items():
            enabled = settings.post_youtube if name == "youtube" else settings.post_tiktok
            posted = [t for t in publisher._posted_times(name) if t > now - 86400]
            out[name] = {"configured": c.configured, "connected": c.connected, "enabled": enabled,
                         "active": name in publisher.platforms(),
                         "posted_24h": len(posted),
                         "daily_limit": settings.youtube_daily_limit if name == "youtube"
                         else settings.tiktok_daily_limit,
                         "next_slot": publisher.next_slot(name, now),
                         "redirect_uri": c.redirect_uri,
                         "privacy": c.privacy}
        return out

    @app.get("/auth/{platform}")
    def auth_start(platform: str):
        c = clients.get(platform)
        if not c:
            raise HTTPException(404, "unknown platform")
        if not c.configured:
            raise HTTPException(400, f"{platform} app credentials are not set (see README)")
        state = secrets.token_urlsafe(24)
        oauth_states[state] = (platform, time.time())
        return RedirectResponse(c.auth_url(state))

    @app.get("/auth/{platform}/callback")
    async def auth_callback(platform: str, state: str = "", code: str = "", error: str = "",
                            error_description: str = ""):
        expected = oauth_states.pop(state, None)
        if not expected or expected[0] != platform or time.time() - expected[1] > 900:
            raise HTTPException(400, "sign-in link expired or invalid; start again")
        if error or not code:
            return HTMLResponse(f"<p>{platform} sign-in was cancelled: {error_description or error}</p>"
                                '<p><a href="/">Back</a></p>', status_code=400)
        try:
            await clients[platform].exchange_code(code)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s sign-in failed: %s", platform, exc)
            return HTMLResponse(f"<p>{platform} sign-in failed: {exc}</p><p><a href='/'>Back</a></p>",
                                status_code=400)
        return RedirectResponse("/?connected=" + platform)

    cookies_path = settings.secrets_dir / "cookies.txt"

    @app.get("/api/cookies")
    def cookies_status():
        if not cookies_path.exists():
            return {"present": False}
        text = cookies_path.read_text(errors="replace")
        domains = sorted({line.split("\t")[0].removeprefix("#HttpOnly_").lstrip(".")
                          for line in text.splitlines() if line.count("\t") >= 6})
        sites = [s for s in ("youtube.com", "twitch.tv", "kick.com") if any(d.endswith(s) for d in domains)]
        return {"present": True, "sites": sites, "updated": cookies_path.stat().st_mtime}

    @app.put("/api/cookies", status_code=204)
    async def upload_cookies(request: Request):
        body = (await request.body()).decode("utf-8", "replace")
        if len(body) > 5_000_000:
            raise HTTPException(413, "file too large")
        lines = [l for l in body.splitlines() if l.count("\t") >= 6]
        if not lines:
            raise HTTPException(400, "that doesn't look like a cookies.txt (Netscape format) file")
        settings.secrets_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(settings.secrets_dir, 0o700)
        tmp = cookies_path.with_suffix(".tmp")
        tmp.write_text("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(cookies_path)

    @app.delete("/api/cookies", status_code=204)
    def delete_cookies():
        cookies_path.unlink(missing_ok=True)

    @app.delete("/api/platforms/{platform}", status_code=204)
    def disconnect(platform: str):
        if platform not in clients:
            raise HTTPException(404, "unknown platform")
        tokens.delete(platform)

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
