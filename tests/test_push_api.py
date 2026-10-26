"""推播的端點：訂閱、試送，以及排程每小時叫的那一支。

/api/push/run 沒有登入者——它是 GitHub Actions 打進來的，靠共用密語把關。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.agents.decision_agent import DecisionAgent
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent
from app.config import Settings
from app.main import create_app
from app.orchestrator import Orchestrator
from app.push import GONE, OK
from app.stores.local_store import LocalJsonStore
from tests.test_decision import valid_json

SUB = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
    "keys": {"p256dh": "pub", "auth": "secret"},
    "tz_offset": 480,
}
SECRET = "cron-secret"


class FakeSender:
    """記下送出去的每一則，並照 results 依序回傳結果。"""

    def __init__(self, results=None):
        self.sent = []
        self.results = list(results or [])

    def __call__(self, sub, payload):
        self.sent.append((sub["endpoint"], payload))
        return self.results.pop(0) if self.results else OK


def make(tmp_path, *, sender=None, now=None, keys=True):
    store = LocalJsonStore(tmp_path / "db.json")
    orchestrator = Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(generate=lambda p: valid_json()),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(tmp_path / "notifications"),
    )
    settings = Settings(
        gemini_api_key=None, data_dir=tmp_path,
        vapid_public_key="BPublic" if keys else None,
        vapid_private_key="private" if keys else None,
        vapid_subject="mailto:me@example.com" if keys else None,
        push_cron_secret=SECRET,
    )
    sender = sender if sender is not None else FakeSender()
    app = create_app(
        settings, store=store, orchestrator=orchestrator,
        push_send=sender if keys else None,
        push_now=(lambda: now) if now else None,
    )
    client = TestClient(app)
    client.store = store
    client.sender = sender
    return client


def overdue_task(client):
    """一筆早就逾期的任務，掃描時會算成 overdue。"""
    client.post("/api/tasks", json={"task": "交報告", "due_date": "2020-01-01"})


# ---- 公鑰 ----

def test_frontend_can_fetch_the_public_key(tmp_path):
    body = make(tmp_path).get("/api/push/key").json()

    assert body["enabled"] is True
    assert body["public_key"] == "BPublic"


def test_without_keys_the_feature_reports_itself_as_off(tmp_path):
    """前端要能說出「這個站台沒設定推播」，而不是跳一個失敗的訂閱流程。"""
    body = make(tmp_path, keys=False).get("/api/push/key").json()

    assert body["enabled"] is False
    assert body.get("public_key") is None


# ---- 訂閱 ----

def test_subscribing_stores_it_for_this_workspace(tmp_path):
    client = make(tmp_path)

    assert client.post("/api/push/subscribe", json=SUB).status_code == 200

    saved = client.store.all_push_subscriptions()
    assert len(saved) == 1
    assert saved[0]["endpoint"] == SUB["endpoint"]
    assert saved[0]["tz_offset"] == 480


def test_subscribing_twice_from_the_same_device_keeps_one_row(tmp_path):
    client = make(tmp_path)
    client.post("/api/push/subscribe", json=SUB)
    client.post("/api/push/subscribe", json={**SUB, "tz_offset": 0})

    saved = client.store.all_push_subscriptions()
    assert len(saved) == 1
    assert saved[0]["tz_offset"] == 0


def test_a_bogus_subscription_is_rejected(tmp_path):
    client = make(tmp_path)

    resp = client.post("/api/push/subscribe", json={"endpoint": "http://evil.example"})

    assert resp.status_code == 400


def test_unsubscribing_removes_it(tmp_path):
    client = make(tmp_path)
    client.post("/api/push/subscribe", json=SUB)

    assert client.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]}).status_code == 200
    assert client.store.all_push_subscriptions() == []


def test_subscribing_is_refused_when_the_site_has_no_keys(tmp_path):
    client = make(tmp_path, keys=False)

    assert client.post("/api/push/subscribe", json=SUB).status_code == 503


# ---- 試送 ----

def test_test_push_goes_out_immediately(tmp_path):
    """手機收不收得到，只有真的送一則才知道。"""
    client = make(tmp_path)
    client.post("/api/push/subscribe", json=SUB)

    resp = client.post("/api/push/test")

    assert resp.status_code == 200
    assert resp.json()["sent"] == 1
    assert client.sender.sent[0][0] == SUB["endpoint"]


def test_test_push_without_a_subscription_says_so(tmp_path):
    client = make(tmp_path)

    assert client.post("/api/push/test").json()["sent"] == 0


def test_test_push_does_not_count_as_the_daily_one(tmp_path):
    """按了試送之後，當天早上還是要收得到真正的提醒。"""
    client = make(tmp_path)
    client.post("/api/push/subscribe", json=SUB)
    client.post("/api/push/test")

    assert client.store.all_push_subscriptions()[0].get("last_sent") is None


# ---- 排程 ----

def utc(hour: int) -> datetime:
    return datetime(2026, 10, 26, hour, tzinfo=timezone.utc)


def run(client, secret=SECRET):
    return client.post("/api/push/run", headers={"X-Push-Secret": secret})


def test_the_scheduler_needs_the_shared_secret(tmp_path):
    client = make(tmp_path, now=utc(1))

    assert run(client, "wrong").status_code == 401
    assert client.post("/api/push/run").status_code == 401


def test_the_scheduler_does_not_need_a_login(tmp_path):
    """GitHub Actions 沒有 Firebase 帳號；擋在登入中介層就永遠跑不到。"""
    client = make(tmp_path, now=utc(1))

    assert run(client).status_code == 200


def test_due_subscriptions_get_the_digest(tmp_path):
    client = make(tmp_path, now=utc(1))  # 台北 09:00
    overdue_task(client)
    client.post("/api/push/subscribe", json=SUB)

    body = run(client).json()

    assert body["sent"] == 1
    assert "1 項逾期" in client.sender.sent[0][1]["body"]


def test_nothing_is_sent_outside_the_window(tmp_path):
    client = make(tmp_path, now=utc(18))  # 台北凌晨 2 點
    overdue_task(client)
    client.post("/api/push/subscribe", json=SUB)

    assert run(client).json()["sent"] == 0
    assert client.sender.sent == []


def test_a_quiet_day_sends_nothing(tmp_path):
    client = make(tmp_path, now=utc(1))
    client.post("/api/push/subscribe", json=SUB)  # 沒有任何任務

    assert run(client).json()["sent"] == 0


def test_running_twice_in_the_window_only_notifies_once(tmp_path):
    client = make(tmp_path, now=utc(1))
    overdue_task(client)
    client.post("/api/push/subscribe", json=SUB)

    run(client)
    assert run(client).json()["sent"] == 0
    assert len(client.sender.sent) == 1


def test_a_quiet_day_still_counts_as_checked_off(tmp_path):
    """不記的話，早上 9 點到 11 點之間會把同一份清單重掃三次。"""
    client = make(tmp_path, now=utc(1))
    client.post("/api/push/subscribe", json=SUB)

    run(client)

    assert client.store.all_push_subscriptions()[0]["last_sent"] == "2026-10-26"


def test_a_dead_subscription_is_removed(tmp_path):
    """手機解除安裝或清掉網站資料之後，不刪的話會每小時被試一次到天荒地老。"""
    client = make(tmp_path, sender=FakeSender([GONE]), now=utc(1))
    overdue_task(client)
    client.post("/api/push/subscribe", json=SUB)

    body = run(client).json()

    assert body["dropped"] == 1
    assert client.store.all_push_subscriptions() == []
