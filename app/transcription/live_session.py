"""即時聆聽 session 管理。

前端每 30~60 秒送來一段「自包含」的錄音檔（前端以重啟 MediaRecorder 的
方式確保每段都有完整檔頭），這裡逐段轉錄並累積逐字稿。

並發安全：前端可能同時有多段上傳中（前一段還在辨識、下一段已送到）。每段
一進來就先在鎖內配位（index），轉錄完成後依 index 填回固定槽位——確保最終
逐字稿順序等於「錄音順序」，而不是「哪段先辨識完」。

跨段講者一致性：每段獨立轉錄時，Gemini 會把講者重新從「講者A」編號，導致
多人會議被壓縮成兩三個講者。把先前已出現的講者清單當提示帶進下一段轉錄，
引導模型沿用同一組標籤、只有新聲音才加新標籤。

回收：使用者關掉分頁不見得會按「結束」，那種 session 的逐字稿會一直留在
記憶體、音檔一直留在磁碟。閒置超過 TTL 的一律清掉——雲端免費層的磁碟與
記憶體都很小，沒有上界的累積遲早把服務拖垮。
"""
from __future__ import annotations

import inspect
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.glossary import terms_hint_line
from app.stores.base import DEFAULT_USER
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
    last_active: float = 0.0  # 單調時鐘：最後一次收到音訊段（或結束）的時間
    user: str = DEFAULT_USER  # 誰開的這場聆聽
    terms: list[dict] = field(default_factory=list)  # 本次專用詞彙（進轉錄提示）


class LiveSessionManager:
    # 閒置多久算被遺棄。比任何一場真實會議都長，但仍有上界
    SESSION_TTL_SECONDS = 2 * 60 * 60

    def __init__(
        self,
        transcriber,
        work_dir: Path | str,
        translator=None,
        now: Callable[[], float] = time.monotonic,
    ):
        self._transcriber = transcriber
        self._translator = translator
        self._work_dir = Path(work_dir)
        self._now = now  # 單調時鐘；可注入，測試不必真的等兩小時
        self._sessions: dict[str, LiveSession] = {}
        self._lock = threading.Lock()

    def start(
        self,
        translate_to: str | None = None,
        user: str = DEFAULT_USER,
        terms: list[dict] | None = None,
    ) -> str:
        session_id = uuid.uuid4().hex[:12]
        session_dir = self._work_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._prune_locked()
            self._sessions[session_id] = LiveSession(
                id=session_id,
                dir=session_dir,
                translate_to=translate_to,
                last_active=self._now(),
                user=user,
                terms=list(terms or []),
            )
        return session_id

    def _prune_locked(self) -> None:
        """清掉閒置超過 TTL 的 session。這是沒按「結束」的那些 session
        唯一會被放掉的時機——逐字稿佔的記憶體與音檔佔的磁碟一起還回去。"""
        cutoff = self._now() - self.SESSION_TTL_SECONDS
        for sid in [
            sid for sid, s in self._sessions.items() if s.last_active < cutoff
        ]:
            shutil.rmtree(self._sessions.pop(sid).dir, ignore_errors=True)

    def _get(self, session_id: str, user: str = DEFAULT_USER) -> LiveSession:
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(session_id)
        # 別人的 session 一律當作不存在：回 403 等於承認「這個 id 有效」
        if session is None or session.user != user:
            raise SessionNotFound(f"找不到聆聽 session：{session_id}")
        return session

    def add_chunk(
        self,
        session_id: str,
        data: bytes,
        suffix: str = ".webm",
        offset_seconds: float | None = None,
        user: str = DEFAULT_USER,
    ) -> dict:
        session = self._get(session_id, user)
        # 配位＋佔槽＋算提示，全在鎖內完成，避免多段並發時互相踩踏
        with self._lock:
            if session.closed:
                raise ValueError("此聆聽 session 已結束，無法再加入音訊")
            session.last_active = self._now()  # 長會議不能被自己的 TTL 清掉
            index = session.chunk_count
            session.chunk_count += 1
            session.parts.append(None)
            hint = _chunk_hint(session)

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

    def transcript(self, session_id: str, user: str = DEFAULT_USER) -> str:
        with self._lock:
            self._prune_locked()
            return _join(self._get_locked(session_id, user).parts)

    def _get_locked(self, session_id: str, user: str = DEFAULT_USER) -> LiveSession:
        session = self._sessions.get(session_id)
        if session is None or session.user != user:
            raise SessionNotFound(f"找不到聆聽 session：{session_id}")
        return session

    def finish(self, session_id: str, user: str = DEFAULT_USER) -> str:
        session = self._get(session_id, user)
        with self._lock:
            session.closed = True
            # 不立刻移除：剛結束時遲到的音訊段要拿到「已結束」的明確訊息，
            # 而不是查無此 session。這筆殘留由 TTL 回收
            session.last_active = self._now()
            transcript = _join(session.parts)
        # 錄音段檔案不再需要，刪掉整個 session 目錄釋放磁碟（雲端暫時性磁碟很小）
        shutil.rmtree(session.dir, ignore_errors=True)
        return transcript


def _chunk_hint(session: LiveSession) -> str | None:
    """這一段轉錄要帶的提示：跨段講者一致性 ＋ 本次專用詞彙。

    兩者是不同面向——前者管講者標籤別重新編號，後者管內文用字別聽錯——所以
    併著送。都沒有時回 None（而不是空字串），_transcribe 才會走「不帶 hint」
    那條路，行為與加詞彙功能之前完全一致。
    """
    combined = (speaker_hint(session.speakers) or "") + terms_hint_line(
        session.terms, "本次會議專用詞彙"
    )
    return combined or None


def _join(parts: list[str | None]) -> str:
    """依 index 順序串接已完成的段落，跳過尚未辨識完（None）與空白段。"""
    return "\n".join(p for p in parts if p)
