"""公開部署該有的回應標頭。

實測部署在 Render 上的站，回應裡一個防護標頭都沒有。站本身是上鎖的（Firebase
帳號制），但「進得來的人是誰」跟「別人能不能借用他的登入狀態」是兩件事：沒有
framing 限制，任何網站都能把這個站嵌成隱形 iframe 蓋在自己的按鈕底下，讓已經
登入的使用者在不知情下點到刪除會議。標頭是唯一能擋這件事的地方——前端做什麼
都沒用，因為攻擊發生在別人的頁面上。

只加不需要盤點資源就安全的那幾個。完整的 Content-Security-Policy 要先把
Firebase SDK（gstatic）、Google Fonts 等來源全部列進白名單，漏一個就是整個
登入流程在正式站上壞掉而本機完全正常——那要另外一輪驗證，不混在這裡做。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json


def make_client(tmp_path, **extra) -> TestClient:
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda p: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    settings = Settings(gemini_api_key=None, data_dir=tmp_path, **extra)
    return TestClient(create_app(settings, store=store, orchestrator=orchestrator))


@pytest.mark.parametrize("path", ["/", "/api/health"])
def test_clickjacking_is_blocked_on_every_response(tmp_path, path):
    """頁面與 API 都不該能被別人嵌進 iframe。

    兩個標頭都送：frame-ancestors 是現行標準，X-Frame-Options 是給還在用的
    舊瀏覽器。Firebase 用 signInWithPopup（另開視窗，不是把本站嵌進 iframe），
    所以 DENY 不會擋到登入。
    """
    resp = make_client(tmp_path).get(path)
    assert resp.status_code == 200
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


def test_mime_sniffing_and_referrer_leakage_are_blocked(tmp_path):
    """nosniff：使用者上傳的檔名/內容不該有機會被瀏覽器改判成可執行型別。
    Referrer-Policy：網址帶有會議 id，不該整條送給外部連結的目的站。
    """
    resp = make_client(tmp_path).get("/api/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_hsts_only_on_public_deploy(tmp_path):
    """HSTS 會讓瀏覽器記住「這個網域只准走 https」，記住之後就改不掉。

    公開站要（Render 有 301 轉址，但第一次連線仍可能被降級攔截）；本機開發
    不能要——那會讓瀏覽器把 localhost 整個鎖成 https，之後每個用 http 跑本機
    服務的專案都跟著壞，而且使用者根本不會聯想到是這裡造成的。
    """
    assert "Strict-Transport-Security" not in make_client(tmp_path).get("/api/health").headers

    public = make_client(tmp_path, is_public_deploy=True, api_token="shared-key")
    hsts = public.get("/api/health").headers["Strict-Transport-Security"]
    assert "max-age=" in hsts


def test_headers_survive_an_error_response(tmp_path):
    """404/401 也是回應。錯誤頁一樣能被嵌進 iframe，漏掉就等於沒設。"""
    resp = make_client(tmp_path).get("/api/meetings/nope")
    assert resp.status_code == 404
    assert resp.headers["X-Frame-Options"] == "DENY"
