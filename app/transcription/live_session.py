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
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# 行首「講者A：」「Kevin:」等講者標註（與前端 SPEAKER_RE 對齊）
_SPEAKER_RE = re.compile(r"^\s*([^：:\n]{1,12})[：:]")

# 行首「[1:02]」「[1:02:03]」時間標記（與前端 TIME_RE 對齊）
_TIME_PREFIX_RE = re.compile(r"^\s*\[(\d{1,2}(?::\d{2}){1,2})\]\s*")


def _parse_time_label(label: str) -> int:
    seconds = 0
    for part in label.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def _format_time(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _shift_timestamps(text: str, offset_seconds: float | None) -> str:
    """把 chunk 內「相對本段開頭」的時間戳平移成整場會議時間。

    每段錄音獨立轉錄，模型標的 [0:05] 是該段的第 5 秒；前端會隨上傳附上
    本段在整場會議中的開始秒數（offset），加總後才是使用者看到的時間軸。
    沒傳 offset（舊前端）就把相對時間剝掉，避免顯示錯誤的時間。
    轉錄後端完全沒標時間時，在段首補一個 offset 標記，維持段落級時間軸。
    """
    lines = []
    any_marker = False
    for line in text.split("\n"):
        m = _TIME_PREFIX_RE.match(line)
        if not m:
            lines.append(line)
            continue
        any_marker = True
        rest = line[m.end():]
        if offset_seconds is None:
            lines.append(rest)
        else:
            t = _format_time(_parse_time_label(m.group(1)) + offset_seconds)
            lines.append(f"[{t}] {rest}")
    if not any_marker and offset_seconds is not None and lines:
        lines[0] = f"[{_format_time(offset_seconds)}] {lines[0]}"
    return "\n".join(lines)


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


def _speaker_hint(speakers: list[str]) -> str | None:
    if not speakers:
        return None
    names = "、".join(speakers)
    return (
        f"這是同一場錄音的後續片段。先前已出現的講者：{names}。"
        "請沿用相同標籤指稱同一個人的聲音，只有出現全新的聲音時才用下一個新標籤"
        "（例如已用到講者B，新的人就用講者C）。"
    )


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
            hint = _speaker_hint(session.speakers)

        chunk_path = session.dir / f"chunk_{index:03d}{suffix}"
        chunk_path.write_bytes(data)

        text = self._transcribe(chunk_path, hint).strip()
        if text:
            text = _shift_timestamps(text, offset_seconds)

        with self._lock:
            session.parts[index] = text
            if text:
                for line in text.split("\n"):
                    m = _SPEAKER_RE.match(_TIME_PREFIX_RE.sub("", line))
                    if m:
                        name = m.group(1).strip()
                        if name and name not in session.speakers:
                            session.speakers.append(name)
            transcript = _join(session.parts)

        translation = None
        if text and session.translate_to and self._translator:
            # 剝掉行首時間標記再翻譯，時間戳不需要翻、也避免譯文格式被帶歪
            plain = "\n".join(_TIME_PREFIX_RE.sub("", ln) for ln in text.split("\n"))
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
