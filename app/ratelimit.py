"""速率限制：每位使用者、每種耗額度的操作各自有「每分鐘」與「每天」上限。

為什麼要有：公開網址上任何能登入的人都能連打上傳、問答，把 Gemini 免費層每專案
每日 500 次的額度燒光——而且那是整個服務共用的，一個人打爆就是所有人都不能用。
啟用 pyannote 講者分離後更直接：按處理時數與 voiceprint 個數計費。

刻意做成記憶體內的簡單計數，不加套件、不進資料庫：這個服務只跑單一行程，
重啟會歸零也無妨——要擋的是連打與濫用，不是精確記帳（精確用量看 usage.py）。

使用者身分取自 current_user()：啟用 Google 登入時每人各自計算；只用共用
API_TOKEN 時所有人都是同一個使用者，等於整個服務共用一份上限。
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from datetime import date
from typing import Callable

from app.timeutil import today_local

# 預設上限：(每分鐘, 每天)，0＝不限。依免費額度估算，一般人正常使用碰不到：
# 一場會議分析約 1~3 次請求，一小時即時聆聽約 80 段
DEFAULT_LIMITS: dict[str, tuple[int, int]] = {
    "analyze": (10, 60),     # 貼上文字分析、重新分析
    "media": (3, 20),        # 檔案上傳轉錄（整份轉錄＋分析，最耗額度）
    "live_start": (3, 20),   # 開一場即時聆聽
    # 每段錄音都是一次轉錄。每分鐘放寬：結束會議時前端會依序補送先前上傳失敗的段，
    # 限太緊會把那幾段吃掉。每天 600 段＝7.5 小時的聆聽
    "live_chunk": (20, 600),
    "ask": (10, 100),        # 跨會議問答
    "translate": (20, 300),  # 摘要翻譯
}

LABELS = {
    "analyze": "會議分析",
    "media": "檔案上傳",
    "live_start": "開始即時聆聽",
    "live_chunk": "即時聆聽錄音段",
    "ask": "詢問會議",
    "translate": "翻譯",
}


class RateLimited(Exception):
    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimiter:
    def __init__(
        self,
        limits: dict[str, tuple[int, int]] | None = None,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
        today: Callable[[], date] = today_local,
    ):
        self.limits = dict(DEFAULT_LIMITS if limits is None else limits)
        self.enabled = enabled
        self._clock = clock
        self._today = today
        self._lock = threading.Lock()
        self._recent: dict[tuple[str, str], deque[float]] = {}
        # (日期, bucket, user) → 次數。換日時舊日期整批丟掉，記憶體不會隨天數累積
        self._daily: dict[tuple[date, str, str], int] = {}

    def check(self, bucket: str, user: str) -> None:
        """允許就記一次；超過上限丟 RateLimited（被擋下的不算次數）。"""
        if not self.enabled or bucket not in self.limits:
            return
        per_minute, per_day = self.limits[bucket]
        label = LABELS.get(bucket, bucket)
        now, today = self._clock(), self._today()
        with self._lock:
            self._prune_days(today)
            recent = self._recent.setdefault((bucket, user), deque())
            while recent and now - recent[0] >= 60:
                recent.popleft()
            if per_minute and len(recent) >= per_minute:
                wait = max(1, math.ceil(60 - (now - recent[0])))
                raise RateLimited(
                    f"操作太頻繁：{label}每分鐘最多 {per_minute} 次，請 {wait} 秒後再試", wait
                )
            day_key = (today, bucket, user)
            if per_day and self._daily.get(day_key, 0) >= per_day:
                raise RateLimited(
                    f"今天的{label}已達上限 {per_day} 次，明天再試（上限保護整個服務共用的 AI 額度）",
                    3600,
                )
            recent.append(now)
            self._daily[day_key] = self._daily.get(day_key, 0) + 1

    def _prune_days(self, today: date) -> None:
        stale = [key for key in self._daily if key[0] != today]
        for key in stale:
            del self._daily[key]


def parse_limits(raw: str | None) -> dict[str, tuple[int, int]]:
    """RATE_LIMITS="media=3/20,ask=10/100" → 覆寫對應項目，其餘沿用預設。"""
    limits = dict(DEFAULT_LIMITS)
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        if name not in DEFAULT_LIMITS:
            raise ValueError(f"RATE_LIMITS 裡有不認得的項目：{name}（可用：{', '.join(DEFAULT_LIMITS)}）")
        per_minute, sep, per_day = value.partition("/")
        if not sep or not per_minute.strip().isdigit() or not per_day.strip().isdigit():
            raise ValueError(f"RATE_LIMITS 格式錯誤：{item}（應為 名稱=每分鐘/每天，例如 media=3/20）")
        limits[name] = (int(per_minute), int(per_day))
    return limits
