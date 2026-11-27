"""Anonymous read-only Twitch chat client (IRC over Twitch's WebSocket endpoint).

WebSocket on port 443 goes through HTTPS proxies and strict firewalls, unlike raw IRC on 6667.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

import websockets

from ._util import deliver

log = logging.getLogger(__name__)

TWITCH_WS = "wss://irc-ws.chat.twitch.tv:443"


def twitch_channel(url: str) -> str | None:
    """Extract the channel name from a twitch.tv URL, or None if not Twitch."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if not (host == "twitch.tv" or host.endswith(".twitch.tv")):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if not parts or parts[0] in ("videos", "directory", "settings", "p"):
        return None
    return parts[0].lower()


def parse_privmsg(line: str) -> tuple[str, str] | None:
    """Return (user, message) for a PRIVMSG line, else None."""
    if line.startswith("@"):
        _, _, line = line.partition(" ")
    if not line.startswith(":") or " PRIVMSG " not in line:
        return None
    prefix, _, rest = line[1:].partition(" PRIVMSG ")
    _, _, msg = rest.partition(" :")
    return prefix.split("!", 1)[0], msg


async def run_twitch_chat(
    channel: str,
    on_message: Callable[[str, str], Awaitable[None] | None],
    on_status: Callable[[str], None] = lambda s: None,
) -> None:
    """Connect to a channel's chat and call on_message forever, reconnecting on errors."""
    backoff = 2.0
    while True:
        try:
            on_status("connecting")
            nick = f"justinfan{random.randint(10000, 99999)}"
            async with websockets.connect(TWITCH_WS, open_timeout=20, ping_interval=60) as ws:
                await ws.send("PASS SCHMOOPIIE")
                await ws.send(f"NICK {nick}")
                await ws.send(f"JOIN #{channel}")
                on_status("connected")
                backoff = 2.0
                while True:
                    frame = await asyncio.wait_for(ws.recv(), timeout=360)
                    if isinstance(frame, bytes):
                        frame = frame.decode("utf-8", "replace")
                    for line in frame.split("\r\n"):  # one frame can hold several IRC lines
                        if not line:
                            continue
                        if line.startswith("PING"):
                            await ws.send("PONG" + line[4:])
                            continue
                        if " RECONNECT" in line[:60]:
                            raise ConnectionError("Twitch asked us to reconnect")
                        parsed = parse_privmsg(line)
                        if parsed:
                            await deliver(on_message, *parsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("chat #%s: %s; reconnecting in %.0fs", channel, exc, backoff)
            on_status(f"disconnected: {exc}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
