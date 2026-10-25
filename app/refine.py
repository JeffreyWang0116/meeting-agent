"""用一句話請 AI 修改分析結果：「把摘要改短一點」「第三個任務的負責人是小明」。

為什麼不直接重跑整份分析：重新分析會把這場會議的任務整批換新 id（狀態、手動
編輯都跟著不見），而且要花一次完整的分析額度。改一個負責人不該有那種代價。

所以模型只回「要改哪些欄位」的修改單（patch），本地套用。模型不改資料本身，
它提議、我們驗證、使用者確認。三道關卡：

1. 白名單：只有這裡列出的欄位能改。模型回了別的（transcript、user…）整份退掉
2. 型別與值域：日期要像日期、優先級只能是那三個；不合法不是「盡量套」而是報錯
3. 預覽：套用結果先回給前端顯示 before/after，使用者按了確定才寫進資料庫

逐字稿永遠不在白名單裡。它是事實紀錄，讓模型重寫等於讓它偽造證據，而且引用
原句會跟著對不回出處。要改逐字稿走就地修正（app/static/js/transcriptedit.js）。
"""
from __future__ import annotations

import json
import re
from datetime import date

_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

# 可以改的欄位，以及顯示給使用者看的名稱
MEETING_FIELDS = {"title": "標題", "summary": "摘要"}
TASK_FIELDS = {
    "task": "任務名稱",
    "owner": "負責人",
    "due_date": "期限",
    "priority": "優先級",
}
# 整份取代的清單欄位（模型給新的全部內容，不做逐項 diff）
LIST_FIELDS = {
    "decisions": "決議",
    "pending_items": "待確認事項",
    "tags": "標籤",
    "sections": "種類專屬欄位",
}
PRIORITIES = {"high", "medium", "low"}


class RefineError(Exception):
    """模型回的修改單不能用（格式壞掉、碰了不該碰的欄位、值不合法）。"""


PROMPT = """你是「會議助手」的修改模組。使用者看著一份會議分析結果，提出了一個修改要求。
你的工作不是重做分析，而是判斷「要改哪幾個欄位」，並只回傳那些欄位。

使用者的要求：
{request}

目前的分析結果：
{current}

這場會議的任務（改任務時用 id 指名是哪一筆）：
{tasks}

務必遵守：
1. 只輸出一個 JSON 物件，不要 markdown 圍欄、不要任何說明文字。
2. 只放「真的需要改」的欄位。沒有要改的欄位一律不要出現。
3. 可以改的只有這些：
   - meeting：title（標題）、summary（摘要）
   - tasks：每筆用 {{"id": "<任務 id>", ...}} 指名，可改 task、owner、due_date、priority
   - decisions、pending_items、tags、sections：要改就給「改完後的完整清單」
4. due_date 一律 YYYY-MM-DD；不知道確切日期就不要改這個欄位。priority 只能是 high / medium / low。
5. 禁止輸出 transcript（逐字稿）。逐字稿是事實紀錄，不在你能改的範圍；
   使用者若要求改逐字稿，回傳 {{}} 即可。
6. 只做使用者要求的改動。不要順手「優化」其他欄位——使用者沒要求的改動他不會發現。
7. 看不懂要求、或要改的不在上面的範圍內，回傳 {{}}。

現在輸出 JSON："""


def refine_prompt(request: str, record: dict, tasks: list[dict]) -> str:
    """組出修改用的提示詞。刻意不帶逐字稿全文：模型不需要它就能改摘要與欄位，
    帶了只是讓每次修改都多花幾千個 token。"""
    current = {
        "meeting": {k: record.get("meeting", {}).get(k) for k in ("title", "summary")},
        "decisions": record.get("decisions", []),
        "pending_items": record.get("pending_items", []),
        "tags": record.get("tags", []),
        "sections": record.get("sections", []),
    }
    slim_tasks = [
        {k: t.get(k) for k in ("id", "task", "owner", "due_date", "priority")}
        for t in tasks
    ]
    return PROMPT.format(
        request=request.strip(),
        current=json.dumps(current, ensure_ascii=False, indent=2),
        tasks=json.dumps(slim_tasks, ensure_ascii=False, indent=2),
    )


def parse_patch(raw: str) -> dict:
    """把模型的回覆解析成修改單，並擋掉不該出現的欄位。"""
    try:
        patch = json.loads(_CODE_FENCE.sub("", (raw or "").strip()))
    except (json.JSONDecodeError, TypeError) as exc:
        raise RefineError("AI 沒有回傳可用的修改內容，請換個說法再試一次") from exc
    if not isinstance(patch, dict):
        raise RefineError("AI 沒有回傳可用的修改內容，請換個說法再試一次")

    allowed = {"meeting", "tasks"} | set(LIST_FIELDS)
    unknown = set(patch) - allowed
    if unknown:
        # transcript 特別點名：它是唯一「模型可能真心想改、而我們永遠不准」的欄位
        if "transcript" in unknown:
            raise RefineError("逐字稿不能用這種方式修改，請用逐字稿的「修正」功能")
        raise RefineError(f"AI 想修改不允許的欄位：{'、'.join(sorted(unknown))}")

    meeting = patch.get("meeting") or {}
    if not isinstance(meeting, dict):
        raise RefineError("AI 回傳的會議欄位格式不正確")
    bad = set(meeting) - set(MEETING_FIELDS)
    if bad:
        raise RefineError(f"AI 想修改不允許的會議欄位：{'、'.join(sorted(bad))}")

    tasks = patch.get("tasks") or []
    if not isinstance(tasks, list):
        raise RefineError("AI 回傳的任務欄位格式不正確")
    for item in tasks:
        if not isinstance(item, dict) or not item.get("id"):
            raise RefineError("AI 沒有指明要改哪一筆任務")
        bad = set(item) - {"id"} - set(TASK_FIELDS)
        if bad:
            raise RefineError(f"AI 想修改不允許的任務欄位：{'、'.join(sorted(bad))}")
    return patch


def _check_task_value(field: str, value) -> None:
    if field == "due_date" and value is not None:
        try:
            date.fromisoformat(str(value))
        except ValueError:
            raise RefineError(f"AI 給的期限不是有效日期：{value}") from None
    if field == "priority" and value not in PRIORITIES:
        raise RefineError(f"AI 給的優先級不正確：{value}")


def apply_patch(record: dict, tasks: list[dict], patch: dict) -> tuple[dict, list[dict], list[dict]]:
    """套用修改單，回傳 (新的會議記錄, 新的任務清單, 改了哪些東西)。

    原本的 record / tasks 不會被改動——預覽階段使用者可能按取消。
    changes 是給預覽看的 before/after 清單；空的代表這次什麼都沒改到。
    """
    new_record = json.loads(json.dumps(record))
    new_tasks = json.loads(json.dumps(tasks))
    changes: list[dict] = []

    for field, label in MEETING_FIELDS.items():
        if field not in (patch.get("meeting") or {}):
            continue
        before = new_record.setdefault("meeting", {}).get(field)
        after = patch["meeting"][field]
        if before != after:
            new_record["meeting"][field] = after
            changes.append({"what": label, "before": before, "after": after})

    for field, label in LIST_FIELDS.items():
        if field not in patch:
            continue
        before, after = new_record.get(field, []), patch[field]
        if not isinstance(after, list):
            raise RefineError(f"AI 回傳的「{label}」格式不正確")
        if before != after:
            new_record[field] = after
            changes.append({"what": label, "before": _summarize(before), "after": _summarize(after)})

    by_id = {t.get("id"): t for t in new_tasks}
    for item in patch.get("tasks") or []:
        target = by_id.get(item["id"])
        if target is None:
            # 模型掰了一個 id，或指到別場會議的任務
            raise RefineError("AI 指到的任務不屬於這場會議")
        for field, label in TASK_FIELDS.items():
            if field not in item:
                continue
            _check_task_value(field, item[field])
            before, after = target.get(field), item[field]
            if before != after:
                target[field] = after
                changes.append({
                    "what": f"{target.get('task', '任務')}的{label}",
                    "before": before, "after": after,
                })
    return new_record, new_tasks, changes


def _summarize(items) -> str:
    """清單欄位的 before/after 在預覽上要讀得懂，不能是一整坨 JSON。"""
    out = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(str(item.get("description") or item.get("topic") or item.get("label") or item))
        else:
            out.append(str(item))
    return "、".join(out) or "（空）"


class Refiner:
    """把修改要求送給 Gemini，拿回修改單。與 Translator 同一套金鑰與退避機制。"""

    def __init__(
        self,
        api_key: str | None = None,
        api_keys=None,
        on_call=None,
        model: str = "gemini-3.5-flash-lite",
        generate=None,
    ):
        from app.gemini_keys import KeyPool

        self._pool = KeyPool(api_keys if api_keys else [api_key])
        self._on_call = on_call
        self.model = model
        # 可注入 callable(prompt) -> str，測試時不需要真的呼叫 Gemini
        self._generate = generate or self._generate_with_gemini

    def propose(self, request: str, record: dict, tasks: list[dict]) -> dict:
        raw = self._generate(refine_prompt(request, record, tasks))
        return parse_patch(raw)

    def _generate_with_gemini(self, prompt: str) -> str:
        from app.gemini_keys import call_with_model_fallback

        if not self._pool:
            raise RefineError("未設定 GEMINI_API_KEY：這個功能需要 Gemini 金鑰")
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
