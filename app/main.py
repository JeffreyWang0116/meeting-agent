"""FastAPI 入口：三種輸入路徑（純文字 / 檔案上傳 / 即時聆聽）的 API。

啟動：.venv\\Scripts\\python -m uvicorn app.main:app --reload
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agents.corrector_agent import CorrectorAgent
from app.agents.speaker_namer_agent import SpeakerNamerAgent
from app.agents.decision_agent import (
    FEATURE_KEYS,
    DEFAULT_KIND,
    KIND_DEFAULT_FEATURES,
    KIND_GROUPS,
    KIND_HINTS,
    LEGACY_KINDS,
    MEETING_KINDS,
    DecisionAgent,
    DecisionAgentError,
    default_features_for_kind,
)
from app.agents.executor_agent import ExecutorAgent
from app.agents.notifier_agent import NotifierAgent
from app.agents.parser_agent import ParserAgent
from app.agents.reminder_agent import scan as scan_reminders
from app.auth import CURRENT_USER, AuthError, bearer_token, verify_firebase_id_token
from app.config import Settings, get_settings
from app.export import meeting_report_md, tasks_to_csv, tasks_to_ics
from app.glossary import Glossary, clean_terms
from app.jobs import MediaJobManager
from app.orchestrator import Orchestrator
from app.rag import AskAgent, GeminiEmbedder, RagIndex
from app.stores import make_store
from app.transcription import media
from app.transcription.segments import parse_time_label, replace_term_in_range
from app.translate import TARGETS as TRANSLATE_TARGETS
from app.translate import Translator
from app.transcription.gemini_transcriber import GeminiTranscriber
from app.transcription.live_session import LiveSessionManager, SessionNotFound
from app.transcription.voice_match import VoiceMatcher
from app.transcription.transcriber import Transcriber
from app.usage import UsageTracker

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def asset_version(static_dir: Path) -> str:
    """前端資產的版本字串，取 style.css / app.js 之中較新的 mtime。

    Cache-Control 只管得到「之後才存進去」的快取。在加上這個標頭之前就被瀏覽器
    存下來的舊 CSS/JS，會依啟發式規則自認新鮮、完全不回來問伺服器——改版後畫面
    壞掉的就是這批人。要叫得動那種快取，只能換掉 URL。

    入口 HTML 本身是 no-cache，每次都會拿到最新的版本字串，所以帶版本的網址
    一定跟得上。開發時改完 CSS 直接重新整理就生效，不必再硬重新整理。
    """
    stamps = []
    # js/ 底下每一支模組都算：改到其中任一支都要能換掉舊快取
    for path in [static_dir / "style.css", *sorted((static_dir / "js").glob("*.js"))]:
        try:
            stamps.append(path.stat().st_mtime_ns)
        except OSError:
            pass  # 檔案不在（測試用的空目錄）就當作版本 0，不要讓首頁掛掉
    return format(max(stamps, default=0) // 1_000_000, "x")


class NoCacheStatic(StaticFiles):
    """前端檔案一改就要生效：只做協商快取（每次帶 ETag 問一次，沒變就回 304），
    不讓瀏覽器用啟發式規則自己留舊檔。部署後看到的還是舊版 CSS 是最難查的 bug。"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# 不需要登入的 API：健康檢查（外部監控要打得到）與登入設定本身
# （還沒登入的人正是要靠它才知道怎麼登入）
PUBLIC_API_PATHS = {"/api/health", "/api/auth/config"}


class RequireFirebaseLogin:
    """驗 ID token，並把 uid 放進 CURRENT_USER 供 current_user() 讀取。

    刻意寫成純 ASGI 中介層而不是 @app.middleware("http")：後者會把下游應用丟到
    另一個 task 執行，contextvar 傳不傳得過去得看 Starlette 版本臉色。純 ASGI
    中介層與端點在同一個 task 內，設進去的值必定讀得到。
    """

    def __init__(self, app, verify):
        self.app = app
        self._verify = verify

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if not path.startswith("/api/") or path in PUBLIC_API_PATHS:
            return await self.app(scope, receive, send)

        raw = dict(scope.get("headers") or [])
        token = bearer_token(
            (raw.get(b"authorization") or b"").decode("latin-1") or None
        )
        if token is None:
            return await self._deny(scope, receive, send, "未登入：請先用 Google 登入")
        try:
            uid = self._verify(token)
        except AuthError as exc:
            return await self._deny(scope, receive, send, str(exc))

        reset = CURRENT_USER.set(uid)
        try:
            await self.app(scope, receive, send)
        finally:
            CURRENT_USER.reset(reset)

    @staticmethod
    async def _deny(scope, receive, send, detail: str):
        await JSONResponse({"detail": detail}, status_code=401)(scope, receive, send)


# 會議種類與各自的預設區塊都定義在 decision_agent（單一來源），這裡只負責驗證與對外暴露


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


# 本次專用詞彙的上限：比全域詞彙表短很多，擋掉「整份貼上來」的誤用
MAX_MEETING_TERMS = 50

# 可以轉錄的副檔名。白名單而非黑名單：轉錄後端只吃得下音影格式，其餘的檔案
# 落地也只是白佔磁碟，不如在寫入前就擋掉
MEDIA_SUFFIXES = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma", ".amr",
    ".webm", ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".3gp",
    ".mpeg", ".mpg", ".ts",
}

_UPLOAD_READ_SIZE = 1024 * 1024


def validate_media_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in MEDIA_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的檔案格式：{suffix or '（無副檔名）'}，請上傳音訊或影片檔",
        )
    return suffix


def read_capped(src, max_bytes: int) -> bytes:
    """整段讀進記憶體的上傳（即時聆聽的音訊段）同樣要有上限。落地的檔案至少
    只佔磁碟，這裡佔的是行程記憶體——免費層更禁不起。多讀一個位元組就能分辨
    「剛好等於上限」與「超過」，不必先把整份收下來才知道太大。"""
    data = src.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"這段音訊超過 {max_bytes // (1024 * 1024)}MB 上限",
        )
    return data


def save_upload(src, dest: Path, max_bytes: int) -> int:
    """把上傳串流寫進 dest，超過上限就中止。回傳實際寫入的位元組數。

    上限必須「邊寫邊檢查」：等檔案整份落地再看大小已經沒有意義，磁碟那時
    早就被吃掉了。中途放棄時要把半截檔案刪掉，否則失敗的上傳反而變成
    清不掉的垃圾。
    """
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := src.read(_UPLOAD_READ_SIZE):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"檔案超過 {max_bytes // (1024 * 1024)}MB 上限，"
                               "請先剪短或轉成音訊檔再上傳",
                    )
                out.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="上傳的檔案是空的")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return written


def validate_terms(raw) -> list[dict] | None:
    """raw 可以是 list[dict]（JSON 請求）或 JSON 字串（multipart 表單欄位）。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="terms 不是合法的 JSON")
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="terms 必須是陣列")
    try:
        return clean_terms(raw, MAX_MEETING_TERMS) or None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
    # 把講者A/B/C 代號換成真實姓名（多一次 API 請求）。預設關閉：台語等
    # 語者辨識不穩的錄音容易對錯，猜錯的名字比代號更糟
    name_speakers: bool = False
    # 本次會議專用詞彙，與全域詞彙表合併使用
    terms: Optional[list[dict]] = None


class FinishRequest(BaseModel):
    meeting_date: Optional[date] = None
    kind: Optional[str] = None
    features: Optional[list[str]] = None
    correct_typos: bool = False
    name_speakers: bool = False
    terms: Optional[list[dict]] = None


class ReanalyzeRequest(BaseModel):
    features: Optional[list[str]] = None
    correct_typos: bool = False
    name_speakers: bool = False


class ReplaceTermRequest(BaseModel):
    old: str
    new: str = ""  # 允許空字串＝把該詞整個刪掉
    add_to_glossary: bool = False
    start: Optional[str] = None  # 時間段下限，如 "12:30"；None/空＝不限
    end: Optional[str] = None    # 時間段上限；只換時間戳落在 [start, end] 的行


class AskRequest(BaseModel):
    question: str
    meeting_ids: Optional[list[str]] = None  # 限定檢索範圍（複選會議）；None = 全部


class PersonNamesRequest(BaseModel):
    names: list[str]


class GlossaryRequest(BaseModel):
    terms: list[dict]


class TaskCreateRequest(BaseModel):
    task: str
    owner: Optional[str] = None
    due_date: Optional[str] = None
    priority: str = "medium"


class LiveStartRequest(BaseModel):
    translate_to: Optional[str] = None  # "en" / "zh"：逐段即時翻譯
    terms: Optional[list[dict]] = None  # 本次專用詞彙：進每段轉錄的提示


class TranslateRequest(BaseModel):
    text: str
    target: str


def current_user(request: Request | None = None) -> str:
    """這個請求屬於誰。

    啟用 Firebase Auth 時，中介層驗完 ID token 就把 uid 放進 CURRENT_USER，
    這裡讀出來；沒啟用（本機開發、或只設了共用 API_TOKEN）時是 DEFAULT_USER，
    行為與帳號制上線前完全一樣。

    舊資料沒有 user 欄位，store 一律視為 DEFAULT_USER 的——所以本機既有的
    會議在啟用登入後不會消失，只是歸在單人模式那一格。
    """
    return CURRENT_USER.get()


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
    verify_token=None,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or make_store(settings)
    # 用量統計要在 agent 之前建好：每個會打 Gemini 的元件都得拿到 record_call，
    # 統計才算得到重試與換金鑰（端點層只知道「使用者按了幾次」，差一個數量級）
    usage = UsageTracker(settings.data_dir / "output" / "usage.json")

    def record_call() -> None:
        usage.record("gemini_call")

    # 自訂詞彙表：持久化交給 store（本地 JSON / 雲端 Firestore，與任務同後端），
    # 以 callable 注入，轉錄/分析每次都讀到最新內容
    glossary = Glossary(store)
    orchestrator = orchestrator or Orchestrator(
        parser=ParserAgent(),
        decision=DecisionAgent(
            api_key=settings.gemini_api_key,
            api_keys=settings.gemini_api_keys,
            on_call=record_call,
            model=settings.gemini_model,
            glossary=glossary.terms,
        ),
        executor=ExecutorAgent(store),
        notifier=NotifierAgent(settings.data_dir / "output" / "notifications"),
        corrector=CorrectorAgent(
            api_key=settings.gemini_api_key,
            api_keys=settings.gemini_api_keys,
            on_call=record_call,
            model=settings.correct_model,
            glossary=glossary.terms,
        ),
        namer=SpeakerNamerAgent(
            api_key=settings.gemini_api_key,
            api_keys=settings.gemini_api_keys,
            on_call=record_call,
            # 依上下文判讀「誰是誰」，與校正同屬機械性工作，用便宜模型即可
            model=settings.correct_model,
            # 人名就是詞彙表裡標成人名的項目：使用者只維護一份清單，
            # 而且那些名字同時餵進轉錄，不會再被聽成別的字
            known_names=glossary.person_names,
            remember_names=glossary.remember_persons,
        ),
    )
    if transcriber is None:
        if settings.transcribe_engine == "gemini":
            # 雲端無 GPU：用 Gemini 直接聽音訊轉錄
            transcriber = GeminiTranscriber(
                api_key=settings.gemini_api_key,
                api_keys=settings.gemini_api_keys,
                on_call=record_call,
                model=settings.transcribe_model,
                glossary=glossary.terms,
                chunk_seconds=settings.transcribe_chunk_seconds,
                fallback_model=settings.transcribe_fallback_model,
                max_fallback_chunks=settings.transcribe_max_fallback_chunks,
                label_retries=settings.transcribe_label_retries,
                overlap_seconds=settings.transcribe_overlap_seconds,
                max_retry_calls=settings.transcribe_max_retry_calls,
                # 長檔改用強模型整份單次轉錄：語者分辨遠優於 lite 分段
                strong_model=settings.transcribe_fallback_model,
                strong_whole_threshold=settings.transcribe_long_file_threshold_seconds,
                # 聲紋跨段接力（暫時停用，程式碼與測試都保留）：分段轉錄時把已
                # 確立的講者聲音樣本接力餵給後續分段，讓模型靠嗓音沿用同一代號。
                # 機制與失敗處理都做完也測過了，但每段轉錄都要重傳全部樣本（重試
                # 也會重傳），一支 77 分鐘的檔約多 240~400 次上傳往返、多花數分鐘，
                # 而「模型是否真的因此標對代號」還沒用真檔驗證過。先不接上去，
                # 等實測確認值得再說。
                # 啟用方式：取消下面這行註解（額度／延遲的取捨見 README）
                # voice_relay_max_speakers=settings.voice_relay_max_speakers,
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
        on_call=record_call,
        model=settings.transcribe_model,
    )
    if live_manager is None:
        # 預錄聲音辨識人（選用）：沒設定人數上限就整個不建，等同功能不存在
        voice_matcher = (
            VoiceMatcher(
                api_key=settings.gemini_api_key,
                api_keys=settings.gemini_api_keys,
                on_call=record_call,
                # 一場會議只打一次，用強模型換準確度很划算——比對嗓音比轉錄
                # 吃力得多，lite 實測容易亂猜
                model=settings.voice_match_model or settings.correct_model,
            )
            if settings.live_enroll_max_speakers > 0
            else None
        )
        live_manager = LiveSessionManager(
            transcriber,
            settings.data_dir / "tmp" / "live",
            translator=translator,
            voice_matcher=voice_matcher,
        )
        live_manager.MAX_ENROLLMENTS = settings.live_enroll_max_speakers
    job_manager = job_manager or MediaJobManager(
        transcriber, orchestrator, settings.data_dir / "tmp"
    )
    rag_index = None
    if ask_agent is None:
        rag_index = RagIndex(
            settings.data_dir / "output" / "rag_index.json",
            GeminiEmbedder(
                api_key=settings.gemini_api_key,
                api_keys=settings.gemini_api_keys,
                on_call=record_call,
            ),
        )
        ask_agent = AskAgent(
            index=rag_index,
            store=store,
            api_key=settings.gemini_api_key,
            api_keys=settings.gemini_api_keys,
            on_call=record_call,
            model=settings.gemini_model,
        )

    def drop_from_rag(meeting_id: str) -> None:
        """會議被編輯/刪除後索引作廢，下次問答時以新內容重建。"""
        if rag_index is not None:
            rag_index.drop_meeting(meeting_id)
    uploads_dir = settings.data_dir / "tmp" / "uploads"
    max_upload_bytes = settings.max_upload_mb * 1024 * 1024

    app = FastAPI(title="會議助手")

    if settings.is_public_deploy and not settings.auth_configured and not settings.allow_no_auth:
        raise RuntimeError(
            "這是公開部署（偵測到 RENDER），但兩種把關方式一個都沒設："
            "沒有 FIREBASE_WEB_API_KEY / FIREBASE_AUTH_DOMAIN（Google 登入），"
            "也沒有 API_TOKEN（共用鑰匙）。"
            "這樣任何拿到網址的人都能讀取全部會議逐字稿、刪除資料、燒光 Gemini 額度。"
            "這裡刻意讓啟動失敗，而不是照常起來——後者最危險的地方在於它看起來"
            "一切正常：服務活著、首頁打得開，沒有任何徵兆顯示門是開的。"
            "真的要開一個沒有門的公開站，設 ALLOW_NO_AUTH=1 明講。"
        )

    if settings.auth_enabled:
        # Firebase Auth：一人一份資料。優先於共用 API_TOKEN——兩個都設的時候，
        # 共用鑰匙不該還能繞過帳號制
        verify = verify_token or verify_firebase_id_token
        if verify is verify_firebase_id_token:
            from app.firebase import (  # 驗簽需要已初始化的 firebase app
                ensure_app,
                require_matching_project,
            )

            if not (settings.firebase_credentials_json or settings.firebase_credentials_file):
                raise RuntimeError(
                    "設了 Firebase 登入（FIREBASE_WEB_API_KEY / FIREBASE_AUTH_DOMAIN）"
                    "卻少了 FIREBASE_CREDENTIALS_JSON 或 FIREBASE_CREDENTIALS_FILE："
                    "驗證 ID token 需要 service account 金鑰。"
                    "這裡刻意讓啟動失敗，而不是悄悄退回單人模式——後者會讓人"
                    "以為網站已經上鎖，實際上是全開的，而且完全沒有徵兆。"
                )
            # 憑證存在還不夠，還得跟前端是同一個專案；不然登入過得去、
            # 每個 API 都失敗，而錯誤完全不指向真正的原因
            require_matching_project(
                project_id=settings.firebase_project_id,
                cred_json=settings.firebase_credentials_json,
                cred_file=settings.firebase_credentials_file,
            )
            ensure_app(
                cred_json=settings.firebase_credentials_json,
                cred_file=settings.firebase_credentials_file,
            )

        app.add_middleware(RequireFirebaseLogin, verify=verify)
    elif settings.api_token:
        # 沒接登入時的退路：一把共用鑰匙。部署到公開網址至少要設這個，
        # 否則 /api/backup、/api/restore 等端點任何人都能直接讀寫全部資料
        expected = f"Bearer {settings.api_token}"

        @app.middleware("http")
        async def require_bearer_token(request: Request, call_next):
            path = request.url.path
            if path.startswith("/api/") and path not in PUBLIC_API_PATHS:
                if request.headers.get("authorization") != expected:
                    return JSONResponse({"detail": "未授權：缺少或錯誤的 API token"}, status_code=401)
            return await call_next(request)

    @app.get("/api/auth/config")
    def auth_config():
        """前端登入需要的設定。這幾個值本來就是公開的（Firebase 的安全性靠
        Auth 規則與後端驗簽，不靠把 apiKey 藏起來），所以不需要認證——何況
        沒登入的人正是要靠它才知道怎麼登入。"""
        return {
            "enabled": settings.auth_enabled,
            "apiKey": settings.firebase_web_api_key,
            "authDomain": settings.firebase_auth_domain,
            "projectId": settings.firebase_project_id,
        }

    def validate_kind(kind: str | None) -> str | None:
        # LEGACY_KINDS 一併放行：改版前存下來的會議帶的是舊的錄音種類值，
        # 編輯或重新分析那些紀錄時不該被擋下來
        if kind and kind not in MEETING_KINDS and kind not in LEGACY_KINDS:
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
        name_speakers: bool = False,
        terms: list[dict] | None = None,
        speaker_prior: dict[str, str] | None = None,
    ) -> dict:
        usage.record("analysis")
        if correct_typos:
            usage.record("correct")  # 校正是額外一次請求，用量面板要分開看得到
        # 會前錄了聲音樣本就等於明確要求對應姓名了，不必再另外勾一次「辨識名稱」
        # ——錄了樣本卻還看到「講者A」，在使用者眼裡就是功能沒生效
        name_speakers = name_speakers or bool(speaker_prior)
        if name_speakers and orchestrator.namer:
            usage.record("speaker_names")  # 講者代號換姓名也是獨立一次請求
        try:
            return orchestrator.process_transcript(
                text,
                meeting_date=meeting_date,
                kind=kind,
                features=features,
                correct_typos=correct_typos,
                name_speakers=name_speakers,
                terms=terms,
                user=current_user(),
                speaker_prior=speaker_prior,
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
        # 入口 HTML 只做協商快取：它一舊，底下所有資產的版本就都跟著錯。
        # 順手把 css/js 的網址蓋上版本（icons.svg 不用，見 asset_version）
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        version = asset_version(STATIC_DIR)
        # 注意：main.js 帶版本不會傳給它 import 的子模組，
        # 那些靠 /static 的 Cache-Control: no-cache 每次重新驗證
        for name in ("style.css", "js/main.js", "orb.js"):
            html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={version}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    # 前端靜態檔（style.css / app.js / icon.svg）統一由 /static 供應
    app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")

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
            # 前端據此決定「預錄聲音辨識人」最多能列幾個人（0＝不顯示這個功能）
            "live_enroll_max_speakers": settings.live_enroll_max_speakers,
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
            name_speakers=req.name_speakers,
            terms=validate_terms(req.terms),
        )

    @app.get("/api/meeting-kinds")
    def list_meeting_kinds():
        """會議種類清單：下拉選單、各種類的預設區塊、提示文字都從這裡來，
        前後端不用各維護一份名單。"""
        return {
            "default": DEFAULT_KIND,
            "groups": [
                {
                    "label": label,
                    "kinds": [
                        {
                            "value": k,
                            "hint": KIND_HINTS[k],
                            "features": sorted(KIND_DEFAULT_FEATURES.get(k, FEATURE_KEYS)),
                        }
                        for k in kinds
                    ],
                }
                for label, kinds in KIND_GROUPS
            ],
        }

    @app.get("/api/meetings")
    def list_meetings():
        return {"meetings": store.list_meetings(user=current_user())}

    @app.get("/api/meetings/{meeting_id}")
    def get_meeting_detail(meeting_id: str):
        record = store.get_meeting(meeting_id, user=current_user())
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
        updated = store.update_meeting(meeting_id, update, user=current_user())
        if updated is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        drop_from_rag(meeting_id)
        return updated

    def _time_bound(label: Optional[str]) -> Optional[int]:
        """把 "12:30" 這種時間標籤轉成秒；空字串／None 回 None（該側不設限）。"""
        label = (label or "").strip()
        if not label:
            return None
        if not re.fullmatch(r"\d{1,2}(:\d{1,2}){0,2}", label):
            raise HTTPException(status_code=400, detail=f"時間格式錯誤：{label}（請用「分:秒」如 12:30）")
        return parse_time_label(label)

    @app.post("/api/meetings/{meeting_id}/replace-term")
    def replace_term(meeting_id: str, req: ReplaceTermRequest):
        """把逐字稿裡某個詞統一換成新詞；可只換某個時間段（避免動到其他時段正確的同字），
        並可一併加入詞彙表，讓之後的錄音轉錄不再聽錯（事後修正兼事前預防）。"""
        old = (req.old or "").strip()
        new = (req.new or "").strip()
        if not old:
            raise HTTPException(status_code=400, detail="原詞不可為空")
        start_sec, end_sec = _time_bound(req.start), _time_bound(req.end)
        if start_sec is not None and end_sec is not None and start_sec > end_sec:
            raise HTTPException(status_code=400, detail="起始時間不能晚於結束時間")
        record = store.get_meeting(meeting_id, user=current_user())
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        transcript = record.get("transcript") or ""
        new_transcript, count = replace_term_in_range(transcript, old, new, start_sec, end_sec)
        updated = record
        if count:
            updated = store.update_meeting(meeting_id, {"transcript": new_transcript}, user=current_user())
            drop_from_rag(meeting_id)  # 逐字稿變了，RAG 索引要作廢重建
        # 只有真的替換到、且新詞非空才動詞彙表；詞彙表滿了就靜默略過（替換本身已成功）
        added = False
        if req.add_to_glossary and new and count:
            terms = glossary.terms(current_user())
            if not any(t.get("term") == new for t in terms):
                try:
                    glossary.replace(terms + [{"term": new, "note": ""}], current_user())
                    added = True
                except ValueError:
                    pass
        return {"meeting": updated, "replaced": count, "glossary_added": added}

    @app.delete("/api/meetings/{meeting_id}")
    def delete_meeting(meeting_id: str):
        if not store.delete_meeting(meeting_id, user=current_user()):
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        drop_from_rag(meeting_id)
        return {"deleted": meeting_id}

    @app.post("/api/meetings/{meeting_id}/reanalyze")
    def reanalyze_meeting(meeting_id: str, req: Optional[ReanalyzeRequest] = None):
        """對（可能已編輯過的）逐字稿重跑 AI 分析：更新會議紀錄、整批換掉任務。"""
        record = store.get_meeting(meeting_id, user=current_user())
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        transcript = (record.get("transcript") or "").strip()
        if not transcript:
            raise HTTPException(status_code=400, detail="此會議沒有逐字稿全文，無法重新分析")

        kind = record.get("kind")
        # 會前打的專用詞彙跟著會議存起來，重新分析時要沿用，不能弄丟
        stored_terms = record.get("terms") or None
        features = resolve_features(req.features if req else None, kind)
        meeting_date = _parse_iso_date_or_none(record.get("meeting", {}).get("date"))
        usage.record("analysis")

        corrections: list[dict] = []
        if req and req.correct_typos and orchestrator.corrector:
            usage.record("correct")
            transcript, corrections = orchestrator.corrector.correct(transcript)

        speaker_names: list[dict] = []
        if req and req.name_speakers and orchestrator.namer:
            usage.record("speaker_names")
            transcript, speaker_names = orchestrator.namer.name_speakers(transcript)

        try:
            analysis = orchestrator.decision.analyze(
                transcript,
                meeting_date=meeting_date,
                kind=kind,
                features=features,
                extra_terms=stored_terms,
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
        if corrections or speaker_names:  # 逐字稿被改過才回寫，沒改就不動原紀錄
            updates["transcript"] = transcript
        store.update_meeting(meeting_id, updates, user=current_user())
        tasks = store.replace_tasks(meeting_id, dumped["todos"], user=current_user())
        notifications = orchestrator.notifier.notify(meeting_id, analysis)
        drop_from_rag(meeting_id)
        return {
            "meeting_id": meeting_id,
            "analysis": dumped,
            "notifications": notifications,
            "tasks": tasks,
            "transcript": transcript,
            "corrections": corrections,
            "speaker_names": speaker_names,
        }

    @app.get("/api/tasks")
    def list_tasks(meeting_id: Optional[str] = None):
        return {"tasks": store.list_tasks(meeting_id=meeting_id, user=current_user())}

    @app.get("/api/usage")
    def get_usage():
        return usage.snapshot()

    @app.get("/api/reminders")
    def get_reminders(days: int = 2):
        """主動提醒：逾期/即將到期/未指派任務的催辦草稿＋未決事項追問。"""
        return scan_reminders(store.list_tasks(user=current_user()), store.list_meetings(user=current_user()), due_soon_days=days)

    @app.get("/api/search")
    def keyword_search(q: str = ""):
        """關鍵字精確搜尋（標題/摘要/決議/逐字稿），與語意問答互補。"""
        keyword = q.strip()
        if not keyword:
            raise HTTPException(status_code=400, detail="請輸入要搜尋的關鍵字")
        kw = keyword.lower()
        hits = []
        for meta in store.list_meetings(user=current_user()):
            record = store.get_meeting(meta["id"], user=current_user()) or meta
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
            return ask_agent.ask(
                req.question, meeting_ids=req.meeting_ids, user=current_user()
            )
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
        return {"terms": glossary.terms(current_user())}

    @app.put("/api/glossary")
    def put_glossary(req: GlossaryRequest):
        try:
            return {"terms": glossary.replace(req.terms, current_user())}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/glossary/persons")
    def remember_persons(req: PersonNamesRequest):
        """記一筆剛用到的姓名並標成人名（前端手動改講者名時呼叫）。

        整份取代（PUT）在這裡不適用：前端只知道剛改的那一個名字，送完整清單
        會把同時開著別的分頁改的東西洗掉。不合格的姓名略過就好，不回報錯誤
        ——這是順手記一筆，不是使用者主動送出的表單。
        """
        user = current_user()
        glossary.remember_persons(req.names, user)
        return {"names": glossary.person_names(user)}

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
        }, user=current_user())

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
        updated = store.update_task(task_id, user=current_user(), **fields)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"找不到任務：{task_id}")
        return updated

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str):
        if not store.delete_task(task_id, user=current_user()):
            raise HTTPException(status_code=404, detail=f"找不到任務：{task_id}")
        return {"deleted": task_id}

    @app.get("/api/backup")
    def download_backup():
        """整份資料（會議＋任務＋詞彙）打包成 JSON 下載，供離線保存或搬移。"""
        return Response(
            content=json.dumps(store.export_all(user=current_user()), ensure_ascii=False, indent=2),
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
        store.import_all(data, user=current_user())
        if rag_index is not None:  # 舊向量已不對應新資料，作廢待重建
            rag_index.reset(current_user())
        return {
            "restored": {"meetings": len(data["meetings"]), "tasks": len(data["tasks"])}
        }

    @app.get("/api/export/tasks.csv")
    def export_tasks_csv():
        return Response(
            content=tasks_to_csv(store.list_tasks(user=current_user())),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="tasks.csv"'},
        )

    @app.get("/api/meetings/{meeting_id}/report.md")
    def meeting_report(meeting_id: str):
        record = store.get_meeting(meeting_id, user=current_user())
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        return Response(
            content=meeting_report_md(record, store.list_tasks(meeting_id=meeting_id, user=current_user())),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="meeting-{meeting_id}.md"'},
        )

    @app.get("/api/meetings/{meeting_id}/events.ics")
    def meeting_events_ics(meeting_id: str):
        """把此會議含期限的任務匯出成 .ics，一鍵加入 Google/Apple 行事曆。"""
        record = store.get_meeting(meeting_id, user=current_user())
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        content = tasks_to_ics(
            record.get("meeting", {}).get("title", ""),
            store.list_tasks(meeting_id=meeting_id, user=current_user()),
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
        name_speakers: Optional[str] = Form(None),
        terms: Optional[str] = Form(None),
    ):
        try:
            parsed_date = date.fromisoformat(meeting_date) if meeting_date else None
        except ValueError:
            raise HTTPException(status_code=400, detail="meeting_date 必須是 YYYY-MM-DD 格式")
        validate_kind(kind)
        resolved_features = resolve_features(features, kind)
        resolved_terms = validate_terms(terms)
        # multipart 表單只有字串，"true"/"1" 都當開啟
        _truthy = ("1", "true", "on", "yes")
        correct = str(correct_typos or "").lower() in _truthy
        name_speakers_on = str(name_speakers or "").lower() in _truthy

        suffix = validate_media_suffix(file.filename)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        dest = uploads_dir / f"{uuid.uuid4().hex[:12]}{suffix}"
        # 2 小時的影片可能數 GB，串流寫入不佔記憶體；同時守住大小上限
        save_upload(file.file, dest, max_upload_bytes)

        usage.record("media_upload")
        if correct:
            usage.record("correct")
        if name_speakers_on and orchestrator.namer:
            usage.record("speaker_names")
        return {
            "job_id": job_manager.submit(
                dest,
                user=current_user(),  # 背景執行緒沒有請求上下文，現在就捕捉
                meeting_date=parsed_date,
                kind=kind,
                features=resolved_features,
                correct_typos=correct,
                name_speakers=name_speakers_on,
                terms=resolved_terms,
            )
        }

    @app.get("/api/media/{job_id}")
    def media_status(job_id: str):
        job = job_manager.get(job_id, user=current_user())
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
        return {
            "session_id": live_manager.start(
                translate_to=translate_to,
                user=current_user(),
                # 會前打的詞彙要在「聽」的當下就生效，不能只留給事後分析
                terms=validate_terms(req.terms if req else None),
            )
        }

    @app.post("/api/live/{session_id}/chunk")
    def live_chunk(
        session_id: str,
        file: UploadFile = File(...),
        offset: Optional[float] = Form(None),  # 本段在整場會議中的開始秒數
    ):
        suffix = Path(file.filename or "chunk.webm").suffix or ".webm"
        data = read_capped(file.file, max_upload_bytes)
        usage.record("live_chunk")
        try:
            return live_manager.add_chunk(
                session_id,
                data,
                suffix=suffix,
                offset_seconds=offset,
                user=current_user(),
            )
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # 轉錄後端故障（額度、格式…）要讓前端看得到原因
            raise HTTPException(status_code=502, detail=f"這段音訊轉錄失敗：{exc}")

    @app.post("/api/live/{session_id}/enroll")
    def live_enroll(
        session_id: str,
        file: UploadFile = File(...),
        name: str = Form(...),  # 這個人的姓名，會用來取代逐字稿裡的講者代號
    ):
        """會前註冊一位與會者的聲音樣本（選用功能）。

        沒呼叫過這支端點的 session，結束時完全不會走聲紋比對——不多打 API，
        行為與這個功能不存在時相同。
        """
        suffix = Path(file.filename or "enroll.webm").suffix or ".webm"
        data = read_capped(file.file, max_upload_bytes)
        try:
            count = live_manager.enroll(
                session_id, name, data, suffix=suffix, user=current_user()
            )
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"enrolled": count}

    @app.post("/api/live/{session_id}/finish")
    def live_finish(session_id: str, req: Optional[FinishRequest] = None):
        try:
            # 聲紋比對務必在 finish 之前：finish 會刪掉整個 session 目錄，
            # 樣本與會議音檔都在裡面，之後就沒有聲音可比了。
            # 沒註冊樣本時這行回 {} 且不打任何 API
            speaker_prior = live_manager.voice_mapping(session_id, user=current_user())
            transcript = live_manager.finish(session_id, user=current_user())
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
            name_speakers=bool(req and req.name_speakers),
            terms=validate_terms(req.terms if req else None),
            speaker_prior=speaker_prior,
        )
        # result 帶著校正後的 transcript，放在後面覆蓋原始版本
        return {"transcript": transcript, **result}

    return app


app = create_app()
