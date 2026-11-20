"""YouTube Live chat reader.

Uses the same internal endpoint the live chat popout uses (no API key or quota needed):
  1. resolve the URL to a live video id
  2. load /live_chat?v=<id> for the API key, client version and first continuation token
  3. poll youtubei/v1/live_chat/get_live_chat, following continuation tokens
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx

from ._util import BROWSER_UA, deliver

log = logging.getLogger(__name__)

_VIDEO_ID = re.compile(r"^[\w-]{11}$")


def youtube_target(url: str) -> str | None:
    """Return a video id, or the channel path (e.g. '@name') for /live URLs; None if not YouTube."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in parsed.path.split("/") if p]
    if host == "youtu.be":
        return parts[0] if parts and _VIDEO_ID.match(parts[0]) else None
    if host not in ("youtube.com", "music.youtube.com"):
        return None
    if parsed.path == "/watch":
        v = parse_qs(parsed.query).get("v", [""])[0]
        return v if _VIDEO_ID.match(v) else None
    if len(parts) >= 2 and parts[0] in ("live", "shorts", "embed") and _VIDEO_ID.match(parts[1]):
        return parts[1]
    if parts and (parts[0].startswith("@") or parts[0] in ("channel", "c", "user")):
        name = "/".join(parts[:2] if not parts[0].startswith("@") else parts[:1])
        return name
    return None


def _between(html: str, pattern: str) -> str | None:
    m = re.search(pattern, html)
    return m.group(1) if m else None


def extract_chat_config(html: str) -> dict:
    """Pull api key, client version and the first continuation token from a live_chat page."""
    key = _between(html, r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"')
    version = _between(html, r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"') or \
        _between(html, r'"clientVersion"\s*:\s*"([^"]+)"')
    continuation = _between(html, r'"continuation"\s*:\s*"([^"]+)"')
    if not (key and version and continuation):
        raise RuntimeError("live chat not available (stream offline, chat disabled, or page changed)")
    return {"key": key, "version": version, "continuation": continuation}


def parse_chat_response(data: dict) -> tuple[list[tuple[str, str]], str | None, float]:
    """Return (messages, next_continuation, wait_seconds) from a get_live_chat response."""
    lcc = data.get("continuationContents", {}).get("liveChatContinuation", {})
    messages: list[tuple[str, str]] = []
    for action in lcc.get("actions", []):
        item = action.get("addChatItemAction", {}).get("item", {})
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r:
            continue
        runs = r.get("message", {}).get("runs", [])
        text = "".join(
            run.get("text") or (run.get("emoji", {}).get("shortcuts") or [""])[0] for run in runs
        )
        user = r.get("authorName", {}).get("simpleText", "")
        messages.append((user, text))
    nxt, wait = None, 5.0
    for c in lcc.get("continuations", []):
        body = c.get("invalidationContinuationData") or c.get("timedContinuationData") or \
            c.get("reloadContinuationData")
        if body and body.get("continuation"):
            nxt = body["continuation"]
            wait = body.get("timeoutMs", 5000) / 1000
            break
    return messages, nxt, min(max(wait, 1.0), 10.0)


async def resolve_video_id(client: httpx.AsyncClient, url: str) -> str:
    target = youtube_target(url)
    if target and _VIDEO_ID.match(target):
        return target
    page = await client.get(f"https://www.youtube.com/{target}/live")
    vid = _between(page.text, r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"') \
        or _between(page.text, r'"videoId"\s*:\s*"([\w-]{11})"')
    if not vid:
        raise RuntimeError(f"no live video found for {target}")
    return vid


async def run_youtube_chat(url: str, on_message, on_status=lambda s: None) -> None:
    backoff = 5.0
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    cookies = {"CONSENT": "YES+1"}  # skip the EU consent interstitial
    async with httpx.AsyncClient(headers=headers, cookies=cookies, timeout=20,
                                 follow_redirects=True) as client:
        while True:
            try:
                on_status("connecting")
                vid = await resolve_video_id(client, url)
                page = await client.get("https://www.youtube.com/live_chat",
                                        params={"is_popout": "1", "v": vid})
                cfg = extract_chat_config(page.text)
                on_status("connected")
                backoff = 5.0
                continuation = cfg["continuation"]
                while continuation:
                    resp = await client.post(
                        "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat",
                        params={"key": cfg["key"], "prettyPrint": "false"},
                        json={"context": {"client": {"clientName": "WEB", "clientVersion": cfg["version"]}},
                              "continuation": continuation},
                    )
                    resp.raise_for_status()
                    messages, continuation, wait = parse_chat_response(resp.json())
                    for user, text in messages:
                        await deliver(on_message, user, text)
                    await asyncio.sleep(wait)
                raise RuntimeError("chat ended")
            except asyncio.CancelledError:
                raise
            except (Exception, json.JSONDecodeError) as exc:  # noqa: BLE001
                log.warning("youtube chat %s: %s; retrying in %.0fs", url, exc, backoff)
                on_status(f"disconnected: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
