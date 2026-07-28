"""環境設定的預設值。

這裡只釘「沒設環境變數時拿到什麼」——預設值直接決定新部署會吃掉多少免費額度，
而 README 的額度表是照這些預設值寫的，兩邊漂開就會讓人照文件部署卻撞牆。
"""
import pytest

from app.config import Settings, get_settings

# 免費層每日額度差 25 倍（Lite 500、Flash 20），預設值選錯的代價不是慢一點而是直接不能用
LITE = "gemini-flash-lite-latest"


@pytest.fixture
def clean_env(monkeypatch):
    """讓 get_settings 完全看不到環境：既不讀開發機的 .env，也清掉已存在的變數。

    get_settings 每次都會 load_dotenv，只 delenv 的話開發機的 .env 會立刻把值
    填回去——測試會因為 .env 剛好設對而假性通過，等到沒有 .env 的 CI 才爆開。
    """
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    for name in ("GEMINI_MODEL", "TRANSCRIBE_MODEL", "CORRECT_MODEL", "TRANSCRIBE_ENGINE"):
        monkeypatch.delenv(name, raising=False)


def test_model_defaults_are_the_high_quota_lite_model(clean_env):
    settings = get_settings()
    assert settings.gemini_model == LITE
    assert settings.transcribe_model == LITE
    assert settings.correct_model == LITE


def test_dataclass_defaults_match_env_defaults():
    """直接建 Settings()（測試常這樣做）不該拿到與實際部署不同的模型。"""
    assert Settings().gemini_model == LITE
    assert Settings().transcribe_model == LITE
    assert Settings().correct_model == LITE


def test_env_var_still_wins(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    assert get_settings().gemini_model == "gemini-3.5-flash"
