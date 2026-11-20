"""YouTube Shorts upload via the YouTube Data API v3 (OAuth + resumable upload)."""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .tokens import TokenStore, expires_at

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
PLATFORM = "youtube"


class YouTube:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, store: TokenStore,
                 privacy: str = "public", category_id: str = "20",
                 client: httpx.AsyncClient | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self.redirect_uri = redirect_uri
        self.store = store
        self.privacy = privacy
        self.category_id = category_id
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def connected(self) -> bool:
        tok = self.store.load(PLATFORM)
        return bool(tok and tok.get("refresh_token"))

    def auth_url(self, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": SCOPE, "access_type": "offline",
            "prompt": "consent", "include_granted_scopes": "true", "state": state,
        })

    def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=httpx.Timeout(60, read=600))

    async def exchange_code(self, code: str) -> None:
        async with self._http() as http:
            r = await http.post(TOKEN_URL, data={
                "code": code, "client_id": self.client_id, "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri, "grant_type": "authorization_code"})
            r.raise_for_status()
            data = r.json()
        if not data.get("refresh_token"):
            raise RuntimeError("Google did not return a refresh token; remove the app's access "
                               "in your Google account settings and connect again")
        self.store.save(PLATFORM, {"refresh_token": data["refresh_token"],
                                   "access_token": data["access_token"],
                                   "expires_at": expires_at(data.get("expires_in"))})

    async def _access_token(self, http: httpx.AsyncClient) -> str:
        tok = self.store.load(PLATFORM)
        if not tok or not tok.get("refresh_token"):
            raise RuntimeError("YouTube is not connected")
        if tok.get("access_token") and tok.get("expires_at", 0) > time.time():
            return tok["access_token"]
        r = await http.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        if r.status_code in (400, 401):
            raise RuntimeError("YouTube sign-in expired or was revoked; connect again")
        r.raise_for_status()
        data = r.json()
        tok.update(access_token=data["access_token"], expires_at=expires_at(data.get("expires_in")))
        self.store.save(PLATFORM, tok)
        return tok["access_token"]

    def _expire_access_token(self) -> None:
        tok = self.store.load(PLATFORM)
        if tok:
            tok["expires_at"] = 0
            self.store.save(PLATFORM, tok)

    async def upload(self, video: Path, title: str, description: str, tags: list[str]) -> dict:
        size = video.stat().st_size
        meta = {
            "snippet": {"title": title[:100], "description": description[:5000],
                        "tags": tags[:30], "categoryId": self.category_id},
            "status": {"privacyStatus": self.privacy, "selfDeclaredMadeForKids": False},
        }
        async with self._http() as http:
            for attempt in range(2):
                token = await self._access_token(http)
                r = await http.post(
                    UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                    headers={"Authorization": f"Bearer {token}",
                             "X-Upload-Content-Type": "video/mp4",
                             "X-Upload-Content-Length": str(size)},
                    json=meta)
                if r.status_code == 401 and attempt == 0:
                    self._expire_access_token()  # token revoked or expired early: refresh once
                    continue
                break
            _raise(r, "start upload")
            location = r.headers["Location"]
            r = await http.put(location, content=video.read_bytes(),
                               headers={"Authorization": f"Bearer {token}",
                                        "Content-Type": "video/mp4"})
            _raise(r, "upload video")
            vid = r.json()["id"]
        return {"id": vid, "url": f"https://youtube.com/shorts/{vid}"}


def _raise(r: httpx.Response, step: str) -> None:
    if r.is_success:
        return
    try:
        err = r.json()["error"]
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        msg = err.get("message", r.text)
    except Exception:  # noqa: BLE001
        reason, msg = "", r.text[:300]
    if reason in ("quotaExceeded", "uploadLimitExceeded"):
        raise QuotaError(f"YouTube {reason}: {msg}")
    raise RuntimeError(f"YouTube {step} failed ({r.status_code}): {msg}")


class QuotaError(RuntimeError):
    """Daily limit hit; retry tomorrow."""
