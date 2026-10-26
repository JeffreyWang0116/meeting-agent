"""會議前準備：議程對照與「上次沒做完的」。

散會後最常見的兩個問題是「議程上有一項好像沒講到」和「上次講好的那件事
後來呢」。兩個都不該靠重讀逐字稿回答——議程是會前就寫好的，上次沒做完的
事資料庫裡也有。

這裡只做純粹的文字整理；要不要帶進提示詞、要不要顯示，由呼叫端決定。
"""
from __future__ import annotations

import re

# 每一項都要進提示詞，而且對照結果要一項一項回。超過這個數量的「議程」
# 通常是整份會議通知被貼進來了
MAX_AGENDA_ITEMS = 30
MAX_CARRY_OVER = 20

STATUSES = ("covered", "partial", "missed")

# 使用者會直接從會議通知複製貼上，那裡面本來就有編號或符號
_LEADER = re.compile(r"^\s*(?:[-*•·–—]|\(?\d{1,2}[.)、]|[一二三四五六七八九十]{1,3}[、.])\s*")


def clean_agenda(raw: str | None) -> list[str]:
    """一行一項。去掉編號、空行與重複項。"""
    out: list[str] = []
    for line in (raw or "").splitlines():
        item = _LEADER.sub("", line).strip()
        # 重複的項目會讓 AI 輸出兩筆一樣的對照結果
        if item and item not in out:
            out.append(item)
    return out[:MAX_AGENDA_ITEMS]


def carry_over_lines(tasks: list[dict], pending: list[dict]) -> list[str]:
    """上次沒做完的事，整理成一行一句。做完的不帶——還一直被帶到下次的話，
    這份清單很快就沒人看了。"""
    lines = []
    for t in tasks or []:
        if t.get("status") == "done":
            continue
        owner = (t.get("owner") or "").strip() or "未指派"
        due = t.get("due_date")
        lines.append(f"代辦：{t.get('task', '')}（{owner}{'，期限 ' + str(due) if due else ''}）")
    for p in pending or []:
        reason = (p.get("reason") or "").strip()
        lines.append(f"未決：{p.get('topic', '')}" + (f"（{reason}）" if reason else ""))
    return lines[:MAX_CARRY_OVER]


def coverage_summary(review) -> dict[str, int]:
    """議程對照的統計。模型偶爾會回規定以外的字——當成「討論過」會讓人
    以為事情辦完了，所以一律歸到 missed。"""
    counts = {s: 0 for s in STATUSES}
    if not isinstance(review, list):
        return counts
    for item in review:
        status = item.get("status") if isinstance(item, dict) else None
        counts[status if status in counts else "missed"] += 1
    return counts
