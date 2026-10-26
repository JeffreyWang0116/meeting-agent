"""手機推播的純邏輯：訂閱驗證、送出時機、摘要文字。

原本的「每日提醒通知」用頁面端的 new Notification()，iOS 不支援、而且要
App 開著才跳得出來——等於手機上根本沒有提醒。改成 Web Push 之後，決定
「什麼時候送、送什麼」的邏輯全部放在這裡，不碰網路也不碰資料庫。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.push import (
    PushError,
    digest,
    due_now,
    normalize_subscription,
    subscription_id,
)

RAW = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
    "keys": {"p256dh": "BPublicKeyBytes", "auth": "AuthSecret"},
    "tz_offset": 480,
}


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


# ---- 訂閱驗證 ----

def test_normalized_subscription_keeps_what_the_sender_needs():
    sub = normalize_subscription(RAW)

    assert sub["endpoint"] == RAW["endpoint"]
    assert sub["keys"] == {"p256dh": "BPublicKeyBytes", "auth": "AuthSecret"}
    assert sub["tz_offset"] == 480


def test_endpoint_is_required():
    with pytest.raises(PushError):
        normalize_subscription({"keys": RAW["keys"]})


def test_endpoint_must_be_https():
    """推播服務的 endpoint 一律是 https；不是的話就是有人在亂塞網址。"""
    with pytest.raises(PushError):
        normalize_subscription({**RAW, "endpoint": "http://evil.example/collect"})


def test_both_keys_are_required():
    with pytest.raises(PushError):
        normalize_subscription({**RAW, "keys": {"p256dh": "x"}})


def test_absurd_timezone_is_rejected():
    with pytest.raises(PushError):
        normalize_subscription({**RAW, "tz_offset": 99999})


def test_missing_timezone_defaults_to_utc():
    sub = normalize_subscription({k: v for k, v in RAW.items() if k != "tz_offset"})

    assert sub["tz_offset"] == 0


def test_hour_defaults_to_nine_in_the_morning():
    assert normalize_subscription(RAW)["hour"] == 9


def test_hour_outside_a_day_is_rejected():
    with pytest.raises(PushError):
        normalize_subscription({**RAW, "hour": 25})


def test_the_same_endpoint_always_gets_the_same_id():
    """同一支手機重新訂閱時要覆蓋舊的那筆，不是多長一筆出來。"""
    assert subscription_id(RAW["endpoint"]) == subscription_id(RAW["endpoint"])
    assert subscription_id(RAW["endpoint"]) != subscription_id(RAW["endpoint"] + "x")


# ---- 送出時機 ----

def sub(**extra) -> dict:
    return {**normalize_subscription(RAW), **extra}


def test_sends_at_the_chosen_local_hour():
    # 台北 +480：UTC 01:00 ＝ 當地 09:00
    assert due_now(sub(), utc(26, 1)) is True


def test_does_not_send_in_the_middle_of_the_night():
    assert due_now(sub(), utc(26, 18)) is False  # 當地凌晨 2 點


def test_a_late_cron_run_still_delivers_that_day():
    """GitHub Actions 的 cron 常常晚十幾分鐘到一小時，卡死在整點會整天不送。"""
    assert due_now(sub(), utc(26, 3)) is True  # 當地 11 點，還在窗內


def test_but_not_all_day_long():
    assert due_now(sub(), utc(26, 8)) is False  # 當地下午 4 點，太晚了


def test_only_once_a_day():
    assert due_now(sub(last_sent="2026-10-26"), utc(26, 1)) is False
    assert due_now(sub(last_sent="2026-10-25"), utc(26, 1)) is True


def test_the_day_is_the_subscribers_own_day_not_utcs():
    """台北 UTC+8 的早上 9 點，UTC 還在前一天的 01:00——比錯日期會每天送兩次。"""
    assert due_now(sub(last_sent="2026-10-26"), utc(26, 1)) is False


def test_a_paused_subscription_never_fires():
    assert due_now(sub(paused=True), utc(26, 1)) is False


# ---- 摘要文字 ----

def reminders(*kinds) -> dict:
    return {"reminders": [{"kind": k, "message": "某某任務", "days": 1} for k in kinds],
            "followups": []}


def test_digest_counts_overdue_and_due_soon():
    out = digest(reminders("overdue", "overdue", "due_soon"))

    assert "2 項逾期" in out["body"]
    assert "1 項即將到期" in out["body"]


def test_digest_leaves_out_the_half_that_is_zero():
    assert "0 項" not in digest(reminders("overdue"))["body"]


def test_nothing_urgent_means_no_notification_at_all():
    """沒有急事還每天跳一則，使用者第一件事就是把通知關掉。"""
    assert digest(reminders("unassigned")) is None
    assert digest(reminders()) is None


def test_digest_does_not_leak_task_text_to_the_lock_screen():
    """推播顯示在鎖定畫面上，旁邊的人也看得到；任務內容常帶人名。"""
    out = digest(reminders("overdue"))

    assert "某某任務" not in out["body"]
    assert "某某任務" not in out["title"]
