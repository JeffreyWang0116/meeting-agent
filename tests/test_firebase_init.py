"""firebase-admin 初始化的錯誤訊息。

金鑰設定填錯是部署時最容易犯的錯——本機相對路徑寫錯、Render 上貼 JSON 時
夾到換行。這種錯如果讓 firebase_admin 從深處丟原始例外，看到的人只會看到
FileNotFoundError 或 JSONDecodeError，完全聯想不到是自己的環境變數有問題。

測的是 build_credential 而不是 ensure_app：後者有「已初始化就直接沿用」的
捷徑，只要 process 裡先有了 app，這些檢查就整組空轉——那種測試單獨跑會過、
全部跑會掛，比沒有測試更糟。
"""
from __future__ import annotations

import pytest

from app.firebase import build_credential


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
