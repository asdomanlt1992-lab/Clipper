"""TikTok upload via the Content Posting API (Direct Post, FILE_UPLOAD)."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .tokens import TokenStore, expires_at
from .youtube import QuotaError

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.publish"
PLATFORM = "tiktok"
MB = 1024 * 1024


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunks) per TikTok's rules: single chunk under 64 MB, else 10 MB chunks
    with the remainder folded into the last one."""
    if size <= 64 * MB:
        return size, 1
    chunk = 10 * MB
    return chunk, size // chunk


class TikTok:
    def __init__(self, client_key: str, client_secret: str, redirect_uri: str, store: TokenStore,
                 privacy: str = "PUBLIC_TO_EVERYONE", client: httpx.AsyncClient | None = None,
                 poll_seconds: float = 5.0):
        self.client_key, self.client_secret = client_key, client_secret
        self.redirect_uri = redirect_uri
        self.store = store
        self.privacy = privacy
        self._client = client
        self.poll_seconds = poll_seconds

    @property
    def configured(self) -> bool:
        return bool(self.client_key and self.client_secret)

    @property
    def connected(self) -> bool:
        tok = self.store.load(PLATFORM)
        return bool(tok and tok.get("refresh_token"))

    def auth_url(self, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_key": self.client_key, "scope": SCOPES, "response_type": "code",
            "redirect_uri": self.redirect_uri, "state": state})

    def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=httpx.Timeout(60, read=600))

    async def _token_request(self, http: httpx.AsyncClient, **form) -> dict:
        r = await http.post(f"{API}/oauth/token/", data={
            "client_key": self.client_key, "client_secret": self.client_secret, **form},
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        data = r.json() if r.content else {}
        if not r.is_success or "access_token" not in data:
            raise RuntimeError(f"TikTok sign-in failed: {data.get('error_description') or data or r.status_code}")
        return data

    async def exchange_code(self, code: str) -> None:
        async with self._http() as http:
            data = await self._token_request(http, code=code, grant_type="authorization_code",
                                             redirect_uri=self.redirect_uri)
        self._save(data)

    def _save(self, data: dict) -> None:
        self.store.save(PLATFORM, {
            "access_token": data["access_token"], "refresh_token": data["refresh_token"],
            "open_id": data.get("open_id", ""), "expires_at": expires_at(data.get("expires_in"))})

    async def _access_token(self, http: httpx.AsyncClient) -> str:
        tok = self.store.load(PLATFORM)
        if not tok or not tok.get("refresh_token"):
            raise RuntimeError("TikTok is not connected")
        if tok.get("expires_at", 0) > time.time():
            return tok["access_token"]
        try:
            data = await self._token_request(http, grant_type="refresh_token",
                                             refresh_token=tok["refresh_token"])
        except RuntimeError as exc:
            raise RuntimeError(f"TikTok sign-in expired or was revoked; connect again ({exc})") from exc
        self._save(data)
        return data["access_token"]

    async def _api(self, http: httpx.AsyncClient, token: str, path: str, body: dict) -> dict:
        r = await http.post(f"{API}{path}", json=body, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
        data = r.json() if r.content else {}
        err = data.get("error", {})
        if err.get("code") not in (None, "", "ok"):
            msg = f"TikTok {path} failed: {err.get('code')}: {err.get('message', '')}"
            if err.get("code") in ("spam_risk_too_many_posts", "rate_limit_exceeded"):
                raise QuotaError(msg)
            raise RuntimeError(msg)
        r.raise_for_status()
        return data.get("data", {})

    async def upload(self, video: Path, caption: str) -> dict:
        size = video.stat().st_size
        chunk, total = chunk_plan(size)
        async with self._http() as http:
            token = await self._access_token(http)
            creator = await self._api(http, token, "/post/publish/creator_info/query/", {})
            options = creator.get("privacy_level_options") or []
            privacy = self.privacy if self.privacy in options else (
                "SELF_ONLY" if "SELF_ONLY" in options or not options else options[0])
            init = await self._api(http, token, "/post/publish/video/init/", {
                "post_info": {"title": caption[:2200], "privacy_level": privacy,
                              "disable_duet": False, "disable_comment": False,
                              "disable_stitch": False, "video_cover_timestamp_ms": 1000},
                "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                "chunk_size": chunk, "total_chunk_count": total},
            })
            publish_id, upload_url = init["publish_id"], init["upload_url"]
            with video.open("rb") as f:
                for i in range(total):
                    start = i * chunk
                    end = size - 1 if i == total - 1 else start + chunk - 1
                    f.seek(start)
                    body = f.read(end - start + 1)
                    r = await http.put(upload_url, content=body, headers={
                        "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                        "Content-Range": f"bytes {start}-{end}/{size}"})
                    if r.status_code not in (200, 201, 206):
                        raise RuntimeError(f"TikTok chunk {i + 1}/{total} upload failed ({r.status_code})")
            # Wait for TikTok to process it.
            for _ in range(120):
                st = await self._api(http, token, "/post/publish/status/fetch/", {"publish_id": publish_id})
                status = st.get("status", "")
                if status == "PUBLISH_COMPLETE":
                    ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
                    return {"id": str(ids[0]) if ids else publish_id, "privacy": privacy,
                            "url": f"https://www.tiktok.com/video/{ids[0]}" if ids else ""}
                if status == "FAILED":
                    raise RuntimeError(f"TikTok rejected the video: {st.get('fail_reason', 'unknown')}")
                if status == "SEND_TO_USER_INBOX":
                    return {"id": publish_id, "privacy": privacy, "url": ""}
                await asyncio.sleep(self.poll_seconds)
            raise RuntimeError("TikTok is still processing the video after 10 minutes")
