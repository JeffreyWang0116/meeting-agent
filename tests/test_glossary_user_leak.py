"""詞彙表跨帳號外洩：四條 prompt 路徑都必須讀「這個使用者自己的」詞彙表。

DEFAULT_USER（"local"）是還沒做登入前的單人模式資料桶。轉錄／校正／分析
呼叫詞彙表時不帶 user，就會一律讀到那一桶——於是舊資料裡的人名被灌進
每一支帳號的每一次請求，而使用者自己設的詞彙反而永遠不生效。
"""
from __future__ import annotations

import pytest

from app.glossary import Glossary
from app.stores.base import DEFAULT_USER
from app.stores.local_store import LocalJsonStore


@pytest.fixture
def glossary(tmp_path):
    g = Glossary(LocalJsonStore(tmp_path / "db.json"))
    g.replace([{"term": "舊資料人名", "note": "人名"}], user=DEFAULT_USER)
    g.replace([{"term": "我的專案代號"}], user="uid-A")
    return g


def test_analysis_prompt_uses_the_callers_glossary(glossary):
    from app.agents.decision_agent import DecisionAgent

    captured = {}
    agent = DecisionAgent(
        generate=lambda prompt: captured.setdefault("prompt", prompt) or "{}",
        glossary=glossary.terms,
    )
    try:
        agent.analyze("講者A：今天進度", user="uid-A")
    except Exception:
        pass  # 回傳值不是合法 JSON 沒關係，我們只看送出去的 prompt
    prompt = captured.get("prompt", "")
    assert "舊資料人名" not in prompt, "分析 prompt 灌進了 local 的舊詞彙"
    assert "我的專案代號" in prompt, "分析 prompt 沒帶到這個帳號自己的詞彙"


def test_corrector_prompt_uses_the_callers_glossary(glossary):
    from app.agents.corrector_agent import CorrectorAgent

    captured = {}
    agent = CorrectorAgent(
        generate=lambda prompt: captured.setdefault("prompt", prompt) or '{"corrections": []}',
        glossary=glossary.terms,
    )
    agent.correct("[1:02] 講者A：今天進度", user="uid-A")
    prompt = captured.get("prompt", "")
    assert "舊資料人名" not in prompt, "校正 prompt 灌進了 local 的舊詞彙"
    assert "我的專案代號" in prompt


def test_gemini_transcribe_prompt_uses_the_callers_glossary(glossary):
    from app.transcription.gemini_transcriber import GeminiTranscriber

    t = GeminiTranscriber(api_key="k", glossary=glossary.terms)
    prompt = t.build_prompt(user="uid-A")
    assert "舊資料人名" not in prompt, "轉錄 prompt 灌進了 local 的舊詞彙"
    assert "我的專案代號" in prompt


def test_live_session_transcribe_passes_the_session_owner(tmp_path):
    """即時聆聽逐段轉錄也要帶 session 的主人。

    這條路徑不經過 jobs.py，是另一支獨立的轉錄呼叫——漏掉的話，手機端即時
    錄音會一路讀到 local 的舊詞彙表。
    """
    from app.transcription.live_session import LiveSessionManager

    seen = []

    class FakeTranscriber:
        def transcribe(self, path, on_progress=None, hint=None, user=None):
            seen.append(user)
            return "[0:01] 講者A：測試"

    mgr = LiveSessionManager(FakeTranscriber(), tmp_path)
    sid = mgr.start(user="uid-A")
    mgr.add_chunk(sid, b"fake-audio", suffix=".webm", user="uid-A")

    assert seen == ["uid-A"], f"轉錄沒帶 session 的主人：{seen}"


def test_gemini_transcriber_never_builds_a_prompt_without_a_user():
    """真正送出的那個 build_prompt 呼叫必須帶 user。

    build_prompt 收了 user 參數不代表事情就對了——實際組 contents 的那行如果
    仍然用預設值呼叫，詞彙表照樣讀到 DEFAULT_USER 那桶。而那一行埋在真正的
    Gemini 呼叫裡，測試注入假 upload/generate 時會提前 return、根本走不到，
    所以改用靜態檢查守（比照 test_frontend_modules.py）。
    """
    import pathlib
    import re

    src = (
        pathlib.Path(__file__).resolve().parent.parent
        / "app" / "transcription" / "gemini_transcriber.py"
    ).read_text(encoding="utf-8")
    calls = re.findall(r"self\.build_prompt\(([^)]*)\)", src)
    assert calls, "找不到 build_prompt 的呼叫，測試本身過期了"
    for args in calls:
        assert "user" in args, f"build_prompt 呼叫沒帶 user：self.build_prompt({args})"
