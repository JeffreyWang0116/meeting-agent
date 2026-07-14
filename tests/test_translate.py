"""Translator 測試：注入假 generate，不呼叫真 Gemini。"""
import pytest

from app.translate import Translator, TranslateError


def test_translate_returns_generated_text():
    t = Translator(generate=lambda prompt: "  Hello everyone.  ")
    assert t.translate("大家好。", "en") == "Hello everyone."


def test_prompt_contains_target_language_and_text():
    captured = {}

    def fake(prompt):
        captured["prompt"] = prompt
        return "x"

    Translator(generate=fake).translate("大家好", "en")
    assert "英文" in captured["prompt"]
    assert "大家好" in captured["prompt"]

    Translator(generate=fake).translate("Hello", "zh")
    assert "繁體中文" in captured["prompt"]


def test_empty_text_returns_empty_without_calling_llm():
    def boom(prompt):
        raise AssertionError("空字串不該觸發 LLM")

    assert Translator(generate=boom).translate("   ", "en") == ""


def test_invalid_target_rejected():
    t = Translator(generate=lambda p: "x")
    with pytest.raises(ValueError):
        t.translate("hi", "fr")


def test_missing_api_key_raises_clear_error():
    t = Translator(api_key=None)  # 未注入 generate → 走真實路徑
    with pytest.raises(TranslateError, match="GEMINI_API_KEY"):
        t.translate("hi", "en")
