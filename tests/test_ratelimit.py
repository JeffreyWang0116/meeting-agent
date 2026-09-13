"""速率限制：每位使用者、每種耗額度的操作各自有「每分鐘」與「每天」上限。

公開網址上沒有任何節流時，任何能登入的人都能連打上傳／問答，把 Gemini 每日額度
（免費層每專案 500 次）燒光；啟用 pyannote 後更是按處理時數直接計費。
"""
from datetime import date

import pytest

from app.ratelimit import DEFAULT_LIMITS, RateLimited, RateLimiter, parse_limits


class Clock:
    def __init__(self):
        self.now = 1_000_000.0
        self.day = date(2026, 9, 14)

    def time(self):
        return self.now

    def today(self):
        return self.day


def make(limits, clock=None):
    clock = clock or Clock()
    return RateLimiter(limits, clock=clock.time, today=clock.today), clock


def test_allows_up_to_the_per_minute_limit_then_rejects():
    limiter, _ = make({"media": (2, 0)})
    limiter.check("media", "u1")
    limiter.check("media", "u1")
    with pytest.raises(RateLimited) as exc:
        limiter.check("media", "u1")
    assert "每分鐘最多 2 次" in str(exc.value)
    assert 0 < exc.value.retry_after <= 60


def test_per_minute_window_slides():
    limiter, clock = make({"media": (1, 0)})
    limiter.check("media", "u1")
    clock.now += 61
    limiter.check("media", "u1")  # 一分鐘後又可以


def test_per_day_limit_resets_on_a_new_day():
    limiter, clock = make({"ask": (0, 2)})
    limiter.check("ask", "u1")
    clock.now += 3600
    limiter.check("ask", "u1")
    with pytest.raises(RateLimited) as exc:
        limiter.check("ask", "u1")
    assert "今天" in str(exc.value)
    clock.day = date(2026, 9, 15)
    limiter.check("ask", "u1")


def test_rejected_calls_do_not_consume_quota():
    limiter, clock = make({"ask": (1, 2)})
    limiter.check("ask", "u1")
    for _ in range(5):
        with pytest.raises(RateLimited):
            limiter.check("ask", "u1")  # 被擋下的不算次數
    clock.now += 61
    limiter.check("ask", "u1")  # 今天第 2 次，仍在每日上限內


def test_users_and_buckets_are_independent():
    limiter, _ = make({"media": (1, 0), "ask": (1, 0)})
    limiter.check("media", "u1")
    limiter.check("media", "u2")
    limiter.check("ask", "u1")


def test_zero_means_unlimited_and_unknown_bucket_is_not_limited():
    limiter, _ = make({"media": (0, 0)})
    for _ in range(100):
        limiter.check("media", "u1")
        limiter.check("not-configured", "u1")


def test_disabled_limiter_never_rejects():
    limiter = RateLimiter({"media": (1, 1)}, enabled=False)
    for _ in range(10):
        limiter.check("media", "u1")


def test_old_days_are_pruned_so_memory_stays_bounded():
    limiter, clock = make({"ask": (0, 5)})
    for day in range(1, 29):
        clock.day = date(2026, 9, day)
        limiter.check("ask", f"user-{day}")
    assert len(limiter._daily) <= 2


def test_parse_limits_overrides_defaults():
    limits = parse_limits("media=1/5, ask=0/0")
    assert limits["media"] == (1, 5)
    assert limits["ask"] == (0, 0)
    assert limits["analyze"] == DEFAULT_LIMITS["analyze"]  # 沒寫到的沿用預設


def test_parse_limits_rejects_garbage():
    with pytest.raises(ValueError):
        parse_limits("media=abc")
    with pytest.raises(ValueError):
        parse_limits("nosuchbucket=1/1")


def test_live_chunk_minute_limit_leaves_room_for_the_end_of_meeting_flush():
    """結束會議時前端會一次補送先前上傳失敗的錄音段，限太緊會把那幾段吃掉。"""
    per_minute, _ = DEFAULT_LIMITS["live_chunk"]
    assert per_minute >= 15
