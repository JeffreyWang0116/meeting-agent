"""手機推播（Web Push / VAPID）。

原本的「每日提醒通知」是頁面端的 `new Notification()`：iOS 不支援，而且要
App 正開著才跳得出來——手機上等於沒有提醒。Web Push 是唯一能在 App 關著、
手機鎖著時送達的方式（iOS 16.4 起支援，但使用者必須先把網站加到主畫面）。

這個模組只管「什麼時候送、送什麼」與「怎麼送出去」，不碰資料庫。
決定時機的部分全是純函式，因為它要在凌晨自己跑，沒有人會在旁邊看。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEFAULT_HOUR = 9
# 送出窗口：當地時間 hour 起算幾小時內都還算數。GitHub Actions 的 cron 常常
# 延遲十幾分鐘到一小時，只認整點會讓某些日子整天不送；但也不能一路送到半夜
SEND_WINDOW_HOURS = 3
# 比 UTC 快幾分鐘。UTC-12（Baker Island）到 UTC+14（Kiribati）是現實世界的範圍
MIN_TZ_OFFSET, MAX_TZ_OFFSET = -720, 840
MAX_ENDPOINT = 2000


class PushError(Exception):
    """訂閱內容不合法。"""


def normalize_subscription(raw: dict) -> dict:
    """驗證瀏覽器送來的 PushSubscription。只留送出時真正會用到的欄位。"""
    if not isinstance(raw, dict):
        raise PushError("訂閱格式不正確")

    endpoint = str(raw.get("endpoint") or "").strip()
    if not endpoint:
        raise PushError("缺少 endpoint")
    if not endpoint.startswith("https://"):
        # 推播服務的 endpoint 一律是 https，不是的話就是有人在亂塞網址
        raise PushError("endpoint 必須是 https")
    if len(endpoint) > MAX_ENDPOINT:
        raise PushError("endpoint 過長")

    keys = raw.get("keys")
    if not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        raise PushError("缺少加密金鑰（p256dh / auth）")

    tz_offset = raw.get("tz_offset")
    tz_offset = 0 if tz_offset is None else tz_offset
    if not isinstance(tz_offset, int) or isinstance(tz_offset, bool):
        raise PushError("tz_offset 必須是整數分鐘")
    if not MIN_TZ_OFFSET <= tz_offset <= MAX_TZ_OFFSET:
        raise PushError("tz_offset 超出合理範圍")

    hour = raw.get("hour")
    hour = DEFAULT_HOUR if hour is None else hour
    if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
        raise PushError("hour 必須是 0~23")

    return {
        "endpoint": endpoint,
        "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
        "tz_offset": tz_offset,
        "hour": hour,
    }


def subscription_id(endpoint: str) -> str:
    """endpoint 太長不能當文件 id，而且同一支手機重新訂閱時要覆蓋舊的那筆。"""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:32]


def local_time(sub: dict, now_utc: datetime) -> datetime:
    return now_utc.astimezone(timezone.utc) + timedelta(minutes=sub.get("tz_offset", 0))


def due_now(sub: dict, now_utc: datetime) -> bool:
    """這筆訂閱現在該不該送。"""
    if sub.get("paused"):
        return False
    local = local_time(sub, now_utc)
    start = sub.get("hour", DEFAULT_HOUR)
    if not start <= local.hour < start + SEND_WINDOW_HOURS:
        return False
    # 日期要用訂閱者自己的日期：台北早上 9 點時 UTC 還在前一天
    return sub.get("last_sent") != local.date().isoformat()


def digest(reminders: dict) -> dict | None:
    """把掃描結果變成一則通知。沒有急事就回 None——沒事也每天跳一則的話，
    使用者第一件事就是把通知關掉。"""
    urgent = [r for r in reminders.get("reminders", [])
              if r.get("kind") in ("overdue", "due_soon")]
    if not urgent:
        return None
    overdue = sum(1 for r in urgent if r["kind"] == "overdue")
    soon = len(urgent) - overdue
    parts = [f"{overdue} 項逾期" if overdue else "", f"{soon} 項即將到期" if soon else ""]
    # 內容只放數量：推播會顯示在鎖定畫面上，而任務文字常帶人名與金額
    return {
        "title": "會議助手待辦提醒",
        "body": "、".join(p for p in parts if p) + "，點開看看吧。",
        "url": "/",
    }


# ---- 真正送出去 ----

#: send() 的回傳值。gone＝對方已經不存在，呼叫端應該把訂閱刪掉
OK, GONE, FAILED = "ok", "gone", "failed"


def make_sender(public_key: str | None, private_key: str | None, subject: str | None):
    """回傳 send(sub, payload) -> OK/GONE/FAILED；沒設金鑰就回 None（功能停用）。"""
    if not (public_key and private_key and subject):
        return None

    def send(sub: dict, payload: dict) -> str:
        from pywebpush import WebPushException, webpush  # 只有真的要送才需要這個套件

        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},
                ttl=60 * 60 * 12,  # 手機關機一整天後才開，昨天的待辦提醒已經沒意義
            )
            return OK
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                # 已解除安裝或清掉網站資料。不刪的話這筆死訂閱會每小時被試一次
                return GONE
            logger.warning("推播送出失敗（%s）：%s", status, e)
            return FAILED
        except Exception as e:  # noqa: BLE001 - 排程不能因為單一訂閱掛掉而中斷
            logger.warning("推播送出失敗：%s", e)
            return FAILED

    return send


def _keygen() -> None:
    """python -m app.push keygen：產生一組 VAPID 金鑰。

    金鑰與網域綁定在使用者的訂閱上——換掉等於所有人的訂閱同時失效，
    所以產生一次就存好，不要每次部署重跑。
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()  # noqa: E731
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    print(f"VAPID_PUBLIC_KEY={b64(public)}")
    print(f"VAPID_PRIVATE_KEY={b64(private)}")
    print("VAPID_SUBJECT=mailto:你的信箱@example.com")


if __name__ == "__main__":  # pragma: no cover - 一次性的工具
    import sys

    if sys.argv[1:2] == ["keygen"]:
        _keygen()
    else:
        print("用法：python -m app.push keygen")
