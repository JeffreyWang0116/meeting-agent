"""音檔/影片的背景轉錄工作。

上傳後立刻回傳 job_id，轉錄（可能耗時數分鐘）在背景執行緒進行，
前端輪詢 GET /api/media/{job_id} 取得進度、部分逐字稿與最終分析結果。
"""
from __future__ import annotations

import threading
import uuid
from datetime import date
from pathlib import Path

from app.transcription import media


class MediaJobManager:
    def __init__(self, transcriber, orchestrator, work_dir: Path | str):
        self._transcriber = transcriber
        self._orchestrator = orchestrator
        self._work_dir = Path(work_dir)
        self._jobs: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def submit(self, file_path: Path | str, meeting_date: date | None = None) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress": 0.0,
                "transcript": "",
                "result": None,
                "error": None,
                "file": Path(file_path).name,
            }
        thread = threading.Thread(
            target=self._run, args=(job_id, Path(file_path), meeting_date), daemon=True
        )
        self._threads[job_id] = thread
        thread.start()
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def wait(self, job_id: str, timeout: float | None = None) -> None:
        """等待工作結束（測試與除錯用）。"""
        thread = self._threads.get(job_id)
        if thread:
            thread.join(timeout)

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            self._jobs[job_id].update(fields)

    def _run(self, job_id: str, file_path: Path, meeting_date: date | None) -> None:
        try:
            path = file_path
            if media.is_video(path) and media.ffmpeg_available():
                self._update(job_id, status="extracting")
                path = media.extract_audio(path)
            # 沒有 ffmpeg 時直接交給 faster-whisper（PyAV 可解常見影片容器的音訊）

            self._update(job_id, status="transcribing")
            parts: list[str] = []

            def on_progress(fraction: float, text: str) -> None:
                parts.append(text)
                self._update(job_id, progress=fraction, transcript="".join(parts).strip())

            transcript = self._transcriber.transcribe(path, on_progress=on_progress)
            if not transcript.strip():
                self._update(
                    job_id,
                    status="error",
                    progress=1.0,
                    error="轉錄不到任何語音：請確認檔案的聲音軌有實際的說話內容（此檔可能是靜音或純音樂）",
                )
                return
            self._update(job_id, transcript=transcript, progress=1.0, status="analyzing")

            result = self._orchestrator.process_transcript(transcript, meeting_date=meeting_date)
            self._update(job_id, status="done", result=result)
        except Exception as exc:  # 背景執行緒的例外必須被記錄，否則前端永遠在等
            self._update(job_id, status="error", error=str(exc))
