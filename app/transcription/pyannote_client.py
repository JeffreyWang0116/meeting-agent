"""pyannoteAI REST 客戶端：專門的講者分離（diarization）與聲紋辨識。

混合式講者辨識的「聽」那一半：Gemini 負責轉文字，這裡負責「誰在何時講話」，
兩者由 speaker_align 依時間戳接合。契約見 https://docs.pyannote.ai/openapi.json

錯誤一律包成 PyannoteError：呼叫端（轉錄工作、即時聆聽）拿到它就退回 Gemini
自己標的代號——講者辨識是加分項，不能讓轉錄因為第三方服務出事而整份失敗。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Callable

import httpx

API_BASE = "https://api.pyannote.ai"

# 429 最多重試幾次、每次最多等幾秒。Retry-After 可能給很大的值，照單全收會讓
# 背景轉錄工作卡死在那裡；等不起就放棄，交給呼叫端退回 Gemini 代號
_MAX_RATE_LIMIT_RETRIES = 5
_MAX_RETRY_AFTER_SECONDS = 60

# 輪詢時伺服器端暫時性錯誤（5xx、連線中斷）最多連續容忍幾次
_MAX_POLL_ERRORS = 5


class PyannoteError(Exception):
    pass


class PyannoteClient:
    def __init__(
        self,
        api_key: str | None,
        model: str = "precision-2",
        base_url: str = API_BASE,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 900.0,
    ):
        if not api_key:
            raise PyannoteError("未設定 PYANNOTE_API_KEY")
        self.model = model
        self._auth = {"Authorization": f"Bearer {api_key}"}
        self._base_url = base_url
        # 不在 client 層設 Authorization：同一個 client 也要 PUT 到預簽網址，
        # 多帶一組認證會被儲存服務拒絕
        self._http = httpx.Client(transport=transport, timeout=httpx.Timeout(60, write=600))
        self._sleep = sleep
        self._clock = clock
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    # ---- 高階操作 ----

    def diarize(self, path: Path | str, **bounds) -> dict:
        """上傳→分講者→等結果，回傳 job output（含 exclusiveDiarization）。"""
        return self.wait(self.submit_diarize(self.upload(path), **bounds))

    def voiceprint(self, path: Path | str) -> str:
        """一段樣本音檔 → voiceprint（base64 字串）。按建立次數計費，呼叫端自行節制。"""
        output = self.wait(self.submit_voiceprint(self.upload(path)))
        voiceprint = output.get("voiceprint")
        if not voiceprint:
            raise PyannoteError(f"voiceprint 工作沒有產出：{output.get('error') or output}")
        return voiceprint

    # ---- 上傳 ----

    def upload(self, path: Path | str) -> str:
        """上傳到 pyannote 暫存區（24~48 小時自動刪除），回傳 media:// 網址。

        用檔案物件串流 PUT：httpx 會依檔案大小帶 Content-Length（預簽網址不收
        chunked），也不必把整個音檔讀進記憶體——Render 免費方案只有 512MB。
        """
        media_url = f"media://meeting-agent/{uuid.uuid4().hex}{Path(path).suffix}"
        presigned = self._api("POST", "/v1/media/input", {"url": media_url})["url"]
        with open(path, "rb") as handle:
            try:
                response = self._http.put(
                    presigned,
                    content=handle,
                    headers={"Content-Type": "application/octet-stream"},
                )
            except httpx.HTTPError as exc:
                raise PyannoteError(f"音檔上傳失敗：{exc}") from exc
        if response.is_error:
            raise PyannoteError(f"音檔上傳失敗：HTTP {response.status_code}")
        return media_url

    # ---- 送出工作 ----

    def submit_diarize(
        self,
        media_url: str,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> str:
        # exclusive：要「同一時間只有一位講者」的版本——逐字稿一行只能掛一個名字
        body: dict = {"url": media_url, "model": self.model, "exclusive": True}
        if min_speakers:
            body["minSpeakers"] = min_speakers
        if max_speakers:
            body["maxSpeakers"] = max_speakers
        return self._api("POST", "/v1/diarize", body)["jobId"]

    def submit_voiceprint(self, media_url: str) -> str:
        return self._api(
            "POST", "/v1/voiceprint", {"url": media_url, "model": self.model}
        )["jobId"]

    def submit_identify(
        self,
        media_url: str,
        voiceprints: dict[str, str],
        threshold: float | None = None,
    ) -> str:
        matching: dict = {"exclusive": True}  # 兩位講者不可以對到同一個人
        if threshold is not None:
            matching["threshold"] = threshold
        body = {
            "url": media_url,
            "model": self.model,
            "exclusive": True,
            "voiceprints": [
                {"label": name, "voiceprint": vp} for name, vp in voiceprints.items()
            ],
            "matching": matching,
        }
        return self._api("POST", "/v1/identify", body)["jobId"]

    # ---- 輪詢 ----

    def wait(self, job_id: str) -> dict:
        """輪詢到工作結束，回傳 output。失敗、取消、逾時都丟 PyannoteError。"""
        deadline = self._clock() + self.timeout_seconds
        errors = 0
        while True:
            if self._clock() > deadline:
                raise PyannoteError(f"pyannote 工作逾時（{self.timeout_seconds:.0f} 秒）：{job_id}")
            self._sleep(self.poll_seconds)
            try:
                data = self._api("GET", f"/v1/jobs/{job_id}")
            except PyannoteError as exc:
                # 查詢是冪等的，暫時性錯誤多試幾次；送出工作則不重試，免得重複建工作重複計費
                errors += 1
                if errors > _MAX_POLL_ERRORS or not getattr(exc, "transient", False):
                    raise
                continue
            errors = 0
            status = data.get("status")
            if status == "succeeded":
                return data.get("output") or {}
            if status in ("failed", "canceled"):
                detail = (data.get("output") or {}).get("error") or ""
                raise PyannoteError(f"pyannote 工作 {status}：{detail}".rstrip("："))

    # ---- 內部 ----

    def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = self._http.request(
                    method, self._base_url + path, json=body, headers=self._auth
                )
            except httpx.HTTPError as exc:
                error = PyannoteError(f"pyannote 連線失敗：{exc}")
                error.transient = True
                raise error from exc
            if response.status_code == 429 and attempt < _MAX_RATE_LIMIT_RETRIES:
                self._sleep(_retry_after(response))
                continue
            if response.is_error:
                error = PyannoteError(
                    f"pyannote {method} {path} 失敗：HTTP {response.status_code} {_message(response)}".rstrip()
                )
                error.transient = response.status_code >= 500
                raise error
            return response.json()
        raise AssertionError("unreachable")


def _retry_after(response: httpx.Response) -> float:
    try:
        seconds = float(response.headers.get("Retry-After", "1"))
    except ValueError:
        seconds = 1.0
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_SECONDS)


def _message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    return str(data.get("message") or "") if isinstance(data, dict) else ""
