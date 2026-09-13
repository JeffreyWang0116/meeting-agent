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
import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.agents.speaker_namer_agent import is_safe_name
from app.glossary import terms_hint_line
from app.stores.base import DEFAULT_USER
from app.transcription.segments import (
    TIME_PREFIX_RE,
    chunk_hint,
    collect_speakers,
    drop_lines_before,
    pick_evidence_chunks,
    shift_timestamps,
    speaker_of,
    transcript_tail,
)


logger = logging.getLogger(__name__)


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
    # 會前錄的聲音樣本：[{"name": 姓名, "path": 檔案}]，依錄製順序
    enrollments: list[dict] = field(default_factory=list)
    # index → 該段音檔路徑。副檔名由上傳決定，所以要記下來而不是事後拼字串
    chunk_paths: dict[int, Path] = field(default_factory=dict)
    # 目前已比對出的 {代號: 姓名}。這份快取是 finish() 刪掉音檔之後唯一還在的
    # 結果——「重試分析」靠它才不會把整組姓名賠掉
    voice_result: dict[str, str] = field(default_factory=dict)
    voice_attempts: int = 0  # 已完成幾次比對（提早比對只做一次）
    voice_matching: bool = False  # 背景比對進行中
    # index → (前端回報的整場 offset, 與前一段重疊秒數)。pyannote 整場重標時
    # 靠它把串接檔上的時間換回整場時間
    timing: dict[int, tuple[float | None, float]] = field(default_factory=dict)
    # pyannote 重標後的逐字稿。有值就代表代號已換成 pyannote 那套：finish() 回傳它、
    # 「重試分析」沿用它，不再重打 API（音檔在 finish 後就刪了）
    diarized_transcript: str | None = None


class LiveSessionManager:
    # 閒置多久算被遺棄。比任何一場真實會議都長，但仍有上界
    SESSION_TTL_SECONDS = 2 * 60 * 60

    # 最多幾個人可以錄聲音樣本。上限存在是為了讓送進比對的音訊量有界；
    # 人再多時嗓音相近的機率也上升，比對本來就不該當唯一依據
    MAX_ENROLLMENTS = 4

    # 最多拿幾段會議錄音當比對證據（見 pick_evidence_chunks）
    MAX_EVIDENCE_CHUNKS = 4

    # 收到幾段「有講者標籤」的逐字稿之後，先在背景比對一次。開完一小時才發現
    # 預錄沒生效已經來不及；提早比一次，使用者當場就看得到認出了誰
    EARLY_MATCH_AFTER_CHUNKS = 2

    def __init__(
        self,
        transcriber,
        work_dir: Path | str,
        translator=None,
        now: Callable[[], float] = time.monotonic,
        voice_matcher=None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
        diarizer=None,
    ):
        self._transcriber = transcriber
        self._translator = translator
        # 聲紋比對器（鴨子型別，需有 match(enrollments, evidence)）。
        # 沒注入就等同這個功能不存在
        self._voice_matcher = voice_matcher
        self._work_dir = Path(work_dir)
        self._now = now  # 單調時鐘；可注入，測試不必真的等兩小時
        # 提早比對怎麼跑。預設丟背景執行緒——比對是強模型、要好幾秒，擋在
        # add_chunk 裡會讓那一段的字幕跟著延遲。可注入以便測試原地執行
        self._spawn = spawn or (
            lambda fn: threading.Thread(target=fn, daemon=True).start()
        )
        # pyannote 講者分離（選用，見 app/transcription/diarizer.py）。None＝結束時
        # 不重標，講者與姓名照舊由 Gemini 轉錄與 voice_matcher 決定
        self._diarizer = diarizer
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
        overlap_seconds: float | None = None,
    ) -> dict:
        """收下一段音訊並轉錄。

        overlap_seconds：本段開頭與前一段重疊了幾秒。前端刻意讓兩個錄音器
        重疊一小段，一句話才不會被硬切點剁成兩半；重疊處會被轉錄兩次，
        這裡依絕對時間濾掉重複的行。
        """
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
        with self._lock:
            # 聲紋比對要回頭取這段音檔，副檔名依上傳而異，記下來才找得到
            session.chunk_paths[index] = chunk_path
            session.timing[index] = (offset_seconds, float(overlap_seconds or 0.0))

        text = self._transcribe(chunk_path, hint, session.user).strip()
        if text:
            text = shift_timestamps(text, offset_seconds)
            # 重疊的那幾秒前一段已經轉過，依絕對時間濾掉，逐字稿才不會出現
            # 兩句一模一樣的話（用字不會完全相同，但時間軸是同一條）
            if overlap_seconds and offset_seconds is not None:
                text = drop_lines_before(
                    text, float(offset_seconds) + float(overlap_seconds)
                ).strip()

        with self._lock:
            session.parts[index] = text
            if text:
                collect_speakers(text, session.speakers)
            transcript = _join(session.parts)
            start_match = self._should_early_match_locked(session)
            if start_match:
                session.voice_matching = True
        # 比對要好幾秒（強模型＋上傳音檔），丟到背景跑：這一段的字幕必須
        # 立刻回給前端，不能等比對
        if start_match:
            self._spawn(lambda: self._run_match(session_id, session.user))
        with self._lock:
            names = dict(session.voice_result)

        translation = None
        if text and session.translate_to and self._translator:
            # 剝掉行首時間標記再翻譯，時間戳不需要翻、也避免譯文格式被帶歪
            plain = "\n".join(TIME_PREFIX_RE.sub("", ln) for ln in text.split("\n"))
            try:
                translation = self._translator.translate(plain, session.translate_to)
            except Exception:  # 翻譯失敗不擋逐字稿主流程
                translation = None

        # names：目前已比對出的 {代號: 姓名}。前端拿它當「預錄有沒有生效」的
        # 當場回饋，不必等整場開完才知道
        return {
            "text": text,
            "translation": translation,
            "transcript": transcript,
            "names": names,
        }

    def _transcribe(self, path: Path, hint: str | None, user: str = DEFAULT_USER) -> str:
        """轉錄一段。transcriber 是注入的鴨子型別，簽名不一定收 hint/user，
        支援才傳（跨段講者提示只對會標講者的後端有意義）。

        user 一定要帶到：詞彙表依帳號分開，少了它會讀到 DEFAULT_USER 那桶
        舊資料，把別人的詞彙灌進這場聆聽的轉錄提示裡。
        """
        fn = self._transcriber.transcribe
        try:
            params = inspect.signature(fn).parameters
            takes = lambda name: name in params or any(
                p.kind == p.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):
            takes = lambda name: False
        kwargs = {}
        if hint and takes("hint"):
            kwargs["hint"] = hint
        if takes("user"):
            kwargs["user"] = user
        return fn(path, **kwargs)

    def _should_early_match_locked(self, session: LiveSession) -> bool:
        """要不要在這一段之後先比對一次。必須在鎖內呼叫。

        只做一次：比對用的是強模型，每段都比既慢又燒額度。沒註冊樣本、
        沒有比對器、已經比過或正在比，一律不做——這個功能沒開就不該有成本。
        """
        if not self._voice_matcher or not session.enrollments:
            return False
        if session.voice_attempts or session.voice_matching or session.closed:
            return False
        return _labelled_chunks(session.parts) >= self.EARLY_MATCH_AFTER_CHUNKS

    def _run_match(self, session_id: str, user: str) -> None:
        """背景比對。失敗一律吞掉：這是加分項，不能讓聆聽本身跟著出事。"""
        try:
            self.voice_mapping(session_id, user)
        except Exception:
            pass
        finally:
            with self._lock:
                session = self._sessions.get(session_id)
                if session is not None:
                    session.voice_matching = False

    def enrolled_names(self, session_id: str, user: str = DEFAULT_USER) -> list[str]:
        """這場註冊過樣本的姓名（依錄製順序）。

        結束時要能告訴使用者「預錄的 4 位認出了哪 2 位」——沒認出來的那幾位
        才是使用者需要知道的，不然他只會看到有些人有名字、有些人沒有。
        """
        session = self._get(session_id, user)
        with self._lock:
            return [e["name"] for e in session.enrollments]

    def transcript(self, session_id: str, user: str = DEFAULT_USER) -> str:
        with self._lock:
            self._prune_locked()
            return _join(self._get_locked(session_id, user).parts)

    def _get_locked(self, session_id: str, user: str = DEFAULT_USER) -> LiveSession:
        session = self._sessions.get(session_id)
        if session is None or session.user != user:
            raise SessionNotFound(f"找不到聆聽 session：{session_id}")
        return session

    def enroll(
        self,
        session_id: str,
        name: str,
        data: bytes,
        suffix: str = ".webm",
        user: str = DEFAULT_USER,
    ) -> int:
        """存一段「這是誰的聲音」的樣本，回傳目前已註冊人數。

        姓名在這裡就驗證：它最後會被寫進逐字稿的講者欄，含冒號或換行的名字
        會造出假標籤、破壞時間軸。擋在入口比等到下游 apply_speaker_names
        整批放棄好——使用者當下就知道名字要改。
        """
        session = self._get(session_id, user)
        name = (name or "").strip()
        if not is_safe_name(name):
            raise ValueError("姓名不可為空、不可含冒號或換行，且不宜過長")
        with self._lock:
            if session.closed:
                raise ValueError("此聆聽 session 已結束，無法再註冊聲音樣本")
            if len(session.enrollments) >= self.MAX_ENROLLMENTS:
                raise ValueError(f"最多只能註冊 {self.MAX_ENROLLMENTS} 個人的聲音")
            # 兩份樣本掛同一個名字，比對只會更混亂；而下游 apply_speaker_names
            # 遇到重複姓名會整批放棄，當場擋下來比事後才無聲失效好
            if any(e["name"] == name for e in session.enrollments):
                raise ValueError(f"「{name}」已經註冊過聲音樣本了")
            session.last_active = self._now()
            index = len(session.enrollments)
            path = session.dir / f"enroll_{index:02d}{suffix}"
            session.enrollments.append({"name": name, "path": path})
            count = len(session.enrollments)
        path.write_bytes(data)
        return count

    def voice_mapping(
        self, session_id: str, user: str = DEFAULT_USER
    ) -> dict[str, str]:
        """依會前錄的樣本比對出 {講者代號: 姓名}。沒註冊樣本就回 {}。

        **務必在 finish() 之前呼叫**：finish() 會刪掉整個 session 目錄，樣本與
        會議音檔都在裡面。比對需要「聽得到聲音」，所以只有這個時機做得到。

        比對是加分項，任何一步出錯都回 {}——代號本身可用，不該讓一場已經開完
        的會議分析失敗。
        """
        session = self._get(session_id, user)
        with self._lock:
            # pyannote 重標過：代號已經換了一套，Gemini 比對的代號對不上新逐字稿，
            # 背景比對也不能再把它們寫回來
            if session.diarized_transcript is not None:
                return dict(session.voice_result)
            cached = dict(session.voice_result)
            enrollments = list(session.enrollments)
            if not enrollments or not self._voice_matcher:
                return {}
            evidence = [
                {"path": session.chunk_paths[i], "transcript": session.parts[i]}
                for i in pick_evidence_chunks(session.parts, self.MAX_EVIDENCE_CHUNKS)
                if i in session.chunk_paths and session.parts[i]
                and session.chunk_paths[i].exists()
            ]
        # finish() 之後樣本與錄音段都被刪了，沒有聲音可比。此時快取是唯一還在的
        # 結果——「重試分析」正是走到這裡，拿不到快取就等於整組姓名憑空消失
        if not evidence or not all(Path(e["path"]).exists() for e in enrollments):
            return cached
        try:
            fresh = self._voice_matcher.match(enrollments, evidence) or {}
        except Exception:
            fresh = {}
        merged = _merge_mappings(cached, fresh)
        with self._lock:
            if session.diarized_transcript is not None:  # 比對期間 pyannote 已經重標完
                return dict(session.voice_result)
            session.voice_result = merged
            session.voice_attempts += 1
        return merged

    def diarize_session(
        self, session_id: str, user: str = DEFAULT_USER
    ) -> dict[str, str] | None:
        """用 pyannote 把整場重標，回傳 {講者代號: 姓名}；做不到或失敗回 None。

        None 代表「照舊」：呼叫端改走 voice_mapping（Gemini 比對），finish() 回傳
        Gemini 標註的逐字稿。**務必在 finish() 之前呼叫**——要聽得到錄音段與樣本。

        聆聽中的提早比對（Gemini）不受影響，照舊當場回報；這裡只在結束時做一次。
        """
        session = self._get(session_id, user)
        if not self._diarizer:
            return None
        with self._lock:
            if session.diarized_transcript is not None:  # 重試分析：沿用上次結果
                return dict(session.voice_result)
            transcript = _join(session.parts)
            timing = dict(session.timing)
            paths = dict(session.chunk_paths)
            enrollments = list(session.enrollments)
        if not transcript.strip():
            return None
        pieces = []
        for index in sorted(paths):
            offset, overlap = timing.get(index, (None, 0.0))
            # 沒 offset＝舊版前端，逐字稿的時間戳已經被剝掉，分群結果對不回任何一行
            if offset is None:
                return None
            if not paths[index].exists():
                continue  # 上傳失敗沒落地的段：少一段聲音，其餘時間照樣對得上
            pieces.append({
                "path": paths[index],
                "skip": overlap,  # 重疊的那幾秒前一段已經有了
                "start": float(offset) + overlap,
            })
        if not pieces:
            return None
        enrollments = [e for e in enrollments if Path(e["path"]).exists()]
        try:
            text, prior = self._diarizer.relabel_session(transcript, pieces, enrollments)
        except Exception as exc:
            logger.warning("即時聆聽講者分離失敗，沿用 Gemini 的代號與比對：%s", exc)
            return None
        with self._lock:
            session.diarized_transcript = text
            session.voice_result = dict(prior)
        return dict(prior)

    def finish(self, session_id: str, user: str = DEFAULT_USER) -> str:
        session = self._get(session_id, user)
        with self._lock:
            session.closed = True
            # 不立刻移除：剛結束時遲到的音訊段要拿到「已結束」的明確訊息，
            # 而不是查無此 session。這筆殘留由 TTL 回收
            session.last_active = self._now()
            transcript = (
                session.diarized_transcript
                if session.diarized_transcript is not None
                else _join(session.parts)
            )
        # 錄音段與聲音樣本都不再需要，刪掉整個 session 目錄釋放磁碟（雲端暫時性
        # 磁碟很小）。聲紋是生物特徵資料，不落地保存也省掉一整類隱私問題
        shutil.rmtree(session.dir, ignore_errors=True)
        return transcript


def _chunk_hint(session: LiveSession) -> str:
    """這一段轉錄要帶的提示：跨段講者一致性 ＋ 本次專用詞彙。

    兩者是不同面向——前者管講者標籤別重新編號，後者管內文用字別聽錯——所以
    併著送。

    講者那半改用長檔分段的同一套 chunk_hint：除了已出現的講者清單，還附上
    前一段結尾的逐字稿（本段開頭的重疊音訊就是那些內容）。只給清單沒有用，
    模型沒聽過前一段，無從知道「講者B」是哪個嗓音，只能從自己這段重新編號。
    它也會要求「即使只有一位講者也要標註」——開場常是主席單人宣讀，第一段
    一旦沒有任何標籤，後續段就沒有可沿用的清單，跨段一致性整條失效。
    """
    return chunk_hint(session.speakers, _previous_tail(session)) + terms_hint_line(
        session.terms, "本次會議專用詞彙"
    )


def _previous_tail(session: LiveSession) -> str:
    """最近一段「已辨識完」的逐字稿結尾。

    parts 可能有還在辨識中的空洞（None），由後往前找第一段有內容的即可——
    那正是本段開頭重疊到的那一段。
    """
    for text in reversed(session.parts):
        if text:
            return transcript_tail(text)
    return ""


def _labelled_chunks(parts: list[str | None]) -> int:
    """有講者標籤的段落數。沒有標籤的段落給不出「代號↔嗓音」的對應關係，
    當比對證據只會干擾判斷，所以不算數。"""
    return sum(
        1
        for text in parts
        if text and any(speaker_of(line) for line in text.splitlines())
    )


def _merge_mappings(cached: dict[str, str], fresh: dict[str, str]) -> dict[str, str]:
    """把新一次的比對結果併進既有的。

    後一次看到的證據比較多（整場 vs 前兩段），衝突時以新的為準；舊的只用來
    補新一次沒認出來的代號——後半場才發言的人與前半場的人本來就分屬不同次。

    合併後同一個姓名不可以落在兩個代號上：下游 apply_speaker_names 遇到重複
    姓名會整批放棄，連正確的那幾筆一起賠掉。
    """
    merged = {**cached, **fresh}
    for label, name in list(merged.items()):
        if label not in fresh and name in fresh.values():
            del merged[label]  # 舊的那筆讓位給新的
    return merged


def _join(parts: list[str | None]) -> str:
    """依 index 順序串接已完成的段落，跳過尚未辨識完（None）與空白段。"""
    return "\n".join(p for p in parts if p)
