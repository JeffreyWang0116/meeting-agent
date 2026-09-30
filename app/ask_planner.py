"""詢問會議的條件解析：把問題裡的時間、會議種類、人名變成可以在本地篩選的條件。

向量檢索聽不懂「上個月」「跟客戶那場」「王小明負責過什麼」——這些不是語意相近
的問題，而是對會議本身的篩選。所以先請模型把問題拆成結構化條件，篩選在本地做，
模型只負責理解，不負責判斷哪場會議符合。

解析失敗（格式錯、額度用完）一律退回「不套條件、用原問題檢索」：這一步是加分項，
不能讓整個提問跟著失敗。
"""
from __future__ import annotations

import json
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from app.agents.decision_agent import LEGACY_KINDS, strip_code_fence


class AskPlan(BaseModel):
    query: str
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    kinds: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    @property
    def has_filters(self) -> bool:
        return bool(self.date_from or self.date_to or self.kinds or self.people)


_PLAN_PROMPT = """你是「會議助手」的檢索規劃器。把使用者對歷史會議的提問拆成 JSON 檢索條件，只輸出 JSON。

今天是 {today}。可用的會議種類：{kinds}

欄位：
- date_from / date_to：問題明確提到時間才填（YYYY-MM-DD，含頭尾），以今天換算相對日期
  （「上個月」＝上個月 1 日到月底、「上週」＝上週一到週日、「最近」不算明確時間）；沒提到填 null
- kinds：問題明確指向某種會議才填，只能從上面的清單挑；沒提到填 []
- people：問題裡點名的人（原文寫法）；沒有填 []。「我」「我們」「客戶」不是人名
- query：拿掉時間、種類、人名條件後，真正要找的內容，改寫成適合語意檢索的一句話
- keywords：2~5 個可能逐字出現在逐字稿裡的關鍵詞

提問：{question}

輸出格式：
{{"date_from": null, "date_to": null, "kinds": [], "people": [], "query": "", "keywords": []}}"""


def build_plan_prompt(question: str, today: date, kinds: list[str]) -> str:
    return _PLAN_PROMPT.format(today=today.isoformat(), kinds="、".join(kinds), question=question)


def _clean(values) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in values or [] if isinstance(v, str) and v.strip()))


def parse_plan(raw: str | None, question: str, kinds: list[str]) -> AskPlan:
    fallback = AskPlan(query=question)
    try:
        data = json.loads(strip_code_fence(raw or ""))
        if not isinstance(data, dict):
            return fallback
        plan = AskPlan(
            query=(data.get("query") or "").strip() or question,
            date_from=data.get("date_from"),
            date_to=data.get("date_to"),
            kinds=[k for k in _clean(data.get("kinds")) if k in kinds],  # 自創種類對不到任何會議
            people=_clean(data.get("people")),
            keywords=_clean(data.get("keywords")),
        )
    except (ValueError, ValidationError, TypeError):
        return fallback
    if plan.date_from and plan.date_to and plan.date_from > plan.date_to:
        plan.date_from, plan.date_to = plan.date_to, plan.date_from
    return plan


def _person_in(name: str, known: list[str], text: str) -> bool:
    # 出席者常只寫「小明」，問的是「王小明」；單字名（「王」）會跟每個人都對上，不算
    return name in text or any(len(k) >= 2 and (name in k or k in name) for k in known)


def filter_meetings(meetings: list[dict], tasks: list[dict], plan: AskPlan) -> list[str]:
    """符合條件的會議 id（保留傳入順序）。人名要全部符合；種類符合任一個。"""
    owners: dict[str, list[str]] = {}
    for t in tasks:
        if t.get("owner"):
            owners.setdefault(t.get("meeting_id"), []).append(t["owner"])
    matched = []
    for m in meetings:
        info = m.get("meeting") or {}
        if plan.date_from or plan.date_to:
            try:
                day = date.fromisoformat(info.get("date") or "")
            except ValueError:
                continue
            if (plan.date_from and day < plan.date_from) or (plan.date_to and day > plan.date_to):
                continue
        if plan.kinds and LEGACY_KINDS.get(m.get("kind"), m.get("kind")) not in plan.kinds:
            continue
        if plan.people:
            known = list(info.get("attendees") or []) + owners.get(m["id"], [])
            text = "\n".join([*known, info.get("summary") or "", m.get("transcript") or ""])
            if not all(_person_in(p, known, text) for p in plan.people):
                continue
        matched.append(m["id"])
    return matched
