"""文字翻譯（Gemini）：即時聆聽逐段翻譯與結果翻譯共用。

用文字翻譯而非轉錄端翻譯的原因：本地 Whisper 只能外語→英文，
Gemini 文字翻譯則兩個轉錄後端都通用，中↔英雙向。
"""
from __future__ import annotations

from app.gemini_keys import KeyPool, call_with_rotation

TARGETS = {"en": "英文", "zh": "繁體中文"}

_PROMPT = """把以下逐字稿翻譯成{target}。

規則：
1. 逐行翻譯，輸出行數與原文相同。
2. 行首的講者標註（如「講者A：」「小明：」）保留原樣，只翻譯冒號後的內容。
3. 人名與專有名詞保留原文寫法。
4. 只輸出譯文，不要任何說明或標題。

原文：
{text}"""


class TranslateError(Exception):
    pass


class Translator:
    def __init__(
        self,
        api_key: str | None = None,
        api_keys=None,
        model: str = "gemini-flash-lite-latest",
        generate=None,
    ):
        self._pool = KeyPool(api_keys if api_keys else [api_key])
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
        return (self._generate(prompt) or "").strip()

    def _generate_with_gemini(self, prompt: str) -> str:
        if not self._pool:
            raise TranslateError("未設定 GEMINI_API_KEY：翻譯需要 Gemini 金鑰")
        return call_with_rotation(self._pool, lambda key: self._call_gemini(key, prompt))

    def _call_gemini(self, key: str, prompt: str) -> str:
        from google import genai

        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=self.model, contents=prompt, config={"temperature": 0.1}
        )
        return response.text or ""
