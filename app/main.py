"""FastAPI 入口：三種輸入路徑（純文字 / 檔案上傳 / 即時聆聽）的 API。

啟動：.venv\\Scripts\\python -m uvicorn app.main:app --reload
"""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agents.corrector_agent import CorrectorAgent
from app.agents.decision_agent import FEATURE_KEYS, DecisionAgent, DecisionAgentError
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent
from app.agents.reminder_agent import scan as scan_reminders
from app.config import Settings, get_settings
from app.export import meeting_report_md, tasks_to_csv, tasks_to_ics
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

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


# 錄音種類：影響 Decision Agent 的分析重點（見 KIND_HINTS），也存進會議紀錄供分類
MEETING_KINDS = {"會議", "通話", "訪談", "語音備忘錄", "講座", "其它"}


def validate_features(raw) -> set[str] | None:
    """raw 可以是 list[str]（JSON 請求）或逗號分隔字串（multipart 表單欄位）。
    None 代表使用者沒有明確指定，交給 default_features_for_kind 決定預設值。"""
    if raw is None:
        return None
    keys = [s.strip() for s in raw.split(",") if s.strip()] if isinstance(raw, str) else list(raw)
    unknown = set(keys) - FEATURE_KEYS
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"不支援的 features：{'、'.join(sorted(unknown))}"
        )
    return set(keys)


def default_features_for_kind(kind: str | None) -> set[str]:
    """會議摘要／決議事項／代辦事項只在錄音種類是「會議」（或未指定）時預設開啟，
    其他錄音種類（通話、訪談…）預設不使用，除非使用者明確用 features 勾選開啟。"""
    return set(FEATURE_KEYS) if kind in (None, "會議") else set()


def resolve_features(raw, kind: str | None) -> set[str]:
    explicit = validate_features(raw)
    return explicit if explicit is not None else default_features_for_kind(kind)


def _is_iso_date(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _parse_iso_date_or_none(value) -> date | None:
    """把可能是字串/None/亂填的日期安全轉成 date；轉不動回 None，絕不拋例外。"""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


class MeetingRequest(BaseModel):
    text: str
    meeting_date: Optional[date] = None
    kind: Optional[str] = None
    # 會議摘要／決議事項／代辦事項可各自開關；None＝依 kind 決定預設值
    features: Optional[list[str]] = None
    # 分析前先用 AI 修掉語音辨識的同音錯字（多一次 API 請求）
    correct_typos: bool = False


class FinishRequest(BaseModel):
    meeting_date: Optional[date] = None
    kind: Optional[str] = None
    features: Optional[list[str]] = None
    correct_typos: bool = False


class ReanalyzeRequest(BaseModel):
    features: Optional[list[str]] = None
    correct_typos: bool = False


class AskRequest(BaseModel):
    question: str
    meeting_ids: Optional[list[str]] = None  # 限定檢索範圍（複選會議）；None = 全部


class GlossaryRequest(BaseModel):
    terms: list[dict]


class TaskCreateRequest(BaseModel):
    task: str
    owner: Optional[str] = None
    due_date: Optional[str] = None
    priority: str = "medium"


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
    # 自訂詞彙表：持久化交給 store（本地 JSON / 雲端 Firestore，與任務同後端），
    # 以 callable 注入，轉錄/分析每次都讀到最新內容
    glossary = Glossary(store)
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
        corrector=CorrectorAgent(
            api_key=settings.gemini_api_key,
            api_keys=settings.gemini_api_keys,
            model=settings.correct_model,
            glossary=glossary.terms,
        ),
    )
    if transcriber is None:
        if settings.transcribe_engine == "gemini":
            # 雲端無 GPU：用 Gemini 直接聽音訊轉錄
            transcriber = GeminiTranscriber(
                api_key=settings.gemini_api_key,
                api_keys=settings.gemini_api_keys,
                model=settings.transcribe_model,
                glossary=glossary.terms,
                chunk_seconds=settings.transcribe_chunk_seconds,
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

    # 設了 API_TOKEN 才驗證：本機開發預設不擋，部署到公開網址時務必設定，
    # 否則 /api/backup、/api/restore 等端點任何人都能直接讀寫全部資料
    if settings.api_token:
        expected = f"Bearer {settings.api_token}"

        @app.middleware("http")
        async def require_bearer_token(request: Request, call_next):
            path = request.url.path
            if path.startswith("/api/") and path != "/api/health":
                if request.headers.get("authorization") != expected:
                    return JSONResponse({"detail": "未授權：缺少或錯誤的 API token"}, status_code=401)
            return await call_next(request)

    def validate_kind(kind: str | None) -> str | None:
        if kind and kind not in MEETING_KINDS:
            raise HTTPException(
                status_code=400,
                detail=f"kind 只能是：{'、'.join(sorted(MEETING_KINDS))}",
            )
        return kind

    def run_analysis(
        text: str,
        meeting_date: date | None,
        kind: str | None = None,
        features: set[str] | None = None,
        correct_typos: bool = False,
    ) -> dict:
        usage.record("analysis")
        if correct_typos:
            usage.record("correct")  # 校正是額外一次請求，用量面板要分開看得到
        try:
            return orchestrator.process_transcript(
                text,
                meeting_date=meeting_date,
                kind=kind,
                features=features,
                correct_typos=correct_typos,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except DecisionAgentError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        except HTTPException:
            raise
        except Exception as exc:
            # 預期外的故障（缺套件、網路斷、SDK 改版…）也要回看得懂的訊息，
            # 而不是讓 stack trace 變成前端的 500 Internal Server Error
            logger.exception("分析失敗")
            raise HTTPException(
                status_code=502, detail=f"分析失敗（{type(exc).__name__}）：{exc}"
            )

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    # 前端靜態檔（style.css / app.js / icon.svg）統一由 /static 供應
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---- PWA：manifest / service worker ----
    # sw.js 必須從根路徑供應，service worker 的 scope 才涵蓋整個站

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def pwa_manifest():
        return FileResponse(
            STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json"
        )

    @app.get("/sw.js", include_in_schema=False)
    def pwa_sw():
        return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript")

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
            # 長音檔分段轉錄的每段秒數（0＝不分段）。放在 health 是為了能從
            # 外部確認部署版到底有沒有帶上這個功能
            "transcribe_chunk_seconds": settings.transcribe_chunk_seconds,
        }

    # ---- 輸入路徑 1：純文字 ----

    @app.post("/api/meetings")
    def analyze_meeting(req: MeetingRequest):
        kind = validate_kind(req.kind)
        return run_analysis(
            req.text,
            req.meeting_date,
            kind,
            resolve_features(req.features, kind),
            correct_typos=req.correct_typos,
        )

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
        if "tags" in fields and not _is_str_list(fields["tags"]):
            raise HTTPException(status_code=400, detail="tags 必須是字串陣列")
        if "attendees" in fields and not _is_str_list(fields["attendees"]):
            raise HTTPException(status_code=400, detail="attendees 必須是字串陣列")
        if "date" in fields and not _is_iso_date(fields["date"]):
            raise HTTPException(status_code=400, detail="date 必須是 YYYY-MM-DD 格式")

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
    def reanalyze_meeting(meeting_id: str, req: Optional[ReanalyzeRequest] = None):
        """對（可能已編輯過的）逐字稿重跑 AI 分析：更新會議紀錄、整批換掉任務。"""
        record = store.get_meeting(meeting_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        transcript = (record.get("transcript") or "").strip()
        if not transcript:
            raise HTTPException(status_code=400, detail="此會議沒有逐字稿全文，無法重新分析")

        kind = record.get("kind")
        features = resolve_features(req.features if req else None, kind)
        meeting_date = _parse_iso_date_or_none(record.get("meeting", {}).get("date"))
        usage.record("analysis")

        corrections: list[dict] = []
        if req and req.correct_typos and orchestrator.corrector:
            usage.record("correct")
            transcript, corrections = orchestrator.corrector.correct(transcript)

        try:
            analysis = orchestrator.decision.analyze(
                transcript, meeting_date=meeting_date, kind=kind, features=features
            )
        except DecisionAgentError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        except Exception as exc:  # 同 run_analysis：預期外故障也要回看得懂的訊息
            logger.exception("重新分析失敗")
            raise HTTPException(
                status_code=502, detail=f"重新分析失敗（{type(exc).__name__}）：{exc}"
            )

        dumped = analysis.model_dump(mode="json")
        updates = {
            "meeting": dumped["meeting"],
            "decisions": dumped["decisions"],
            "pending_items": dumped["pending_items"],
            "highlights": dumped.get("highlights", []),
            "tags": dumped.get("tags", []),
        }
        if corrections:  # 校正過才回寫逐字稿，沒改就不動原紀錄
            updates["transcript"] = transcript
        store.update_meeting(meeting_id, updates)
        tasks = store.replace_tasks(meeting_id, dumped["todos"])
        notifications = orchestrator.notifier.notify(meeting_id, analysis)
        drop_from_rag(meeting_id)
        return {
            "meeting_id": meeting_id,
            "analysis": dumped,
            "notifications": notifications,
            "tasks": tasks,
            "transcript": transcript,
            "corrections": corrections,
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

    @app.get("/api/search")
    def keyword_search(q: str = ""):
        """關鍵字精確搜尋（標題/摘要/決議/逐字稿），與語意問答互補。"""
        keyword = q.strip()
        if not keyword:
            raise HTTPException(status_code=400, detail="請輸入要搜尋的關鍵字")
        kw = keyword.lower()
        hits = []
        for meta in store.list_meetings():
            record = store.get_meeting(meta["id"]) or meta
            info = record.get("meeting", {})
            fields = [
                ("標題", info.get("title") or ""),
                ("摘要", info.get("summary") or ""),
                ("決議", "\n".join(d.get("description") or "" for d in record.get("decisions", []))),
                ("逐字稿", record.get("transcript") or ""),
            ]
            for label, text in fields:
                idx = text.lower().find(kw)
                if idx < 0:
                    continue
                start = max(0, idx - 30)
                end = min(len(text), idx + len(keyword) + 50)
                snippet = (
                    ("…" if start > 0 else "")
                    + text[start:end].replace("\n", " ")
                    + ("…" if end < len(text) else "")
                )
                hits.append({
                    "meeting_id": record["id"],
                    "title": info.get("title", ""),
                    "date": info.get("date", ""),
                    "field": label,
                    "snippet": snippet,
                })
                break  # 每場會議最多回一筆命中
            if len(hits) >= 20:
                break
        return {"keyword": keyword, "hits": hits}

    @app.post("/api/ask")
    def ask_meetings(req: AskRequest):
        """RAG 跨會議問答：檢索歷史會議片段，交給 Gemini 依據回答。"""
        usage.record("ask")
        try:
            return ask_agent.ask(req.question, meeting_ids=req.meeting_ids)
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

    @app.post("/api/tasks")
    def create_task(req: TaskCreateRequest):
        """手動新增一筆任務（會議之外臨時想到的待辦），不綁定任何會議。"""
        task = req.task.strip()
        if not task:
            raise HTTPException(status_code=400, detail="任務名稱不可為空")
        if req.priority not in {"high", "medium", "low"}:
            raise HTTPException(status_code=400, detail="priority 只能是 high / medium / low")
        if req.due_date not in (None, "") and not _is_iso_date(req.due_date):
            raise HTTPException(status_code=400, detail="due_date 必須是 YYYY-MM-DD 格式或留空")
        return store.add_task({
            "task": task,
            "owner": (req.owner or "").strip() or None,
            "due_date": req.due_date or None,
            "priority": req.priority,
            "meeting_id": None,
        })

    @app.patch("/api/tasks/{task_id}")
    def patch_task(task_id: str, fields: dict):
        unknown = set(fields) - _EDITABLE_FIELDS
        if unknown:
            raise HTTPException(status_code=400, detail=f"不允許修改的欄位：{'、'.join(sorted(unknown))}")
        if "status" in fields and fields["status"] not in _VALID_STATUS:
            raise HTTPException(status_code=400, detail="status 只能是 todo / doing / done")
        if "priority" in fields and fields["priority"] not in {"high", "medium", "low"}:
            raise HTTPException(status_code=400, detail="priority 只能是 high / medium / low")
        if (
            "due_date" in fields
            and fields["due_date"] not in (None, "")
            and not _is_iso_date(fields["due_date"])
        ):
            raise HTTPException(status_code=400, detail="due_date 必須是 YYYY-MM-DD 格式或留空")
        updated = store.update_task(task_id, **fields)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"找不到任務：{task_id}")
        return updated

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str):
        if not store.delete_task(task_id):
            raise HTTPException(status_code=404, detail=f"找不到任務：{task_id}")
        return {"deleted": task_id}

    @app.get("/api/backup")
    def download_backup():
        """整份資料（會議＋任務＋詞彙）打包成 JSON 下載，供離線保存或搬移。"""
        return Response(
            content=json.dumps(store.export_all(), ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="meeting-agent-backup.json"'},
        )

    @app.post("/api/restore")
    def restore_backup(data: dict):
        """以備份 JSON 整份覆蓋現有資料。"""
        if not isinstance(data.get("meetings"), list) or not isinstance(data.get("tasks"), list):
            raise HTTPException(
                status_code=400, detail="備份格式不正確：需要 meetings 與 tasks 陣列"
            )
        store.import_all(data)
        if rag_index is not None:  # 舊向量已不對應新資料，整份作廢待重建
            rag_index.reset()
        return {
            "restored": {"meetings": len(data["meetings"]), "tasks": len(data["tasks"])}
        }

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

    @app.get("/api/meetings/{meeting_id}/events.ics")
    def meeting_events_ics(meeting_id: str):
        """把此會議含期限的任務匯出成 .ics，一鍵加入 Google/Apple 行事曆。"""
        record = store.get_meeting(meeting_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        content = tasks_to_ics(
            record.get("meeting", {}).get("title", ""),
            store.list_tasks(meeting_id=meeting_id),
        )
        return Response(
            content=content,
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="meeting-{meeting_id}.ics"'},
        )

    # ---- 輸入路徑 2：音檔 / 影片上傳（背景轉錄） ----

    @app.post("/api/media")
    def upload_media(
        file: UploadFile = File(...),
        meeting_date: Optional[str] = Form(None),
        kind: Optional[str] = Form(None),
        features: Optional[str] = Form(None),
        correct_typos: Optional[str] = Form(None),
    ):
        try:
            parsed_date = date.fromisoformat(meeting_date) if meeting_date else None
        except ValueError:
            raise HTTPException(status_code=400, detail="meeting_date 必須是 YYYY-MM-DD 格式")
        validate_kind(kind)
        resolved_features = resolve_features(features, kind)
        # multipart 表單只有字串，"true"/"1" 都當開啟
        correct = str(correct_typos or "").lower() in ("1", "true", "on", "yes")

        suffix = Path(file.filename or "upload.bin").suffix or ".bin"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        dest = uploads_dir / f"{uuid.uuid4().hex[:12]}{suffix}"
        with dest.open("wb") as out:  # 2 小時的影片可能數 GB，串流寫入不佔記憶體
            shutil.copyfileobj(file.file, out)

        usage.record("media_upload")
        if correct:
            usage.record("correct")
        return {
            "job_id": job_manager.submit(
                dest,
                meeting_date=parsed_date,
                kind=kind,
                features=resolved_features,
                correct_typos=correct,
            )
        }

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
    def live_chunk(
        session_id: str,
        file: UploadFile = File(...),
        offset: Optional[float] = Form(None),  # 本段在整場會議中的開始秒數
    ):
        suffix = Path(file.filename or "chunk.webm").suffix or ".webm"
        usage.record("live_chunk")
        try:
            return live_manager.add_chunk(
                session_id, file.file.read(), suffix=suffix, offset_seconds=offset
            )
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
        kind = validate_kind(req.kind if req else None)
        features = resolve_features(req.features if req else None, kind)
        result = run_analysis(
            transcript,
            req.meeting_date if req else None,
            kind,
            features,
            correct_typos=bool(req and req.correct_typos),
        )
        # result 帶著校正後的 transcript，放在後面覆蓋原始版本
        return {"transcript": transcript, **result}

    return app


app = create_app()
