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
