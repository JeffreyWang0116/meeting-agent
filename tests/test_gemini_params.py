"""Gemini 3.6 起的請求規則（2026-10 官方 what's new／deprecations）。

- temperature／top_p／top_k 已棄用：3.6 Flash 與 3.5 Flash-Lite 目前直接忽略，官方寫明「未來的
  模型世代會回傳錯誤」——留著它們，等於換新模型那天每個請求都 400。thinking_budget、
  candidate_count 同理（Gemini 3 起不支援）
- gemini-3.5-flash 的請求被官方自動轉到 gemini-3.6-flash。本專案的原則是模型版本釘死、
  不依賴別人轉送，所以設定直接寫 3.6
"""
import re
from pathlib import Path

from app.config import Settings, get_settings
from app.gemini_keys import OVERLOAD_ALTERNATES

APP = Path(__file__).resolve().parent.parent / "app"
DEPRECATED = re.compile(r"""["'](temperature|top_p|top_k|thinking_budget|candidate_count)["']\s*:""")


def test_no_deprecated_generation_params_in_requests():
    hits = [
        f"{path.relative_to(APP.parent)}:{n}"
        for path in APP.rglob("*.py")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if DEPRECATED.search(line)
    ]
    assert hits == []


def test_strong_model_defaults_are_pinned_to_3_6(monkeypatch):
    for name in ("VOICE_MATCH_MODEL", "TRANSCRIBE_FALLBACK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    s = get_settings()
    assert s.voice_match_model == "gemini-3.6-flash"
    assert s.transcribe_fallback_model == "gemini-3.6-flash"
    assert Settings().voice_match_model == "gemini-3.6-flash"
    assert Settings().transcribe_fallback_model == "gemini-3.6-flash"


def test_overload_alternates_pair_lite_with_3_6():
    assert OVERLOAD_ALTERNATES == {
        "gemini-3.5-flash-lite": "gemini-3.6-flash",
        "gemini-3.6-flash": "gemini-3.5-flash-lite",
    }
