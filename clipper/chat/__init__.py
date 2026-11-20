"""Live chat readers per platform. Each one feeds (user, text) into the hype detector."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from . import kick, twitch, youtube
from .twitch import parse_privmsg, twitch_channel  # noqa: F401  (re-exported)

OnMessage = Callable[[str, str], Awaitable[None] | None]
OnStatus = Callable[[str], None]


@dataclass
class ChatSource:
    platform: str  # "twitch" | "youtube" | "kick"
    channel: str   # display name / id used to connect
    run: Callable[[OnMessage, OnStatus], Awaitable[None]]


def chat_source_for(url: str) -> ChatSource | None:
    """Pick a chat reader for a stream URL, or None if the site has no chat support."""
    if ch := twitch.twitch_channel(url):
        return ChatSource("twitch", ch, lambda m, s: twitch.run_twitch_chat(ch, m, s))
    if target := youtube.youtube_target(url):
        return ChatSource("youtube", target, lambda m, s: youtube.run_youtube_chat(url, m, s))
    if slug := kick.kick_channel(url):
        return ChatSource("kick", slug, lambda m, s: kick.run_kick_chat(slug, m, s))
    return None
