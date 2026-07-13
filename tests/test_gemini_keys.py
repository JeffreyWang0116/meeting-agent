"""Gemini 多金鑰輪替測試。

免費層每把 key 每天只有少量配額（20 次/天），demo 現場撞到 429 會翻車。
KeyPool 用 round-robin：每次呼叫都換下一把 key（循環），把配額平均分攤；
單次呼叫撞到 429（RESOURCE_EXHAUSTED）時，同一次呼叫內續試下一把。
"""
import pytest

from app.gemini_keys import KeyPool, call_with_rotation, is_quota_error


# ---- is_quota_error ----

def test_quota_error_detected_from_google_429_message():
    exc = RuntimeError(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, "
        "'message': 'You exceeded your current quota'}}"
    )
    assert is_quota_error(exc)


def test_non_quota_error_not_detected():
    assert not is_quota_error(ValueError("JSON 解析失敗"))


# ---- KeyPool ----

def test_next_key_round_robin_cycles():
    pool = KeyPool(["k1", "k2", "k3"])
    assert [pool.next_key() for _ in range(4)] == ["k1", "k2", "k3", "k1"]


def test_first_does_not_advance_cursor():
    """first 僅供顯示（health 端點），不可推進輪替游標。"""
    pool = KeyPool(["k1", "k2"])
    assert pool.first == "k1"
    assert pool.first == "k1"
    assert pool.next_key() == "k1"  # 游標仍從頭開始


def test_pool_skips_blank_keys_and_empty_is_falsy():
    assert not KeyPool([])
    assert not KeyPool([None, "", "  "])
    assert len(KeyPool(["k1", " ", "k2"])) == 2


def test_empty_pool_next_key_and_first_are_none():
    pool = KeyPool([])
    assert pool.next_key() is None
    assert pool.first is None


# ---- call_with_rotation ----

def _quota_exc():
    return RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")


def test_each_call_uses_next_key_round_robin():
    """連續呼叫：這次用第一把、下次用第二把…循環。"""
    pool = KeyPool(["k1", "k2", "k3"])
    used = []

    def fn(key):
        used.append(key)
        return key

    for _ in range(4):
        call_with_rotation(pool, fn)
    assert used == ["k1", "k2", "k3", "k1"]


def test_rotation_skips_exhausted_key_within_a_call():
    pool = KeyPool(["k1", "k2"])
    used = []

    def fn(key):
        used.append(key)
        if key == "k1":
            raise _quota_exc()
        return f"ok-{key}"

    assert call_with_rotation(pool, fn) == "ok-k2"
    assert used == ["k1", "k2"]


def test_rotation_raises_when_all_keys_exhausted():
    pool = KeyPool(["k1", "k2"])

    def fn(key):
        raise _quota_exc()

    with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
        call_with_rotation(pool, fn)


def test_non_quota_error_propagates_without_trying_more_keys():
    pool = KeyPool(["k1", "k2"])
    used = []

    def fn(key):
        used.append(key)
        raise ValueError("網路斷線")

    with pytest.raises(ValueError):
        call_with_rotation(pool, fn)
    assert used == ["k1"]  # 非配額錯誤不續試其他 key


def test_empty_pool_calls_fn_with_none():
    """沒設定金鑰時 fn(None) 自己丟出「未設定金鑰」的友善錯誤。"""
    def fn(key):
        assert key is None
        raise LookupError("未設定 GEMINI_API_KEY")

    with pytest.raises(LookupError):
        call_with_rotation(KeyPool([]), fn)


# ---- DecisionAgent 整合 ----

def test_decision_agent_rotates_key_on_quota_error(monkeypatch):
    from app.agents.decision_agent import DecisionAgent
    from tests.test_decision import valid_json

    agent = DecisionAgent(api_keys=["k1", "k2"])
    used = []

    def fake_call(key, prompt):
        used.append(key)
        if key == "k1":
            raise _quota_exc()
        return valid_json()

    monkeypatch.setattr(agent, "_call_gemini", fake_call)
    analysis = agent.analyze("鈺翔下週一交 prompt")
    assert analysis.meeting.title
    assert used == ["k1", "k2"]


# ---- GeminiTranscriber 整合 ----

def test_gemini_transcriber_rotates_key_on_quota_error(tmp_path, monkeypatch):
    from app.transcription.gemini_transcriber import GeminiTranscriber

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    t = GeminiTranscriber(api_keys=["k1", "k2"])
    used = []

    def fake_run(key, path):
        used.append(key)
        if key == "k1":
            raise _quota_exc()
        return "逐字稿"

    monkeypatch.setattr(t, "_transcribe_with_key", fake_run)
    assert t.transcribe(wav) == "逐字稿"
    assert used == ["k1", "k2"]


# ---- Settings ----

def test_settings_parses_multiple_keys(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("GEMINI_API_KEYS", "aaa, bbb ,ccc")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    s = get_settings()
    assert s.gemini_api_keys == ("aaa", "bbb", "ccc")
    assert s.gemini_api_key == "aaa"  # 向後相容：單數欄位＝第一把


def test_settings_falls_back_to_single_key(monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "solo")
    s = get_settings()
    assert s.gemini_api_keys == ("solo",)
    assert s.gemini_api_key == "solo"
