"""本地時區工具。

伺服器可能部署在 UTC 主機（如 Render），但使用者在台灣。「今日用量」、
提醒的「今天到期／逾期」都應以使用者所在時區判斷，否則會整整偏移數小時。

台灣沒有日光節約時間，用固定 UTC+8 偏移即可，不需要 IANA tz 資料庫
（Windows 預設沒有），也可用 APP_TZ_OFFSET_HOURS 覆寫成其他地區。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

_OFFSET_HOURS = float(os.environ.get("APP_TZ_OFFSET_HOURS", "8"))
LOCAL_TZ = timezone(timedelta(hours=_OFFSET_HOURS))


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def today_local() -> date:
    return now_local().date()
