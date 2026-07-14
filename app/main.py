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
from app.agents.reminder_agent import scan as scan_reminders
from app.config import Settings, get_settings
from app.export import meeting_report_md, tasks_to_csv
from app.glossary import Glossary
from app.jobs import MediaJobManager
from app.orchestrator import Orchestrator
from app.rag import AskAgent, GeminiEmbedder, RagIndex
from app.stores import make_store
from app.transcription import media
from app.translate import TARGETS as TRANSLATE_TARGETS
from app.translate import Translator
from app.transcription.gemini_transcriber import GeminiTranscriber
from app.transcription.live_session import LiveSessionManager, SessionNotFound
from app.transcription.transcriber import Transcriber
from app.usage import UsageTracker

STATIC_DIR = Path(__file__).resolve().parent / "static"


# 錄音種類：影響 Decision Agent 的分析重點（見 KIND_HINTS），也存進會議紀錄供分類
MEETING_KINDS = {"會議", "通話", "訪談", "語音備忘錄", "講座", "其它"}


class MeetingRequest(BaseModel):
    text: str
    meeting_date: Optional[date] = None
    kind: Optional[str] = None


class FinishRequest(BaseModel):
    meeting_date: Optional[date] = None
    kind: Optional[str] = None


class AskRequest(BaseModel):
    question: str


class GlossaryRequest(BaseModel):
    terms: list[dict]


class LiveStartRequest(BaseModel):
    translate_to: Optional[str] = None  # "en" / "zh"：逐段即時翻譯


class TranslateRequest(BaseModel):
    text: str
    target: str


def create_app(
    settings: Settings | None = None,
    *,
    store=None,
    orchestrator=None,
    transcriber=None,
    live_manager=None,
    job_manager=None,
    ask_agent=None,
    translator=None,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or make_store(settings)
    # 自訂詞彙表：以 callable 注入，轉錄/分析每次都讀到最新內容
    glossary = Glossary(settings.data_dir / "output" / "glossary.json")
    orchestrator = orchestrator or Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(
            api_key=settings.gemini_api_key,
            api_keys=settings.gemini_api_keys,
            model=settings.gemini_model,
            glossary=glossary.terms,
        ),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(settings.data_dir / "output" / "notifications"),
    )
    if transcriber is None:
        if settings.transcribe_engine == "gemini":
            # 雲端無 GPU：用 Gemini 直接聽音訊轉錄
            transcriber = GeminiTranscriber(
                api_key=settings.gemini_api_key,
                api_keys=settings.gemini_api_keys,
                model=settings.transcribe_model,
                glossary=glossary.terms,
            )
        else:
            transcriber = Transcriber(
                model_size=settings.whisper_model,
                device=settings.whisper_device,
                glossary=glossary.terms,
            )
    # 翻譯與轉錄同樣高頻（即時聆聽逐段翻），用高額度的轉錄模型
    translator = translator or Translator(
        api_key=settings.gemini_api_key,
        api_keys=settings.gemini_api_keys,
        model=settings.transcribe_model,
    )
    live_manager = live_manager or LiveSessionManager(
        transcriber, settings.data_dir / "tmp" / "live", translator=translator
    )
    job_manager = job_manager or MediaJobManager(
        transcriber, orchestrator, settings.data_dir / "tmp"
    )
    rag_index = None
    if ask_agent is None:
        rag_index = RagIndex(
            settings.data_dir / "output" / "rag_index.json",
            GeminiEmbedder(
                api_key=settings.gemini_api_key, api_keys=settings.gemini_api_keys
            ),
        )
        ask_agent = AskAgent(
            index=rag_index,
            store=store,
            api_key=settings.gemini_api_key,
            api_keys=settings.gemini_api_keys,
            model=settings.gemini_model,
        )

    def drop_from_rag(meeting_id: str) -> None:
        """會議被編輯/刪除後索引作廢，下次問答時以新內容重建。"""
        if rag_index is not None:
            rag_index.drop_meeting(meeting_id)
    uploads_dir = settings.data_dir / "tmp" / "uploads"
    usage = UsageTracker(settings.data_dir / "output" / "usage.json")

    app = FastAPI(title="主動式會議 Agent")

    def validate_kind(kind: str | None) -> str | None:
        if kind and kind not in MEETING_KINDS:
            raise HTTPException(
                status_code=400,
                detail=f"kind 只能是：{'、'.join(sorted(MEETING_KINDS))}",
            )
        return kind

    def run_analysis(text: str, meeting_date: date | None, kind: str | None = None) -> dict:
        usage.record("analysis")
        try:
            return orchestrator.process_transcript(text, meeting_date=meeting_date, kind=kind)
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
            "store_backend": getattr(store, "backend", "unknown"),
            "whisper_device": transcriber.device,
            "whisper_model": transcriber.model_size
            or settings.whisper_model
            or "auto（首次轉錄時載入）",
            "live_chunk_seconds": settings.live_chunk_seconds,
        }

    # ---- 輸入路徑 1：純文字 ----

    @app.post("/api/meetings")
    def analyze_meeting(req: MeetingRequest):
        return run_analysis(req.text, req.meeting_date, validate_kind(req.kind))

    @app.get("/api/meetings")
    def list_meetings():
        return {"meetings": store.list_meetings()}

    @app.get("/api/meetings/{meeting_id}")
    def get_meeting_detail(meeting_id: str):
        record = store.get_meeting(meeting_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        return record

    # 會議資訊欄位（存在 meeting 子物件）與頂層欄位分開處理
    _MEETING_INFO_FIELDS = {"title", "date", "summary", "attendees"}
    _MEETING_TOP_FIELDS = {"transcript", "kind", "tags"}

    @app.patch("/api/meetings/{meeting_id}")
    def patch_meeting(meeting_id: str, fields: dict):
        unknown = set(fields) - _MEETING_INFO_FIELDS - _MEETING_TOP_FIELDS
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"不允許修改的欄位：{'、'.join(sorted(unknown))}"
            )
        if "kind" in fields:
            validate_kind(fields["kind"])
        if "tags" in fields and not isinstance(fields["tags"], list):
            raise HTTPException(status_code=400, detail="tags 必須是字串陣列")

        update = {k: v for k, v in fields.items() if k in _MEETING_TOP_FIELDS}
        nested = {k: v for k, v in fields.items() if k in _MEETING_INFO_FIELDS}
        if nested:
            update["meeting"] = nested
        updated = store.update_meeting(meeting_id, update)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        drop_from_rag(meeting_id)
        return updated

    @app.delete("/api/meetings/{meeting_id}")
    def delete_meeting(meeting_id: str):
        if not store.delete_meeting(meeting_id):
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        drop_from_rag(meeting_id)
        return {"deleted": meeting_id}

    @app.post("/api/meetings/{meeting_id}/reanalyze")
    def reanalyze_meeting(meeting_id: str):
        """對（可能已編輯過的）逐字稿重跑 AI 分析：更新會議紀錄、整批換掉任務。"""
        record = store.get_meeting(meeting_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        transcript = (record.get("transcript") or "").strip()
        if not transcript:
            raise HTTPException(status_code=400, detail="此會議沒有逐字稿全文，無法重新分析")

        try:
            meeting_date = date.fromisoformat(record.get("meeting", {}).get("date", ""))
        except ValueError:
            meeting_date = None
        usage.record("analysis")
        try:
            analysis = orchestrator.decision.analyze(
                transcript, meeting_date=meeting_date, kind=record.get("kind")
            )
        except DecisionAgentError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

        dumped = analysis.model_dump(mode="json")
        store.update_meeting(meeting_id, {
            "meeting": dumped["meeting"],
            "decisions": dumped["decisions"],
            "pending_items": dumped["pending_items"],
            "tags": dumped.get("tags", []),
        })
        tasks = store.replace_tasks(meeting_id, dumped["todos"])
        notifications = orchestrator.notifier.notify(meeting_id, analysis)
        drop_from_rag(meeting_id)
        return {
            "meeting_id": meeting_id,
            "analysis": dumped,
            "notifications": notifications,
            "tasks": tasks,
        }

    @app.get("/api/tasks")
    def list_tasks(meeting_id: Optional[str] = None):
        return {"tasks": store.list_tasks(meeting_id=meeting_id)}

    @app.get("/api/usage")
    def get_usage():
        return usage.snapshot()

    @app.get("/api/reminders")
    def get_reminders(days: int = 2):
        """主動提醒：逾期/即將到期/未指派任務的催辦草稿＋未決事項追問。"""
        return scan_reminders(store.list_tasks(), store.list_meetings(), due_soon_days=days)

    @app.post("/api/ask")
    def ask_meetings(req: AskRequest):
        """RAG 跨會議問答：檢索歷史會議片段，交給 Gemini 依據回答。"""
        usage.record("ask")
        try:
            return ask_agent.ask(req.question)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # 金鑰未設、配額爆掉…原因要透明
            raise HTTPException(status_code=502, detail=f"問答失敗：{exc}")

    # ---- 翻譯 ----

    @app.post("/api/translate")
    def translate_text(req: TranslateRequest):
        """通用翻譯：翻譯摘要、歷史會議內容等。"""
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="翻譯內容不可為空")
        if req.target not in TRANSLATE_TARGETS:
            raise HTTPException(
                status_code=400,
                detail=f"target 只支援：{'、'.join(sorted(TRANSLATE_TARGETS))}",
            )
        usage.record("translate")
        try:
            return {"translation": translator.translate(req.text, req.target)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # 金鑰未設、配額爆掉…原因要透明
            raise HTTPException(status_code=502, detail=f"翻譯失敗：{exc}")

    # ---- 自訂詞彙 ----

    @app.get("/api/glossary")
    def get_glossary():
        return {"terms": glossary.terms()}

    @app.put("/api/glossary")
    def put_glossary(req: GlossaryRequest):
        try:
            return {"terms": glossary.replace(req.terms)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

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
        file: UploadFile = File(...),
        meeting_date: Optional[str] = Form(None),
        kind: Optional[str] = Form(None),
    ):
        try:
            parsed_date = date.fromisoformat(meeting_date) if meeting_date else None
        except ValueError:
            raise HTTPException(status_code=400, detail="meeting_date 必須是 YYYY-MM-DD 格式")
        validate_kind(kind)

        suffix = Path(file.filename or "upload.bin").suffix or ".bin"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        dest = uploads_dir / f"{uuid.uuid4().hex[:12]}{suffix}"
        with dest.open("wb") as out:  # 2 小時的影片可能數 GB，串流寫入不佔記憶體
            shutil.copyfileobj(file.file, out)

        usage.record("media_upload")
        return {"job_id": job_manager.submit(dest, meeting_date=parsed_date, kind=kind)}

    @app.get("/api/media/{job_id}")
    def media_status(job_id: str):
        job = job_manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"找不到工作：{job_id}")
        return job

    # ---- 輸入路徑 3：即時聆聽 ----

    @app.post("/api/live/start")
    def live_start(req: Optional[LiveStartRequest] = None):
        translate_to = req.translate_to if req else None
        if translate_to and translate_to not in TRANSLATE_TARGETS:
            raise HTTPException(
                status_code=400,
                detail=f"translate_to 只支援：{'、'.join(sorted(TRANSLATE_TARGETS))}",
            )
        return {"session_id": live_manager.start(translate_to=translate_to)}

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
        result = run_analysis(
            transcript,
            req.meeting_date if req else None,
            validate_kind(req.kind if req else None),
        )
        return {"transcript": transcript, **result}

    return app


app = create_app()
