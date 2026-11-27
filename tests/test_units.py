from pathlib import Path

from clipper.chat import parse_privmsg, twitch_channel
from clipper.recorder import Segment, build_source_cmds, select_segments


def test_twitch_channel():
    assert twitch_channel("https://www.twitch.tv/SomeOne") == "someone"
    assert twitch_channel("twitch.tv/foo?x=1") == "foo"
    assert twitch_channel("https://twitch.tv/videos/123") is None
    assert twitch_channel("https://youtube.com/@x/live") is None


def test_parse_privmsg():
    line = "@badge-info=;color=#FF0000 :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :hello : world"
    assert parse_privmsg(line) == ("bob", "hello : world")
    assert parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome") is None


def test_select_segments():
    segs = [Segment(Path(f"s{i}"), i * 2.0, i * 2.0 + 2) for i in range(10)]
    picked = select_segments(segs, 5.0, 9.0)
    assert [s.path.name for s in picked] == ["s2", "s3", "s4"]


def test_source_cmds():
    sl, ff = build_source_cmds("https://twitch.tv/x", "720p")
    assert sl[1:3] == ["-m", "streamlink_cli"] and "720p" in sl and ff == ["-i", "pipe:0"]
    sl, ff = build_source_cmds("lavfi:testsrc", "best")
    assert sl is None and ff[-1] == "testsrc"
