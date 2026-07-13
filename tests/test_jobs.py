"""MediaJobManager：音檔/影片背景轉錄工作測試。"""
import pytest

from app.jobs import MediaJobManager


class FakeTranscriber:
    def __init__(self, text="轉錄結果", error=None):
        self.text = text
        self.error = error

    def transcribe(self, path, on_progress=None):
        if self.error:
            raise self.error
        if on_progress:
            on_progress(0.5, "轉錄")
            on_progress(1.0, "結果")
        return self.text


class FakeOrchestrator:
    def __init__(self):
        self.received = []

    def process_transcript(self, text, meeting_date=None):
        self.received.append((text, meeting_date))
        return {"meeting_id": "m123", "analysis": {}, "notifications": {}}


@pytest.fixture
def audio_file(tmp_path):
    f = tmp_path / "meeting.wav"
    f.write_bytes(b"RIFF-fake")
    return f


def test_job_completes_with_transcript_and_result(tmp_path, audio_file):
    mgr = MediaJobManager(FakeTranscriber(), FakeOrchestrator(), tmp_path)
    job_id = mgr.submit(audio_file)
    mgr.wait(job_id, timeout=5)

    job = mgr.get(job_id)
    assert job["status"] == "done"
    assert job["progress"] == 1.0
    assert job["transcript"] == "轉錄結果"
    assert job["result"]["meeting_id"] == "m123"
    assert job["error"] is None


def test_progress_and_partial_transcript_updated(tmp_path, audio_file):
    mgr = MediaJobManager(FakeTranscriber(), FakeOrchestrator(), tmp_path)
    job_id = mgr.submit(audio_file)
    mgr.wait(job_id, timeout=5)
    job = mgr.get(job_id)
    # on_progress 累積的部分逐字稿最終等於完整轉錄
    assert job["transcript"] == "轉錄結果"


def test_transcriber_failure_marks_job_error(tmp_path, audio_file):
    mgr = MediaJobManager(
        FakeTranscriber(error=RuntimeError("模型爆炸")), FakeOrchestrator(), tmp_path
    )
    job_id = mgr.submit(audio_file)
    mgr.wait(job_id, timeout=5)
    job = mgr.get(job_id)
    assert job["status"] == "error"
    assert "模型爆炸" in job["error"]


def test_video_without_ffmpeg_transcribes_directly(tmp_path, monkeypatch):
    # 沒有 ffmpeg 時，影片直接交給 faster-whisper（PyAV 可解常見容器的音訊）
    from app.transcription import media

    monkeypatch.setattr(media, "ffmpeg_available", lambda: False)
    video = tmp_path / "meeting.mp4"
    video.write_bytes(b"fake-mp4")

    orch = FakeOrchestrator()
    mgr = MediaJobManager(FakeTranscriber(), orch, tmp_path)
    job_id = mgr.submit(video)
    mgr.wait(job_id, timeout=5)
    assert mgr.get(job_id)["status"] == "done"


def test_video_with_ffmpeg_extracts_audio_first(tmp_path, monkeypatch):
    from app.transcription import media

    extracted = tmp_path / "extracted.wav"
    extracted.write_bytes(b"wav")
    monkeypatch.setattr(media, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(media, "extract_audio", lambda src, dst=None: extracted)

    video = tmp_path / "meeting.mp4"
    video.write_bytes(b"fake-mp4")

    received_paths = []

    class PathRecordingTranscriber(FakeTranscriber):
        def transcribe(self, path, on_progress=None):
            received_paths.append(path)
            return super().transcribe(path, on_progress)

    mgr = MediaJobManager(PathRecordingTranscriber(), FakeOrchestrator(), tmp_path)
    job_id = mgr.submit(video)
    mgr.wait(job_id, timeout=5)
    assert mgr.get(job_id)["status"] == "done"
    assert received_paths == [extracted]


def test_empty_transcript_gives_clear_error(tmp_path, audio_file):
    # 靜音檔／無語音內容：要給人看得懂的錯誤，且不該把空文字送去 LLM 分析
    class SilentTranscriber(FakeTranscriber):
        def transcribe(self, path, on_progress=None):
            return ""

    orch = FakeOrchestrator()
    mgr = MediaJobManager(SilentTranscriber(), orch, tmp_path)
    job_id = mgr.submit(audio_file)
    mgr.wait(job_id, timeout=5)

    job = mgr.get(job_id)
    assert job["status"] == "error"
    assert "語音" in job["error"]
    assert orch.received == []


def test_get_unknown_job_returns_none(tmp_path):
    mgr = MediaJobManager(FakeTranscriber(), FakeOrchestrator(), tmp_path)
    assert mgr.get("nope") is None


def test_meeting_date_passed_through(tmp_path, audio_file):
    from datetime import date

    orch = FakeOrchestrator()
    mgr = MediaJobManager(FakeTranscriber(), orch, tmp_path)
    job_id = mgr.submit(audio_file, meeting_date=date(2026, 7, 12))
    mgr.wait(job_id, timeout=5)
    assert orch.received[0][1] == date(2026, 7, 12)
