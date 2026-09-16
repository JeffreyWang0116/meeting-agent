"""MediaJobManager：音檔/影片背景轉錄工作測試。"""
import pytest

from app.jobs import MediaJobManager


class FakeTranscriber:
    def __init__(self, text="轉錄結果", error=None):
        self.text = text
        self.error = error

    def transcribe(self, path, on_progress=None, user=None):
        if self.error:
            raise self.error
        if on_progress:
            on_progress(0.5, "轉錄")
            on_progress(1.0, "結果")
        return self.text


class FakeOrchestrator:
    def __init__(self, corrected=None):
        self.received = []
        # 模擬校正後的逐字稿（None＝不改動）
        self._corrected = corrected

    def process_transcript(
        self, text, meeting_date=None, kind=None, features=None,
        correct_typos=False, terms=None, user=None,
    ):
        self.received.append(
            (text, meeting_date, kind, features, correct_typos, terms, user)
        )
        return {
            "meeting_id": "m123",
            "analysis": {},
            "notifications": {},
            "transcript": self._corrected if correct_typos and self._corrected else text,
        }


class HintRecordingTranscriber(FakeTranscriber):
    """記下轉錄時收到的 hint，驗證本次專用詞彙有沒有真的送到轉錄那一層。"""

    def __init__(self, text="轉錄結果"):
        super().__init__(text)
        self.hints = []

    def transcribe(self, path, on_progress=None, hint=None, user=None):
        self.hints.append(hint)
        return super().transcribe(path, on_progress)


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
        def transcribe(self, path, on_progress=None, user=None):
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
        def transcribe(self, path, on_progress=None, user=None):
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


def test_kind_passed_through(tmp_path, audio_file):
    orch = FakeOrchestrator()
    mgr = MediaJobManager(FakeTranscriber(), orch, tmp_path)
    job_id = mgr.submit(audio_file, kind="講座")
    mgr.wait(job_id, timeout=5)
    assert orch.received[0][2] == "講座"


def test_features_passed_through(tmp_path, audio_file):
    orch = FakeOrchestrator()
    mgr = MediaJobManager(FakeTranscriber(), orch, tmp_path)
    job_id = mgr.submit(audio_file, features={"summary"})
    mgr.wait(job_id, timeout=5)
    assert orch.received[0][3] == {"summary"}


def test_correct_typos_flag_passed_through(tmp_path, audio_file):
    orch = FakeOrchestrator()
    mgr = MediaJobManager(FakeTranscriber(), orch, tmp_path)
    mgr.wait(mgr.submit(audio_file, correct_typos=True), timeout=5)
    assert orch.received[0][4] is True


def test_job_transcript_replaced_by_corrected_version(tmp_path, audio_file):
    """校正過的話，前端看到的逐字稿要是校正後的版本。"""
    orch = FakeOrchestrator(corrected="校正後的逐字稿")
    mgr = MediaJobManager(FakeTranscriber(), orch, tmp_path)
    job_id = mgr.submit(audio_file, correct_typos=True)
    mgr.wait(job_id, timeout=5)
    assert mgr.get(job_id)["transcript"] == "校正後的逐字稿"


def test_job_transcript_untouched_when_correction_off(tmp_path, audio_file):
    orch = FakeOrchestrator(corrected="不該出現")
    mgr = MediaJobManager(FakeTranscriber(), orch, tmp_path)
    job_id = mgr.submit(audio_file)
    mgr.wait(job_id, timeout=5)
    assert mgr.get(job_id)["transcript"] == "轉錄結果"


# ---- 失敗時要留下足以查明原因的線索 ----

def test_error_without_message_still_names_the_exception_type(tmp_path, audio_file):
    """訊息為空的例外不能讓前端只剩一句「轉錄失敗」。

    MemoryError、OSError、TimeoutError、ConnectionResetError 的 str() 都是空
    字串。原本只記 str(exc)，前端拿到空字串就退回顯示無資訊的後備文字——雲端
    免費層 512MB 撞 OOM 時看到的正是那句，等於什麼都沒說。
    型別名稱是這種情況下唯一的線索，不能跟著訊息一起被丟掉。
    """
    mgr = MediaJobManager(FakeTranscriber(error=MemoryError()), FakeOrchestrator(), tmp_path)
    job_id = mgr.submit(audio_file)
    mgr.wait(job_id, timeout=5)

    assert mgr.get(job_id)["error"] == "MemoryError"


def test_message_is_kept_when_the_exception_has_one(tmp_path, audio_file):
    """有訊息的例外照舊只顯示訊息：使用者看到的是「請換一個檔案再試」這種
    可行動的句子，不該被加上一層型別名稱變成雜訊。"""
    mgr = MediaJobManager(
        FakeTranscriber(error=RuntimeError("模型爆炸")), FakeOrchestrator(), tmp_path
    )
    job_id = mgr.submit(audio_file)
    mgr.wait(job_id, timeout=5)

    assert mgr.get(job_id)["error"] == "模型爆炸"


def test_job_failure_writes_a_traceback_to_the_log(tmp_path, audio_file, caplog):
    """背景執行緒的例外必須真的進 log。

    原本那行的註解就寫著「例外必須被記錄」，但實作只更新了 job 狀態，整個
    模組沒有任何 logging——雲端 Logs 分頁因此一片空白，遠端除錯無從下手。
    """
    mgr = MediaJobManager(FakeTranscriber(error=MemoryError()), FakeOrchestrator(), tmp_path)
    with caplog.at_level("ERROR", logger="app.jobs"):
        job_id = mgr.submit(audio_file)
        mgr.wait(job_id, timeout=5)

    record = next(r for r in caplog.records if r.name == "app.jobs")
    assert record.exc_info is not None, "要留 traceback，只記一行訊息查不出是哪裡爆的"
    assert job_id in record.getMessage()


# ---- 本次專用詞彙要進轉錄，不能只進分析 ----

def test_meeting_terms_reach_the_transcriber(tmp_path, audio_file):
    """上傳檔案時打的專用詞彙，要在轉錄當下就生效。

    terms 本來就一路傳到 submit 了，但只餵給分析——逐字稿裡的字仍然是聽錯的。
    """
    tr = HintRecordingTranscriber()
    mgr = MediaJobManager(tr, FakeOrchestrator(), tmp_path)
    mgr.wait(mgr.submit(audio_file, terms=[{"term": "Kessel 專案", "note": ""}]), timeout=5)
    assert tr.hints and "Kessel 專案" in tr.hints[0]


def test_no_terms_means_no_hint_at_all(tmp_path, audio_file):
    """沒打詞彙就別多送一個空提示——維持與加這個功能之前完全一樣的呼叫。"""
    tr = HintRecordingTranscriber()
    mgr = MediaJobManager(tr, FakeOrchestrator(), tmp_path)
    mgr.wait(mgr.submit(audio_file), timeout=5)
    assert tr.hints == [None]


# ---- pyannote 講者分離（混合式講者辨識）----

class PendingFuture:
    """還沒完成的分群工作：用來確認轉錄完後會切到「辨識講者中」。"""

    def done(self):
        return False


class FakeDiarizer:
    def __init__(self, relabelled="[0:00] 講者B：重標後的逐字稿", error=None):
        self.started = []
        self.applied = []
        self.relabelled = relabelled
        self.error = error
        self.status_seen = None
        self.mgr = None
        self.job_id = None

    def start(self, path):
        self.started.append(path)
        return PendingFuture()

    def apply(self, future, transcript):
        if self.mgr and self.job_id:
            self.status_seen = self.mgr.get(self.job_id)["status"]
        self.applied.append(transcript)
        if self.error:
            raise self.error
        return self.relabelled


def test_diarizer_relabels_transcript_before_analysis(tmp_path):
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"x")
    orch = FakeOrchestrator()
    diarizer = FakeDiarizer()
    mgr = MediaJobManager(FakeTranscriber("[0:00] 講者A：原本"), orch, tmp_path, diarizer=diarizer)
    job_id = mgr.submit(audio)
    mgr.wait(job_id, timeout=5)

    assert diarizer.started == [audio]
    assert diarizer.applied == ["[0:00] 講者A：原本"]
    assert orch.received[0][0] == "[0:00] 講者B：重標後的逐字稿"
    job = mgr.get(job_id)
    assert job["status"] == "done"
    assert job["transcript"] == "[0:00] 講者B：重標後的逐字稿"


def test_job_shows_diarizing_status_while_waiting_for_speakers(tmp_path):
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"x")
    diarizer = FakeDiarizer()
    mgr = MediaJobManager(FakeTranscriber("[0:00] 講者A：原本"), FakeOrchestrator(), tmp_path, diarizer=diarizer)
    diarizer.mgr = mgr
    # submit 之後才知道 job_id；apply 在背景執行緒裡才讀，來得及塞進去
    original_start = diarizer.start

    def start(path):
        diarizer.job_id = next(iter(mgr._jobs))
        return original_start(path)

    diarizer.start = start
    job_id = mgr.submit(audio)
    mgr.wait(job_id, timeout=5)
    assert diarizer.status_seen == "diarizing"


def test_unexpected_diarizer_crash_keeps_gemini_transcript(tmp_path):
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"x")
    orch = FakeOrchestrator()
    mgr = MediaJobManager(
        FakeTranscriber("[0:00] 講者A：原本"), orch, tmp_path,
        diarizer=FakeDiarizer(error=RuntimeError("bug")),
    )
    job_id = mgr.submit(audio)
    mgr.wait(job_id, timeout=5)
    assert mgr.get(job_id)["status"] == "done"
    assert orch.received[0][0] == "[0:00] 講者A：原本"


def test_diarizer_does_not_relabel_when_transcription_is_empty(tmp_path):
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"x")
    diarizer = FakeDiarizer()
    mgr = MediaJobManager(FakeTranscriber(""), FakeOrchestrator(), tmp_path, diarizer=diarizer)
    job_id = mgr.submit(audio)
    mgr.wait(job_id, timeout=5)
    assert mgr.get(job_id)["status"] == "error"
    assert diarizer.applied == []  # 沒有逐字稿可重標


def test_overload_error_is_translated_to_actionable_message(tmp_path):
    """Google 端過載時，使用者看到的不該是 SDK 吐的原始 JSON——那既看不懂，
    也不知道該重試還是該換檔案。503 是等一下就會好的暫時狀況，要講明。"""
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"x")
    overload = RuntimeError(
        "503 UNAVAILABLE. {'error': {'code': 503, 'message': "
        "'This model is currently experiencing high demand.'}}"
    )
    mgr = MediaJobManager(FakeTranscriber(error=overload), FakeOrchestrator(), tmp_path)
    job_id = mgr.submit(audio)
    mgr.wait(job_id, timeout=5)
    message = mgr.get(job_id)["error"]
    assert "UNAVAILABLE" not in message and "{" not in message
    assert "再試" in message  # 要講清楚這是可以重試的暫時狀況


def test_other_errors_keep_their_original_message(tmp_path):
    """只有暫時性過載該被改寫。其他錯誤的原始訊息是唯一的線索，不能吃掉。"""
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"x")
    mgr = MediaJobManager(
        FakeTranscriber(error=ValueError("音檔格式不支援")), FakeOrchestrator(), tmp_path
    )
    job_id = mgr.submit(audio)
    mgr.wait(job_id, timeout=5)
    assert mgr.get(job_id)["error"] == "音檔格式不支援"
