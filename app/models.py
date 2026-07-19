"""會議分析結果的結構化 schema。

這是 Decision Agent（LLM）產出的 JSON 契約，也是整個系統流轉的核心資料結構。
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field


class MeetingInfo(BaseModel):
    title: str = "未命名會議"
    date: date
    # 「會議摘要」功能沒被使用時（非會議種類、或使用者取消勾選）沒有摘要
    summary: Optional[str] = None
    attendees: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    description: str
    context: Optional[str] = None


class TodoItem(BaseModel):
    task: str
    owner: Optional[str] = None
    due_date: Optional[date] = None
    priority: Literal["high", "medium", "low"] = "medium"
    priority_reason: Optional[str] = None
    source_quote: Optional[str] = None


class PendingItem(BaseModel):
    topic: str
    reason: Optional[str] = None


class Highlight(BaseModel):
    """會議重點：一句話的關鍵時刻，帶逐字稿時間戳供前端點擊跳轉。"""

    text: str
    # 逐字稿行首的時間標記（"1:02" 或 "1:02:03"）；逐字稿沒有時間標記時為 None
    time: Optional[str] = None
    source_quote: Optional[str] = None


class MeetingAnalysis(BaseModel):
    meeting: MeetingInfo
    decisions: list[Decision] = Field(default_factory=list)
    todos: list[TodoItem] = Field(default_factory=list)
    pending_items: list[PendingItem] = Field(default_factory=list)
    # 會議重點（依時間順序）；「會議重點」功能沒被使用時為空
    highlights: list[Highlight] = Field(default_factory=list)
    # AI 建議的分類標籤（供歷史會議篩選），使用者可再自訂修改
    tags: list[str] = Field(default_factory=list)
