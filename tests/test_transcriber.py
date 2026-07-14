"""Transcriber 測試：注入假的 WhisperModel，不下載、不載入真模型。"""
from types import SimpleNamespace

from app.transcription.transcriber import Transcriber, detect_device


class FakeWhisperModel:
    """模仿 faster_whisper.WhisperModel.transcribe 的介面。"""

    def __init__(self, segments):
        self._segments = segments

    def transcribe(self, path, **kwargs):
        info = SimpleNamespace(duration=100.0, language="zh")
        return iter(self._segments), info


def seg(text, end):
    return SimpleNamespace(text=text, end=end)


def test_transcribe_joins_segment_texts():
    model = FakeWhisperModel([seg("大家好，", 40.0), seg("今天討論分工。", 100.0)])
    t = Transcriber(model=model)
    assert t.transcribe("fake.wav") == "大家好，今天討論分工。"


def test_progress_callback_receives_fraction_and_text():
    model = FakeWhisperModel([seg("A", 25.0), seg("B", 50.0), seg("C", 100.0)])
    t = Transcriber(model=model)
    calls = []
    t.transcribe("fake.wav", on_progress=lambda frac, text: calls.append((frac, text)))
    assert [c[0] for c in calls] == [0.25, 0.5, 1.0]
    assert [c[1] for c in calls] == ["A", "B", "C"]


def test_progress_fraction_capped_at_one():
    # 段落結束時間可能略超過 info.duration
    model = FakeWhisperModel([seg("尾段", 105.0)])
    t = Transcriber(model=model)
    calls = []
    t.transcribe("fake.wav", on_progress=lambda frac, text: calls.append(frac))
    assert calls == [1.0]


def test_glossary_terms_injected_into_initial_prompt():
    """自訂詞彙要進 whisper 的 initial_prompt，人名/專有名詞才不會被聽錯。"""
    captured = {}

    class PromptCapturingModel(FakeWhisperModel):
        def transcribe(self, path, **kwargs):
            captured.update(kwargs)
            return super().transcribe(path, **kwargs)

    t = Transcriber(
        model=PromptCapturingModel([seg("x", 1.0)]),
        glossary=lambda: [{"term": "王霖翔", "note": "人名"}],
    )
    t.transcribe("fake.wav")
    assert "王霖翔" in captured["initial_prompt"]


def test_detect_device_returns_valid_value():
    assert detect_device() in ("cuda", "cpu")


def test_injected_model_skips_loading():
    t = Transcriber(model=FakeWhisperModel([seg("x", 1.0)]))
    # 不應嘗試載入真模型（若嘗試會因下載/裝置問題丟例外）
    assert t.transcribe("fake.wav") == "x"


class CudaBrokenModel:
    """模擬 CUDA 函式庫缺失：建構成功，實際轉錄時才丟錯（ctranslate2 的行為）。"""

    def transcribe(self, path, **kwargs):
        raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")


def test_cuda_runtime_error_falls_back_to_cpu():
    t = Transcriber(model=CudaBrokenModel())
    t.device = "cuda"
    t._rebuild_on_cpu = lambda: FakeWhisperModel([seg("退回成功", 1.0)])
    assert t.transcribe("fake.wav") == "退回成功"


def test_non_cuda_runtime_error_is_raised():
    class BrokenModel:
        def transcribe(self, path, **kwargs):
            raise RuntimeError("file not found: fake.wav")

    t = Transcriber(model=BrokenModel())
    t.device = "cuda"
    try:
        t.transcribe("fake.wav")
    except RuntimeError as exc:
        assert "file not found" in str(exc)
    else:
        raise AssertionError("非 CUDA 錯誤不應被吞掉")
