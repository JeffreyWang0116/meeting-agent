"""FastAPI 入口：三種輸入路徑（純文字 / 檔案上傳 / 即時聆聽）的 API。

啟動：.venv\\Scripts\\python -m uvicorn app.main:app --reload
"""
from __future__ import annotations

import json
import logging
import math
import re
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from app.agents.corrector_agent import CorrectorAgent
from app.agents.decision_agent import (
    FEATURE_KEYS,
    DEFAULT_KIND,
    KIND_DEFAULT_FEATURES,
    KIND_GROUPS,
    KIND_DESCRIPTIONS,
    LEGACY_KINDS,
    MEETING_KINDS,
    DecisionAgent,
    DecisionAgentError,
    default_features_for_kind,
)
from app.agents.notifier_agent import (
    build_notifications,
    email_draft_from_record,
    notifications_from_record,
)
from app.agents.reminder_agent import scan as scan_reminders
from app import activity as activity_log
from app.agenda import carry_over_lines, clean_agenda, coverage_summary
from app import groups as group_svc
from app.auth import (
    CURRENT_ACCOUNT,
    CURRENT_EMAIL,
    CURRENT_GUEST,
    CURRENT_IP,
    CURRENT_USER,
    AuthError,
    bearer_token,
    identity_of,
    verify_firebase_id_token,
)
from app.comments import CommentError, author_name, clean_text, normalize_target
from app.config import Settings, get_settings
from app.docexport import PdfFontMissing, meeting_to_docx, meeting_to_pdf
from app.export import meeting_report_md, tasks_to_csv, tasks_to_ics
from app.gemini_keys import is_quota_error
from app.glossary import Glossary, clean_terms
from app.insights import open_items, people_stats, period_report
from app.jobs import MediaJobManager
from app.orchestrator import Orchestrator
from app.push import (
    GONE,
    OK,
    PushError,
    digest,
    due_now,
    local_time,
    make_sender,
    normalize_subscription,
    subscription_id,
)
from app.rag import AskAgent, GeminiEmbedder, RagIndex
from app.speaker_rename import name_problem, rename_meeting_fields, rename_task_fields
from app.stores import make_store
from app.subtitles import to_srt, to_vtt
from app.transcription import media
from app.transcription.segments import parse_time_label, replace_term_in_range
from app.refine import RefineError, Refiner, apply_patch, parse_patch
from app.templates import TemplateError, is_custom, kind_value, normalize_template, prompt_hint, sections_map, template_id
from app.translate import TARGETS as TRANSLATE_TARGETS
from app.translate import Translator
from app.transcription.diarizer import build_diarizer
from app.transcription.gemini_transcriber import GeminiTranscriber
from app.transcription.live_session import LiveSessionManager, SessionNotFound
from app.transcription.voice_match import VoiceMatcher
from app.ratelimit import GUEST_DEFAULT_LIMITS, RateLimited, RateLimiter, parse_limits
from app.usage import UsageTracker

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def asset_version(static_dir: Path) -> str:
    """前端資產的版本字串，取 style.css 與 js/*.js 之中最新的 mtime。

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
# /api/push/run 沒有登入者：它是排程（GitHub Actions）打進來的，改用共用密語把關
# 垃圾桶保留幾天。再長就只是讓誤刪的資料在資料庫裡堆著
TRASH_DAYS = 30

PUBLIC_API_PATHS = {"/api/health", "/api/auth/config", "/api/push/run"}

# 群組裡「只能看」的成員還是可以用的 POST：問答、翻譯只讀資料、不改資料
VIEWER_ALLOWED_POSTS = {"/api/ask", "/api/translate"}
# 留言是例外中的例外：「只能看」是不能改資料，不是不能說話——
# 討論本來就是看的人最需要的。自己的留言也要刪得掉，所以 DELETE 一併放行
VIEWER_ALLOWED_SUFFIXES = ("/comments",)
VIEWER_ALLOWED_PREFIXES = ("/api/comments/",)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def viewer_may(path: str) -> bool:
    return (
        path in VIEWER_ALLOWED_POSTS
        or path.endswith(VIEWER_ALLOWED_SUFFIXES)
        or path.startswith(VIEWER_ALLOWED_PREFIXES)
    )


class RequireFirebaseLogin:
    """驗 ID token，並把這個請求的資料範圍放進 CURRENT_USER 供 current_user() 讀取。

    個人工作區的範圍就是自己的 uid；帶了 X-Workspace 標頭就是群組工作區——先確認
    這個人是成員，範圍切成 group:<id>，「只能看」的成員擋掉寫入。既有端點只認
    current_user()，不必知道群組存在。實際登入的人另外放 CURRENT_ACCOUNT／CURRENT_EMAIL，
    給群組管理與速率限制用。

    刻意寫成純 ASGI 中介層而不是 @app.middleware("http")：後者會把下游應用丟到
    另一個 task 執行，contextvar 傳不傳得過去得看 Starlette 版本臉色。純 ASGI
    中介層與端點在同一個 task 內，設進去的值必定讀得到。
    """

    def __init__(self, app, verify, store, allow_guest=True):
        self.app = app
        self._verify = verify
        self._store = store
        self._allow_guest = allow_guest

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
            uid, email, guest = identity_of(self._verify(token))
        except AuthError as exc:
            return await self._deny(scope, receive, send, str(exc))
        if guest and not self._allow_guest:
            return await self._deny(scope, receive, send, "這個網站沒有開放訪客使用，請用 Google 登入", status=403)

        data_scope = uid
        workspace = (raw.get(b"x-workspace") or b"").decode("latin-1").strip()
        # 群組管理本身不進工作區：被移出群組的人也要能回到側欄、看到自己剩下的群組
        if workspace and not path.startswith("/api/groups"):
            role = group_svc.role_of(self._store.get_group(workspace), uid)
            if role is None:
                return await self._deny(
                    scope, receive, send, "你不是這個群組的成員（可能已被移出或群組已解散）",
                    status=403, workspace_denied=True,
                )
            if role == "viewer" and scope.get("method") not in SAFE_METHODS and not viewer_may(path):
                return await self._deny(
                    scope, receive, send, "你在這個群組的角色是「只能看」，無法新增或修改資料", status=403,
                )
            data_scope = group_svc.scope_of(workspace)

        resets = [
            (CURRENT_USER, CURRENT_USER.set(data_scope)),
            (CURRENT_ACCOUNT, CURRENT_ACCOUNT.set(uid)),
            (CURRENT_EMAIL, CURRENT_EMAIL.set(email)),
            (CURRENT_GUEST, CURRENT_GUEST.set(guest)),
            (CURRENT_IP, CURRENT_IP.set(client_ip(scope, raw))),
        ]
        try:
            await self.app(scope, receive, send)
        finally:
            for var, token_ in resets:
                var.reset(token_)

    @staticmethod
    async def _deny(scope, receive, send, detail: str, status: int = 401, **extra):
        await JSONResponse({"detail": detail, **extra}, status_code=status)(scope, receive, send)


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
    # 本次會議專用詞彙，與全域詞彙表合併使用
    terms: Optional[list[dict]] = None
    # 聆聽 session 遺失時會改走這支端點救援，標記與筆記不該跟著不見
    marks: Optional[list[str]] = None
    notes: Optional[str] = Field(default=None, max_length=20000)
    # 會前準備（見 app/agenda.py）：貼進來的議程（一行一項）、這場屬於哪個系列、
    # 以及要不要把同系列上一場沒做完的事一起餵給模型
    agenda: Optional[str] = Field(default=None, max_length=5000)
    series: Optional[str] = Field(default=None, max_length=60)
    carry_over: bool = False


class FinishRequest(BaseModel):
    meeting_date: Optional[date] = None
    kind: Optional[str] = None
    features: Optional[list[str]] = None
    correct_typos: bool = False
    terms: Optional[list[dict]] = None
    # 會前準備（見 app/agenda.py）
    agenda: Optional[str] = Field(default=None, max_length=5000)
    series: Optional[str] = Field(default=None, max_length=60)
    carry_over: bool = False
    # 聆聽中按「標記這一刻」的時間點（"5:23"）。不是時間格式的會在提示詞那層丟掉
    marks: Optional[list[str]] = None
    # 會議中自己打的筆記。直接進提示詞，所以要有長度上限
    notes: Optional[str] = Field(default=None, max_length=20000)


class RefineRequest(BaseModel):
    # 預覽：使用者的修改要求（一句話）。套用：前端把預覽拿到的 patch 原樣送回
    request: Optional[str] = Field(default=None, max_length=2000)
    patch: Optional[dict] = None


# 回報 AI 哪裡錯了。原因做成固定幾種：自由文字沒辦法統計，也沒辦法拿來篩選案例
FEEDBACK_REASONS = {
    "not_discussed": "會議中沒有這件事",
    "owner_wrong": "負責人錯了",
    "date_wrong": "日期錯了",
    "wrong_content": "內容理解錯誤",
    "missing": "漏掉了重要的事",
    "other": "其他",
}
FEEDBACK_TARGETS = {"todo", "decision", "summary", "highlight", "pending", "section"}


class FeedbackRequest(BaseModel):
    meeting_id: str
    target: str                       # 哪一類產出（代辦、決議、摘要…）
    target_text: str = Field(default="", max_length=2000)   # 被回報的那一筆內容
    reason: str
    note: str = Field(default="", max_length=2000)          # 選填：正確答案是什麼


class ReanalyzeRequest(BaseModel):
    features: Optional[list[str]] = None
    correct_typos: bool = False


class ReplaceTermRequest(BaseModel):
    old: str
    new: str = ""  # 允許空字串＝把該詞整個刪掉
    add_to_glossary: bool = False
    start: Optional[str] = None  # 時間段下限，如 "12:30"；None/空＝不限
    end: Optional[str] = None    # 時間段上限；只換時間戳落在 [start, end] 的行


class RenameSpeakerRequest(BaseModel):
    old: str
    new: str


class GroupNameRequest(BaseModel):
    name: str


class GroupInviteRequest(BaseModel):
    email: str
    role: str = "editor"


class GroupRoleRequest(BaseModel):
    role: str


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
    terms: Optional[list[dict]] = None  # 本次專用詞彙：進每段轉錄的提示


class TranslateRequest(BaseModel):
    text: str
    target: str


def _title_of(record: dict) -> str:
    return (record.get("meeting") or {}).get("title") or "（未命名會議）"


def _task_action(before: dict | None, after: dict) -> str:
    """任務怎麼變的。「完成」與「改回未完成」在紀錄上長得一樣就沒有意義了。"""
    was, now = (before or {}).get("status"), after.get("status")
    if was == now:
        return "task.edit"
    if now == "done":
        return "task.done"
    return "task.reopen" if was == "done" else "task.edit"


def current_user(request: Request | None = None) -> str:
    """這個請求屬於誰。

    啟用 Firebase Auth 時，中介層驗完 ID token 就把 uid 放進 CURRENT_USER，
    這裡讀出來；沒啟用（本機開發、或只設了共用 API_TOKEN）時是 DEFAULT_USER，
    行為與帳號制上線前完全一樣。

    舊資料沒有 user 欄位，store 一律視為 DEFAULT_USER 的——所以本機既有的
    會議在啟用登入後不會消失，只是歸在單人模式那一格。
    """
    return CURRENT_USER.get()


def client_ip(scope, raw_headers: dict) -> str:
    """連線來源 IP。X-Forwarded-For 取最後一段：前段是用戶端自己填的、可以偽造，
    最後一段才是我們前面那層反向代理（Render）實際看到的來源。"""
    forwarded = (raw_headers.get(b"x-forwarded-for") or b"").decode("latin-1")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    if hops:
        return hops[-1]
    client = scope.get("client")
    return client[0] if client else ""


def current_account() -> str:
    """實際登入的人（uid）。在群組工作區裡 current_user() 是群組，這個仍然是人。"""
    return CURRENT_ACCOUNT.get()


def in_group_workspace() -> bool:
    return current_user().startswith("group:")


def _configure_logging() -> None:
    """讓 app.* 的 INFO 真的印得出來。

    uvicorn 只設定 uvicorn / uvicorn.error / uvicorn.access 三個 logger，不碰
    root。app.* 的記錄於是一路 propagate 到沒有 handler 的 root，落進 Python
    的 lastResort——而那個只放行 WARNING 以上。結果是所有 logger.info 在雲端
    完全看不到：「音檔切成 N 段」「開始轉錄第 N/M 段」「講者分離對齊」全是
    INFO，而那些正是進度條停在 0% 時唯一想知道的事。

    掛在 "app" 而不是 root：只管自己的記錄，不去改動 uvicorn 或第三方套件的
    輸出。不設 propagate=False——pytest 的 caplog 靠 root 收記錄，關掉會讓
    測試收不到。有 handler 就不重複掛，create_app 被呼叫幾次都一樣。
    """
    logger = logging.getLogger("app")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


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
    diarizer=None,
    delete_auth_user=None,
    refine_generate=None,
    push_send=None,
    clock=None,
) -> FastAPI:
    _configure_logging()
    settings = settings or get_settings()
    store = store or make_store(settings)
    # 用量統計要在 agent 之前建好：每個會打 Gemini 的元件都得拿到 record_call，
    # 統計才算得到重試與換金鑰（端點層只知道「使用者按了幾次」，差一個數量級）
    usage = UsageTracker(settings.data_dir / "output" / "usage.json")
    # 速率限制：設定寫錯（parse_limits 丟 ValueError）就讓啟動失敗——悄悄忽略的話，
    # 公開網址等於沒有節流
    rate_limiter = RateLimiter(
        parse_limits(settings.rate_limits), enabled=settings.rate_limit_enabled
    )
    guest_limiter = RateLimiter(
        parse_limits(settings.guest_rate_limits, GUEST_DEFAULT_LIMITS, "GUEST_RATE_LIMITS"),
        enabled=settings.rate_limit_enabled,
    )

    def limit(bucket: str) -> None:
        """耗額度的端點一進來先檢查；超過上限回 429 並附 Retry-After。"""
        try:
            if CURRENT_GUEST.get():
                # 訪客照 IP 算：清掉瀏覽器資料就是新的匿名帳號，照帳號算等於沒上限
                guest_limiter.check(bucket, f"ip:{CURRENT_IP.get()}")
            else:
                # 照「人」算：照工作區算的話，同群組的人共用一份，換個工作區又多一份
                rate_limiter.check(bucket, current_account())
        except RateLimited as exc:
            raise HTTPException(
                status_code=429, detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            )

    def record_call() -> None:
        usage.record("gemini_call")

    # 自訂詞彙表：持久化交給 store（本地 JSON / 雲端 Firestore，與任務同後端），
    # 以 callable 注入，轉錄/分析每次都讀到最新內容
    glossary = Glossary(store)
    orchestrator = orchestrator or Orchestrator(
        decision=DecisionAgent(
            api_key=settings.gemini_api_key,
            api_keys=settings.gemini_api_keys,
            on_call=record_call,
            model=settings.gemini_model,
            glossary=glossary.terms,
        ),
        store=store,
        corrector=CorrectorAgent(
            api_key=settings.gemini_api_key,
            api_keys=settings.gemini_api_keys,
            on_call=record_call,
            model=settings.correct_model,
            glossary=glossary.terms,
        ),
    )
    # pyannote 講者分離（選用）：沒設 PYANNOTE_API_KEY 就是 None，講者照舊由轉錄模型標
    diarizer = diarizer or build_diarizer(
        settings, on_call=lambda: usage.record("diarize")
    )
    if transcriber is None:
        transcriber = GeminiTranscriber(
            api_key=settings.gemini_api_key,
            api_keys=settings.gemini_api_keys,
            on_call=record_call,
            model=settings.transcribe_model,
            glossary=glossary.terms,
            chunk_seconds=settings.transcribe_chunk_seconds,
            label_retries=settings.transcribe_label_retries,
            overlap_seconds=settings.transcribe_overlap_seconds,
            max_retry_calls=settings.transcribe_max_retry_calls,
            # 聲紋跨段接力（長檔分段轉錄用，見 GeminiTranscriber）。要關掉設
            # VOICE_RELAY_MAX_SPEAKERS=0
            voice_relay_max_speakers=settings.voice_relay_max_speakers,
            # 有 pyannote 時講者會被整份重標；只為講者標籤服務的機制由轉錄器自己關掉
            relabeled_later=diarizer is not None,
        )
    # 翻譯與轉錄同樣高頻（即時聆聽逐段翻），用高額度的轉錄模型
    translator = translator or Translator(
        api_key=settings.gemini_api_key,
        api_keys=settings.gemini_api_keys,
        on_call=record_call,
        model=settings.transcribe_model,
    )
    # 用一句話改分析結果。只回「要改哪些欄位」，所以用額度高的輕量模型就夠
    refiner = Refiner(
        api_key=settings.gemini_api_key,
        api_keys=settings.gemini_api_keys,
        on_call=record_call,
        model=settings.gemini_model,
        generate=refine_generate,
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
            diarizer=diarizer,
        )
        live_manager.MAX_ENROLLMENTS = settings.live_enroll_max_speakers
    job_manager = job_manager or MediaJobManager(
        transcriber, orchestrator, settings.data_dir / "tmp", diarizer=diarizer,
        store=store,
    )
    rag_index = None
    if ask_agent is None:
        rag_index = RagIndex(
            store,
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
            kinds=[k for _, kinds in KIND_GROUPS for k in kinds],
        )

    # 「現在幾點」的唯一入口。垃圾桶的 30 天、推播的當地時間都讀它，
    # 測試才有辦法把時間推到 31 天後而不用等
    now_utc = clock or (lambda: datetime.now(timezone.utc))

    def log_activity(action: str, label: str, target_id: str | None = None) -> None:
        """記一筆群組活動。個人工作區會被 activity.record 自己擋掉。"""
        activity_log.record(
            store, current_user(), action=action, target_label=label, target_id=target_id,
            actor_uid=current_account(), actor_email=CURRENT_EMAIL.get(), at=now_utc().isoformat(),
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

    def remove_auth_user(uid: str) -> None:
        if delete_auth_user is not None:
            delete_auth_user(uid)
            return
        from firebase_admin import auth as firebase_auth

        firebase_auth.delete_user(uid)

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

        app.add_middleware(
            RequireFirebaseLogin, verify=verify, store=store, allow_guest=settings.guest_login_enabled,
        )
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

    # 刻意加在認證中介層之後：Starlette 把最後加入的那層包在最外面，所以這層
    # 也蓋得到認證直接回的 401。漏掉錯誤回應等於沒設——攻擊者要的就是那些頁面。
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """站是上鎖的，但「誰進得來」跟「別人能不能借用他的登入狀態」是兩件事。

        沒有 framing 限制，任何網站都能把這個站嵌成隱形 iframe 蓋在自己的按鈕
        底下，讓已經登入的使用者在不知情下點到刪除會議。這只能由標頭擋，前端
        做什麼都沒用——攻擊發生在別人的頁面上。

        這裡不下完整的 Content-Security-Policy：那要先把 Firebase SDK
        （gstatic）、Google Fonts 等來源全部列進白名單，漏一個就是登入流程在
        正式站上壞掉而本機完全正常。只取不需要盤點資源就安全的那幾個。
        """
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # 網址帶會議 id，不該整條送給使用者從確認信點出去的外部網站
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # HSTS 只給公開站：瀏覽器記住之後改不掉，對 localhost 下等於把每個用
        # http 跑本機服務的專案一起鎖死，而且沒人會聯想到是這裡造成的
        if settings.is_public_deploy:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

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
            "guestEnabled": settings.auth_enabled and settings.guest_login_enabled,
        }

    def kind_spec_of(kind: str | None) -> dict | None:
        """自訂種類 → 餵進提示詞的 {label, hint, sections}；內建種類回 None。

        範本被刪掉時也回 None：用它分析過的舊會議還在，重新分析時退回通用格式，
        總比整個失敗好。"""
        if not is_custom(kind):
            return None
        tpl = store.get_template(template_id(kind), current_user())
        if tpl is None:
            return None
        return {"label": tpl["name"], "hint": prompt_hint(tpl), "sections": sections_map(tpl)}

    def validate_kind(kind: str | None, *, existing: bool = False) -> str | None:
        """existing=True：這個 kind 來自已存檔的會議（編輯、重新分析）。
        範本可能已經被刪掉，不該因此打不開那場會議。"""
        if is_custom(kind):
            if existing or store.get_template(template_id(kind), current_user()):
                return kind
            raise HTTPException(status_code=400, detail="找不到這個自訂範本")
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
        terms: list[dict] | None = None,
        speaker_prior: dict[str, str] | None = None,
        attendees: list[str] | None = None,
        marks: list[str] | None = None,
        notes: str | None = None,
        agenda: list[str] | None = None,
        series: str | None = None,
        carry_over: list[str] | None = None,
    ) -> dict:
        usage.record("analysis")
        spec = kind_spec_of(kind)
        if correct_typos:
            usage.record("correct")  # 校正是額外一次請求，用量面板要分開看得到
        try:
            result = orchestrator.process_transcript(
                text,
                meeting_date=meeting_date,
                kind=kind,
                features=features,
                correct_typos=correct_typos,
                terms=terms,
                user=current_user(),
                speaker_prior=speaker_prior,
                attendees=attendees,
                marks=marks,
                notes=notes,
                kind_spec=spec,
                agenda=agenda,
                series=series,
                carry_over=carry_over,
            )
            log_activity("meeting.create", _title_of(result), result.get("meeting_id"))
            if agenda:
                # 前端要畫「3 項討論過、1 項沒談到」的那一列，不必自己再掃一遍
                result["agenda_coverage"] = coverage_summary(
                    result.get("analysis", {}).get("agenda_review")
                )
            return result
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

    # 前端靜態檔（style.css / js/*.js / orb.js / icons.svg）統一由 /static 供應
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
            "transcribe_model": settings.transcribe_model,
            "store_backend": getattr(store, "backend", "unknown"),
            "live_chunk_seconds": settings.live_chunk_seconds,
            # 前端據此決定「預錄聲音辨識人」最多能列幾個人（0＝不顯示這個功能）
            "live_enroll_max_speakers": settings.live_enroll_max_speakers,
            # 長音檔分段轉錄的每段秒數（0＝不分段）。放在 health 是為了能從
            # 外部確認部署版到底有沒有帶上這個功能
            "transcribe_chunk_seconds": settings.transcribe_chunk_seconds,
            # 講者分離後端："pyannote"＝已啟用；None＝講者由轉錄模型標。部署後
            # 從這裡確認金鑰有沒有生效（填了但引擎不是 gemini 也會是 None）
            "speaker_diarization": "pyannote" if diarizer else None,
            # 速率限制有沒有開（公開部署預設開）
            "rate_limit": rate_limiter.enabled,
            # 進行中的工作。推 master 會觸發重新部署、清掉記憶體裡的聆聽 session，
            # 部署前看一眼有沒有人正在用
            "active_work": {
                "media_jobs": job_manager.active_count(),
                "live_sessions": live_manager.active_count(),
            },
        }

    # ---- 輸入路徑 1：純文字 ----

    @app.post("/api/meetings")
    def analyze_meeting(req: MeetingRequest):
        kind = validate_kind(req.kind)
        limit("analyze")
        series = (req.series or "").strip() or None
        return run_analysis(
            req.text,
            req.meeting_date,
            kind,
            resolve_features(req.features, kind),
            correct_typos=req.correct_typos,
            terms=validate_terms(req.terms),
            marks=req.marks,
            notes=req.notes,
            agenda=clean_agenda(req.agenda),
            series=series,
            # 帶上次沒做完的是選配：沒有系列可參照時，勾了也沒東西可帶
            carry_over=carryover_for(series)["lines"] if (req.carry_over and series) else None,
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
                            # 下拉選單上顯示的字。自訂範本的 value 是 custom:<id>，
                            # 不能直接拿來當標籤
                            "label": k,
                            # 給人看的說明，不是拼進 prompt 的 KIND_HINTS
                            "hint": KIND_DESCRIPTIONS[k],
                            "features": sorted(KIND_DEFAULT_FEATURES.get(k, FEATURE_KEYS)),
                        }
                        for k in kinds
                    ],
                }
                for label, kinds in KIND_GROUPS
            ] + custom_kind_groups(),
        }

    def custom_kind_groups() -> list[dict]:
        """自訂範本在下拉選單自成一組，排在內建種類後面。"""
        mine = sorted(store.list_templates(current_user()), key=lambda t: t.get("name", ""))
        if not mine:
            return []
        return [{
            "label": "我的範本",
            "kinds": [
                {
                    "value": kind_value(t["id"]),
                    "label": t["name"],
                    "hint": t.get("hint") or "自訂範本",
                    "features": sorted(t.get("features") or FEATURE_KEYS),
                }
                for t in mine
            ],
        }]

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
            # 編輯時前端會把目前的 kind 原樣帶回來。範本已經被刪掉的話，
            # 嚴格檢查會讓那場會議連標題都改不了——沒換種類就放行
            current = store.get_meeting(meeting_id, user=current_user()) or {}
            validate_kind(fields["kind"], existing=fields["kind"] == current.get("kind"))
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
        log_activity("meeting.edit", _title_of(updated), meeting_id)
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

    # ---- 自訂會議範本：自己定義種類名稱、要哪些區塊、每個區塊抓什麼 ----

    @app.get("/api/templates")
    def list_templates():
        mine = sorted(store.list_templates(current_user()), key=lambda t: t.get("name", ""))
        return {"templates": mine}

    @app.post("/api/templates")
    def create_template(body: dict):
        try:
            fields = normalize_template(body)
        except TemplateError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        template = {"id": uuid.uuid4().hex[:12], "user": current_user(), **fields}
        store.save_template(template)
        return {"template": template}

    @app.patch("/api/templates/{tpl_id}")
    def update_template(tpl_id: str, body: dict):
        user = current_user()
        if store.get_template(tpl_id, user) is None:
            raise HTTPException(status_code=404, detail="找不到這個範本")
        try:
            fields = normalize_template(body)
        except TemplateError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        template = {"id": tpl_id, "user": user, **fields}
        store.save_template(template)
        return {"template": template}

    @app.delete("/api/templates/{tpl_id}")
    def delete_template(tpl_id: str):
        # 用它分析過的會議照舊存在，只是重新分析時退回通用格式（見 kind_spec_of）
        store.delete_template(tpl_id, current_user())
        return {"deleted": tpl_id}

    # ---- 回報 AI 的錯誤：累積真實案例，換模型前拿來回歸測試 ----

    @app.post("/api/feedback")
    def add_feedback(req: FeedbackRequest):
        if req.reason not in FEEDBACK_REASONS:
            raise HTTPException(
                status_code=400, detail=f"reason 只能是：{'、'.join(sorted(FEEDBACK_REASONS))}"
            )
        if req.target not in FEEDBACK_TARGETS:
            raise HTTPException(
                status_code=400, detail=f"target 只能是：{'、'.join(sorted(FEEDBACK_TARGETS))}"
            )
        user = current_user()
        if store.get_meeting(req.meeting_id, user=user) is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{req.meeting_id}")
        item = {
            "id": uuid.uuid4().hex[:12],
            "user": user,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **req.model_dump(),
        }
        store.save_feedback(item)
        return {"feedback": item}

    @app.get("/api/feedback")
    def list_feedback():
        items = sorted(
            store.list_feedback(current_user()), key=lambda f: f.get("created_at", ""), reverse=True
        )
        return {"feedback": items, "reasons": FEEDBACK_REASONS}

    @app.delete("/api/feedback/{feedback_id}")
    def delete_feedback(feedback_id: str):
        store.delete_feedback(feedback_id, current_user())
        return {"deleted": feedback_id}

    @app.get("/api/feedback/export.jsonl")
    def export_feedback():
        """匯出成一行一筆的 JSONL，格式比照 eval/dataset.jsonl，可直接拿去跑回歸測試。

        逐字稿一定要帶：少了它就重現不了那次錯誤，回報也就沒用了。
        """
        user = current_user()
        lines = []
        for f in sorted(store.list_feedback(user), key=lambda x: x.get("created_at", "")):
            record = store.get_meeting(f.get("meeting_id", ""), user=user)
            if record is None:
                continue  # 會議已刪除，沒有逐字稿可以重現
            lines.append(json.dumps({
                "id": f["id"],
                "meeting_date": (record.get("meeting") or {}).get("date"),
                "kind": record.get("kind"),
                "transcript": record.get("transcript") or "",
                "reported": {
                    "target": f.get("target"),
                    "target_text": f.get("target_text"),
                    "reason": f.get("reason"),
                    "reason_label": FEEDBACK_REASONS.get(f.get("reason"), ""),
                    "note": f.get("note", ""),
                },
            }, ensure_ascii=False))
        return Response(
            content="\n".join(lines) + ("\n" if lines else ""),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="ai-feedback.jsonl"'},
        )

    # ---- 跨會議追蹤（見 app/insights.py）。全部是既有資料的彙整，不打 AI ----

    def _window(days: int) -> tuple[str, str]:
        end = now_utc().date()
        return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()

    @app.get("/api/insights/open-items")
    def insights_open_items():
        """未決事項看板：同一件事在哪幾場出現過、後來哪一條決議回答了它。"""
        return {"items": open_items(store.list_meetings(user=current_user()))}

    @app.get("/api/insights/people")
    def insights_people():
        user = current_user()
        return {"people": people_stats(
            store.list_meetings(user=user), store.list_tasks(user=user),
            today=now_utc().date().isoformat(),
        )}

    @app.get("/api/insights/report")
    def insights_report(days: int = 7):
        """週報（7）／月報（30）。days 只開放這兩種：中間的數字沒有人會想看，
        但每一種都要在前端多一個按鈕。"""
        if days not in (7, 30):
            raise HTTPException(status_code=400, detail="days 只能是 7 或 30")
        user = current_user()
        start, end = _window(days)
        return period_report(
            store.list_meetings(user=user), store.list_tasks(user=user), start=start, end=end
        )

    # ---- 會議前準備：系列與「上次沒做完的」（見 app/agenda.py）----

    def carryover_for(series: str) -> dict:
        """同系列上一場會議留下來的事。第一次開這個系列的會時也會被呼叫到，
        所以沒有歷史不是錯誤，是空清單。"""
        user = current_user()
        previous = next(
            (m for m in store.list_meetings(user=user) if (m.get("series") or "") == series), None
        )
        if previous is None:
            return {"from": None, "lines": []}
        return {
            "from": {"id": previous["id"],
                     "title": (previous.get("meeting") or {}).get("title"),
                     "date": (previous.get("meeting") or {}).get("date")},
            "lines": carry_over_lines(
                store.list_tasks(meeting_id=previous["id"], user=user),
                previous.get("pending_items") or [],
            ),
        }

    @app.get("/api/series")
    def list_series():
        """已經用過的系列名稱（新到舊）。新會議表單的下拉選單用這個。"""
        names: list[str] = []
        for m in store.list_meetings(user=current_user()):
            name = (m.get("series") or "").strip()
            if name and name not in names:
                names.append(name)
        return {"series": names}

    @app.get("/api/series/{series}/carryover")
    def series_carryover(series: str):
        return carryover_for(series)

    # ---- 留言：對摘要、決議、任務的討論 ----

    def with_author(comment: dict) -> dict:
        # mine 讓前端知道哪幾則畫得出刪除鈕。只比對 uid，不比 email——
        # 訪客沒有 email，全部都會變成「我的」
        return {
            **comment,
            "author_name": author_name(comment),
            "mine": comment.get("author_uid") == current_account(),
        }

    @app.post("/api/meetings/{meeting_id}/comments")
    def add_comment(meeting_id: str, req: dict):
        user = current_user()
        record = store.get_meeting(meeting_id, user=user)
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        try:
            target, text = normalize_target(req.get("target")), clean_text(req.get("text"))
        except CommentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        comment = {
            "id": uuid.uuid4().hex[:12],
            "user": user,
            "meeting_id": meeting_id,
            "target": target,
            "text": text,
            "author_uid": current_account(),
            "author_email": CURRENT_EMAIL.get(),
            "at": now_utc().isoformat(),
        }
        store.save_comment(comment)
        log_activity("comment.add", _title_of(record), meeting_id)
        return {"comment": with_author(comment)}

    @app.get("/api/meetings/{meeting_id}/comments")
    def list_meeting_comments(meeting_id: str):
        comments = store.list_comments(current_user(), meeting_id=meeting_id)
        counts: dict[str, int] = {}
        for c in comments:
            counts[c["target"]] = counts.get(c["target"], 0) + 1
        # counts 讓前端一次畫出「摘要旁 2 則、第 3 條決議旁 1 則」，不必自己再掃一遍
        return {"comments": [with_author(c) for c in comments], "counts": counts}

    @app.delete("/api/comments/{comment_id}")
    def delete_comment(comment_id: str):
        user = current_user()
        comment = next((c for c in store.list_comments(user) if c["id"] == comment_id), None)
        if comment is None:
            raise HTTPException(status_code=404, detail="找不到這則留言")
        if comment.get("author_uid") != current_account():
            # 群組裡可編輯的人很多，但別人說過的話不該被默默抹掉
            raise HTTPException(status_code=403, detail="只能刪除自己的留言")
        store.delete_comment(comment_id, user)
        return {"deleted": comment_id}

    @app.get("/api/activity")
    def list_activity():
        """誰在什麼時候改了什麼。個人工作區一律是空的（見 app/activity.py）。"""
        entries = store.get_activity(current_user())
        return {"activity": [{**e, "text": activity_log.describe(e)} for e in entries]}

    # ---- 手機推播提醒：App 關著、手機鎖著也跳得出來（見 docs/plans/2026-10-26-phase4-push.md）----

    push_sender = push_send if push_send is not None else make_sender(
        settings.vapid_public_key, settings.vapid_private_key, settings.vapid_subject
    )

    def require_push() -> None:
        if push_sender is None:
            raise HTTPException(status_code=503, detail="這個站台沒有設定推播（缺少 VAPID 金鑰）")

    def push_to(subs: list[dict], payload: dict) -> tuple[int, int]:
        """送給這些裝置，回傳（成功幾則，刪掉幾筆死訂閱）。"""
        sent = dropped = 0
        for sub in subs:
            result = push_sender(sub, payload)
            if result == GONE:
                store.delete_push_subscription(sub["id"])
                dropped += 1
            elif result == OK:
                sent += 1
        return sent, dropped

    @app.get("/api/push/key")
    def push_key():
        """前端訂閱前要先拿公鑰；沒設定時要能說出「這個站台沒有推播」，
        而不是跳一個注定失敗的訂閱流程。"""
        on = push_sender is not None
        return {"enabled": on, "public_key": settings.vapid_public_key if on else None}

    @app.post("/api/push/subscribe")
    def push_subscribe(req: dict):
        require_push()
        try:
            sub = normalize_subscription(req)
        except PushError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.save_push_subscription({
            "id": subscription_id(sub["endpoint"]),  # 同一支手機重訂會蓋掉舊的那筆
            "user": current_user(),  # 訂閱跟著「當下在看的工作區」，切換時前端會重訂
            "uid": current_account(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            **sub,
        })
        return {"ok": True}

    @app.post("/api/push/unsubscribe")
    def push_unsubscribe(req: dict):
        endpoint = str(req.get("endpoint") or "").strip()
        if not endpoint:
            raise HTTPException(status_code=400, detail="缺少 endpoint")
        store.delete_push_subscription(subscription_id(endpoint))
        return {"ok": True}

    @app.post("/api/push/test")
    def push_test():
        """立刻送一則。手機到底收不收得到，只有真的送一次才知道。"""
        require_push()
        subs = store.list_push_subscriptions(current_user())
        sent, dropped = push_to(subs, {
            "title": "會議助手",
            "body": "通知測試成功，之後的待辦提醒會像這樣跳出來。",
            "url": "/",
        })
        # 刻意不寫 last_sent：按過試送之後，當天早上還是要收得到真正的提醒
        return {"sent": sent, "devices": len(subs), "dropped": dropped}

    @app.post("/api/push/run")
    def push_run(request: Request):
        """排程每小時叫一次，決定誰現在該收到今天的提醒。

        沒有登入者（GitHub Actions 打進來的），所以不在登入中介層的管轄內，
        改用共用密語把關——沒設密語就整支關掉，不留一個誰都能觸發的入口。
        """
        secret = settings.push_cron_secret
        if not secret or request.headers.get("X-Push-Secret") != secret:
            raise HTTPException(status_code=401, detail="密語不正確")
        if push_sender is None:
            return {"enabled": False, "checked": 0, "sent": 0, "quiet": 0, "dropped": 0}

        now = now_utc()
        payloads: dict[str, dict | None] = {}  # 同一個工作區的多支手機只掃一次
        checked = sent = quiet = dropped = 0
        for sub in store.all_push_subscriptions():
            user = sub.get("user")
            if not user or not due_now(sub, now):
                continue
            checked += 1
            if user not in payloads:
                payloads[user] = digest(scan_reminders(
                    store.list_tasks(user=user), store.list_meetings(user=user), due_soon_days=2
                ))
            payload = payloads[user]
            if payload is None:
                quiet += 1
            else:
                ok, gone = push_to([sub], payload)
                sent += ok
                dropped += gone
                if gone:
                    continue  # 訂閱已經刪掉了，不要再把它寫回去
            # 沒東西可送也記一筆：不記的話早上 9 點到 11 點之間會把同一份清單重掃三次
            sub["last_sent"] = local_time(sub, now).date().isoformat()
            store.save_push_subscription(sub)
        return {"enabled": True, "checked": checked, "sent": sent, "quiet": quiet, "dropped": dropped}

    @app.post("/api/meetings/{meeting_id}/refine")
    def refine_meeting(meeting_id: str, req: RefineRequest):
        """用一句話請 AI 修改分析結果。兩段式：

        - 帶 request：問 AI 要改什麼，回傳 changes（給使用者看）與 patch（原樣送回來套用）。
          **不寫資料庫**——AI 默默改掉摘要或負責人，使用者不會發現。
        - 帶 patch：套用。這一步不打 AI，所以不佔 refine 額度，預覽完一定套得上。
        """
        user = current_user()
        record = store.get_meeting(meeting_id, user=user)
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        tasks = store.list_tasks(meeting_id, user=user)

        try:
            if req.patch is not None:
                # 前端送回來的 patch 一樣要驗：這一步沒有 AI，但有使用者
                patch = parse_patch(json.dumps(req.patch, ensure_ascii=False))
            elif (req.request or "").strip():
                limit("refine")  # 只有這條路會打 AI
                patch = refiner.propose(req.request, record, tasks)
            else:
                raise HTTPException(status_code=400, detail="請說明要修改什麼")
            new_record, new_tasks, changes = apply_patch(record, tasks, patch)
        except RefineError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if req.patch is None:
            return {"changes": changes, "patch": patch}

        if changes:
            fields = {k: new_record[k] for k in ("meeting", "decisions", "pending_items", "tags", "sections")
                      if k in new_record}
            store.update_meeting(meeting_id, fields, user=user)
            for before, after in zip(tasks, new_tasks):
                changed = {k: v for k, v in after.items() if before.get(k) != v}
                if changed:
                    store.update_task(after["id"], user=user, **changed)
            drop_from_rag(meeting_id)  # 摘要、決議變了，問答索引要重建
        return {
            "changes": changes,
            "meeting": store.get_meeting(meeting_id, user=user),
            "tasks": store.list_tasks(meeting_id, user=user),
        }

    @app.post("/api/meetings/{meeting_id}/rename-speaker")
    def rename_speaker(meeting_id: str, req: RenameSpeakerRequest):
        """講者改名：逐字稿講者欄、出席者、摘要與其他 AI 文字、這場會議的任務，一次改完。"""
        old, new = req.old.strip(), req.new.strip()
        if not old:
            raise HTTPException(status_code=400, detail="原名稱不可為空")
        problem = name_problem(new)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        user = current_user()
        record = store.get_meeting(meeting_id, user=user)
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        updated = store.update_meeting(meeting_id, rename_meeting_fields(record, old, new), user=user)
        for task in store.list_tasks(meeting_id, user=user):
            changed = rename_task_fields(task, old, new)
            if changed:
                store.update_task(task["id"], user=user, **changed)
        drop_from_rag(meeting_id)
        tasks = store.list_tasks(meeting_id, user=user)
        # 確認信草稿是分析當下產的，不重產的話出席者、負責人還是舊代號。
        # 草稿是附帶的：舊資料湊不出合法的分析結構就不重產，改名本身已經成功
        try:
            email_draft = email_draft_from_record(updated, tasks)
        except ValidationError as exc:
            logger.warning("改名後重產確認信失敗（會議 %s）：%s", meeting_id, exc)
            email_draft = None
        return {"meeting": updated, "tasks": tasks, "email_draft": email_draft}

    @app.delete("/api/meetings/{meeting_id}")
    def delete_meeting(meeting_id: str):
        """刪除＝丟進垃圾桶。群組裡一個人誤刪會影響所有人，而這在以前是不可逆的。"""
        title = _title_of(store.get_meeting(meeting_id, user=current_user()) or {})
        if not store.trash_meeting(meeting_id, now_utc().isoformat(), user=current_user()):
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        log_activity("meeting.trash", title, meeting_id)
        drop_from_rag(meeting_id)
        return {"deleted": meeting_id, "trashed": True, "days_to_recover": TRASH_DAYS}

    # ---- 垃圾桶 ----

    def sweep_trash(user: str) -> None:
        """清掉超過 30 天的。排程已經有一支了（推播），不值得再加一支——
        開垃圾桶的時候順手清，反正沒人看的時候清不清都一樣。"""
        cutoff = (now_utc() - timedelta(days=TRASH_DAYS)).isoformat()
        for item in store.list_trash(user):
            if (item.get("deleted_at") or "") <= cutoff:
                store.delete_meeting(item["id"], user=user)

    def days_left(item: dict) -> int:
        """還剩幾天可以救。無條件進位：剩 10 小時要說「1 天」，不是「0 天」。"""
        try:
            gone = datetime.fromisoformat(item["deleted_at"])
        except (KeyError, TypeError, ValueError):
            return TRASH_DAYS
        used = (now_utc() - gone).total_seconds() / 86400
        return max(0, math.ceil(TRASH_DAYS - used))

    @app.get("/api/trash")
    def list_trash():
        user = current_user()
        sweep_trash(user)
        return {
            "trash": [{**item, "days_left": days_left(item)} for item in store.list_trash(user)],
            "days": TRASH_DAYS,
        }

    @app.post("/api/trash/{meeting_id}/restore")
    def restore_meeting(meeting_id: str):
        if not store.restore_meeting(meeting_id, user=current_user()):
            raise HTTPException(status_code=404, detail="垃圾桶裡沒有這場會議")
        log_activity("meeting.restore", _title_of(store.get_meeting(meeting_id, user=current_user()) or {}), meeting_id)
        drop_from_rag(meeting_id)  # 內容回來了，索引要重建
        return {"restored": meeting_id}

    @app.delete("/api/trash/{meeting_id}")
    def purge_meeting(meeting_id: str):
        """永久刪除。這一步才是真的沒有了。"""
        user = current_user()
        item = next((m for m in store.list_trash(user) if m["id"] == meeting_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail="垃圾桶裡沒有這場會議")
        store.delete_meeting(meeting_id, user=user)
        for c in store.list_comments(user, meeting_id=meeting_id):
            store.delete_comment(c["id"], user)
        log_activity("meeting.purge", _title_of(item), meeting_id)
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
        limit("analyze")

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

        try:
            analysis = orchestrator.decision.analyze(
                transcript,
                meeting_date=meeting_date,
                kind=kind,
                features=features,
                extra_terms=stored_terms,
                kind_spec=kind_spec_of(kind),
                # 當初貼的議程跟著會議存著。不帶的話重新分析會把對照結果整個弄丟
                agenda=record.get("agenda") or None,
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
            "agenda_review": dumped.get("agenda_review", []),
            "off_agenda": dumped.get("off_agenda", []),
            "meeting": dumped["meeting"],
            "decisions": dumped["decisions"],
            "pending_items": dumped["pending_items"],
            "highlights": dumped.get("highlights", []),
            "tags": dumped.get("tags", []),
            # 種類專屬區塊也要換新：漏掉的話重新分析後畫面上還是舊的區塊內容
            "sections": dumped.get("sections", []),
        }
        if corrections:  # 逐字稿被改過才回寫，沒改就不動原紀錄
            updates["transcript"] = transcript
        store.update_meeting(meeting_id, updates, user=current_user())
        tasks = store.replace_tasks(meeting_id, dumped["todos"], user=current_user())
        notifications = build_notifications(analysis, meeting_id)
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
        return {"tasks": store.list_tasks(meeting_id=meeting_id, user=current_user())}

    @app.get("/api/quota")
    def get_quota():
        """自己今天的額度。/api/usage 是全站合計，不能拿來告訴使用者還剩多少"""
        if CURRENT_GUEST.get():
            limiter, key = guest_limiter, f"ip:{CURRENT_IP.get()}"
        else:
            limiter, key = rate_limiter, current_account()
        if not limiter.enabled:
            return {"enabled": False, "buckets": {}}
        return {"enabled": True, "buckets": limiter.usage(key)}

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
        # 一次列出含逐字稿的完整紀錄：逐場 get_meeting 在 Firestore 上是 N+1 次讀取
        for record in store.list_meetings(user=current_user(), include_transcript=True):
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
        limit("ask")
        usage.record("ask")
        try:
            return ask_agent.ask(
                req.question, meeting_ids=req.meeting_ids, user=current_user()
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            # 配額 429 回的是一整包英文 JSON（quotaMetric、violations、RetryInfo…），
            # 那是給程式看的。使用者只需要知道「不是壞掉，是額度，等一下再來」。
            # 其餘錯誤（金鑰沒設、網路斷線）原因仍要透明——那是唯一的線索
            if is_quota_error(exc):
                logger.warning("問答撞到配額上限：%s", exc)
                raise HTTPException(
                    status_code=502,
                    detail="問答失敗：Gemini 額度暫時用完了。"
                    "跨會議問答要先把會議轉成向量，一次問太多場容易撞到每分鐘上限；"
                    "請等一分鐘再試，或縮小詢問的會議範圍。",
                )
            raise HTTPException(status_code=502, detail=f"問答失敗：{exc}")

    # ---- 翻譯 ----

    @app.post("/api/translate")
    def translate_text(req: TranslateRequest):
        """通用翻譯：翻譯摘要、歷史會議內容等。"""
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="翻譯內容不可為空")
        limit("translate")
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
        created = store.add_task({
            "task": task,
            "owner": (req.owner or "").strip() or None,
            "due_date": req.due_date or None,
            "priority": req.priority,
            "meeting_id": None,
        }, user=current_user())
        log_activity("task.add", task, created.get("id"))
        return created

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
        # 複製一份：LocalJsonStore 回的是儲存層裡同一個 dict，update_task 會就地改掉它，
        # 直接留著參照的話前後比對永遠相等
        before = next(
            (dict(t) for t in store.list_tasks(user=current_user()) if t["id"] == task_id), None
        )
        updated = store.update_task(task_id, user=current_user(), **fields)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"找不到任務：{task_id}")
        log_activity(_task_action(before, updated), updated.get("task") or "", task_id)
        return updated

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str):
        before = next((t for t in store.list_tasks(user=current_user()) if t["id"] == task_id), None)
        if not store.delete_task(task_id, user=current_user()):
            raise HTTPException(status_code=404, detail=f"找不到任務：{task_id}")
        log_activity("task.delete", (before or {}).get("task") or "", task_id)
        return {"deleted": task_id}

    # ---- 群組工作區（需要 Google 登入：沒有帳號就沒有「誰」可以邀請） ----

    def group_call(fn, *args):
        if not settings.auth_enabled:
            raise HTTPException(status_code=404, detail="群組功能需要啟用 Google 登入")
        if CURRENT_GUEST.get():
            raise HTTPException(status_code=403, detail="訪客不能使用群組，請改用 Google 登入")
        try:
            fn(*args)
        except group_svc.GroupError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail)
        # 每個操作都回傳側欄要的最新狀態，前端不必再多打一次
        return group_svc.summary_for(store, current_account(), CURRENT_EMAIL.get())

    @app.get("/api/groups")
    def list_groups():
        return group_call(lambda: None)

    @app.post("/api/groups")
    def create_group(req: GroupNameRequest):
        created = {}

        def make():
            created.update(group_svc.create(store, req.name, current_account(), CURRENT_EMAIL.get()))

        summary = group_call(make)
        return {"id": created["id"], **summary}

    @app.patch("/api/groups/{group_id}")
    def rename_group(group_id: str, req: GroupNameRequest):
        return group_call(group_svc.rename, store, group_id, current_account(), req.name)

    @app.delete("/api/groups/{group_id}")
    def disband_group(group_id: str):
        def drop_rag(scope: str) -> None:
            if rag_index is not None:
                rag_index.reset(user=scope)

        return group_call(group_svc.disband, store, group_id, current_account(), drop_rag)

    @app.post("/api/groups/{group_id}/invites")
    def invite_to_group(group_id: str, req: GroupInviteRequest):
        return group_call(group_svc.invite, store, group_id, current_account(),
                          CURRENT_EMAIL.get(), req.email, req.role)

    @app.delete("/api/groups/{group_id}/invites/{email}")
    def revoke_group_invite(group_id: str, email: str):
        return group_call(group_svc.revoke, store, group_id, current_account(), email)

    @app.post("/api/groups/{group_id}/accept")
    def accept_group_invite(group_id: str):
        return group_call(group_svc.accept, store, group_id, current_account(), CURRENT_EMAIL.get())

    @app.post("/api/groups/{group_id}/decline")
    def decline_group_invite(group_id: str):
        return group_call(group_svc.decline, store, group_id, CURRENT_EMAIL.get())

    @app.patch("/api/groups/{group_id}/members/{member_uid}")
    def set_group_role(group_id: str, member_uid: str, req: GroupRoleRequest):
        return group_call(group_svc.set_role, store, group_id, current_account(), member_uid, req.role)

    @app.delete("/api/groups/{group_id}/members/{member_uid}")
    def remove_group_member(group_id: str, member_uid: str):
        return group_call(group_svc.remove_member, store, group_id, current_account(), member_uid)

    @app.delete("/api/account")
    def delete_account():
        """刪除帳號與全部個人資料，最後刪掉 Firebase 帳號本身。"""
        if not settings.auth_enabled:
            # 共用 API Token 模式所有人是同一個使用者，刪下去等於清空整個服務
            raise HTTPException(status_code=400, detail="這個伺服器沒有帳號制，沒有帳號可以刪除")
        uid = current_account()

        def drop_rag(scope: str) -> None:
            if rag_index is not None:
                rag_index.reset(user=scope)

        try:
            count = group_svc.delete_account(store, uid, CURRENT_EMAIL.get(), drop_rag)
        except group_svc.GroupError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail)
        try:
            remove_auth_user(uid)
            auth_deleted = True
        except Exception:
            # 資料已經刪光了，這一步失敗不能回 500 讓人以為什麼都沒刪；
            # 剩下一個空帳號，前端照樣登出
            logger.exception("刪除 Firebase 帳號失敗：%s", uid)
            auth_deleted = False
        return {"deleted_meetings": count, "auth_deleted": auth_deleted}

    @app.delete("/api/guest/data")
    def wipe_guest_data():
        """訪客離開：刪掉這個匿名帳號的所有資料。只限訪客——誤打這支端點不能清掉正式帳號。"""
        if not CURRENT_GUEST.get():
            raise HTTPException(status_code=403, detail="只有訪客能使用這個功能")

        def drop_rag(scope: str) -> None:
            if rag_index is not None:
                rag_index.reset(user=scope)

        return {"deleted_meetings": group_svc.purge_scope(store, current_account(), drop_rag)}

    def personal_only(action: str) -> None:
        # 還原會整份覆蓋這個工作區的資料；在群組裡等於一個人就能把大家的會議換掉
        if in_group_workspace():
            raise HTTPException(status_code=400, detail=f"{action}只在個人工作區可用，請先切回「個人」")

    @app.get("/api/backup")
    def download_backup():
        """整份資料（會議＋任務＋詞彙）打包成 JSON 下載，供離線保存或搬移。"""
        personal_only("備份")
        return Response(
            content=json.dumps(store.export_all(user=current_user()), ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="meeting-agent-backup.json"'},
        )

    @app.post("/api/restore")
    def restore_backup(data: dict):
        """以備份 JSON 整份覆蓋現有資料。"""
        personal_only("還原")
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
        return download(
            meeting_report_md(record, store.list_tasks(meeting_id=meeting_id, user=current_user())),
            "text/markdown; charset=utf-8", (record.get("meeting") or {}).get("title"), "md",
        )

    def download(content, media_type: str, title: str, ext: str) -> Response:
        """帶上會議標題的下載回應。下載資料夾裡躺著五個 meeting-a1b2c3.pdf 的時候，
        誰也認不出哪個是哪個——所以檔名用標題，並照 RFC 5987 編中文。"""
        safe = re.sub(r'[\/:*?"<>|]', "_", (title or "會議紀錄")).strip() or "會議紀錄"
        quoted = quote(f"{safe}.{ext}")
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition":
                     f"attachment; filename=\"meeting.{ext}\"; filename*=UTF-8''{quoted}"},
        )

    def export_record(meeting_id: str) -> tuple[dict, list[dict]]:
        user = current_user()
        record = store.get_meeting(meeting_id, user=user)
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        return record, store.list_tasks(meeting_id=meeting_id, user=user)

    @app.get("/api/meetings/{meeting_id}/report.docx")
    def meeting_report_docx(meeting_id: str):
        record, tasks = export_record(meeting_id)
        return download(
            meeting_to_docx(record, tasks),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            (record.get("meeting") or {}).get("title"), "docx",
        )

    @app.get("/api/meetings/{meeting_id}/report.pdf")
    def meeting_report_pdf(meeting_id: str):
        record, tasks = export_record(meeting_id)
        try:
            blob = meeting_to_pdf(record, tasks)
        except PdfFontMissing as exc:
            # 產出亂碼 PDF 比產不出來更糟：使用者會把它寄出去才發現
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return download(blob, "application/pdf", (record.get("meeting") or {}).get("title"), "pdf")

    @app.get("/api/meetings/{meeting_id}/transcript.srt")
    def meeting_srt(meeting_id: str):
        return _subtitles(meeting_id, to_srt, "srt")

    @app.get("/api/meetings/{meeting_id}/transcript.vtt")
    def meeting_vtt(meeting_id: str):
        return _subtitles(meeting_id, to_vtt, "vtt")

    def _subtitles(meeting_id: str, render, ext: str) -> Response:
        record, _ = export_record(meeting_id)
        content = render(record.get("transcript") or "")
        if not content:
            raise HTTPException(
                status_code=422, detail="這場會議的逐字稿沒有時間戳，做不出字幕檔"
            )
        media = "application/x-subrip" if ext == "srt" else "text/vtt"
        return download(content.encode("utf-8"), f"{media}; charset=utf-8",
                        (record.get("meeting") or {}).get("title"), ext)

    @app.get("/api/meetings/{meeting_id}/notifications")
    def meeting_notifications(meeting_id: str):
        """歷史會議用分析結果視窗完整檢視時，重產確認信與行事曆事件（不打 API）。"""
        user = current_user()
        record = store.get_meeting(meeting_id, user=user)
        if record is None:
            raise HTTPException(status_code=404, detail=f"找不到會議：{meeting_id}")
        try:
            return notifications_from_record(record, store.list_tasks(meeting_id, user=user))
        except ValidationError as exc:
            logger.warning("重產確認信失敗（會議 %s）：%s", meeting_id, exc)
            raise HTTPException(status_code=422, detail="這場會議的資料不完整，無法產生確認信與行事曆事件")

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
        return download(content, "text/calendar; charset=utf-8",
                        record.get("meeting", {}).get("title"), "ics")

    # ---- 輸入路徑 2：音檔 / 影片上傳（背景轉錄） ----

    @app.post("/api/media")
    def upload_media(
        file: UploadFile = File(...),
        meeting_date: Optional[str] = Form(None),
        kind: Optional[str] = Form(None),
        features: Optional[str] = Form(None),
        correct_typos: Optional[str] = Form(None),
        terms: Optional[str] = Form(None),
        agenda: Optional[str] = Form(None),
        series: Optional[str] = Form(None),
        carry_over: Optional[str] = Form(None),
    ):
        limit("media")  # 在存檔之前擋：被擋下的上傳不佔暫存磁碟
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

        suffix = validate_media_suffix(file.filename)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        dest = uploads_dir / f"{uuid.uuid4().hex[:12]}{suffix}"
        # 2 小時的影片可能數 GB，串流寫入不佔記憶體；同時守住大小上限
        save_upload(file.file, dest, max_upload_bytes)

        usage.record("media_upload")
        if correct:
            usage.record("correct")
        return {
            "job_id": job_manager.submit(
                dest,
                user=current_user(),  # 背景執行緒沒有請求上下文，現在就捕捉
                meeting_date=parsed_date,
                kind=kind,
                features=resolved_features,
                correct_typos=correct,
                terms=resolved_terms,
                kind_spec=kind_spec_of(kind),
                agenda=clean_agenda(agenda),
                series=(series or "").strip() or None,
                # 背景執行緒沒有請求上下文，上次沒做完的事現在就查好
                carry_over=(carryover_for(series.strip())["lines"]
                            if (str(carry_over or "").lower() in _truthy and series
                                and series.strip()) else None),
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
        limit("live_start")
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
        # 本段開頭與前一段重疊了幾秒。前端刻意讓兩個錄音器重疊一小段，一句話
        # 才不會被硬切點剁成兩半；重疊處會被轉錄兩次，後端依絕對時間濾掉
        overlap: Optional[float] = Form(None),
    ):
        limit("live_chunk")
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
                overlap_seconds=overlap,
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
            # 沒註冊樣本時這行回 {} 且不打任何 API。
            # 重試分析會再走一次這裡，那時音檔已經沒了，靠 session 裡的快取回答
            # 有 pyannote：整場重標並用 voiceprint 比對姓名，finish() 會回傳重標後的
            # 逐字稿；做不到或失敗回 None，照舊走 Gemini 的聲紋比對
            speaker_prior = live_manager.diarize_session(session_id, user=current_user())
            if speaker_prior is None:
                speaker_prior = live_manager.voice_mapping(session_id, user=current_user())
            enrolled = live_manager.enrolled_names(session_id, user=current_user())
            transcript = live_manager.finish(session_id, user=current_user())
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not transcript.strip():
            raise HTTPException(
                status_code=400, detail="這場聆聽沒有收到任何語音內容，無法分析"
            )
        kind = validate_kind(req.kind if req else None)
        features = resolve_features(req.features if req else None, kind)
        live_series = ((req.series if req else None) or "").strip() or None
        result = run_analysis(
            transcript,
            req.meeting_date if req else None,
            kind,
            features,
            correct_typos=bool(req and req.correct_typos),
            terms=validate_terms(req.terms if req else None),
            speaker_prior=speaker_prior,
            # 會前登記的人＝使用者親自指認「這些人在場、姓名這樣寫」
            attendees=enrolled,
            # 會議當下才有的資訊：按下的標記時間點、手打的筆記
            marks=req.marks if req else None,
            notes=req.notes if req else None,
            agenda=clean_agenda(req.agenda if req else None),
            series=live_series,
            carry_over=(carryover_for(live_series)["lines"]
                        if (req and req.carry_over and live_series) else None),
        )
        # result 帶著校正後的 transcript，放在後面覆蓋原始版本。
        # speakers_matched/unmatched 讓前端說得出「預錄的 4 位認出了哪 2 位」——
        # 沒認出來的那幾位才是使用者需要知道的
        return {
            "transcript": transcript,
            **result,
            "speakers_matched": speaker_prior,
            "speakers_unmatched": [n for n in enrolled if n not in speaker_prior.values()],
        }

    return app


app = create_app()
