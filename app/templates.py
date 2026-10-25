"""自訂會議範本：使用者自己定義種類名稱、要哪些區塊、每個區塊抓什麼。

內建的八種是通用的，但學生的「專題 meeting」、社團的「幹部會議」各有自己想看的
欄位。Section 本來就是「標籤＋清單」的通用形狀（見 app/models.py），所以輸出端
——前端、Markdown 匯出、儲存層——一行都不用改，只要分析時把區塊定義餵進提示詞。

範本存在工作區底下（user 欄位），群組的範本群組成員自動共用，與會議、任務、
詞彙表同一套隔離方式。
"""
from __future__ import annotations

from app.agents.decision_agent import FEATURE_KEYS

# 每個區塊都會進提示詞，不設上限等於讓人塞爆 token（與帳單）
MAX_SECTIONS = 20
MAX_NAME = 40
MAX_HINT = 500
MAX_DESC = 200

# 自訂種類在請求裡長這樣，與內建種類區分開來
PREFIX = "custom:"


class TemplateError(Exception):
    """範本內容不合法。"""


def is_custom(kind: str | None) -> bool:
    return bool(kind) and str(kind).startswith(PREFIX)


def template_id(kind: str) -> str:
    return str(kind)[len(PREFIX):]


def kind_value(template_id: str) -> str:
    return f"{PREFIX}{template_id}"


def normalize_template(raw: dict) -> dict:
    """驗證並整理使用者送來的範本。只回內容欄位，id / user 由呼叫端補上。"""
    name = str(raw.get("name") or "").strip()
    if not name:
        raise TemplateError("範本名稱不可為空")
    if len(name) > MAX_NAME:
        raise TemplateError(f"範本名稱請控制在 {MAX_NAME} 字以內")

    hint = str(raw.get("hint") or "").strip()
    if len(hint) > MAX_HINT:
        raise TemplateError(f"說明請控制在 {MAX_HINT} 字以內")

    raw_sections = raw.get("sections")
    if raw_sections is None:
        raw_sections = []
    if not isinstance(raw_sections, list):
        raise TemplateError("sections 必須是陣列")
    if len(raw_sections) > MAX_SECTIONS:
        raise TemplateError(f"區塊最多 {MAX_SECTIONS} 個")

    sections, seen = [], set()
    for item in raw_sections:
        if not isinstance(item, dict):
            raise TemplateError("每個區塊都要是物件")
        label = str(item.get("label") or "").strip()
        if not label:
            raise TemplateError("每個區塊都要有名稱")
        if label in seen:
            # label 是輸出的 key，重複的話畫面上會出現兩個一樣的區塊
            raise TemplateError(f"區塊名稱重複：{label}")
        seen.add(label)
        desc = str(item.get("desc") or "").strip()
        if len(desc) > MAX_DESC:
            raise TemplateError(f"「{label}」的說明請控制在 {MAX_DESC} 字以內")
        sections.append({"label": label, "desc": desc})

    features = raw.get("features")
    if features is None:
        features = sorted(FEATURE_KEYS)
    if not isinstance(features, list) or any(f not in FEATURE_KEYS for f in features):
        raise TemplateError(f"features 只能是：{'、'.join(sorted(FEATURE_KEYS))}")

    return {"name": name, "hint": hint, "sections": sections, "features": sorted(set(features))}


def sections_map(template: dict) -> dict[str, str]:
    """範本的區塊 → build_prompt 要的 {label: 說明} 形狀。"""
    return {s["label"]: s.get("desc") or f"{s['label']}的相關內容" for s in template.get("sections", [])}


def prompt_hint(template: dict) -> str:
    """拼進提示詞的那一句。使用者寫什麼就用什麼，沒寫就只講種類名稱。"""
    name = template.get("name", "")
    hint = (template.get("hint") or "").strip()
    return f"{name}：{hint}" if hint else name
