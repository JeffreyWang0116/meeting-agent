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
    import pathlib

    import firebase_admin
    from firebase_admin import credentials

    try:
        return firebase_admin.get_app()
    except ValueError:
        pass

    # 金鑰設定填錯是部署時最容易犯的錯。訊息要直接指向「是哪個環境變數、
    # 錯在哪」——讓 firebase_admin 從深處丟 FileNotFoundError / JSONDecodeError
    # 的話，看到的人完全聯想不到是自己的設定有問題
    if cred_file:
        path = pathlib.Path(cred_file)
        if not path.is_file():
            raise ValueError(
                f"找不到 Firebase service account 金鑰檔：{path}"
                f"（FIREBASE_CREDENTIALS_FILE 指到這裡，目前工作目錄是 {pathlib.Path.cwd()}）。"
                "請確認金鑰已下載並放到這個路徑，或改用 FIREBASE_CREDENTIALS_JSON。"
            )
        cred = credentials.Certificate(str(path))
    elif cred_json:
        try:
            parsed = json.loads(cred_json)
        except ValueError as exc:
            raise ValueError(
                f"FIREBASE_CREDENTIALS_JSON 不是合法的 JSON（{exc}）。"
                "貼進環境變數時要整份、單行、含頭尾大括號——夾到換行或只貼半截都會這樣。"
            ) from exc
        cred = credentials.Certificate(parsed)
    else:
        raise ValueError("需要 FIREBASE_CREDENTIALS_FILE 或 FIREBASE_CREDENTIALS_JSON")
    return firebase_admin.initialize_app(cred)
