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
import time
from urllib.parse import parse_qs, urlparse

import httpx

from ._util import BROWSER_UA, deliver

log = logging.getLogger(__name__)

_VIDEO_ID = re.compile(r"^[\w-]{11}$")


def youtube_target(url: str) -> str | None:
    """Return a video id, or the channel path (e.g. '@name') for /live URLs; None if not YouTube."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return None
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
    # Prefer the unfiltered "Live chat" view over the default "Top chat" (which hides messages).
    continuation = _between(
        html, r'"title"\s*:\s*"Live chat"\s*,\s*"selected"\s*:\s*(?:true|false)\s*,\s*"continuation"\s*:\s*'
              r'\{\s*"reloadContinuationData"\s*:\s*\{\s*"continuation"\s*:\s*"([^"]+)"'
    ) or _between(html, r'"continuation"\s*:\s*"([^"]+)"')
    if not (key and version and continuation):
        raise RuntimeError("live chat not available (stream offline, chat disabled, or page changed)")
    return {"key": key, "version": version, "continuation": continuation}


# Support events that count as hype even though they carry no chat text.
_SUPPORT_ITEMS = {
    "liveChatMembershipItemRenderer": "new member hype",
    "giftMessageViewModel": "gifted membership hype",
    "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer": "gifted memberships hype",
    "liveChatPaidStickerRenderer": "super sticker hype",
}


def _runs_text(runs: list) -> str:
    return "".join(r.get("text") or (r.get("emoji", {}).get("shortcuts") or [""])[0] for r in runs)


def parse_chat_response(data: dict) -> tuple[list[dict], str | None, float]:
    """Return (items, next_continuation, wait_seconds) from a get_live_chat response.

    Each item: {"id", "ts" (unix seconds or None), "user", "text"}.
    """
    lcc = data.get("continuationContents", {}).get("liveChatContinuation", {})
    items: list[dict] = []
    for action in lcc.get("actions", []):
        item = action.get("addChatItemAction", {}).get("item", {})
        for kind, r in item.items():
            ts = int(r["timestampUsec"]) / 1e6 if r.get("timestampUsec") else None
            user = r.get("authorName", {}).get("simpleText", "")
            if kind in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer"):
                text = _runs_text(r.get("message", {}).get("runs", []))
                if kind == "liveChatPaidMessageRenderer":
                    text = (text + " super chat hype").strip()
            elif kind in _SUPPORT_ITEMS:
                text = _SUPPORT_ITEMS[kind]
            else:
                continue
            items.append({"id": r.get("id") or "", "ts": ts, "user": user, "text": text})
    nxt, wait = None, 5.0
    for c in lcc.get("continuations", []):
        body = c.get("invalidationContinuationData") or c.get("timedContinuationData") or \
            c.get("reloadContinuationData")
        if body and body.get("continuation"):
            nxt = body["continuation"]
            wait = body.get("timeoutMs", 5000) / 1000
            break
    return items, nxt, min(max(wait, 1.0), 10.0)


async def resolve_video_id(client: httpx.AsyncClient, url: str) -> str:
    target = youtube_target(url)
    if target and _VIDEO_ID.match(target):
        return target
    page = (await client.get(f"https://www.youtube.com/{target}/live")).text
    if '"isLive":true' not in page:
        raise RuntimeError(f"{target} is not live right now")
    # The video this page is showing; other video ids on the page are recommendations.
    i = page.find('"currentVideoEndpoint"')
    vid = _between(page[i:i + 2000], r'"videoId"\s*:\s*"([\w-]{11})"') if i >= 0 else None
    vid = vid or _between(page, r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"')
    if not vid:
        raise RuntimeError(f"could not find the live video for {target}")
    return vid


async def resolve_watch_url(url: str) -> str:
    """Turn a channel /live URL into the current live video's watch URL (others unchanged)."""
    target = youtube_target(url)
    if not target or _VIDEO_ID.match(target):
        return url
    async with httpx.AsyncClient(headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US"},
                                 cookies={"CONSENT": "YES+1"}, timeout=20, follow_redirects=True) as c:
        return f"https://www.youtube.com/watch?v={await resolve_video_id(c, url)}"


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
                on_status(f"connected ({vid})")
                backoff = 5.0
                continuation = cfg["continuation"]
                # The first response replays up to hours of old chat; only count recent messages.
                cutoff = time.time() - 15
                seen: set[str] = set()
                first_batch = True
                while continuation:
                    resp = await client.post(
                        "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat",
                        params={"key": cfg["key"], "prettyPrint": "false"},
                        json={"context": {"client": {"clientName": "WEB", "clientVersion": cfg["version"]}},
                              "continuation": continuation},
                    )
                    resp.raise_for_status()
                    items, continuation, wait = parse_chat_response(resp.json())
                    if first_batch:
                        # History replayed on connect (gifts and memberships have no timestamp).
                        seen.update(it["id"] for it in items if it["id"])
                        first_batch = False
                        await asyncio.sleep(wait)
                        continue
                    for it in items:
                        if it["id"] and it["id"] in seen:
                            continue
                        if it["id"]:
                            seen.add(it["id"])
                        if it["ts"] is not None and it["ts"] < cutoff:
                            continue
                        await deliver(on_message, it["user"], it["text"])
                    if len(seen) > 5000:
                        seen = set(list(seen)[-2000:])
                    await asyncio.sleep(wait)
                raise RuntimeError("chat ended")
            except asyncio.CancelledError:
                raise
            except (Exception, json.JSONDecodeError) as exc:  # noqa: BLE001
                log.warning("youtube chat %s: %s; retrying in %.0fs", url, exc, backoff)
                on_status(f"disconnected: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
