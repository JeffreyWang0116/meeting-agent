"""Decision Agent：檢索與決策模組。

把非結構化的會議文字交給 Gemini，產出符合 MeetingAnalysis schema 的
結構化 JSON。驗證失敗會把錯誤回饋給模型重試。

RAG（向量資料庫檢索專案上下文）預計 10 月導入：屆時在 build_prompt 前
檢索相關歷史紀錄、拼進 prompt 即可，介面不需變動。
"""
from __future__ import annotations

import json
import re
from datetime import date

from pydantic import ValidationError

from app.gemini_keys import KeyPool, call_with_rotation
from app.glossary import glossary_prompt_line
from app.models import MeetingAnalysis


class DecisionAgentError(Exception):
    pass


_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

# 可由使用者勾選開關的功能；title/date/attendees/pending_items/tags 是基本
# 欄位，不受 features 控制，任何錄音種類都會產生
FEATURE_KEYS = {"summary", "decisions", "todos", "highlights"}


def _schema_example(features: set[str]) -> str:
    meeting: dict = {"title": "會議標題（從內容歸納）", "date": "YYYY-MM-DD"}
    if "summary" in features:
        meeting["summary"] = "3~5 句繁體中文摘要"
    meeting["attendees"] = ["發言或被提及在場的人名"]

    schema: dict = {"meeting": meeting}
    if "decisions" in features:
        schema["decisions"] = [
            {"description": "已定案的決議", "context": "決策背景或原因，沒有就填 null"}
        ]
    if "todos" in features:
        schema["todos"] = [
            {
                "task": "具體的代辦事項",
                "owner": "負責人名字，無法確定填 null",
                "due_date": "YYYY-MM-DD，無法確定填 null",
                "priority": "high 或 medium 或 low",
                "priority_reason": "一句話說明優先級判斷依據，不明顯就填 null",
                "source_quote": "逐字稿中的原句（可截斷）",
            }
        ]
    if "highlights" in features:
        schema["highlights"] = [
            {
                "text": "會議重點（一句話）",
                "time": "該重點出處行首的時間標記，如 1:02；逐字稿沒有時間標記就填 null",
                "source_quote": "逐字稿中該重點的原句（可截斷）",
            }
        ]
    schema["pending_items"] = [{"topic": "議而未決的議題", "reason": "未決原因，沒有就填 null"}]
    schema["tags"] = ["2~4 個簡短分類標籤（2~6 字），例如：產品、客戶會議、週會"]
    return json.dumps(schema, ensure_ascii=False, indent=2)


_WEEKDAY_ZH = "一二三四五六日"

# 錄音種類 → 分析重點提示。輸出結構（schema）不變，只調整內容重點，
# 讓任務庫、跨會議問答等下游功能對所有種類一體適用。
KIND_HINTS = {
    "一般會議": "一般性的多人會議：完整萃取決議、代辦與未決事項。",
    "團隊站會": "每日站會／每週同步：摘要壓到三句以內，逐人整理「進度／接下來要做／卡住的地方」。阻礙（blocker）一律視為高優先代辦；例行進度報告不是決議，不要寫進 decisions。",
    "專案進度會議": "專案進度會議：摘要聚焦「原訂 vs 實際」的落差。重點放在里程碑狀態、延誤原因、風險與因應方式；時程或範圍的變更算決議。",
    "專案啟動會": "專案啟動會（kickoff）：摘要要交代專案目標、範圍，以及明確排除不做的事。重點放在角色分工、里程碑時程與成功指標；範圍界定與分工都算決議。",
    "需求訪談": "需求訪談／客戶發現：摘要用受訪者自己的話整理痛點與需求。重點放在現況問題、期望與限制條件；未經對方確認的推論一律放 pending_items，不要當成需求。代辦是訪談後的跟進事項。",
    "設計技術評審": "設計／技術評審：摘要先說明被評的方案在解什麼問題。重點放在提出的選項與各自取捨；決議務必連同「為什麼選它」一起寫進 context。未解決的疑慮放 pending_items。",
    "回顧會議": "回顧會議（retrospective）：摘要分成「做得好」與「待改善」兩段。重點擷取雙方的具體事例而非籠統感想；改善行動一律列成代辦並指定負責人，沒人認領就放 pending_items。",
    "一對一": "一對一：摘要聚焦當事人的目標、遭遇的困難與獲得的回饋。重點放在雙方的承諾事項，決議通常沒有。內容私密，只記錄實際說出口的話，禁止推測情緒或加上評價。",
    "銷售拜訪": "銷售拜訪／客戶會議：摘要要回答 BANT——預算、決策權責、需求、時程，問不到的那項就明說沒談到。重點放在客戶提出的異議與我方回應，以及明確的下一步（demo、報價、再約時間）。客戶與我方的承諾都要列成代辦。",
    "面試": "面試：摘要按評估面向整理（技術能力、相關經驗、溝通、動機），每個面向都要附上對話中的具體佐證。重點放在候選人回答的關鍵事例。禁止輸出錄取建議或對人的主觀評價，只整理事實與雙方待辦。",
    "教育訓練": "教育訓練／講座／課程：摘要條列講者的知識點與結論。重點是可帶走的觀念與範例，通常沒有決議；代辦是聽眾要跟進的行動（作業、延伸閱讀、練習）。",
    "腦力激盪": "腦力激盪：摘要說明發想的題目與收斂方向。重點是被提出的點子，相近的合併成一條，少數人提的也不要漏掉；點子在被明確採納前一律放 pending_items 而不是決議。",
    "事故檢討": "事故檢討（postmortem）：摘要依「發生什麼／影響範圍／怎麼解掉」三段整理。重點依時間順序還原事件時間軸。已確認的根因寫進決議，還在推測的放 pending_items；預防措施列成代辦。只描述系統與流程，禁止指名究責。",
    "全體會議": "全體會議／公司宣達：摘要條列宣布的事項與政策變更。重點放在對聽眾有實質影響的內容，以及 Q&A 中主管的回答；已定案的公司決定算決議，代辦是聽眾要配合的事。",
    "語音備忘錄": "個人語音備忘錄：通常只有一位講者，重點是講者自己的待辦與想法；attendees 可留空、決議通常沒有。",
    "其它": "種類不明：依內容自行判斷重點。",
}

# 舊資料相容：改版前存下來的會議帶的是「錄音種類」。這些值不再出現在選單裡，
# 但既有紀錄仍要能讀取、重新分析與編輯，所以保留對應關係。
LEGACY_KINDS = {
    "會議": "一般會議",
    "通話": "一般會議",
    "訪談": "需求訪談",
    "講座": "教育訓練",
}

MEETING_KINDS = set(KIND_HINTS)

# 各種類預設產生哪些區塊；沒列到的＝四項全開。
# 挑掉的是那個種類「本來就不會有」的東西（站會沒有正式決議、面試不該有代辦決議），
# 使用者仍可自己勾回來。
KIND_DEFAULT_FEATURES = {
    "團隊站會": {"summary", "todos"},
    "需求訪談": {"summary", "highlights", "todos"},
    "一對一": {"summary", "highlights", "todos"},
    "銷售拜訪": {"summary", "highlights", "todos"},
    "面試": {"summary", "highlights"},
    "教育訓練": {"summary", "highlights", "todos"},
    "腦力激盪": {"summary", "highlights"},
    "語音備忘錄": {"summary", "todos"},
    "其它": {"summary", "highlights", "todos"},
}


# 下拉選單的分組與排序。前端直接拿這份拼 optgroup，
# 不在 HTML 裡再拄一份名單（兩邊各維護一份必定會不同步）
KIND_GROUPS = [
    ("常用", ["一般會議", "團隊站會", "一對一", "語音備忘錄"]),
    ("專案", ["專案啟動會", "專案進度會議", "設計技術評審", "回顧會議", "事故檢討"]),
    ("對外", ["需求訪談", "銷售拜訪", "面試"]),
    ("其他", ["教育訓練", "腦力激盪", "全體會議", "其它"]),
]

DEFAULT_KIND = "一般會議"


def resolve_kind(kind: str | None) -> str | None:
    """把舊的錄音種類值換成對應的會議種類；新值與 None 原樣回傳。"""
    return LEGACY_KINDS.get(kind, kind)


def default_features_for_kind(kind: str | None) -> set[str]:
    if kind is None:
        return set(FEATURE_KEYS)
    return set(KIND_DEFAULT_FEATURES.get(resolve_kind(kind), FEATURE_KEYS))

PROMPT_TEMPLATE = """你是「會議助手」的決策模組。以下是一場會議的逐字稿或文字紀錄，內容可能中英夾雜、口語且混亂。請仔細閱讀並萃取結構化資訊。

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
10. highlights（會議重點）挑出整場最關鍵的 3~8 個時刻，依時間順序排列；time 一律照抄該重點出處行首方括號內的時間標記（如逐字稿有「[1:02]」就填「1:02」），逐字稿完全沒有時間標記時填 null，禁止自行推算或編造時間。
{feature_note}
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
    features: set[str] | None = None,
    extra_terms: list[dict] | None = None,
) -> str:
    # features=None：向後相容，等同全部功能都開（沒有勾選框限制的舊行為）
    features = FEATURE_KEYS if features is None else features
    kind_line = ""
    if kind:
        resolved = resolve_kind(kind)
        kind_line = f"\n會議種類：{resolved}。{KIND_HINTS.get(resolved, '')}"
    glossary_line = ""
    # 本次專用詞彙排在全域詞彙表後面：同一個詞若兩邊都有，後出現的寫法更貼近這場會議
    terms = glossary_prompt_line((glossary or []) + (extra_terms or []))
    if terms:
        glossary_line = f"\n已知詞彙表（輸出的人名與專有名詞一律以此寫法為準）：{terms}。"
    disabled = FEATURE_KEYS - features
    feature_note = f"\n11. 這次不需要 {'、'.join(sorted(disabled))} 欄位，不要輸出。" if disabled else ""
    return PROMPT_TEMPLATE.format(
        meeting_date=meeting_date.isoformat(),
        weekday=_WEEKDAY_ZH[meeting_date.weekday()],
        kind_line=kind_line,
        glossary_line=glossary_line,
        feature_note=feature_note,
        schema=_schema_example(features),
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
        self,
        transcript: str,
        meeting_date: date | None = None,
        kind: str | None = None,
        features: set[str] | None = None,
        extra_terms: list[dict] | None = None,
    ) -> MeetingAnalysis:
        meeting_date = meeting_date or date.today()
        # features=None：向後相容，等同全部功能都開
        features = FEATURE_KEYS if features is None else features
        base_prompt = build_prompt(
            transcript,
            meeting_date,
            kind=kind,
            glossary=self._glossary() if self._glossary else None,
            features=features,
            extra_terms=extra_terms,
        )

        prompt = base_prompt
        last_error: Exception | None = None
        for _ in range(self.max_attempts):
            raw = self._generate(prompt)
            try:
                analysis = MeetingAnalysis.model_validate_json(strip_code_fence(raw))
                return self._enforce_features(analysis, features)
            except (ValidationError, ValueError) as exc:
                last_error = exc
                prompt = base_prompt + _RETRY_SUFFIX.format(error=exc)

        raise DecisionAgentError(
            f"LLM 連續 {self.max_attempts} 次無法產出合法的 JSON：{last_error}"
        )

    @staticmethod
    def _enforce_features(analysis: MeetingAnalysis, features: set[str]) -> MeetingAnalysis:
        """防禦性保護：不管 LLM 有沒有聽話，沒開的功能一律強制清空。"""
        if "summary" not in features:
            analysis.meeting.summary = None
        if "decisions" not in features:
            analysis.decisions = []
        if "todos" not in features:
            analysis.todos = []
        if "highlights" not in features:
            analysis.highlights = []
        return analysis
