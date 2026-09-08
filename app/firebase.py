"""firebase-admin 的初始化。

一個 process 只能 initialize_app 一次，而現在有兩個地方需要它——Firestore
儲存與 Auth 的 ID token 驗簽——且兩者可以獨立啟用（用 Firebase 登入但資料
存本地 JSON 是合理的組合）。集中在這裡，兩邊才不會互相踩到。

匯入維持延遲：沒裝 firebase-admin 的本機環境仍要能正常跑起來。
"""
from __future__ import annotations


def build_credential(*, cred_json: str | None = None, cred_file: str | None = None):
    """把設定變成 firebase-admin 的憑證物件。

    金鑰設定填錯是部署時最容易犯的錯，訊息要直接指向「是哪個環境變數、錯在
    哪」——讓 firebase_admin 從深處丟 FileNotFoundError / JSONDecodeError 的話，
    看到的人完全聯想不到是自己的設定有問題。

    刻意與 ensure_app 分開：ensure_app 有「已經初始化過就直接沿用」的捷徑，
    驗證邏輯若埋在那條捷徑後面，只要 process 裡已經有 app 就整組空轉，測試
    也會隨執行順序時綠時紅。這個函式沒有任何全域狀態。
    """
    import json
    import pathlib

    from firebase_admin import credentials

    if cred_file:
        path = pathlib.Path(cred_file)
        if not path.is_file():
            raise ValueError(
                f"找不到 Firebase service account 金鑰檔：{path}"
                f"（FIREBASE_CREDENTIALS_FILE 指到這裡，目前工作目錄是 {pathlib.Path.cwd()}）。"
                "請確認金鑰已下載並放到這個路徑，或改用 FIREBASE_CREDENTIALS_JSON。"
            )
        return credentials.Certificate(str(path))

    if cred_json:
        try:
            parsed = json.loads(cred_json)
        except ValueError as exc:
            raise ValueError(
                f"FIREBASE_CREDENTIALS_JSON 不是合法的 JSON（{exc}）。"
                "貼進環境變數時要整份、單行、含頭尾大括號——夾到換行或只貼半截都會這樣。"
            ) from exc
        return credentials.Certificate(parsed)

    raise ValueError("需要 FIREBASE_CREDENTIALS_FILE 或 FIREBASE_CREDENTIALS_JSON")


def _credential_project_id(*, cred_json: str | None = None, cred_file: str | None = None):
    """從 service account 金鑰讀出它屬於哪個專案。

    刻意不經過 firebase_admin：這個值要在初始化「之前」拿來比對，而
    initialize_app 一旦跑過就沒有回頭路。格式錯誤在這裡一律回 None，交給
    build_credential 去給那句講得清楚多了的訊息。
    """
    import json
    import pathlib

    raw = None
    if cred_file:
        path = pathlib.Path(cred_file)
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
    elif cred_json:
        raw = cred_json
    if not raw:
        return None
    try:
        return json.loads(raw).get("project_id")
    except ValueError:
        return None


def require_matching_project(
    *,
    project_id: str | None,
    cred_json: str | None = None,
    cred_file: str | None = None,
) -> None:
    """前端設定與後端金鑰必須是同一個 Firebase 專案。

    四個值分兩批來源：三個公開值給前端簽 ID token，service account 給後端
    驗簽。混到兩個專案時症狀極難聯想——登入畫面過得去、看起來登入成功，但
    進去之後每個 API 都失敗，而錯誤只說 InvalidIdTokenError，訊息還寫著
    「或已過期」，把人往 token 生命週期的方向帶，完全不指向真正的原因。

    多人共用一組雲端環境變數時特別容易發生：各自填各自的專案，四個值就混了。
    """
    if not project_id:
        return  # 無從比對。啟動失敗的門檻留給「確定是錯的」，不是「不知道」
    cred_project = _credential_project_id(cred_json=cred_json, cred_file=cred_file)
    if not cred_project or cred_project == project_id:
        return
    raise RuntimeError(
        f"Firebase 設定混到兩個專案：FIREBASE_PROJECT_ID 是 {project_id}，"
        f"但 service account 金鑰屬於 {cred_project}。"
        "前端用前者簽發 ID token、後端用後者驗簽，兩邊對不起來。"
        "這裡刻意讓啟動失敗，而不是照常起來——後者的症狀是登入畫面過得去、"
        "看起來登入成功，但進去之後每個 API 都失敗，錯誤只說 InvalidIdTokenError，"
        "完全不會指向真正的原因。"
        "四個值（FIREBASE_WEB_API_KEY / AUTH_DOMAIN / PROJECT_ID / "
        "CREDENTIALS_JSON 或 _FILE）必須全部來自同一個專案。"
    )


def ensure_app(*, cred_json: str | None = None, cred_file: str | None = None):
    """確保 firebase-admin 已初始化，回傳那個 app。已經初始化過就直接沿用。"""
    import firebase_admin

    try:
        return firebase_admin.get_app()
    except ValueError:
        pass
    return firebase_admin.initialize_app(
        build_credential(cred_json=cred_json, cred_file=cred_file)
    )
