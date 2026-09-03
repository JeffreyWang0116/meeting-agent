"""firebase-admin 的初始化。

一個 process 只能 initialize_app 一次，而現在有兩個地方需要它——Firestore
儲存與 Auth 的 ID token 驗簽——且兩者可以獨立啟用（用 Firebase 登入但資料
存本地 JSON 是合理的組合）。集中在這裡，兩邊才不會互相踩到。

匯入維持延遲：沒裝 firebase-admin 的本機環境仍要能正常跑起來。
"""
from __future__ import annotations


def ensure_app(*, cred_json: str | None = None, cred_file: str | None = None):
    """確保 firebase-admin 已初始化，回傳那個 app。已經初始化過就直接沿用。"""
    import json

    import firebase_admin
    from firebase_admin import credentials

    try:
        return firebase_admin.get_app()
    except ValueError:
        pass

    if cred_file:
        cred = credentials.Certificate(cred_file)
    elif cred_json:
        cred = credentials.Certificate(json.loads(cred_json))
    else:
        raise ValueError("需要 FIREBASE_CREDENTIALS_FILE 或 FIREBASE_CREDENTIALS_JSON")
    return firebase_admin.initialize_app(cred)
