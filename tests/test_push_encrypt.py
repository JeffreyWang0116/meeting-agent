"""真正送出去的那一段：VAPID 簽章與內容加密。

這是整個推播裡唯一無法在本機瀏覽器驗證的部分——Playwright 的 Chromium 沒有
推播服務，訂閱直接就失敗了。而金鑰格式寫錯的後果是「手機永遠收不到通知，
伺服器日誌卻一片安靜」。所以這裡自己扮演訂閱端：造一組真的 P-256 金鑰，
把訊息加密出來再解回去，確認繞一圈還是同一句話。
"""
from __future__ import annotations

import base64
import json

import pytest

pytest.importorskip("pywebpush")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

# app/push.py 的註解說明了金鑰怎麼產（python -m app.push keygen）。
# 這組只用在測試裡，不是任何環境的真金鑰。
VAPID_PUBLIC = "BOxbjLmNykxJoHbCioN7eK7bDrRiXNy5BDZoUQYKacHH5YV1Z54OXRtDkEWXOurZbL2UG7X3_XD_DeSdkoHbMuk"
VAPID_PRIVATE = "P2cfZNT1TdxlNYL63ytgym6S1Mp27ePdMfVh5zdErxw"
SUBJECT = "mailto:test@example.com"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def subscriber():
    """扮演手機：一組 P-256 金鑰加上 16 bytes 的 auth secret，就是瀏覽器給的那些。"""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = b"0123456789abcdef"
    return {
        "private": key,
        "auth": auth,
        "info": {
            "endpoint": "https://fcm.googleapis.com/fcm/send/pretend",
            "keys": {"p256dh": b64(public), "auth": b64(auth)},
        },
    }


def test_the_message_survives_the_round_trip(subscriber):
    import http_ece
    from pywebpush import WebPusher

    payload = {"title": "會議助手待辦提醒", "body": "3 項逾期、1 項即將到期，點開看看吧。", "url": "/"}
    # WebPusher.encode 只吃 bytes；真正的 webpush() 會先幫忙編碼（__init__.py:328）
    raw = json.dumps(payload, ensure_ascii=False).encode()
    encoded = WebPusher(subscriber["info"]).encode(raw)

    plain = http_ece.decrypt(
        encoded["body"],
        private_key=subscriber["private"],
        auth_secret=subscriber["auth"],
        version="aes128gcm",
    )

    assert json.loads(plain) == payload


def test_our_private_key_format_is_the_one_pywebpush_accepts(subscriber):
    """金鑰存在環境變數裡，只能是字串。寫成 PEM、DER 還是 raw base64url，
    pywebpush 的解讀方式不同——弄錯就簽不出 Authorization 標頭。"""
    from py_vapid import Vapid02

    vapid = Vapid02.from_string(VAPID_PRIVATE)
    headers = vapid.sign({"aud": "https://fcm.googleapis.com", "sub": SUBJECT})

    assert headers["Authorization"].startswith("vapid ")
    # 推播服務靠標頭裡的公鑰比對訂閱，與前端拿到的那把必須是同一組
    assert VAPID_PUBLIC in headers["Authorization"]


def test_a_dead_subscription_is_reported_as_gone(subscriber, monkeypatch):
    """404/410 要變成 GONE，呼叫端才知道該把那筆訂閱刪掉。"""
    import pywebpush

    from app.push import GONE, make_sender

    class Response:
        status_code = 410

    def fail(*args, **kwargs):
        raise pywebpush.WebPushException("gone", response=Response())

    monkeypatch.setattr(pywebpush, "webpush", fail)
    send = make_sender(VAPID_PUBLIC, VAPID_PRIVATE, SUBJECT)

    assert send(subscriber["info"], {"title": "x", "body": "y"}) == GONE


def test_other_failures_do_not_delete_the_subscription(subscriber, monkeypatch):
    """推播服務暫時掛掉（500）不代表使用者退訂了。"""
    import pywebpush

    from app.push import FAILED, make_sender

    class Response:
        status_code = 500

    def fail(*args, **kwargs):
        raise pywebpush.WebPushException("boom", response=Response())

    monkeypatch.setattr(pywebpush, "webpush", fail)
    send = make_sender(VAPID_PUBLIC, VAPID_PRIVATE, SUBJECT)

    assert send(subscriber["info"], {"title": "x", "body": "y"}) == FAILED
