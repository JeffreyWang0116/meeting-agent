"""Orchestrator：把分析流程串成完整 pipeline。

文字（貼上 / 轉錄產生）→ 正規化 →（Corrector）→（預錄姓名）→ Decision
→ 存進 store → 產生確認信與行事曆事件。

Corrector 是選用的：開啟時先修掉語音辨識的同音錯字，後面的分析與存檔
都吃校正後的版本（存進資料庫的逐字稿也是校正後的）。

講者一律維持「講者A/B/C」代號，系統不從對話內容推斷姓名——講者口中的稱謂
指的是別人，模型會把「主席好」的人標成主席。唯一的例外是即時聆聽會前預錄的
聲音樣本：姓名是使用者自己填的，比對出來就直接套用。其餘由使用者事後改名。
"""
from __future__ import annotations

import re
from datetime import date

from app.stores.base import DEFAULT_USER, TaskStore
from app.transcription.speaker_names import apply_speaker_names, is_safe_name

from app.agents.decision_agent import DecisionAgent
from app.agents.notifier_agent import build_notifications

_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_text(raw: str) -> str:
    """貼上的文字或轉錄稿：統一換行、去掉零寬字元與行尾空白、壓掉多餘空行。"""
    if not raw or not raw.strip():
        raise ValueError("輸入內容是空的，請提供會議紀錄文字")

    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _EXCESS_BLANK_LINES.sub("\n\n", text)
    return text.strip()


class Orchestrator:
    def __init__(self, decision: DecisionAgent, store: TaskStore, corrector=None):
        self.decision = decision
        self.store = store
        self.corrector = corrector

    def process_transcript(
        self,
        raw_text: str,
        meeting_date: date | None = None,
        kind: str | None = None,
        features: set[str] | None = None,
        correct_typos: bool = False,
        terms: list[dict] | None = None,
        user: str = DEFAULT_USER,
        speaker_prior: dict[str, str] | None = None,
        attendees: list[str] | None = None,
        # 聆聽時按的「標記這一刻」時間點，與會議中自己打的筆記
        marks: list[str] | None = None,
        notes: str | None = None,
        # 自訂會議範本的區塊定義（見 app/templates.py）
        kind_spec: dict | None = None,
    ) -> dict:
        text = normalize_text(raw_text)
        corrections: list[dict] = []
        if correct_typos and self.corrector:
            text, corrections = self.corrector.correct(text, user)
        speaker_names: list[dict] = []
        # speaker_prior：會前預錄聲音樣本比對出的 {代號: 使用者填的姓名}。
        # 姓名來自其他模組，一樣要過安全檢查——來源不同不是跳過驗證的理由
        if speaker_prior:
            text, speaker_names = apply_speaker_names(
                text, {k: v for k, v in speaker_prior.items() if is_safe_name(v)}
            )
        analysis = self.decision.analyze(
            text, meeting_date=meeting_date, kind=kind, features=features,
            extra_terms=terms, user=user,
            # attendees：會前錄過聲音樣本的人，等於使用者指認的出席名單。
            # 只用於補全 attendees 與統一姓名寫法，不得用來猜 owner（見 build_prompt）
            attendees=attendees,
            marks=marks,
            notes=notes,
            kind_spec=kind_spec,
        )
        meeting_id = self.store.save_meeting(
            analysis, transcript=text, kind=kind, terms=terms, user=user
        )
        notifications = build_notifications(analysis, meeting_id)
        return {
            "meeting_id": meeting_id,
            "analysis": analysis.model_dump(mode="json"),
            "notifications": notifications,
            # 校正後的逐字稿：呼叫端（媒體工作、即時聆聽）要用這份顯示與存檔，
            # 而不是傳進來的原始文字
            "transcript": text,
            "corrections": corrections,
            # 實際套用的預錄姓名（代號 → 姓名），供前端顯示「誰是誰」
            "speaker_names": speaker_names,
        }
