"""Corrector Agent：轉錄後的錯字校正模組。

語音辨識常把同音字聽錯（「函式」→「涵式」、人名「林佳蓉」→「林家容」）。
自訂詞彙表（glossary）只能逐詞比對發音，無法靠上下文判斷；這個 Agent 補上
那一段：把整份逐字稿交給 LLM，用上下文找出聽錯的字詞。

**為什麼是「回傳修正清單」而不是「回傳整份改好的逐字稿」**：
1. 整份重寫會讓模型順手做它沒被要求的事——刪贅字、合併句子、拿掉行首的
   [1:02] 時間標記與講者標籤。時間標記一旦掉了，會議重點的點擊跳轉就壞了。
2. 輸出 token 從「整份逐字稿」降到「幾十筆修正」，長會議差好幾倍。
3. 可稽核：使用者看得到到底改了哪些字，而不是拿到一份不知道哪裡變了的文字。

修正一律在本地用字串取代套用，並逐條驗證（見 apply_corrections），模型
講錯或亂回傳時最多是「沒改到」，不會把逐字稿弄壞。
"""
from __future__ import annotations

import json
import re

from app.gemini_keys import KeyPool, call_with_model_fallback
from app.glossary import glossary_prompt_line
from app.stores.base import DEFAULT_USER

_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

# 行首時間標記（與 live_session._TIME_PREFIX_RE、前端 TIME_RE 對齊）
_TIME_MARKER = re.compile(r"\[\d{1,2}(?::\d{2}){1,2}\]")

# 單筆修正的長度上限：校正的對象是字詞，不是整句。超過這個長度多半代表
# 模型想改寫句子，直接擋掉
MAX_TERM_LEN = 60
# 單次校正的筆數上限：正常一場會議錯字不會超過這個量級，超過代表模型失控
MAX_CORRECTIONS = 100

# 人名與稱謂一律不改。實測交通部預算協商：「曾清農」（其實是陳清龍）每跑一次就被改成
# 不同的立委——陳菁徽、曾玟培、陳素月、林楚茵——詞彙表是空的也照編；使用者回報的
# 「陳總召」被改成詞彙表裡的「陳澤安」是同一類。提示寫了 lite 仍會犯，所以本地再擋：
# - 模型自報 type 是人名／稱謂
# - 理由提到人名（type 不可靠，模型常把改人名標成一般字詞）
# - 稱謂在修正前後不一致（「陳總召」→「陳澤安」、「原長」→「主席」）
# - 改成或改掉詞彙表裡的人名（詞彙表只影響轉錄聽寫）
_NAME_TYPES = {"人名", "姓名", "稱謂", "職稱"}
_NAME_REASON = re.compile(r"人名|姓名|名字|立委")
_TITLE = re.compile(
    r"總召|召委|委員|議員|議長|部長|次長|署長|局長|處長|司長|院長|主委|主席|總統|"
    r"市長|縣長|首長|總裁|董事長|經理|教授|老師|先生|女士|小姐"
)
_PERSON_NOTE = re.compile(r"人名|姓名|人物")

PROMPT_TEMPLATE = """你是會議逐字稿的錯字校正模組。以下逐字稿由語音辨識產生，可能把同音或近音的字詞聽錯。請用上下文判斷，找出被聽錯的字詞。

務必遵守的規則：
1. 只輸出一個 JSON 物件。不要 markdown 圍欄、不要任何額外說明文字。
2. 只修正「聽錯的字詞」：同音／近音錯字、專有名詞（機關、產品、術語）誤植。禁止潤稿、禁止改寫句子、禁止刪除口語贅詞（「呃」「就是」等一律保留）。
3. 人名與稱謂（總召、委員、部長、主席、院長等）一律不要修改，即使你認為聽錯也不要改：你無從確認現場是誰，換成你知道的人名只會編出不在場的人。
4. wrong 必須是逐字稿中原封不動出現過的字串，且要短（只包含錯的字詞本身，最多 {max_len} 字）。
5. 絕對不要修改：行首的時間標記（如 [1:02]）、講者標籤（如「講者A：」「Kevin：」）、標點符號。
6. 語意沒有明顯錯誤時就不要改。沒有任何需要修正的地方，corrections 回傳空陣列。
7. 每筆修正在 reason 用一句話說明為什麼判斷是聽錯（例如「同音字，上下文在談程式」），type 填「一般字詞」或「專有名詞」。
{glossary_line}
JSON 結構：
{{
  "corrections": [
    {{"wrong": "逐字稿中的錯字", "right": "正確的寫法", "reason": "判斷依據", "type": "一般字詞"}}
  ]
}}

逐字稿：
---
{transcript}
---"""


class CorrectorAgent:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-3.5-flash-lite",
        generate=None,
        api_keys=None,
        on_call=None,
        glossary=None,
    ):
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        # 每打一次 API 就回報一次，用量統計才會算到重試與換金鑰
        self._on_call = on_call
        self.api_key = self._pool.first
        self.model = model
        # 可注入 callable(prompt) -> str，測試時不需要真的呼叫 Gemini
        self._generate = generate or self._generate_with_gemini
        # callable() -> list[dict]：自訂詞彙表，每次校正時讀最新內容
        self._glossary = glossary

    # ---- 對外介面 ----

    def correct(self, transcript: str, user: str = DEFAULT_USER) -> tuple[str, list[dict]]:
        """回傳 (校正後的逐字稿, 實際套用的修正清單)。

        任何一步出錯都回傳原文＋空清單：校正是加分項，不該擋住整個分析流程。
        """
        if not transcript or not transcript.strip():
            return transcript, []
        try:
            terms = self._glossary(user) if self._glossary else []
            raw = self._generate(self.build_prompt(transcript, user))
            corrections = _parse_corrections(raw)
        except Exception:
            return transcript, []
        names = {t["term"] for t in terms if _PERSON_NOTE.search(t.get("note") or "")}
        return apply_corrections(transcript, corrections, protected_names=names)

    def build_prompt(self, transcript: str, user: str = DEFAULT_USER) -> str:
        glossary_line = ""
        terms = glossary_prompt_line(self._glossary(user) if self._glossary else [])
        if terms:
            # 舊版寫「人名一律以此寫法為準，聽到相近發音卻寫成別的字就要修正」，
            # 正是把「陳總召」改成詞彙表裡「陳澤安」的推手
            glossary_line = (
                f"8. 已知詞彙表（專有名詞的正確寫法；其中的人名不可拿來修改逐字稿）：{terms}。\n"
            )
        return PROMPT_TEMPLATE.format(
            max_len=MAX_TERM_LEN, glossary_line=glossary_line, transcript=transcript
        )

    # ---- 內部 ----

    def _generate_with_gemini(self, prompt: str) -> str:
        if not self._pool:
            return '{"corrections": []}'  # 沒金鑰就等同「沒有要修正的地方」
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
            model=model or self.model,
            contents=prompt,
            # temperature=0：校正是機械性工作，不要創意
            config={"response_mime_type": "application/json", "temperature": 0.0},
        )
        return response.text or ""


def _parse_corrections(raw: str) -> list[dict]:
    data = json.loads(_CODE_FENCE.sub("", (raw or "").strip()))
    items = data.get("corrections") if isinstance(data, dict) else data
    return [x for x in (items or []) if isinstance(x, dict)]


def _touches_names(item: dict, wrong: str, right: str, protected_names) -> bool:
    """這筆修正是不是在改人名或稱謂（見 _NAME_TYPES 上方的說明）。"""
    if str(item.get("type") or "").strip() in _NAME_TYPES:
        return True
    if _NAME_REASON.search(str(item.get("reason") or "")):
        return True
    if sorted(_TITLE.findall(wrong)) != sorted(_TITLE.findall(right)):
        return True
    return any(name in right or name in wrong for name in protected_names)


def _is_safe(wrong: str, right: str, transcript: str) -> bool:
    """逐條驗證模型給的修正，擋掉會破壞逐字稿結構的項目。"""
    if not wrong or not right or wrong == right:
        return False
    if len(wrong) > MAX_TERM_LEN or len(right) > MAX_TERM_LEN:
        return False
    # 跨行取代會把兩行併成一行，破壞「一句一行」的時間軸結構
    if "\n" in wrong or "\n" in right:
        return False
    # 碰到時間標記就拒絕：時間軸是會議重點跳轉的依據，不接受任何改動
    if _TIME_MARKER.search(wrong) or _TIME_MARKER.search(right):
        return False
    return wrong in transcript


def apply_corrections(
    transcript: str, corrections: list[dict], protected_names=frozenset()
) -> tuple[str, list[dict]]:
    """在本地套用修正清單，回傳 (新逐字稿, 實際生效的修正)。

    模型只負責「找出哪裡錯」，實際改動由這裡執行——這樣逐字稿的行結構、
    時間標記與講者標籤都在我們自己的控制下，模型無法間接改掉它們。
    """
    text = transcript
    applied: list[dict] = []
    for item in corrections[:MAX_CORRECTIONS]:
        wrong = str(item.get("wrong") or "")
        right = str(item.get("right") or "")
        if not _is_safe(wrong, right, text):
            continue
        if _touches_names(item, wrong, right, protected_names):
            continue
        count = text.count(wrong)
        text = text.replace(wrong, right)
        applied.append(
            {
                "wrong": wrong,
                "right": right,
                "reason": str(item.get("reason") or ""),
                "count": count,
            }
        )

    # 最終保險：行數與時間標記數量都不該變。任一不符就整批放棄，回傳原文——
    # 寧可不校正，也不要交出一份結構被動過的逐字稿
    if text.count("\n") != transcript.count("\n") or len(
        _TIME_MARKER.findall(text)
    ) != len(_TIME_MARKER.findall(transcript)):
        return transcript, []
    return text, applied
