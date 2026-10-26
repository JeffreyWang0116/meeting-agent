"""對摘要、決議、任務留言：群組成員可以在結果上討論。

AI 的產出常常只是「大致上對」。以前要反映「這條決議其實沒有定案」只有兩條路：
直接改掉（別人不知道原本是什麼，也不知道為什麼改），或者在群組聊天室講
（那句話與這場會議從此失聯）。留言讓討論留在它討論的東西旁邊。

target 綁的是「位置」而不是內容：決議被重新分析換掉之後，留言會指向新的
第 2 條決議。這是刻意的取捨——用內容雜湊綁定的話，重新分析一次所有留言
就全部變成孤兒，那比指錯更糟。
"""
from __future__ import annotations

MAX_TEXT = 2000

# 可以留言的位置。section 的標籤由使用者自訂，所以只檢查前綴
SIMPLE_TARGETS = {"summary", "transcript"}
INDEXED_TARGETS = {"decision", "pending", "highlight", "agenda"}
ID_TARGETS = {"task"}
LABEL_TARGETS = {"section"}


class CommentError(Exception):
    """留言內容或位置不合法。"""


def normalize_target(raw: str) -> str:
    """驗證留言掛在哪裡。放行任意字串的話，前端寫錯一個字就是一筆永遠顯示不出來的留言。"""
    target = str(raw or "").strip()
    if not target:
        raise CommentError("缺少留言位置")
    if target in SIMPLE_TARGETS:
        return target

    kind, _, rest = target.partition(":")
    if not rest:
        raise CommentError(f"無法辨識的留言位置：{target}")
    if kind in INDEXED_TARGETS:
        if not rest.isdigit():
            raise CommentError(f"{kind} 的位置必須是數字")
        return f"{kind}:{int(rest)}"
    if kind in ID_TARGETS or kind in LABEL_TARGETS:
        return f"{kind}:{rest}"
    raise CommentError(f"無法辨識的留言位置：{target}")


def clean_text(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise CommentError("留言不可為空")
    if len(text) > MAX_TEXT:
        raise CommentError(f"留言請控制在 {MAX_TEXT} 字以內")
    return text


def author_name(comment: dict) -> str:
    """顯示用的名字。成員紀錄裡只有 email（見 app/groups.py）。"""
    email = comment.get("author_email") or ""
    if "@" in email:
        return email.split("@", 1)[0]
    uid = comment.get("author_uid") or ""
    return f"訪客 {uid[:6]}" if uid else "某人"
