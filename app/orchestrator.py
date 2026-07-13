"""Orchestrator：把四個 Agent 串成完整 pipeline。

文字（貼上 / 轉錄產生）→ Parser → Decision → Executor → Notifier。
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
    ):
        self.parser = parser
        self.decision = decision
        self.executor = executor
        self.notifier = notifier

    def process_transcript(self, raw_text: str, meeting_date: date | None = None) -> dict:
        text = self.parser.parse(raw_text)
        analysis = self.decision.analyze(text, meeting_date=meeting_date)
        meeting_id = self.executor.execute(analysis)
        notifications = self.notifier.notify(meeting_id, analysis)
        return {
            "meeting_id": meeting_id,
            "analysis": analysis.model_dump(mode="json"),
            "notifications": notifications,
        }
