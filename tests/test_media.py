"""媒體處理模組測試：ffmpeg 呼叫全部以 mock 取代。"""
import subprocess
from pathlib import Path

import pytest

from app.transcription import media


def test_is_video_by_extension():
    assert media.is_video("meeting.mp4")
    assert media.is_video(Path("C:/x/會議錄影.MOV"))
    assert not media.is_video("recording.wav")
    assert not media.is_video("notes.txt")


def test_ffmpeg_available_uses_which(monkeypatch, tmp_path):
    monkeypatch.setattr(media.shutil, "which", lambda name: "C:/ffmpeg/ffmpeg.exe")
    assert media.ffmpeg_available()
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    monkeypatch.setattr(media, "_WINGET_FFMPEG", tmp_path / "nonexistent.exe")
    assert not media.ffmpeg_available()


def test_ffmpeg_available_falls_back_to_winget_link(monkeypatch, tmp_path):
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    link = tmp_path / "ffmpeg.exe"
    link.write_bytes(b"fake-exe")
    monkeypatch.setattr(media, "_WINGET_FFMPEG", link)
    assert media.ffmpeg_available()


def test_extract_audio_builds_correct_command(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    video = tmp_path / "meeting.mp4"
    out = media.extract_audio(video)

    cmd = captured["cmd"]
    assert Path(cmd[0]).stem == "ffmpeg"
    assert str(video) in cmd
    # 去影像、單聲道、16kHz —— whisper 的標準輸入格式
    assert "-vn" in cmd
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-ar") + 1] == "16000"
    assert out.suffix == ".wav"
    assert str(out) in cmd


def test_extract_audio_failure_raises_media_error(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Invalid data found")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    with pytest.raises(media.MediaError, match="Invalid data found"):
        media.extract_audio(tmp_path / "broken.mp4")


def test_extract_audio_custom_output_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    out = media.extract_audio(tmp_path / "a.mkv", tmp_path / "custom.wav")
    assert out == tmp_path / "custom.wav"


# ---- cut_clip：剪出一小段音檔，供聲紋接力當講者的聲音樣本 ----


def test_cut_clip_builds_correct_command(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    src = tmp_path / "chunk_000.wav"
    out = media.cut_clip(src, 20.0, 28.0)

    cmd = captured["cmd"]
    assert Path(cmd[0]).stem == "ffmpeg"
    assert cmd[cmd.index("-ss") + 1] == "20.000"
    assert cmd[cmd.index("-t") + 1] == "8.000"  # 長度 = end - start
    assert str(src) in cmd
    assert "-vn" in cmd
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-ar") + 1] == "16000"
    assert out.suffix == ".wav"
    assert str(out) in cmd


def test_cut_clip_failure_raises_media_error(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="seek out of range")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    with pytest.raises(media.MediaError, match="seek out of range"):
        media.cut_clip(tmp_path / "chunk_000.wav", 0.0, 5.0)


def test_cut_clip_custom_output_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    out = media.cut_clip(tmp_path / "chunk_000.wav", 1.0, 3.0, tmp_path / "sample.wav")
    assert out == tmp_path / "sample.wav"


def test_cut_clip_default_output_names_include_span_and_are_distinct(monkeypatch, tmp_path):
    """同一個 chunk 檔剪不同代號的樣本時，預設檔名不能互相覆蓋。"""
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    src = tmp_path / "chunk_000.wav"
    a = media.cut_clip(src, 20.0, 28.0)
    b = media.cut_clip(src, 40.0, 48.0)
    assert a != b


# ---- 講者分離上傳用的壓縮音檔 ----

def test_encode_for_diarization_builds_opus_command(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    src = tmp_path / "meeting.wav"
    out = media.encode_for_diarization(src)

    assert out == tmp_path / "meeting_diarize.ogg"
    cmd = captured["cmd"]
    assert cmd[cmd.index("-i") + 1] == str(src)
    # PoC 實測：Opus 32kbps 是 16k FLAC 的 1/6，分群結果逐 0.1 秒 99.6% 一致
    assert cmd[cmd.index("-c:a") + 1] == "libopus"
    assert cmd[cmd.index("-b:a") + 1] == "32k"
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert "-vn" in cmd
    assert cmd[-1] == str(out)


def test_encode_for_diarization_custom_output_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    dest = tmp_path / "x" / "custom.ogg"
    assert media.encode_for_diarization(tmp_path / "a.mp4", dest) == dest


def test_encode_for_diarization_failure_raises_media_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Unknown encoder 'libopus'"),
    )
    with pytest.raises(media.MediaError, match="libopus"):
        media.encode_for_diarization(tmp_path / "a.wav")


def test_encode_for_diarization_can_cap_length(monkeypatch, tmp_path):
    # voiceprint 樣本上限 30 秒；「用音檔」上傳的樣本可能是一整段錄音
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    media.encode_for_diarization(tmp_path / "enroll.webm", max_seconds=30)
    cmd = captured["cmd"]
    assert cmd[cmd.index("-t") + 1] == "30"
    assert cmd.index("-t") < cmd.index("-i")  # 輸入端截斷：不必解碼整份檔案


def test_encode_for_diarization_does_not_cap_by_default(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        media.subprocess, "run",
        lambda cmd, **kw: captured.setdefault("cmd", cmd) and subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    media.encode_for_diarization(tmp_path / "meeting.wav")
    assert "-t" not in captured["cmd"]


# ---- 即時聆聽：各段錄音串成一份給講者分離 ----

def test_concat_for_diarization_skips_overlap_and_reports_piece_durations(monkeypatch, tmp_path):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    # MediaRecorder 的 webm 常沒有長度資訊，所以量的是轉出來的 wav
    lengths = {"piece_000.wav": 45.0, "piece_001.wav": 42.0}
    monkeypatch.setattr(media, "audio_duration", lambda p: lengths.get(Path(p).name))

    a, b = tmp_path / "chunk_000.webm", tmp_path / "chunk_001.webm"
    dest = tmp_path / "session.ogg"
    durations = media.concat_for_diarization([(a, 0.0), (b, 3.0)], dest)

    assert durations == [45.0, 42.0]
    first, second, final = commands
    assert first[first.index("-i") + 1] == str(a)
    assert "-ss" not in first  # 第一段沒有重疊
    assert second[second.index("-ss") + 1] == "3.000"  # 重疊的 3 秒前一段已經有了
    assert final[final.index("-f") + 1] == "concat"
    assert final[final.index("-c:a") + 1] == "libopus"
    assert final[-1] == str(dest)


def test_concat_for_diarization_cleans_up_intermediate_wavs(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"x")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    monkeypatch.setattr(media, "audio_duration", lambda p: 10.0)
    dest = tmp_path / "out" / "session.ogg"
    media.concat_for_diarization([(tmp_path / "c0.webm", 0.0), (tmp_path / "c1.webm", 3.0)], dest)
    assert sorted(p.name for p in dest.parent.iterdir()) == ["session.ogg"]


def test_concat_for_diarization_needs_every_piece_duration(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(media, "audio_duration", lambda p: None)
    with pytest.raises(media.MediaError):
        media.concat_for_diarization([(tmp_path / "c0.webm", 0.0)], tmp_path / "s.ogg")
