"""Checks the maths before every run. If any check fails, nothing is published."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "watcher"))
import watch  # noqa: E402

# Wilder's RSI reference values (StockCharts' published 14-day example)
CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89,
          46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]


def close(a, b, tol):
    assert abs(a - b) <= tol, f"{a} != {b}"


def test_rsi_reference():
    # StockCharts' sheet rounds its averages, so allow 0.1 of difference
    ref = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97]
    for k, want in zip(range(15, 21), ref):
        close(watch.rsi(CLOSES[:k]), want, 0.1)


def test_rsi_edges():
    assert watch.rsi([1, 2, 3]) is None
    assert watch.rsi(list(range(1, 30))) == 100.0


def test_sma_and_pct():
    assert watch.sma([1, 2, 3, 4], 2) == 3.5
    assert watch.sma([1], 2) is None
    close(watch.pct(110, 100), 10.0, 1e-9)
    assert watch.pct(1, 0) is None


def test_tiers():
    assert [watch.tier(n) for n in range(8)] == [0, 0, 0, 1, 2, 2, 3, 3]


def test_validate_rejects_bad_data():
    now = int(time.time())
    good = [{"t": now - (40 - i) * 86400, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1} for i in range(40)]
    watch.validate(good, "X", 1440)
    for bad in (good[:5], list(reversed(good)), good[:-1] + [dict(good[-1], l=12)]):
        try:
            watch.validate(bad, "X", 1440)
        except RuntimeError:
            continue
        raise AssertionError("bad data was accepted")


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)
