"""使用者把講者代號改成真名：逐字稿講者欄、出席者、摘要與其他 AI 產生的文字、任務。

逐字稿內文與 `source_quote` 刻意不動：內文是講者說的話（「剛剛講者A問的」），
引句要能在逐字稿裡原文找到，點了才跳得回出處。
"""
from __future__ import annotations

import re

from app.transcription.speaker_names import MAX_NAME_LEN

_TIME_HEAD = r"\s*(?:\[\d{1,2}(?::\d{1,2}){0,2}\]\s*)?"


def name_problem(name: str) -> str:
    """新名字不合格的原因；合格回空字串。與前端 speakers.js 的 speakerNameProblem 同一套。"""
    n = (name or "").strip()
    if not n:
        return "名字不可為空"
    if len(n) > MAX_NAME_LEN:
        return f"名字最多 {MAX_NAME_LEN} 個字"
    if re.search(r"[：:\n\[\]]", n):
        return "名字不可包含冒號、換行或方括號（會破壞逐字稿格式）"
    return ""


def rename_speaker_in_transcript(text: str | None, old: str, new: str) -> str:
    """只換講者欄：內文裡提到的「講者A」是說話內容，「講者AB」是另一個人。"""
    pattern = re.compile(rf"^({_TIME_HEAD}){re.escape(old)}(\s*[：:])")
    return "\n".join(
        pattern.sub(lambda m: m.group(1) + new + m.group(2), line)
        for line in (text or "").split("\n")
    )


def rename_in_list(names: list[str] | None, old: str, new: str) -> list[str]:
    """出席者名單：換掉舊名，已經有同名的（兩個代號其實是同一人）就併成一筆。"""
    return list(dict.fromkeys(new if x == old else x for x in names or []))


def rename_in_text(text: str | None, old: str, new: str) -> str | None:
    """自由文字裡的講者名稱。沒有講者欄可以對，所以要防三種誤換：

    - 「講者A」撞上「講者AB」、「Speaker 2」撞上「Speaker 20」：英數字頭尾要落在英數字邊界
    - 新名字包含舊名字（翁曉→翁曉玲）：已經寫對的地方不能再換一次變成「翁曉玲玲」
    - 單字名（王、林）在中文裡到處都是，不換
    """
    if not text or not old or len(old) < 2:
        return text
    within = new.find(old)  # 舊名在新名裡的位置（-1＝不包含）
    out, start = [], 0
    while (at := text.find(old, start)) != -1:
        end = at + len(old)
        cuts_word = (
            (_is_ascii_word(old[0]) and at > 0 and _is_ascii_word(text[at - 1]))
            or (_is_ascii_word(old[-1]) and end < len(text) and _is_ascii_word(text[end]))
        )
        already_new = within >= 0 and at >= within and text.startswith(new, at - within)
        out.append(text[start:at] + (old if cuts_word or already_new else new))
        start = end
    out.append(text[start:])
    return "".join(out)


def _is_ascii_word(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def rename_meeting_fields(record: dict, old: str, new: str) -> dict:
    """整場會議要寫回 store 的欄位（`update_meeting` 的格式）。標題與種類欄位標題不動。"""
    info = record.get("meeting") or {}
    nested = {"attendees": rename_in_list(info.get("attendees"), old, new)}
    if info.get("summary"):
        nested["summary"] = rename_in_text(info["summary"], old, new)
    fields = {
        "transcript": rename_speaker_in_transcript(record.get("transcript"), old, new),
        "meeting": nested,
    }
    text_keys = {
        "highlights": ("text",),
        "decisions": ("description", "context"),
        "pending_items": ("topic", "reason"),
    }
    for key, names in text_keys.items():
        if key in record:
            fields[key] = [
                {**item, **{n: rename_in_text(item.get(n), old, new) for n in names if n in item}}
                for item in record[key] or []
            ]
    if "sections" in record:
        fields["sections"] = [
            {**sec, "items": [rename_in_text(x, old, new) for x in sec.get("items") or []]}
            for sec in record["sections"] or []
        ]
    return fields


def rename_task_fields(task: dict, old: str, new: str) -> dict:
    """一筆任務要更新的欄位；沒有變動的欄位不列，全都沒變回空 dict。"""
    changed = {}
    if task.get("owner") == old:
        changed["owner"] = new
    for key in ("task", "priority_reason"):
        renamed = rename_in_text(task.get(key), old, new)
        if renamed != task.get(key):
            changed[key] = renamed
    return changed
