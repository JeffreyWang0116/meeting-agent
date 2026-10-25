"""音檔/影片的背景轉錄工作。

上傳後立刻回傳 job_id，轉錄（可能耗時數分鐘）在背景執行緒進行，
前端輪詢 GET /api/media/{job_id} 取得進度、部分逐字稿與最終分析結果。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import date
from pathlib import Path

from app.gemini_keys import is_transient_error
from app.glossary import terms_hint_line
from app.stores.base import DEFAULT_USER
from app.transcription import media

logger = logging.getLogger(__name__)


def _error_message(exc: BaseException) -> str:
    """把例外轉成使用者看得懂的訊息。

    Google 端過載（503）吐的是一整包英文 JSON，使用者既看不懂，也不知道該
    重試還是該換檔案——而這偏偏是所有錯誤裡最值得重試的一種。其餘錯誤保留
    原始訊息：那是唯一的線索。MemoryError、OSError 等的 str() 是空字串，
    只記訊息會讓前端退回顯示無資訊的「轉錄失敗」，型別名稱是這時唯一的線索。
    """
    if is_transient_error(exc):
        return (
            "Google 的 AI 模型目前流量過載，自動重試多次仍未成功。"
            "這是暫時狀況，請過幾分鐘再試一次（檔案本身沒有問題）。"
        )
    return str(exc) or type(exc).__name__


INTERRUPTED_MESSAGE = (
    "伺服器重新啟動，這次轉錄中斷了。已經轉好的部分可以先拿去分析，"
    "或重新上傳檔案再轉一次。"
)


class MediaJobManager:
    # 工作紀錄只留一天：夠前端重新整理後找回結果，又不會在資料庫裡越積越多
    JOB_TTL_SECONDS = 24 * 60 * 60

    def __init__(
        self, transcriber, orchestrator, work_dir: Path | str, diarizer=None,
        store=None, clock=time.time,
    ):
        # store：工作紀錄落地，重新部署後還查得到（None＝只放記憶體）
        self._store = store
        self._clock = clock
        self._transcriber = transcriber
        self._orchestrator = orchestrator
        # pyannote 講者分離（選用，見 app/transcription/diarizer.py）。None＝講者照舊由轉錄模型標
        self._diarizer = diarizer
        self._work_dir = Path(work_dir)
        self._jobs: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # 保留最近的工作記錄即可；避免長時間運行下 _jobs / _threads 無限成長
    _MAX_JOBS = 100

    def submit(
        self,
        file_path: Path | str,
        meeting_date: date | None = None,
        kind: str | None = None,
        terms: list[dict] | None = None,
        features: set[str] | None = None,
        correct_typos: bool = False,
        user: str = DEFAULT_USER,
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        self._prune_stored(user)
        with self._lock:
            self._prune_locked()
            self._jobs[job_id] = {
                "id": job_id,
                # 誰送出的。背景執行緒沒有請求上下文，所以在 submit 當下
                # 就把 user 捕捉進工作紀錄，之後轉錄與輪詢都靠它
                "user": user,
                "status": "queued",
                "progress": 0.0,
                "transcript": "",
                "result": None,
                "error": None,
                "file": Path(file_path).name,
                "created_at": self._clock(),
            }
            snapshot = dict(self._jobs[job_id])
        self._persist(snapshot)
        thread = threading.Thread(
            target=self._run,
            args=(
                job_id, Path(file_path), meeting_date, kind, features,
                correct_typos, terms, user,
            ),
            daemon=True,
        )
        self._threads[job_id] = thread
        thread.start()
        return job_id

    def get(self, job_id: str, *, user: str = DEFAULT_USER) -> dict | None:
        """查工作進度。別人的工作一律當作不存在——逐字稿與分析結果都在裡面，
        知道 job_id 就看得到等於沒有隔離。"""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return self._get_stored(job_id, user)
        if job.get("user", DEFAULT_USER) != user:
            return None
        with self._lock:
            return dict(job)

    def _get_stored(self, job_id: str, user: str) -> dict | None:
        """記憶體裡沒有＝伺服器重啟過（或工作早被清掉）。去 store 找。"""
        if self._store is None:
            return None
        try:
            job = self._store.get_job(job_id)
        except Exception:
            logger.exception("讀取工作紀錄失敗：%s", job_id)
            return None
        if job is None or job.get("user", DEFAULT_USER) != user:
            return None
        if self._expired(job):
            self._store.delete_job(job_id)
            return None
        if job.get("status") not in ("done", "error"):
            # 背景執行緒跟著舊行程一起沒了，這份工作不會再有進展
            job.update(status="error", interrupted=True, error=INTERRUPTED_MESSAGE)
        return job

    def _expired(self, job: dict) -> bool:
        return self._clock() - job.get("created_at", 0) > self.JOB_TTL_SECONDS

    def _prune_stored(self, user: str) -> None:
        if self._store is None:
            return
        try:
            for job in self._store.list_jobs(user):
                if self._expired(job):
                    self._store.delete_job(job["id"])
        except Exception:
            logger.exception("清理舊工作紀錄失敗")

    def _persist(self, job: dict) -> None:
        """存工作紀錄只是保險：store 出錯只記 log，轉錄照常進行。"""
        if self._store is None:
            return
        try:
            self._store.save_job(job)
        except Exception:
            logger.exception("儲存工作紀錄失敗：%s", job.get("id"))

    def wait(self, job_id: str, timeout: float | None = None) -> None:
        """等待工作結束（測試與除錯用）。"""
        thread = self._threads.get(job_id)
        if thread:
            thread.join(timeout)

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            self._jobs[job_id].update(fields)
            snapshot = dict(self._jobs[job_id])
        # 鎖外寫入：Firestore 一次往返幾十毫秒，不該卡住前端的輪詢
        self._persist(snapshot)

    def _prune_locked(self) -> None:
        """在鎖內把已結束的舊工作清掉，只保留最近 _MAX_JOBS 筆。"""
        if len(self._jobs) < self._MAX_JOBS:
            return
        finished = [
            jid for jid, j in self._jobs.items() if j["status"] in ("done", "error")
        ]
        for jid in finished[: len(self._jobs) - self._MAX_JOBS + 1]:
            self._jobs.pop(jid, None)
            self._threads.pop(jid, None)

    def _relabel(self, job_id: str, diarization, transcript: str) -> str:
        """用 pyannote 的分群結果重標講者。Diarizer 自己會吞掉 API 失敗；這裡再
        包一層，是防它本身的 bug——講者辨識出錯不能賠上整份已經轉好的逐字稿。"""
        if diarization is not None and not diarization.done():
            self._update(job_id, status="diarizing", progress=1.0, transcript=transcript)
        try:
            return self._diarizer.apply(diarization, transcript)
        except Exception:
            logger.exception("講者重標失敗，沿用轉錄模型的代號：%s", job_id)
            return transcript

    def _run(
        self,
        job_id: str,
        file_path: Path,
        meeting_date: date | None,
        kind: str | None = None,
        features: set[str] | None = None,
        correct_typos: bool = False,
        terms: list[dict] | None = None,
        user: str = DEFAULT_USER,
    ) -> None:
        path = file_path
        try:
            if media.is_video(path) and media.ffmpeg_available():
                self._update(job_id, status="extracting")
                path = media.extract_audio(path)
            # 沒有 ffmpeg 時直接交給 faster-whisper（PyAV 可解常見影片容器的音訊）

            # 分群在轉錄開始前就送出、背景並行：上傳＋分群只要一分鐘左右，
            # 轉錄卻要好幾分鐘，排在後面等於讓使用者多等
            diarization = self._diarizer.start(path) if self._diarizer else None

            self._update(job_id, status="transcribing")
            parts: list[str] = []

            def on_progress(fraction: float, text: str) -> None:
                parts.append(text)
                self._update(job_id, progress=fraction, transcript="".join(parts).strip())

            # 本次專用詞彙走 hint 送進轉錄，「聽」的當下就用對的寫法；
            # 沒有詞彙時不帶這個參數，呼叫形狀與加這個功能之前一模一樣
            hint = terms_hint_line(terms, "本次會議專用詞彙")
            transcript = (
                self._transcriber.transcribe(
                    path, on_progress=on_progress, hint=hint, user=user
                )
                if hint
                else self._transcriber.transcribe(
                    path, on_progress=on_progress, user=user
                )
            )
            if not transcript.strip():
                self._update(
                    job_id,
                    status="error",
                    progress=1.0,
                    error="轉錄不到任何語音：請確認檔案的聲音軌有實際的說話內容（此檔可能是靜音或純音樂）",
                )
                return
            if self._diarizer:
                transcript = self._relabel(job_id, diarization, transcript)
            self._update(job_id, transcript=transcript, progress=1.0, status="analyzing")

            result = self._orchestrator.process_transcript(
                transcript,
                meeting_date=meeting_date,
                kind=kind,
                features=features,
                correct_typos=correct_typos,
                terms=terms,
                # 背景執行緒沒有請求上下文，使用者在 submit 當下就捕捉好
                user=user,
            )
            # 校正過的話逐字稿會變，job 要換成校正後的版本（前端顯示的就是這份）
            self._update(
                job_id,
                status="done",
                result=result,
                transcript=result.get("transcript") or transcript,
            )
        except Exception as exc:
            # 背景執行緒的例外沒有人會看到 traceback，這裡是唯一的記錄點；
            # 少了它，雲端 Logs 一片空白，遠端只能靠猜
            logger.exception("背景轉錄工作失敗：%s", job_id)
            self._update(job_id, status="error", error=_error_message(exc))
        finally:
            # 轉錄後原始上傳檔與抽出的音軌都不再需要，刪掉釋放磁碟
            for p in {file_path, path}:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
