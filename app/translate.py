"""文字翻譯（Gemini）：即時聆聽逐段翻譯與結果翻譯共用。

用文字翻譯而非轉錄端翻譯的原因：本地 Whisper 只能外語→英文，
Gemini 文字翻譯則兩個轉錄後端都通用，中↔英雙向。
"""
from __future__ import annotations

import re

from app.gemini_keys import KeyPool, call_with_model_fallback

TARGETS = {"en": "English", "zh": "Traditional Chinese (Taiwan)"}

# 指令刻意用英文寫：gemini-3.5-flash-lite 收到中文寫的「翻譯成英文」時，
# 會把中文原文原樣吐回來（實測每次都是），英文指令才會真的翻
_PROMPT = """Translate the following transcript into {target}.

Rules:
1. Translate line by line; the output must have the same number of lines as the source.
2. Some lines may begin with a speaker label followed by a colon (for example "Speaker A:" or "Jenny:"). Keep such a label unchanged and translate the rest of the line. Lines without a label are translated in full.
3. Leave people's names and proper nouns in their original script (do not romanize them), but translate every other word of the sentence.
4. Output only the translation, with no explanations or headings. Never return the source text untranslated.

The source text is between <source> tags.
<source>
{text}
</source>"""

_CJK = re.compile(r"[一-鿿]")
_LATIN = re.compile(r"[A-Za-z]")


def _needs_translation(text: str, target: str) -> bool:
    """原文裡有沒有要翻的字。沒有的話（英文那段要翻成英文），回傳原文是對的。"""
    return bool(_CJK.search(text)) if target == "en" else bool(_LATIN.search(text))


class TranslateError(Exception):
    pass


class Translator:
    def __init__(
        self,
        api_key: str | None = None,
        api_keys=None,
        on_call=None,
        model: str = "gemini-3.5-flash-lite",
        generate=None,
    ):
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        # 每打一次 API 就回報一次，用量統計才會算到重試與換金鑰
        self._on_call = on_call
        self.model = model
        # 可注入 callable(prompt) -> str，測試時不需要真的呼叫 Gemini
        self._generate = generate or self._generate_with_gemini

    def translate(self, text: str, target: str) -> str:
        if target not in TARGETS:
            raise ValueError(f"target 只支援：{'、'.join(sorted(TARGETS))}")
        text = text.strip()
        if not text:
            return ""
        prompt = _PROMPT.format(target=TARGETS[target], text=text)
        # 提示詞修好後 flash-lite 仍偶爾把原文原樣吐回來，重試一次；
        # 兩次都沒翻就報錯，不能把原文當成譯文交出去
        for _ in range(2):
            result = (self._generate(prompt) or "").strip()
            if result != text or not _needs_translation(text, target):
                return result
        raise TranslateError("模型沒有翻譯，回傳的是原文，請再試一次")

    def _generate_with_gemini(self, prompt: str) -> str:
        if not self._pool:
            raise TranslateError("未設定 GEMINI_API_KEY：翻譯需要 Gemini 金鑰")
        # 過載時自動改用另一個模型（見 call_with_model_fallback）
        return call_with_model_fallback(
            self._pool,
            self.model,
            lambda key, model: self._call_gemini(key, prompt, model),
            on_call=self._on_call,
        )

    def _call_gemini(self, key: str, prompt: str, model: str | None = None) -> str:
        from google import genai

        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=model or self.model, contents=prompt
        )
        return response.text or ""
