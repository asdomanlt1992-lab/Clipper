import time

from clipper.analysis import Analysis, build_user_content, decide
from clipper.clips import Clip, ClipLibrary
from clipper.config import Settings
from clipper.hype import HypeDetector
from clipper.scoring import score_moment

S = Settings(review_uncertain=False)


def test_score_formula():
    assert score_moment(3, 3, 0, 0)["score"] == 37.5       # at threshold, neutral, quiet
    assert score_moment(3, 3, -1, 0)["score"] == 25         # negative chat
    assert score_moment(6, 3, 1, 12)["score"] == 100        # big, hyped and loud
    assert score_moment(0, 3, 0, 24)["audio"] == 25         # audio capped


def test_mood():
    d = HypeDetector(window=10, keywords=["pog"], negative_keywords=["cringe", "calm down"])
    for t, msg in enumerate(["pog", "pog", "cringe", "hello", "CALM DOWN bro"]):
        d.feed(t, msg)
    assert d.mood(5) == 0  # 2 positive vs 2 negative
    d.feed(6, "POG")
    assert d.mood(6) > 0


def cls(**kw):
    base = {"category": "funny", "keep": True, "confidence": 0.9, "title": "t",
            "summary": "s", "reason": "r"}
    base.update(kw)
    return Analysis(classification=base)


def test_decide_with_classifier():
    assert decide("auto", 10, cls(), S)[0] == "approved"           # classifier overrides low score
    assert decide("auto", 90, cls(category="rage"), S)[0] == "rejected"
    assert decide("auto", 90, cls(keep=False), S)[0] == "rejected"
    assert decide("auto", 90, cls(refused=True), S)[0] == "rejected"
    assert decide("manual", 0, cls(category="rage"), S)[0] == "approved"
    review = Settings(review_uncertain=True)
    assert decide("auto", 90, cls(confidence=0.3), review)[0] == "review"


def test_decide_without_classifier():
    assert decide("auto", 55, Analysis(), S)[0] == "approved"
    assert decide("auto", 45, Analysis(), S)[0] == "rejected"
    assert decide("auto", 45, Analysis(), Settings(review_uncertain=True))[0] == "review"


def test_purge_rejected(tmp_path):
    lib = ClipLibrary(tmp_path)
    old = time.time() - 48 * 3600
    for cid, verdict in (("a", "rejected"), ("b", "approved")):
        (tmp_path / f"{cid}.mp4").write_bytes(b"x")
        lib.clips[cid] = Clip(id=cid, stream_id="s", channel="c", title="", reason="auto",
                              detail="", created_at=old, pre=1, post=1, status="ready",
                              file=f"{cid}.mp4", verdict=verdict)
    assert lib.purge_rejected(24) == 1
    assert not (tmp_path / "a.mp4").exists() and (tmp_path / "b.mp4").exists()
    assert lib.clips["a"].purged
    try:
        lib.set_verdict("a", "approved")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_build_user_content():
    content = build_user_content({"channel": "x", "chat": ["[0.0s] a: hi"], "duration": 30},
                                 "[00:01] hello", [b"jpg1", b"jpg2"])
    assert [b["type"] for b in content] == ["text", "image", "text", "image", "text"]
    assert "hello" in content[-1]["text"] and "a: hi" in content[-1]["text"]
