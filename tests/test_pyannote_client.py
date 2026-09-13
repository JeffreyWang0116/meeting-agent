"""pyannoteAI 客戶端測試：用 httpx.MockTransport 模擬 API，不打真的網路。

契約取自 https://docs.pyannote.ai/openapi.json：上傳是「先要預簽網址、再 PUT」兩步，
工作是「送出拿 jobId、再輪詢 /v1/jobs/{id}」。
"""
import json

import httpx
import pytest

from app.transcription.pyannote_client import PyannoteClient, PyannoteError


class FakeApi:
    """依 (method, path) 回應的假 API，記下每個收到的請求。"""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.routes: dict[tuple[str, str], list] = {}

    def on(self, method, path, *responses):
        self.routes.setdefault((method, path), []).extend(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(404, json={"message": "no route"})
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        return response() if callable(response) else response

    def json_of(self, method, path):
        for request, body in zip(self.requests, self.bodies):
            if request.method == method and request.url.path == path:
                return json.loads(body)
        raise AssertionError(f"沒有收到 {method} {path}")


@pytest.fixture
def api():
    fake = FakeApi()
    fake.on("POST", "/v1/media/input", httpx.Response(
        200, json={"url": "https://storage.example/upload?sig=abc"}
    ))
    fake.on("PUT", "/upload", httpx.Response(200))
    return fake


def make_client(api, **kwargs):
    sleeps: list[float] = []
    client = PyannoteClient(
        api_key="test-key",
        transport=httpx.MockTransport(api),
        sleep=sleeps.append,
        **kwargs,
    )
    client.sleeps = sleeps
    return client


def job(status, output=None):
    body = {"jobId": "job-1", "status": status}
    if output is not None:
        body["output"] = output
    return httpx.Response(200, json=body)


# ---- 上傳 ----

def test_upload_requests_presigned_url_then_puts_file(api, tmp_path):
    audio = tmp_path / "meeting.ogg"
    audio.write_bytes(b"OggS-fake-audio")
    client = make_client(api)

    media_url = client.upload(audio)

    assert media_url.startswith("media://")
    assert api.json_of("POST", "/v1/media/input") == {"url": media_url}
    put_index = [r.method for r in api.requests].index("PUT")
    put = api.requests[put_index]
    assert api.bodies[put_index] == b"OggS-fake-audio"
    assert put.headers["content-type"] == "application/octet-stream"
    assert put.headers["content-length"] == str(len(b"OggS-fake-audio"))


def test_api_calls_carry_bearer_but_presigned_put_does_not(api, tmp_path):
    # 預簽網址自帶簽章；再附 Authorization 會被儲存服務當成第二種認證而拒絕
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"x")
    make_client(api).upload(audio)
    post, put = api.requests
    assert post.headers["authorization"] == "Bearer test-key"
    assert "authorization" not in put.headers


def test_upload_media_urls_are_unique(api, tmp_path):
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"x")
    client = make_client(api)
    assert client.upload(audio) != client.upload(audio)


def test_upload_failure_raises_pyannote_error(tmp_path):
    fake = FakeApi()
    fake.on("POST", "/v1/media/input", httpx.Response(402, json={"message": "Subscription is required"}))
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"x")
    with pytest.raises(PyannoteError, match="402"):
        make_client(fake).upload(audio)


# ---- 送出工作 ----

def test_submit_diarize_body(api):
    api.on("POST", "/v1/diarize", job("created"))
    job_id = make_client(api).submit_diarize("media://m1")
    assert job_id == "job-1"
    assert api.json_of("POST", "/v1/diarize") == {
        "url": "media://m1", "model": "precision-2", "exclusive": True,
    }


def test_submit_diarize_passes_speaker_bounds_only_when_given(api):
    api.on("POST", "/v1/diarize", job("created"))
    make_client(api).submit_diarize("media://m1", min_speakers=2, max_speakers=30)
    body = api.json_of("POST", "/v1/diarize")
    assert body["minSpeakers"] == 2 and body["maxSpeakers"] == 30


def test_submit_voiceprint_body(api):
    api.on("POST", "/v1/voiceprint", job("created"))
    make_client(api).submit_voiceprint("media://v1")
    assert api.json_of("POST", "/v1/voiceprint") == {"url": "media://v1", "model": "precision-2"}


def test_submit_identify_body(api):
    api.on("POST", "/v1/identify", job("created"))
    make_client(api).submit_identify(
        "media://m1", {"王小明": "vp-aaa", "李大華": "vp-bbb"}, threshold=40
    )
    assert api.json_of("POST", "/v1/identify") == {
        "url": "media://m1",
        "model": "precision-2",
        "exclusive": True,
        "voiceprints": [
            {"label": "王小明", "voiceprint": "vp-aaa"},
            {"label": "李大華", "voiceprint": "vp-bbb"},
        ],
        "matching": {"threshold": 40, "exclusive": True},
    }


# ---- 輪詢 ----

def test_wait_polls_until_succeeded_and_returns_output(api):
    output = {"exclusiveDiarization": [{"speaker": "SPEAKER_00", "start": 0, "end": 1}]}
    api.on("GET", "/v1/jobs/job-1", job("created"), job("running"), job("succeeded", output))
    client = make_client(api, poll_seconds=5)
    assert client.wait("job-1") == output
    assert client.sleeps == [5, 5, 5]


@pytest.mark.parametrize("status", ["failed", "canceled"])
def test_wait_raises_when_job_does_not_succeed(api, status):
    api.on("GET", "/v1/jobs/job-1", job(status, {"error": "bad audio"}))
    with pytest.raises(PyannoteError, match=status):
        make_client(api).wait("job-1")


def test_wait_times_out(api):
    api.on("GET", "/v1/jobs/job-1", job("running"))
    ticks = iter(range(0, 10_000, 30))
    client = make_client(api, poll_seconds=30, timeout_seconds=100, clock=lambda: next(ticks))
    with pytest.raises(PyannoteError, match="逾時"):
        client.wait("job-1")


def test_rate_limit_waits_retry_after_then_retries(api):
    api.on("POST", "/v1/diarize",
           httpx.Response(429, headers={"Retry-After": "7"}), job("created"))
    client = make_client(api)
    assert client.submit_diarize("media://m1") == "job-1"
    assert client.sleeps == [7]


def test_rate_limit_retry_after_is_capped(api):
    api.on("POST", "/v1/diarize",
           httpx.Response(429, headers={"Retry-After": "3600"}), job("created"))
    client = make_client(api)
    client.submit_diarize("media://m1")
    assert client.sleeps == [60]


def test_rate_limit_gives_up_after_limited_retries(api):
    api.on("POST", "/v1/diarize", httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(PyannoteError, match="429"):
        make_client(api).submit_diarize("media://m1")


def test_transient_server_error_while_polling_is_retried(api):
    output = {"diarization": []}
    api.on("GET", "/v1/jobs/job-1", httpx.Response(503), job("succeeded", output))
    assert make_client(api).wait("job-1") == output


# ---- 高階操作 ----

def test_diarize_uploads_submits_and_waits(api, tmp_path):
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"x")
    output = {"exclusiveDiarization": []}
    api.on("POST", "/v1/diarize", job("created"))
    api.on("GET", "/v1/jobs/job-1", job("succeeded", output))
    assert make_client(api).diarize(audio) == output


def test_voiceprint_returns_base64_string(api, tmp_path):
    audio = tmp_path / "enroll.ogg"
    audio.write_bytes(b"x")
    api.on("POST", "/v1/voiceprint", job("created"))
    api.on("GET", "/v1/jobs/job-1", job("succeeded", {"voiceprint": "dm9pY2U="}))
    assert make_client(api).voiceprint(audio) == "dm9pY2U="


def test_voiceprint_without_output_raises(api, tmp_path):
    audio = tmp_path / "enroll.ogg"
    audio.write_bytes(b"x")
    api.on("POST", "/v1/voiceprint", job("created"))
    api.on("GET", "/v1/jobs/job-1", job("succeeded", {"error": "too short"}))
    with pytest.raises(PyannoteError):
        make_client(api).voiceprint(audio)


def test_missing_api_key_is_rejected_up_front():
    with pytest.raises(PyannoteError, match="PYANNOTE_API_KEY"):
        PyannoteClient(api_key="")
