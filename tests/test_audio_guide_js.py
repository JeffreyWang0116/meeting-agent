"""收音防護（app/static/js/audioguide.js）的行為測試。

使用者用 Discord 開會時錄不到對方聲音：最常見是 Discord 輸出到別的裝置，而 Chrome
分享整個螢幕只錄 Windows 預設播放裝置——分享成功、音軌也在，錄到的卻是空白。
這支模組放「依平台給說明」「偵測新插上的裝置」「系統音源一直沒聲音」這些純邏輯，
不碰 DOM，直接用 node 載入測試；沒有 node 的環境跳過。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "audioguide.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 執行前端模組")

WIN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141.0 Safari/537.36"
MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/141.0 Safari/537.36"
IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
ANDROID_UA = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/141.0 Mobile Safari/537.36"
LINUX_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/141.0 Safari/537.36"


def js(body: str):
    script = f"""
import * as g from {json.dumps(MODULE.as_uri())};
process.stdout.write(JSON.stringify(await (async () => {{ {body} }})()));
"""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def call(fn: str, *args):
    return js(f"return g.{fn}(...{json.dumps(list(args), ensure_ascii=False)});")


# ---- 平台判斷 ----

@pytest.mark.parametrize("ua, expected", [
    (WIN_UA, "windows"), (MAC_UA, "mac"), (IPHONE_UA, "ios"),
    (ANDROID_UA, "android"), (LINUX_UA, "other"), ("", "other"),
])
def test_detect_platform(ua, expected):
    assert call("detectPlatform", ua) == expected


# ---- 分享畫面時的說明 ----

def test_windows_guide_names_the_discord_output_device_fix():
    guide = call("systemAudioGuide", "windows")
    assert "整個螢幕" in guide and "系統音訊" in guide
    assert "Discord" in guide and "輸出裝置" in guide and "預設" in guide


def test_mac_guide_points_to_browser_tab_or_blackhole():
    guide = call("systemAudioGuide", "mac")
    assert "分頁" in guide and "BlackHole" in guide


@pytest.mark.parametrize("platform", ["ios", "android"])
def test_phones_are_told_it_does_not_work_there(platform):
    assert "桌機" in call("systemAudioGuide", platform)


def test_loopback_hint_per_platform():
    assert "立體聲混音" in call("loopbackSetupHint", "windows")
    assert "VB-CABLE" in call("loopbackSetupHint", "windows")
    assert "BlackHole" in call("loopbackSetupHint", "mac")
    assert call("loopbackSetupHint", "ios") == ""


def test_silence_warning_leads_with_the_most_likely_cause():
    win = call("silenceWarning", "windows")
    assert "Discord" in win and "輸出裝置" in win
    assert "分頁" in call("silenceWarning", "mac")


# ---- 裝置插拔 ----

def dev(device_id, label):
    return {"deviceId": device_id, "label": label}


def test_added_devices_lists_only_new_real_devices():
    before = [dev("default", "預設 - 內建麥克風"), dev("a1", "內建麥克風")]
    after = before + [dev("u2", "USB Microphone"), dev("communications", "通訊 - USB Microphone")]
    assert call("addedDevices", before, after) == ["USB Microphone"]


def test_added_devices_ignores_unlabeled_lists():
    """還沒授權麥克風時列舉不到名稱、id 也是空的，不能每次都當成新裝置。"""
    assert call("addedDevices", [], [dev("", ""), dev("", "")]) == []


def test_removed_device_is_not_reported_as_added():
    assert call("addedDevices", [dev("a1", "內建"), dev("u2", "USB")], [dev("a1", "內建")]) == []


@pytest.mark.parametrize("name, device, expected", [
    ("OverconstrainedError", "u2", True),
    ("NotFoundError", "u2", True),
    ("NotFoundError", "", False),       # 本來就用系統預設，退無可退
    ("NotAllowedError", "u2", False),   # 權限問題換裝置也沒用
])
def test_fallback_to_default_mic_only_when_the_chosen_device_is_gone(name, device, expected):
    assert call("shouldFallbackToDefaultMic", name, device) is expected


# ---- 系統音源一直沒聲音 ----

def test_silence_watch_fires_once_after_continuous_quiet():
    out = js("""
const w = g.createSilenceWatch({ seconds: 20 });
const hits = [];
for (let t = 0; t <= 40; t += 0.5) hits.push(w.push(0.0001, t));
return hits.filter(Boolean).length + ":" + hits.indexOf(true) * 0.5;
""")
    assert out == "1:20"


def test_silence_watch_resets_when_sound_arrives():
    out = js("""
const w = g.createSilenceWatch({ seconds: 20 });
let fired = false;
for (let t = 0; t <= 60; t += 0.5) {
  const rms = Math.floor(t) % 15 === 0 ? 0.2 : 0.0001;  // 每 15 秒有人講一下話
  fired = w.push(rms, t) || fired;
}
return fired;
""")
    assert out is False


def test_unlabeled_baseline_is_not_compared():
    """授權前列到的清單沒有名稱也沒有 id；授權後第一次插拔，不能把每支麥克風都報成新裝置。"""
    before = [dev("", ""), dev("", "")]
    after = [dev("a1", "內建麥克風"), dev("u2", "USB Microphone")]
    assert call("addedDevices", before, after) == []
