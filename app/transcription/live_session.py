"""即時聆聽 session 管理。

前端每 30~60 秒送來一段「自包含」的錄音檔（前端以重啟 MediaRecorder 的
方式確保每段都有完整檔頭），這裡逐段轉錄並累積逐字稿。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path


class SessionNotFound(KeyError):
    pass


@dataclass
class LiveSession:
    id: str
    dir: Path
    parts: list[str] = field(default_factory=list)
    chunk_count: int = 0
    closed: bool = False
    translate_to: str | None = None  # "en" / "zh"：逐段即時翻譯的目標語言


class LiveSessionManager:
    def __init__(self, transcriber, work_dir: Path | str, translator=None):
        self._transcriber = transcriber
        self._translator = translator
        self._work_dir = Path(work_dir)
        self._sessions: dict[str, LiveSession] = {}
        self._lock = threading.Lock()

    def start(self, translate_to: str | None = None) -> str:
        session_id = uuid.uuid4().hex[:12]
        session_dir = self._work_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._sessions[session_id] = LiveSession(
                id=session_id, dir=session_dir, translate_to=translate_to
            )
        return session_id

    def _get(self, session_id: str) -> LiveSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(f"找不到聆聽 session：{session_id}")
        return session

    def add_chunk(self, session_id: str, data: bytes, suffix: str = ".webm") -> dict:
        session = self._get(session_id)
        if session.closed:
            raise ValueError("此聆聽 session 已結束，無法再加入音訊")

        chunk_path = session.dir / f"chunk_{session.chunk_count:03d}{suffix}"
        chunk_path.write_bytes(data)
        session.chunk_count += 1

        text = self._transcriber.transcribe(chunk_path).strip()
        if text:
            session.parts.append(text)

        translation = None
        if text and session.translate_to and self._translator:
            try:
                translation = self._translator.translate(text, session.translate_to)
            except Exception:  # 翻譯失敗不擋逐字稿主流程
                translation = None

        return {
            "text": text,
            "translation": translation,
            "transcript": self.transcript(session_id),
        }

    def transcript(self, session_id: str) -> str:
        return "\n".join(self._get(session_id).parts)

    def finish(self, session_id: str) -> str:
        session = self._get(session_id)
        session.closed = True
        return self.transcript(session_id)
