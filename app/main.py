"""FastAPI 入口：三種輸入路徑（純文字 / 檔案上傳 / 即時聆聽）的 API。

啟動：.venv\\Scripts\\python -m uvicorn app.main:app --reload
"""
from __future__ import annotations

import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.agents.decision_agent import DecisionAgent, DecisionAgentError
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent
from app.config import Settings, get_settings
from app.export import meeting_report_md, tasks_to_csv
from app.jobs import MediaJobManager
from app.orchestrator import Orchestrator
from app.stores.local_store import LocalJsonStore
from app.transcription import media
from app.transcription.gemini_transcriber import GeminiTranscriber
from app.transcription.live_session import LiveSessionManager, SessionNotFound
from app.transcription.transcriber import Transcriber
from app.usage import UsageTracker

STATIC_DIR = Path(__file__).resolve().parent / "static"


class MeetingRequest(BaseModel):
    text: str
    meeting_date: Optional[date] = None


class FinishRequest(BaseModel):
    meeting_date: Optional[date] = None


def create_app(
    settings: Settings | None = None,
    *,
    store=None,
    orchestrator=None,
    transcriber=None,
    live_manager=None,
    job_manager=None,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or LocalJsonStore(settings.data_dir / "output" / "db.json")
    orchestrator = orchestrator or Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(
            api_key=settings.gemini_api_key, model=settings.gemini_model
        ),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(settings.data_dir / "output" / "notifications"),
    )
    if transcriber is None:
        if settings.transcribe_engine == "gemini":
            # 雲端無 GPU：用 Gemini 直接聽音訊轉錄
            transcriber = GeminiTranscriber(
                api_key=settings.gemini_api_key, model=settings.gemini_model
            )
        else:
            transcriber = Transcriber(
                model_size=settings.whisper_model, device=settings.whisper_device
            )
    live_manager = live_manager or LiveSessionManager(
        transcriber, settings.data_dir / "tmp" / "live"
    )
    job_manager = job_manager or MediaJobManager(
        transcriber, orchestrator, settings.data_dir / "tmp"
    )
    uploads_dir = settings.data_dir / "tmp" / "uploads"
    usage = UsageTracker(settings.data_dir / "output" / "usage.json")

    app = FastAPI(title="主動式會議 Agent")

    def run_analysis(text: str, meeting_date: date | None) -> dict:
        usage.record("analysis")
        try:
            return orchestrator.process_transcript(text, meeting_date=meeting_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except DecisionAgentError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "ffmpeg": media.ffmpeg_available(),
            "gemini_key_set": bool(settings.gemini_api_key),
            "gemini_model": settings.gemini_model,
            "transcribe_engine": settings.transcribe_engine,
            "whisper_device": transcriber.device,
            "whisper_model": transcriber.model_size
            or settings.whisper_model
            or "auto（首次轉錄時載入）",
            "live_chunk_seconds": settings.live_chunk_seconds,
        }

    # ---- 輸入路徑 1：純文字 ----

    @app.post("/api/meetings")
    def analyze_meeting(req: MeetingRequest):
        return run_analysis(req.text, req.meeting_date)

    @app.get("/api/meetings")
    def list_meetings():
        return {"meetings": store.list_meetings()}

    @app.get("/api/tasks")
    def list_tasks(meeting_id: Optional[str] = None):
        return {"tasks": store.list_tasks(meeting_id=meeting_id)}

    @app.get("/api/usage")
    def get_usage():
        return usage.snapshot()

    # ---- 任務管理 ----

    _EDITABLE_FIELDS = {"status", "task", "owner", "due_date", "priority"}
    _VALID_STATUS = {"todo", "doing", "done"}

    @app.patch("/api/tasks/{task_id}")
    def patch_task(task_id: str, fields: dict):
        unknown = set(fields) - _EDITABLE_FIELDS
        if unknown:
            raise HTTPException(status_code=400, detail=f"不允許修改的欄位：{'、'.join(sorted(unknown))}")
        if "status" in fields and fields["status"] not in _VALID_STATUS:
            raise HTTPException(status_code=400, detail="status 只能是 todo / doing / done")
        if "priority" in fields and fields["priority"] not in {"high", "medium", "low"}:
            raise HTTPException(status_code=400, detail="priority 只能是 high / medium / low")
        updated = store.update_task(task_id, **fields)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"找不到任務：{task_id}")
        return updated

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str):
        if not store.delete_task(task_id):
            raise HTTPException(status_code=404, detail=f"找不到任務：{task_id}")
        return {"deleted": task_id}

    @app.get("/api/export/tasks.csv")
    def export_tasks_csv():
        return Response(
            content=tasks_to_csv(store.list_tasks()),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="tasks.csv"'},
        )

    @app.get("/api/meetings/{meeting_id}/report.md")
    def meeting_report(meeting_id: str):
        record = store.get_meeting(meeting_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        return Response(
            content=meeting_report_md(record, store.list_tasks(meeting_id=meeting_id)),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="meeting-{meeting_id}.md"'},
        )

    # ---- 輸入路徑 2：音檔 / 影片上傳（背景轉錄） ----

    @app.post("/api/media")
    def upload_media(
        file: UploadFile = File(...), meeting_date: Optional[str] = Form(None)
    ):
        try:
            parsed_date = date.fromisoformat(meeting_date) if meeting_date else None
        except ValueError:
            raise HTTPException(status_code=400, detail="meeting_date 必須是 YYYY-MM-DD 格式")

        suffix = Path(file.filename or "upload.bin").suffix or ".bin"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        dest = uploads_dir / f"{uuid.uuid4().hex[:12]}{suffix}"
        with dest.open("wb") as out:  # 2 小時的影片可能數 GB，串流寫入不佔記憶體
            shutil.copyfileobj(file.file, out)

        usage.record("media_upload")
        return {"job_id": job_manager.submit(dest, meeting_date=parsed_date)}

    @app.get("/api/media/{job_id}")
    def media_status(job_id: str):
        job = job_manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"找不到工作：{job_id}")
        return job

    # ---- 輸入路徑 3：即時聆聽 ----

    @app.post("/api/live/start")
    def live_start():
        return {"session_id": live_manager.start()}

    @app.post("/api/live/{session_id}/chunk")
    def live_chunk(session_id: str, file: UploadFile = File(...)):
        suffix = Path(file.filename or "chunk.webm").suffix or ".webm"
        usage.record("live_chunk")
        try:
            return live_manager.add_chunk(session_id, file.file.read(), suffix=suffix)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # 轉錄後端故障（額度、格式…）要讓前端看得到原因
            raise HTTPException(status_code=502, detail=f"這段音訊轉錄失敗：{exc}")

    @app.post("/api/live/{session_id}/finish")
    def live_finish(session_id: str, req: Optional[FinishRequest] = None):
        try:
            transcript = live_manager.finish(session_id)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not transcript.strip():
            raise HTTPException(
                status_code=400, detail="這場聆聽沒有收到任何語音內容，無法分析"
            )
        result = run_analysis(transcript, req.meeting_date if req else None)
        return {"transcript": transcript, **result}

    return app


app = create_app()
