"""Orchestrator：把各個 Agent 串成完整 pipeline。

文字（貼上 / 轉錄產生）→ Parser →（Corrector）→ Decision → Executor → Notifier。

Corrector 是選用的：開啟時先修掉語音辨識的同音錯字，後面的分析與存檔
都吃校正後的版本（存進資料庫的逐字稿也是校正後的）。
"""
from __future__ import annotations

from datetime import date

from app.agents.decision_agent import DecisionAgent
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent


class Orchestrator:
    def __init__(
        self,
        parser: ParserAgent,
        decision: DecisionAgent,
        executor: ExecutorAgent,
        notifier: NotifierAgent,
        corrector=None,
    ):
        self.parser = parser
        self.decision = decision
        self.executor = executor
        self.notifier = notifier
        self.corrector = corrector

    def process_transcript(
        self,
        raw_text: str,
        meeting_date: date | None = None,
        kind: str | None = None,
        features: set[str] | None = None,
        correct_typos: bool = False,
    ) -> dict:
        text = self.parser.parse(raw_text)
        corrections: list[dict] = []
        if correct_typos and self.corrector:
            text, corrections = self.corrector.correct(text)
        analysis = self.decision.analyze(
            text, meeting_date=meeting_date, kind=kind, features=features
        )
        meeting_id = self.executor.execute(analysis, transcript=text, kind=kind)
        notifications = self.notifier.notify(meeting_id, analysis)
        return {
            "meeting_id": meeting_id,
            "analysis": analysis.model_dump(mode="json"),
            "notifications": notifications,
            # 校正後的逐字稿：呼叫端（媒體工作、即時聆聽）要用這份顯示與存檔，
            # 而不是傳進來的原始文字
            "transcript": text,
            "corrections": corrections,
        }
