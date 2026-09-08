"""firebase-admin 初始化的錯誤訊息。

金鑰設定填錯是部署時最容易犯的錯——本機相對路徑寫錯、Render 上貼 JSON 時
夾到換行。這種錯如果讓 firebase_admin 從深處丟原始例外，看到的人只會看到
FileNotFoundError 或 JSONDecodeError，完全聯想不到是自己的環境變數有問題。

測的是 build_credential 而不是 ensure_app：後者有「已初始化就直接沿用」的
捷徑，只要 process 裡先有了 app，這些檢查就整組空轉——那種測試單獨跑會過、
全部跑會掛，比沒有測試更糟。
"""
from __future__ import annotations

import json

import pytest

from app.firebase import build_credential, require_matching_project


def test_missing_credential_file_names_the_path(tmp_path):
    missing = tmp_path / "firebase-service-account.json"

    with pytest.raises(ValueError, match="firebase-service-account.json"):
        build_credential(cred_file=str(missing))


def test_malformed_credential_json_says_so():
    """Render 上把 service account JSON 貼成環境變數時，最常見的災難是
    夾到換行或只貼了半截。"""
    with pytest.raises(ValueError, match="FIREBASE_CREDENTIALS_JSON"):
        build_credential(cred_json='{"type": "service_account"')


def test_no_credentials_at_all_says_which_variables():
    with pytest.raises(ValueError, match="FIREBASE_CREDENTIALS"):
        build_credential()


# ---- 前端設定與後端金鑰必須是同一個專案 ----

def _sa(project: str) -> str:
    return json.dumps({"type": "service_account", "project_id": project})


def test_mismatched_projects_are_rejected_with_both_names():
    """實際踩到的最貴的一次：兩個人各自把自己 Firebase 專案的金鑰填進同一個
    Render 服務，四個值因此混到兩個專案。

    症狀極難聯想：登入畫面過得去、看起來登入成功，但進去之後每個 API 都失敗
    ——因為 token 由 A 專案簽發，後端拿 B 專案的身分驗簽。錯誤只說
    InvalidIdTokenError，訊息還寫著「或已過期」，把人往完全錯誤的方向帶。
    這種錯必須在啟動時就講清楚是哪兩個專案對不上。
    """
    with pytest.raises(RuntimeError, match="meeting-agent-aaaaa.*meeting-agent-bbbbb"):
        require_matching_project(
            project_id="meeting-agent-aaaaa", cred_json=_sa("meeting-agent-bbbbb")
        )


def test_matching_projects_pass():
    require_matching_project(
        project_id="meeting-agent-aaaaa", cred_json=_sa("meeting-agent-aaaaa")
    )


def test_credential_file_is_read_too(tmp_path):
    """本機用檔案、雲端用 JSON 字串，兩條路都要擋得住。"""
    f = tmp_path / "sa.json"
    f.write_text(_sa("meeting-agent-bbbbb"), encoding="utf-8")

    with pytest.raises(RuntimeError, match="meeting-agent-bbbbb"):
        require_matching_project(project_id="meeting-agent-aaaaa", cred_file=str(f))


def test_no_project_id_configured_skips_the_check():
    """沒設 FIREBASE_PROJECT_ID 就無從比對。這裡不該自己編一個答案出來擋人
    ——啟動失敗的門檻要留給「確定是錯的」，不是「不知道」。"""
    require_matching_project(project_id=None, cred_json=_sa("meeting-agent-bbbbb"))
