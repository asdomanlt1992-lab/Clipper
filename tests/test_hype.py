from clipper.hype import HypeDetector


def make(**kw):
    defaults = dict(window=10, baseline=120, multiplier=3, min_messages=10, cooldown=30,
                    warmup=30, keywords=["pog", "no way"])
    defaults.update(kw)
    return HypeDetector(**defaults)


def steady(d, start, end, per_sec=1.0, text="hello"):
    t = start
    step = 1.0 / per_sec
    while t < end:
        d.feed(t, text)
        t += step


def test_no_trigger_on_steady_chat():
    d = make()
    for t in range(200):
        steady(d, t, t + 1, per_sec=2)
        assert d.check(t + 1) is None


def test_triggers_on_spike_then_cools_down():
    d = make()
    steady(d, 0, 100, per_sec=1)
    steady(d, 100, 110, per_sec=8, text="POG")
    ev = d.check(110)
    assert ev is not None and ev.score >= 3
    assert "pog" in ev.top_words
    steady(d, 110, 115, per_sec=8)
    assert d.check(115) is None  # cooldown


def test_warmup_blocks_early_trigger():
    d = make()
    steady(d, 0, 10, per_sec=10)
    assert d.check(10) is None


def test_min_messages_on_dead_chat():
    d = make()
    steady(d, 0, 100, per_sec=0.05)
    for t in (100, 101, 102, 103, 104):
        d.feed(t, "hi")
    assert d.check(105) is None


def test_keyword_weight():
    d = make()
    assert d.weight("that was POG") == 2
    assert d.weight("NO WAY dude") == 2
    assert d.weight("pogger") == 1
    assert d.weight("hello") == 1
