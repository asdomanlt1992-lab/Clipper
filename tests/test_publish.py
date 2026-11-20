import asyncio
import json
import shutil
import subprocess
import time

import httpx
import pytest

from clipper.clips import Clip, ClipLibrary
from clipper.config import Settings
from clipper.publish.publisher import Publisher
from clipper.publish.tiktok import MB, TikTok, chunk_plan
from clipper.publish.tokens import TokenStore
from clipper.publish.youtube import QuotaError, YouTube
from clipper.vertical import caption_chunks


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- youtube
def test_youtube_refresh_and_resumable_upload(tmp_path):
    store = TokenStore(tmp_path / "secrets")
    store.save("youtube", {"refresh_token": "R", "access_token": "old", "expires_at": 0})
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video-bytes")
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.host + req.url.path))
        if req.url.path == "/token":
            assert b"grant_type=refresh_token" in req.content
            return httpx.Response(200, json={"access_token": "NEW", "expires_in": 3600})
        if req.method == "POST" and "upload/youtube" in str(req.url):
            assert req.headers["Authorization"] == "Bearer NEW"
            meta = json.loads(req.content)
            assert meta["snippet"]["title"] == "Clutch #Shorts"
            assert meta["status"]["privacyStatus"] == "public"
            return httpx.Response(200, headers={"Location": "https://upload.example/session1"})
        if req.method == "PUT":
            assert req.content == b"video-bytes"
            return httpx.Response(200, json={"id": "abc123"})
        return httpx.Response(404)

    yt = YouTube("id", "secret", "http://localhost/cb", store, client=mock_client(handler))
    result = asyncio.run(yt.upload(video, "Clutch #Shorts", "desc", ["gaming"]))
    assert result == {"id": "abc123", "url": "https://youtube.com/shorts/abc123"}
    assert store.load("youtube")["access_token"] == "NEW"
    assert [c[0] for c in calls] == ["POST", "POST", "PUT"]


def test_youtube_quota_error(tmp_path):
    store = TokenStore(tmp_path)
    store.save("youtube", {"refresh_token": "R", "access_token": "A", "expires_at": time.time() + 999})
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    def handler(req):
        return httpx.Response(403, json={"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})

    yt = YouTube("id", "s", "cb", store, client=mock_client(handler))
    with pytest.raises(QuotaError):
        asyncio.run(yt.upload(video, "t", "d", []))


def test_youtube_auth_url():
    yt = YouTube("my-id", "s", "http://localhost:8000/auth/youtube/callback", TokenStore("/tmp/x"))
    url = yt.auth_url("STATE")
    assert "access_type=offline" in url and "youtube.upload" in url and "state=STATE" in url


# ---------------------------------------------------------------- tiktok
def test_tiktok_chunk_plan():
    assert chunk_plan(3 * MB) == (3 * MB, 1)
    assert chunk_plan(64 * MB) == (64 * MB, 1)
    assert chunk_plan(105 * MB) == (10 * MB, 10)  # last chunk carries the extra 5 MB


def test_tiktok_direct_post_falls_back_to_private(tmp_path):
    store = TokenStore(tmp_path)
    store.save("tiktok", {"access_token": "T", "refresh_token": "R", "expires_at": time.time() + 999})
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0123456789")
    seen = {}
    polls = iter(["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"])

    def handler(req: httpx.Request):
        path = req.url.path
        if path.endswith("/creator_info/query/"):
            return httpx.Response(200, json={"data": {"privacy_level_options": ["SELF_ONLY"]},
                                             "error": {"code": "ok"}})
        if path.endswith("/video/init/"):
            seen["init"] = json.loads(req.content)
            return httpx.Response(200, json={"data": {"publish_id": "P1", "upload_url": "https://up.example/u"},
                                             "error": {"code": "ok"}})
        if req.method == "PUT":
            seen["range"] = req.headers["Content-Range"]
            return httpx.Response(201)
        if path.endswith("/status/fetch/"):
            status = next(polls)
            data = {"status": status}
            if status == "PUBLISH_COMPLETE":
                data["publicaly_available_post_id"] = [777]
            return httpx.Response(200, json={"data": data, "error": {"code": "ok"}})
        return httpx.Response(404)

    tt = TikTok("key", "secret", "cb", store, client=mock_client(handler), poll_seconds=0)
    result = asyncio.run(tt.upload(video, "Clutch #gaming"))
    assert seen["init"]["post_info"]["privacy_level"] == "SELF_ONLY"  # unaudited app
    assert seen["init"]["source_info"] == {"source": "FILE_UPLOAD", "video_size": 10,
                                           "chunk_size": 10, "total_chunk_count": 1}
    assert seen["range"] == "bytes 0-9/10"
    assert result["id"] == "777" and result["privacy"] == "SELF_ONLY"


# ---------------------------------------------------------------- captions
def test_caption_chunks():
    words = [[0, .3, "no"], [.3, .6, "way"], [.6, .9, "that"], [.9, 1.2, "just"], [3.0, 3.5, "happened"]]
    assert caption_chunks(words) == [(0, .9, "no way that"), (.9, 1.2, "just"), (3.0, 3.5, "happened")]


# ---------------------------------------------------------------- publisher
class FakePlatform:
    configured = connected = True
    privacy = "public"
    redirect_uri = ""

    def __init__(self):
        self.uploads = []

    async def upload(self, video, *args):
        assert video.exists()
        self.uploads.append((video.name, args))
        return {"id": f"id{len(self.uploads)}", "url": f"https://example/{len(self.uploads)}"}


def make_clip(cid, **kw):
    base = dict(id=cid, stream_id="s", channel="chan", title="Big clutch", reason="auto", detail="",
                created_at=time.time(), pre=30, post=5, status="ready", file=f"{cid}.mp4",
                verdict="approved", duration=35,
                meta={"classification": {"title": "Big clutch", "keep": True}})
    base.update(kw)
    return Clip(**base)


def publisher(tmp_path, **kw):
    s = Settings(data_dir=tmp_path, vertical=False, post_interval_minutes=0, **kw)
    lib = ClipLibrary(s.clips_dir, s.min_clip_seconds)
    yt, tt = FakePlatform(), FakePlatform()
    return Publisher(s, lib, yt, tt), lib, yt, tt


def test_publisher_posts_to_both(tmp_path):
    pub, lib, yt, tt = publisher(tmp_path)
    clip = make_clip("c1")
    (lib.dir / clip.file).write_bytes(b"x")
    lib.clips[clip.id] = clip
    asyncio.run(pub.process(clip))
    assert clip.posts["youtube"]["status"] == "posted" and clip.posts["tiktok"]["status"] == "posted"
    title, desc, tags = yt.uploads[0][1]
    assert title == "Big clutch #Shorts" and "#gaming" in desc and "chan" in tags
    assert tt.uploads[0][1][0].startswith("Big clutch #shorts")


def test_publisher_waits_for_claude_when_score_only(tmp_path):
    pub, lib, yt, _ = publisher(tmp_path)
    clip = make_clip("c2", meta={})  # approved by score only
    lib.clips[clip.id] = clip
    asyncio.run(pub.process(clip))
    assert clip.posts["youtube"]["status"] == "waiting" and not yt.uploads
    assert "Claude" in clip.posts["youtube"]["error"]


def test_publisher_daily_limit_schedules(tmp_path):
    async def run():
        pub, lib, yt, _ = publisher(tmp_path, youtube_daily_limit=1, tiktok_daily_limit=1)
        done = make_clip("old", posts={"youtube": {"status": "posted", "posted_at": time.time() - 60},
                                       "tiktok": {"status": "posted", "posted_at": time.time() - 60}})
        new = make_clip("new")
        (lib.dir / new.file).write_bytes(b"x")
        lib.clips.update({done.id: done, new.id: new})
        await pub.process(new)
        assert new.posts["youtube"]["status"] == "scheduled"
        assert new.posts["youtube"]["next_try"] > time.time() + 86000
        assert not yt.uploads
        await pub.stop()
    asyncio.run(run())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_publisher_renders_vertical(tmp_path):
    s = Settings(data_dir=tmp_path, post_interval_minutes=0)
    lib = ClipLibrary(s.clips_dir)
    src = lib.dir / "c3.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                    "-f", "lavfi", "-i", "sine=f=440", "-t", "2", "-c:v", "libx264", "-c:a", "aac",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    yt, tt = FakePlatform(), FakePlatform()
    pub = Publisher(s, lib, yt, tt)
    clip = make_clip("c3", duration=40, meta={"classification": {"title": "t"}, "words": [[0, .5, "hello"]]})
    lib.clips[clip.id] = clip
    asyncio.run(pub.process(clip))
    assert clip.vertical == "c3_vertical.mp4" and yt.uploads[0][0] == "c3_vertical.mp4"
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(lib.dir / clip.vertical)],
                           capture_output=True, text=True).stdout.strip()
    assert probe == "1080,1920"


def test_oauth_routes(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from fastapi.testclient import TestClient

    from clipper.app import create_app

    s = Settings(data_dir=tmp_path, youtube_client_id="cid", youtube_client_secret="sec")
    app = create_app(s)

    async def fake_exchange(self, code):
        assert code == "CODE"
        self.store.save("youtube", {"refresh_token": "R"})

    monkeypatch.setattr(YouTube, "exchange_code", fake_exchange)
    with TestClient(app) as client:
        assert client.get("/auth/tiktok", follow_redirects=False).status_code == 400  # not configured
        r = client.get("/auth/youtube", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"].startswith("https://accounts.google.com/")
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        assert client.get("/auth/youtube/callback?state=bad&code=CODE").status_code == 400
        r = client.get(f"/auth/youtube/callback?state={state}&code=CODE", follow_redirects=False)
        assert r.status_code == 307
        info = client.get("/api/platforms").json()
        assert info["youtube"]["connected"] and info["youtube"]["active"]
        assert not info["tiktok"]["configured"]
        client.delete("/api/platforms/youtube")
        assert not client.get("/api/platforms").json()["youtube"]["connected"]


def test_youtube_refreshes_on_401(tmp_path):
    store = TokenStore(tmp_path)
    store.save("youtube", {"refresh_token": "R", "access_token": "stale", "expires_at": time.time() + 999})
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    def handler(req):
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        if req.method == "POST":
            if req.headers["Authorization"] == "Bearer stale":
                return httpx.Response(401, json={"error": {"message": "invalid"}})
            return httpx.Response(200, headers={"Location": "https://up.example/s"})
        return httpx.Response(200, json={"id": "v1"})

    yt = YouTube("id", "s", "cb", store, client=mock_client(handler))
    assert asyncio.run(yt.upload(video, "t", "d", []))["id"] == "v1"
