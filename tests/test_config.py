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


# ---- .env 裡的相對路徑 ----

def test_relative_credential_path_resolves_against_the_project_root(monkeypatch):
    """.env 就放在專案根目錄，使用者寫 ./firebase-service-account.json 時，
    指的顯然是那裡。

    但相對路徑實際上是對「啟動時的工作目錄」解析的——從上層目錄、IDE 或
    預覽工具啟動時就會找不到檔案，而且錯誤看起來像是金鑰沒下載。
    """
    from pathlib import Path

    from app.config import BASE_DIR

    monkeypatch.setenv("FIREBASE_CREDENTIALS_FILE", "./firebase-service-account.json")
    resolved = Path(get_settings().firebase_credentials_file)

    assert resolved.is_absolute()
    assert resolved == BASE_DIR / "firebase-service-account.json"


def test_absolute_credential_path_left_alone(monkeypatch, tmp_path):
    key = tmp_path / "elsewhere.json"
    monkeypatch.setenv("FIREBASE_CREDENTIALS_FILE", str(key))

    from pathlib import Path

    assert Path(get_settings().firebase_credentials_file) == key


def test_relative_data_dir_also_resolves_against_the_project_root(monkeypatch):
    """DATA_DIR 有同樣的陷阱：相對路徑會讓資料落在啟動目錄底下。"""
    monkeypatch.setenv("DATA_DIR", "./data")
    assert get_settings().data_dir.is_absolute()
