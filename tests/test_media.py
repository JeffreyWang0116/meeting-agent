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
