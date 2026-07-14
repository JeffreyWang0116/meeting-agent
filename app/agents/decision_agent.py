"""Decision Agent：檢索與決策模組。

把非結構化的會議文字交給 Gemini，產出符合 MeetingAnalysis schema 的
結構化 JSON。驗證失敗會把錯誤回饋給模型重試。

RAG（向量資料庫檢索專案上下文）預計 10 月導入：屆時在 build_prompt 前
檢索相關歷史紀錄、拼進 prompt 即可，介面不需變動。
"""
from __future__ import annotations

import re
from datetime import date

from pydantic import ValidationError

from app.gemini_keys import KeyPool, call_with_rotation
from app.glossary import glossary_prompt_line
from app.models import MeetingAnalysis


class DecisionAgentError(Exception):
    pass


_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

_SCHEMA_EXAMPLE = """{
  "meeting": {
    "title": "會議標題（從內容歸納）",
    "date": "YYYY-MM-DD",
    "summary": "3~5 句繁體中文摘要",
    "attendees": ["發言或被提及在場的人名"]
  },
  "decisions": [
    {"description": "已定案的決議", "context": "決策背景或原因，沒有就填 null"}
  ],
  "todos": [
    {
      "task": "具體的代辦事項",
      "owner": "負責人名字，無法確定填 null",
      "due_date": "YYYY-MM-DD，無法確定填 null",
      "priority": "high 或 medium 或 low",
      "priority_reason": "一句話說明優先級判斷依據，不明顯就填 null",
      "source_quote": "逐字稿中的原句（可截斷）"
    }
  ],
  "pending_items": [
    {"topic": "議而未決的議題", "reason": "未決原因，沒有就填 null"}
  ],
  "tags": ["2~4 個簡短分類標籤（2~6 字），例如：產品、客戶會議、週會"]
}"""

_WEEKDAY_ZH = "一二三四五六日"

# 錄音種類 → 分析重點提示。輸出結構（schema）不變，只調整內容重點，
# 讓任務庫、跨會議問答等下游功能對所有種類一體適用。
KIND_HINTS = {
    "會議": "這是一場多人會議：完整萃取決議、代辦與未決事項。",
    "通話": "這是一通通話（通常只有兩位講者）：重點放在雙方承諾的事項與後續行動；正式決議通常較少。",
    "訪談": "這是一場訪談：摘要應整理受訪者的重點回答與觀點（問答要點）；代辦通常是訪談後的跟進事項。",
    "語音備忘錄": "這是個人語音備忘錄：通常只有一位講者，重點是講者自己的待辦與想法；attendees 可留空、決議通常沒有。",
    "講座": "這是一場講座或課程：摘要應條列講者的重點內容與知識點；通常沒有決議；代辦是聽眾要跟進的行動（如作業、延伸閱讀）。",
    "其它": "種類不明：依內容自行判斷重點。",
}

PROMPT_TEMPLATE = """你是「主動式會議 Agent」的決策模組。以下是一場會議的逐字稿或文字紀錄，內容可能中英夾雜、口語且混亂。請仔細閱讀並萃取結構化資訊。

會議日期：{meeting_date}（星期{weekday}）{kind_line}{glossary_line}

務必遵守的規則：
1. 只輸出一個 JSON 物件。不要 markdown 圍欄、不要任何額外說明文字。
2. 所有相對日期（「下週五」「月底前」「後天」等）必須以上面的會議日期為基準，換算成 YYYY-MM-DD 絕對日期；無法確定具體日期時 due_date 填 null，禁止猜測。
3. 找不到明確負責人的代辦事項，owner 填 null，並同時在 pending_items 加入一筆「需指派負責人」的說明。
4. 每個代辦事項的 source_quote 必須引用紀錄中的原句（可截斷），方便人工核對。
5. 只記錄紀錄中真實出現的內容，禁止編造。討論過但沒有結論的議題放入 pending_items。
6. priority 依急迫性與影響程度判斷：high / medium / low，並在 priority_reason 用一句話說明判斷依據。
7. 摘要與說明使用繁體中文；人名與專有名詞（如工具、技術名）保留原文寫法。
8. attendees 列出所有發言者或被明確提及在場的人；逐字稿若有「講者A」等標註，盡量從上下文推斷真實名字。
9. 同一件事在會議中被提到多次時，只輸出一筆代辦，把補充資訊（負責人、期限）合併進去，禁止重複。

JSON 結構（欄位名稱與型別必須完全一致）：
{schema}

會議紀錄：
---
{transcript}
---"""

_RETRY_SUFFIX = """

注意：上一次的輸出無法解析，錯誤如下：
{error}
請修正並重新只輸出一個符合上述結構的 JSON 物件。"""


def build_prompt(
    transcript: str,
    meeting_date: date,
    kind: str | None = None,
    glossary: list[dict] | None = None,
) -> str:
    kind_line = ""
    if kind:
        kind_line = f"\n錄音種類：{kind}。{KIND_HINTS.get(kind, '')}"
    glossary_line = ""
    terms = glossary_prompt_line(glossary or [])
    if terms:
        glossary_line = f"\n已知詞彙表（輸出的人名與專有名詞一律以此寫法為準）：{terms}。"
    return PROMPT_TEMPLATE.format(
        meeting_date=meeting_date.isoformat(),
        weekday=_WEEKDAY_ZH[meeting_date.weekday()],
        kind_line=kind_line,
        glossary_line=glossary_line,
        schema=_SCHEMA_EXAMPLE,
        transcript=transcript,
    )


def strip_code_fence(raw: str) -> str:
    return _CODE_FENCE.sub("", raw.strip())


class DecisionAgent:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-flash-latest",
        generate=None,
        max_attempts: int = 3,
        api_keys=None,
        glossary=None,
    ):
        # 多把 key 輪替（429 換下一把）；單把 api_key 為向後相容寫法
        self._pool = KeyPool(api_keys if api_keys else [api_key])
        self.api_key = self._pool.first
        self.model = model
        self.max_attempts = max_attempts
        # 可注入 callable(prompt) -> str，測試時不需要真的呼叫 Gemini
        self._generate = generate or self._generate_with_gemini
        # callable() -> list[dict]：自訂詞彙表，每次分析時讀最新內容
        self._glossary = glossary

    def _generate_with_gemini(self, prompt: str) -> str:
        if not self._pool:
            raise DecisionAgentError(
                "未設定 GEMINI_API_KEY：請到 https://aistudio.google.com/apikey "
                "取得金鑰並填入專案根目錄的 .env 檔"
            )
        return call_with_rotation(self._pool, lambda key: self._call_gemini(key, prompt))

    def _call_gemini(self, key: str, prompt: str) -> str:
        from google import genai

        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"response_mime_type": "application/json", "temperature": 0.2},
        )
        return response.text or ""

    def analyze(
        self, transcript: str, meeting_date: date | None = None, kind: str | None = None
    ) -> MeetingAnalysis:
        meeting_date = meeting_date or date.today()
        base_prompt = build_prompt(
            transcript,
            meeting_date,
            kind=kind,
            glossary=self._glossary() if self._glossary else None,
        )

        prompt = base_prompt
        last_error: Exception | None = None
        for _ in range(self.max_attempts):
            raw = self._generate(prompt)
            try:
                return MeetingAnalysis.model_validate_json(strip_code_fence(raw))
            except (ValidationError, ValueError) as exc:
                last_error = exc
                prompt = base_prompt + _RETRY_SUFFIX.format(error=exc)

        raise DecisionAgentError(
            f"LLM 連續 {self.max_attempts} 次無法產出合法的 JSON：{last_error}"
        )
