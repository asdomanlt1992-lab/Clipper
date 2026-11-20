import json

from clipper.chat import chat_source_for
from clipper.chat.kick import kick_channel, parse_pusher_message
from clipper.chat.youtube import extract_chat_config, parse_chat_response, youtube_target


def test_chat_source_detection():
    assert chat_source_for("https://www.twitch.tv/foo").platform == "twitch"
    assert chat_source_for("https://www.youtube.com/watch?v=dQw4w9WgXcQ").platform == "youtube"
    assert chat_source_for("https://kick.com/somebody").platform == "kick"
    assert chat_source_for("https://example.com/stream") is None
    assert chat_source_for("lavfi:testsrc") is None


def test_youtube_target():
    assert youtube_target("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3") == "dQw4w9WgXcQ"
    assert youtube_target("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube_target("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube_target("https://www.youtube.com/@SomeCreator/live") == "@SomeCreator"
    assert youtube_target("https://www.youtube.com/channel/UCabc/live") == "channel/UCabc"
    assert youtube_target("https://vimeo.com/123") is None


def test_youtube_extract_config():
    html = ('<script>ytcfg.set({"INNERTUBE_API_KEY":"AIzaKEY","INNERTUBE_CLIENT_VERSION":"2.20261101.00.00"});'
            'window["ytInitialData"] = {"contents":{"liveChatRenderer":{"continuations":'
            '[{"invalidationContinuationData":{"continuation":"TOKEN1","timeoutMs":10000}}]}}};</script>')
    assert extract_chat_config(html) == {"key": "AIzaKEY", "version": "2.20261101.00.00", "continuation": "TOKEN1"}


def test_youtube_parse_response():
    data = {"continuationContents": {"liveChatContinuation": {
        "continuations": [{"timedContinuationData": {"continuation": "TOKEN2", "timeoutMs": 3500}}],
        "actions": [
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                "authorName": {"simpleText": "alice"},
                "message": {"runs": [{"text": "no way "}, {"emoji": {"shortcuts": [":fire:"]}}]}}}}},
            {"addChatItemAction": {"item": {"liveChatViewerEngagementMessageRenderer": {}}}},
            {"markChatItemAsDeletedAction": {}},
        ]}}}
    msgs, nxt, wait = parse_chat_response(data)
    assert msgs == [("alice", "no way :fire:")]
    assert nxt == "TOKEN2" and wait == 3.5


def test_kick_channel():
    assert kick_channel("https://kick.com/SomeStreamer") == "somestreamer"
    assert kick_channel("kick.com/categories/just-chatting") is None
    assert kick_channel("https://twitch.tv/x") is None


def test_kick_parse_pusher():
    frame = {"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.1.v2",
             "data": json.dumps({"id": "x", "content": "KEKW", "sender": {"username": "bob"}})}
    assert parse_pusher_message(json.dumps(frame)) == ("bob", "KEKW")
    assert parse_pusher_message('{"event":"pusher:connection_established","data":"{}"}') is None
    assert parse_pusher_message("not json") is None
