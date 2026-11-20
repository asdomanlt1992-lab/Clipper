"""Kick chat reader (Kick's public Pusher websocket; no login needed)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import urlparse

import httpx
import websockets

from ._util import BROWSER_UA, deliver

log = logging.getLogger(__name__)

# Kick's public Pusher app key/cluster (the same one its web player uses). Overridable in case it changes.
PUSHER_KEY = os.environ.get("CLIPPER_KICK_PUSHER_KEY", "32cbd69e4b950bf97679")
PUSHER_CLUSTER = os.environ.get("CLIPPER_KICK_PUSHER_CLUSTER", "us2")
_RESERVED = {"categories", "browse", "following", "search", "video", "videos", "dashboard", "api"}


def kick_channel(url: str) -> str | None:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host != "kick.com":
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if not parts or parts[0].lower() in _RESERVED:
        return None
    return parts[0].lower()


def parse_pusher_message(raw: str) -> tuple[str, str] | None:
    """Return (user, text) for a chat event frame, else None."""
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if frame.get("event") != "App\\Events\\ChatMessageEvent":
        return None
    data = frame.get("data")
    if isinstance(data, str):  # Pusher double-encodes event data
        data = json.loads(data)
    sender = (data.get("sender") or {}).get("username", "")
    return sender, data.get("content", "")


async def get_chatroom_id(slug: str) -> int:
    override = os.environ.get(f"CLIPPER_KICK_CHATROOM_{slug.upper()}")
    if override:
        return int(override)
    async with httpx.AsyncClient(headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
                                 timeout=20) as client:
        resp = await client.get(f"https://kick.com/api/v2/channels/{slug}")
        if resp.status_code in (403, 429):
            raise RuntimeError(
                f"kick.com refused the channel lookup ({resp.status_code}); set "
                f"CLIPPER_KICK_CHATROOM_{slug.upper()}=<chatroom id> to skip it")
        resp.raise_for_status()
        return int(resp.json()["chatroom"]["id"])


async def run_kick_chat(slug: str, on_message, on_status=lambda s: None) -> None:
    backoff = 5.0
    url = (f"wss://ws-{PUSHER_CLUSTER}.pusher.com/app/{PUSHER_KEY}"
           "?protocol=7&client=js&version=8.4.0&flash=false")
    chatroom: int | None = None
    while True:
        try:
            on_status("connecting")
            if chatroom is None:
                chatroom = await get_chatroom_id(slug)
            async with websockets.connect(url, open_timeout=20, ping_interval=None) as ws:
                await ws.send(json.dumps({"event": "pusher:subscribe",
                                          "data": {"auth": "", "channel": f"chatrooms.{chatroom}.v2"}}))
                on_status("connected")
                backoff = 5.0
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=150)
                    if '"pusher:ping"' in raw:
                        await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                        continue
                    parsed = parse_pusher_message(raw)
                    if parsed:
                        await deliver(on_message, *parsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("kick chat %s: %s; retrying in %.0fs", slug, exc, backoff)
            on_status(f"disconnected: {exc}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
