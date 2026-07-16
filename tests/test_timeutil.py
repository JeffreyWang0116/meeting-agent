"""本地時區工具測試：部署在 UTC 主機時，今日/日期仍以使用者時區為準。"""
from datetime import timedelta, timezone

from app import timeutil


def test_local_tz_is_offset_based_and_dst_free():
    # 預設 UTC+8（台灣，無日光節約）
    assert timeutil.LOCAL_TZ.utcoffset(None) == timedelta(hours=8)


def test_now_local_carries_offset():
    assert timeutil.now_local().utcoffset() == timedelta(hours=8)


def test_today_local_matches_now_local_date():
    now = timeutil.now_local()
    assert timeutil.today_local() == now.date()


def test_today_differs_from_utc_around_midnight():
    """UTC 半夜（台灣清晨 8 點前）時，本地日期會比 UTC 日期多一天——這正是
    修正的重點：用 date.today() 在 UTC 主機上會把當天算成前一天。"""
    from datetime import datetime

    utc_late = datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc)  # UTC 20:00
    local = utc_late.astimezone(timeutil.LOCAL_TZ)
    assert local.date().isoformat() == "2026-07-17"  # 台灣已經是隔天凌晨 4 點
