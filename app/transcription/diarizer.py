"""Diarizer：把 pyannote 講者分離接進轉錄工作。

- start()：壓縮＋上傳＋分群丟到背景，與 Gemini 轉錄並行——PoC 裡上傳加分群
  只要一分鐘，轉錄要七八分鐘，排在後面等於白等
- apply()：轉錄完成後拿分群結果重標逐字稿。**任何失敗都原樣回傳**：講者辨識
  是加分項，pyannote 當掉、額度用完、ffmpeg 壓不動，都退回 Gemini 自己標的代號
"""
from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from app.transcription import media
from app.transcription.pyannote_client import PyannoteClient
from app.transcription.speaker_align import (
    code_map,
    relabel,
    speaker_prior_from_identify,
    to_session_time,
)

logger = logging.getLogger(__name__)

# 同時進行的分群工作上限。瓶頸在上傳頻寬與 pyannote 端，不在本機 CPU
_MAX_PARALLEL_JOBS = 2

# pyannote 建 voiceprint 的樣本上限；「用音檔」上傳的樣本可能是一整段錄音
VOICEPRINT_MAX_SECONDS = 30


class Diarizer:
    def __init__(
        self,
        client: PyannoteClient,
        on_call: Callable[[], None] | None = None,
        encode: Callable[..., Path] = media.encode_for_diarization,
        duration: Callable[[Path], float | None] = media.audio_duration,
        submit: Callable[[Callable[[], dict]], Future] | None = None,
        concat: Callable[..., list[float]] = media.concat_for_diarization,
        voiceprint_threshold: float | None = 50,
        voiceprints_enabled: bool = False,
    ):
        self.client = client
        self._on_call = on_call
        self._encode = encode
        self._duration = duration
        self._concat = concat
        self.voiceprint_threshold = voiceprint_threshold
        # voiceprint 按建立次數計費（試用只有 10 個），預設不建；姓名改由 LiveSessionManager
        # 在重標後交給 Gemini 比對。見 Settings.pyannote_voiceprint_enabled
        self.voiceprints_enabled = voiceprints_enabled
        self._submit = submit or ThreadPoolExecutor(
            max_workers=_MAX_PARALLEL_JOBS, thread_name_prefix="diarize"
        ).submit

    def start(self, audio_path: Path | str) -> Future:
        """背景開始分群，回傳 Future（結果是 (segments, 音檔長度)）。"""
        return self._submit(lambda: self._run(Path(audio_path)))

    def apply(self, future: Future | None, transcript: str) -> str:
        try:
            segments, duration = future.result()
        except Exception as exc:
            logger.warning("講者分離失敗，沿用 Gemini 標註的代號：%s", exc)
            return transcript
        if not segments:
            return transcript
        text, stats = relabel(transcript, segments, duration)
        logger.info("講者分離對齊：%s", stats)
        return text

    def relabel_session(
        self, transcript: str, pieces: list[dict], enrollments: list[dict]
    ) -> tuple[str, dict[str, str]]:
        """即時聆聽結束時整場重標，回傳 (重標後逐字稿, {講者代號: 姓名})。

        pieces：[{"path": 錄音段, "skip": 開頭略過秒數, "start": 保留部分在整場的起點}]
        enrollments：[{"name", "path"}] 會前錄的聲音樣本；有的話建 voiceprint 並比對。

        失敗直接丟出：由 LiveSessionManager 決定退回 Gemini 的代號與聲紋比對。
        """
        session_audio = Path(pieces[0]["path"]).parent / "diarize_session.ogg"
        temporary = [session_audio]
        try:
            durations = self._concat(
                [(p["path"], p["skip"]) for p in pieces], session_audio
            )
            placements, cursor = [], 0.0
            for piece, length in zip(pieces, durations):
                placements.append((cursor, cursor + length, float(piece["start"])))
                cursor += length

            voiceprints: dict[str, str] = {}
            for person in enrollments if self.voiceprints_enabled else []:
                # 一個人建不起來（樣本太短、錄壞）只少那一個名字，其他人照做
                try:
                    sample = self._encode(person["path"], max_seconds=VOICEPRINT_MAX_SECONDS)
                    temporary.append(sample)
                    self._report()
                    voiceprints[person["name"]] = self.client.voiceprint(sample)
                except Exception as exc:
                    logger.warning("「%s」的 voiceprint 建立失敗，這個人維持代號：%s", person["name"], exc)

            self._report()
            if voiceprints:
                output = self.client.identify(
                    session_audio, voiceprints, self.voiceprint_threshold
                )
            else:
                output = self.client.diarize(session_audio)
        finally:
            for path in temporary:
                Path(path).unlink(missing_ok=True)

        raw = output.get("exclusiveDiarization") or output.get("diarization") or []
        segments = to_session_time(raw, placements)
        if not segments:
            return transcript, {}
        duration = max(start + (end - begin) for begin, end, start in placements)
        text, stats = relabel(transcript, segments, duration)
        logger.info("即時聆聽講者分離對齊：%s", stats)
        # 只有真的比對過聲紋才有姓名；純分群的結果不該被當成誰的名字
        prior = (
            speaker_prior_from_identify(output.get("voiceprints") or [], code_map(segments))
            if voiceprints else {}
        )
        return text, prior

    def _report(self) -> None:
        if self._on_call:
            self._on_call()

    def _run(self, audio_path: Path) -> tuple[list[dict], float | None]:
        # 音檔長度用原檔量：丟掉「時間戳超出音檔長度」的幻覺行靠的就是它
        duration = self._duration(audio_path)
        encoded = self._encode(audio_path)
        try:
            self._report()
            output = self.client.diarize(encoded)
        finally:
            Path(encoded).unlink(missing_ok=True)
        segments = output.get("exclusiveDiarization") or output.get("diarization") or []
        return segments, duration


def build_diarizer(settings, on_call: Callable[[], None] | None = None) -> Diarizer | None:
    """有金鑰、且轉錄引擎是 Gemini 才建。

    本地 Whisper 的逐字稿沒有時間戳，分群結果對不回任何一行，送去只是白花試用額度。
    """
    if not settings.pyannote_api_key or settings.transcribe_engine != "gemini":
        return None
    client = PyannoteClient(
        settings.pyannote_api_key,
        model=settings.pyannote_model,
        timeout_seconds=settings.diarize_timeout_seconds,
    )
    return Diarizer(
        client,
        on_call=on_call,
        voiceprint_threshold=settings.voiceprint_match_threshold,
        voiceprints_enabled=settings.pyannote_voiceprint_enabled,
    )
