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


# ---- 「這是不是公開部署」 ----

def test_render_env_var_marks_this_as_a_public_deploy(monkeypatch):
    """Render 一定會自己設 RENDER=true，官方文件明講就是給程式判斷用的。

    本機開發不設認證是刻意的方便，公開網址不設認證是災難——兩者唯一的差別
    就是這個旗標，所以它必須讀得準。
    """
    monkeypatch.setenv("RENDER", "true")
    assert get_settings().is_public_deploy is True


def test_no_render_var_means_local_dev(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    assert get_settings().is_public_deploy is False


def test_allow_no_auth_needs_an_explicit_opt_in(monkeypatch):
    """「忘記設定」與「就是要開一個沒門的站」後果差太多，不該共用同一個預設值。"""
    monkeypatch.delenv("ALLOW_NO_AUTH", raising=False)
    assert get_settings().allow_no_auth is False

    monkeypatch.setenv("ALLOW_NO_AUTH", "1")
    assert get_settings().allow_no_auth is True


def test_auth_configured_accepts_either_mechanism():
    """帳號制或共用鑰匙，有一個就算有把關。"""
    assert Settings().auth_configured is False
    assert Settings(api_token="shared-key").auth_configured is True
    assert Settings(
        firebase_web_api_key="web-key",
        firebase_auth_domain="demo.firebaseapp.com",
    ).auth_configured is True


# ---- 長檔轉錄走哪條路 ----

def test_long_files_stay_on_the_high_quota_model_by_default(monkeypatch):
    """長檔預設不再走強模型「整份單次轉錄」。

    那條路用 gemini-3.5-flash：免費層每天每專案只有 20 次，而且實測連打 5 次
    會中 1 次 503——長檔又是「整份音訊一次大呼叫」，在高峰期比小請求更容易被
    Google 端丟棄，撞上就整份失敗，使用者白等十分鐘什麼都沒有。

    門檻設 0＝長檔改走跟短檔一樣的 lite 分段。講者分辨會差一些（分段會破壞
    模型賴以分辨講者的全局脈絡），但寧可品質差一點，也不要報錯。
    """
    monkeypatch.delenv("TRANSCRIBE_LONG_FILE_THRESHOLD_SECONDS", raising=False)
    assert get_settings().transcribe_long_file_threshold_seconds == 0


def test_strong_whole_pass_can_still_be_opted_back_in(monkeypatch):
    """額度充裕或升級付費後，設個秒數就換回強模型整份轉錄。"""
    monkeypatch.setenv("TRANSCRIBE_LONG_FILE_THRESHOLD_SECONDS", "600")
    assert get_settings().transcribe_long_file_threshold_seconds == 600


# ---- 聲紋跨段接力（approach A）：分段轉錄邊轉邊建聲音簿，接力餵給後續分段 ----

def test_voice_relay_defaults_to_twenty_speakers(monkeypatch):
    """預設開、封頂 20 位——這是每段轉錄多附帶的參考音訊數量上限，
    設太高會讓每次呼叫的延遲與流量跟著膨脹。"""
    monkeypatch.delenv("VOICE_RELAY_MAX_SPEAKERS", raising=False)
    assert get_settings().voice_relay_max_speakers == 20


def test_voice_relay_can_be_disabled(monkeypatch):
    monkeypatch.setenv("VOICE_RELAY_MAX_SPEAKERS", "0")
    assert get_settings().voice_relay_max_speakers == 0


def test_voice_relay_env_var_wins(monkeypatch):
    monkeypatch.setenv("VOICE_RELAY_MAX_SPEAKERS", "5")
    assert get_settings().voice_relay_max_speakers == 5
