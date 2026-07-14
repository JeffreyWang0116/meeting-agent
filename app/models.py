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
    summary: str
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


class MeetingAnalysis(BaseModel):
    meeting: MeetingInfo
    decisions: list[Decision] = Field(default_factory=list)
    todos: list[TodoItem] = Field(default_factory=list)
    pending_items: list[PendingItem] = Field(default_factory=list)
    # AI 建議的分類標籤（供歷史會議篩選），使用者可再自訂修改
    tags: list[str] = Field(default_factory=list)
