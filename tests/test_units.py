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


def test_streamlink_uses_signed_in_cookies(tmp_path):
    from clipper.recorder import twitch_auth_token
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n"
                       ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tyt\n"
                       "#HttpOnly_.twitch.tv\tTRUE\t/\tTRUE\t0\tauth-token\tTOKEN123\n")
    assert twitch_auth_token(cookies) == "TOKEN123"
    sl, _ = build_source_cmds("https://twitch.tv/x", "best", cookies, "--hls-live-edge 2")
    assert sl[sl.index("--http-cookies-file") + 1] == str(cookies)
    assert "Authorization=OAuth TOKEN123" in sl and sl[-4:] == ["--hls-live-edge", "2", "https://twitch.tv/x", "best"]
    sl, _ = build_source_cmds("https://youtube.com/watch?v=abcdefghijk", "best", cookies)
    assert "--http-cookies-file" in sl and not any("OAuth" in a for a in sl)
    sl, _ = build_source_cmds("https://twitch.tv/x", "best", tmp_path / "missing.txt")
    assert "--http-cookies-file" not in sl


def test_cookies_upload_api(tmp_path):
    from fastapi.testclient import TestClient
    from clipper.app import create_app
    from clipper.config import Settings
    with TestClient(create_app(Settings(data_dir=tmp_path))) as c:
        assert c.get("/api/cookies").json() == {"present": False}
        assert c.put("/api/cookies", content="not cookies").status_code == 400
        body = ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n.twitch.tv\tTRUE\t/\tTRUE\t0\tauth-token\ty\n"
        assert c.put("/api/cookies", content=body).status_code == 204
        info = c.get("/api/cookies").json()
        assert info["present"] and info["sites"] == ["youtube.com", "twitch.tv"]
        assert oct((tmp_path / "secrets" / "cookies.txt").stat().st_mode)[-3:] == "600"
        c.delete("/api/cookies")
        assert not c.get("/api/cookies").json()["present"]
