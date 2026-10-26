"""前端推播模組的純函式（app/static/js/push.js）。

iPhone 的使用者如果沒先把網站加到主畫面，開啟通知一定會失敗——而瀏覽器
給的錯誤是「不支援」，看起來像這個 App 做不到。那句話要說對，不然使用者
只會放棄。這裡測的就是「什麼情況該說哪一句」與金鑰的格式轉換。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "push.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 執行前端模組")


def call(expr: str) -> object:
    script = f"""
import {{ blockedReason, urlBase64ToUint8Array }} from {json.dumps(MODULE.as_uri())};
process.stdout.write(JSON.stringify({expr}));
"""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def reason(**env) -> object:
    base = {"supported": True, "ios": False, "standalone": False, "serverEnabled": True}
    return call(f"blockedReason({json.dumps({**base, **env})})")


def test_nothing_blocks_a_normal_browser():
    assert reason() is None


def test_an_iphone_that_is_not_on_the_home_screen_gets_told_what_to_do():
    """iOS 只有「加到主畫面」的 PWA 收得到推播。瀏覽器只會說「不支援」，
    那句話會讓使用者以為是這個 App 做不到。"""
    msg = reason(supported=False, ios=True, standalone=False)

    assert "主畫面" in msg


def test_an_iphone_already_on_the_home_screen_is_not_told_to_add_it_again():
    assert "主畫面" not in (reason(supported=False, ios=True, standalone=True) or "")


def test_other_unsupported_browsers_just_say_so():
    msg = reason(supported=False)

    assert msg and "主畫面" not in msg


def test_a_site_without_keys_says_it_is_the_site_not_the_phone():
    msg = reason(serverEnabled=False)

    assert msg and "手機" not in msg


# ---- 公鑰格式 ----

def test_the_public_key_becomes_the_bytes_the_browser_wants():
    """pushManager.subscribe 只吃 Uint8Array，吃不下伺服器給的 base64url 字串。"""
    # "BA" 的 base64url -> 0x04 0x00
    assert call('Array.from(urlBase64ToUint8Array("BAA"))') == [4, 0]


def test_padding_is_added_back():
    """base64url 去掉了結尾的 =，atob 不補回去會直接丟 InvalidCharacterError。"""
    assert call('Array.from(urlBase64ToUint8Array("-_8")).length') == 2


def test_the_url_safe_alphabet_is_translated():
    assert call('Array.from(urlBase64ToUint8Array("-_8"))') == [251, 255]
