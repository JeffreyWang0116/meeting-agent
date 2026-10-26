"""Translator 測試：注入假 generate，不呼叫真 Gemini。"""
import re

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
    assert "English" in captured["prompt"]
    assert "大家好" in captured["prompt"]

    Translator(generate=fake).translate("Hello", "zh")
    assert "Traditional Chinese" in captured["prompt"]


@pytest.mark.parametrize("target", ["en", "zh"])
def test_instructions_are_written_in_english(target):
    # 實測 gemini-3.5-flash-lite 收到中文寫的「翻譯成英文」時，會把中文原文
    # 原樣吐回來（每次都是）；指令改用英文寫才會真的翻。所以除了原文本身，
    # 提示詞裡不能有任何中文字
    captured = {}

    def fake(prompt):
        captured["prompt"] = prompt
        return "x"

    source = "小林負責補寫文件"
    Translator(generate=fake).translate(source, target)
    instructions = captured["prompt"].replace(source, "")
    assert not re.search(r"[一-鿿]", instructions)


def test_prompt_forbids_romanizing_names():
    # 英文指令下模型會把「小林」譯成 Kobayashi，得明講人名照抄
    captured = {}

    def fake(prompt):
        captured["prompt"] = prompt
        return "x"

    Translator(generate=fake).translate("小林負責", "en")
    assert "romanize" in captured["prompt"]


def _scripted(*replies):
    calls = []

    def fake(prompt):
        calls.append(prompt)
        return replies[min(len(calls), len(replies)) - 1]

    return fake, calls


def test_echoed_source_is_retried_once():
    # 實測提示詞修好後 flash-lite 仍偶爾原樣吐回原文，重試一次通常就翻了
    fake, calls = _scripted("小林負責補寫文件", "小林 will write the docs.")
    assert Translator(generate=fake).translate("小林負責補寫文件", "en") == "小林 will write the docs."
    assert len(calls) == 2


def test_echo_twice_raises_instead_of_passing_source_off_as_translation():
    fake, calls = _scripted("決議下週三前修好")
    with pytest.raises(TranslateError):
        Translator(generate=fake).translate("決議下週三前修好", "en")
    assert len(calls) == 2


def test_source_already_in_target_language_is_not_retried():
    # 英文講者那段要翻成英文，回傳原文是對的，不該多花一次請求
    fake, calls = _scripted("OK, that's it.")
    assert Translator(generate=fake).translate("OK, that's it.", "en") == "OK, that's it."
    assert len(calls) == 1


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
