"""即時聆聽 session 管理。

前端每 30~60 秒送來一段「自包含」的錄音檔（前端以重啟 MediaRecorder 的
方式確保每段都有完整檔頭），這裡逐段轉錄並累積逐字稿。

並發安全：前端可能同時有多段上傳中（前一段還在辨識、下一段已送到）。每段
一進來就先在鎖內配位（index），轉錄完成後依 index 填回固定槽位——確保最終
逐字稿順序等於「錄音順序」，而不是「哪段先辨識完」。

跨段講者一致性：每段獨立轉錄時，Gemini 會把講者重新從「講者A」編號，導致
多人會議被壓縮成兩三個講者。把先前已出現的講者清單當提示帶進下一段轉錄，
引導模型沿用同一組標籤、只有新聲音才加新標籤。
"""
from __future__ import annotations

import inspect
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.transcription.segments import (
    TIME_PREFIX_RE,
    collect_speakers,
    shift_timestamps,
    speaker_hint,
)


class SessionNotFound(KeyError):
    pass


@dataclass
class LiveSession:
    id: str
    dir: Path
    parts: list[str | None] = field(default_factory=list)  # 依 index 定位，None＝辨識中
    chunk_count: int = 0
    closed: bool = False
    speakers: list[str] = field(default_factory=list)  # 已出現的講者標籤（依出場序）
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

    def add_chunk(
        self,
        session_id: str,
        data: bytes,
        suffix: str = ".webm",
        offset_seconds: float | None = None,
    ) -> dict:
        session = self._get(session_id)
        # 配位＋佔槽＋算提示，全在鎖內完成，避免多段並發時互相踩踏
        with self._lock:
            if session.closed:
                raise ValueError("此聆聽 session 已結束，無法再加入音訊")
            index = session.chunk_count
            session.chunk_count += 1
            session.parts.append(None)
            hint = speaker_hint(session.speakers)

        chunk_path = session.dir / f"chunk_{index:03d}{suffix}"
        chunk_path.write_bytes(data)

        text = self._transcribe(chunk_path, hint).strip()
        if text:
            text = shift_timestamps(text, offset_seconds)

        with self._lock:
            session.parts[index] = text
            if text:
                collect_speakers(text, session.speakers)
            transcript = _join(session.parts)

        translation = None
        if text and session.translate_to and self._translator:
            # 剝掉行首時間標記再翻譯，時間戳不需要翻、也避免譯文格式被帶歪
            plain = "\n".join(TIME_PREFIX_RE.sub("", ln) for ln in text.split("\n"))
            try:
                translation = self._translator.translate(plain, session.translate_to)
            except Exception:  # 翻譯失敗不擋逐字稿主流程
                translation = None

        return {"text": text, "translation": translation, "transcript": transcript}

    def _transcribe(self, path: Path, hint: str | None) -> str:
        """轉錄一段。transcriber 是注入的鴨子型別，簽名不一定收 hint，
        支援才傳（跨段講者提示只對會標講者的後端有意義）。"""
        fn = self._transcriber.transcribe
        if hint:
            try:
                params = inspect.signature(fn).parameters
                if "hint" in params or any(
                    p.kind == p.VAR_KEYWORD for p in params.values()
                ):
                    return fn(path, hint=hint)
            except (TypeError, ValueError):
                pass
        return fn(path)

    def transcript(self, session_id: str) -> str:
        with self._lock:
            return _join(self._get_locked(session_id).parts)

    def _get_locked(self, session_id: str) -> LiveSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(f"找不到聆聽 session：{session_id}")
        return session

    def finish(self, session_id: str) -> str:
        session = self._get(session_id)
        with self._lock:
            session.closed = True
            transcript = _join(session.parts)
        # 錄音段檔案不再需要，刪掉整個 session 目錄釋放磁碟（雲端暫時性磁碟很小）
        shutil.rmtree(session.dir, ignore_errors=True)
        return transcript


def _join(parts: list[str | None]) -> str:
    """依 index 順序串接已完成的段落，跳過尚未辨識完（None）與空白段。"""
    return "\n".join(p for p in parts if p)
