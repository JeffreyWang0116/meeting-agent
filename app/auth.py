"""把一個 HTTP 請求換成「這是誰」。

兩種模式，由設定決定：

- **Firebase Auth**（設了 FIREBASE_WEB_API_KEY 就啟用）：前端用 Google 登入
  拿到 ID token，每個請求帶著它，這裡驗簽後取 uid 當使用者。真正的帳號制，
  換裝置、換瀏覽器都是同一份資料。
- **沒設定**：維持原本的單人模式——API_TOKEN 是一把共用鑰匙，只分「進不進得
  來」，所有人都是 DEFAULT_USER。本機開發不必為了跑起來去申請 Firebase 專案。

為什麼用 ContextVar 而不是 FastAPI 的 Depends：current_user() 散落在四十幾個
端點以及 run_analysis / drop_from_rag 這類巢狀輔助函式裡，改成 Depends 要動
每一個簽名、還要把 user 一層層往內傳。ContextVar 由中介層設定、呼叫端維持原
樣，改動面積小得多。Starlette 把同步端點丟進執行緒池時會複製 context，所以
同步端點也讀得到；真正的背景執行緒（MediaJobManager 的轉錄）不會複製——那正
是 user 必須在 submit 當下就捕捉進工作紀錄的原因。
"""
from __future__ import annotations

import logging
from contextvars import ContextVar

from app.stores.base import DEFAULT_USER

logger = logging.getLogger(__name__)

# 目前這個請求屬於誰。沒有中介層設定時（單人模式、背景執行緒）就是預設值
CURRENT_USER: ContextVar[str] = ContextVar("current_user", default=DEFAULT_USER)


class AuthError(Exception):
    """ID token 缺少、過期或無效。"""


def bearer_token(authorization: str | None) -> str | None:
    """從 Authorization 標頭取出 token；格式不對回 None。"""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def verify_firebase_id_token(id_token: str) -> str:
    """驗證 Google 登入簽發的 ID token，回傳 Firebase uid。

    verify_id_token 會檢查簽章、發行者與過期時間，所以前端偽造不了；uid 由
    Firebase 指派且不會重複使用，拿來當資料的主人剛好。
    """
    from firebase_admin import auth as firebase_auth

    try:
        return firebase_auth.verify_id_token(id_token)["uid"]
    except Exception as exc:  # SDK 會丟各種子類別
        # firebase 的例外「訊息」才是關鍵：aud 專案不符（Render 的 service account
        # 跟前端 web 設定不是同一個專案）、時鐘偏移（Token used too early）、還是
        # 真的過期，全靠這句話分辨。只印型別＝把唯一的線索丟掉。連 log 一起記，
        # 這樣即使前端只看到摘要，伺服器端也查得到。
        reason = str(exc).strip() or type(exc).__name__
        logger.warning("ID token 驗證失敗：%s", reason)
        raise AuthError(f"登入憑證無效：{reason}") from exc
