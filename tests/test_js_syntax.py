"""每一支前端模組都要能被 JS 剖析器讀完。

前端是 ES module，沒有建置步驟，所以任何語法錯誤都要等瀏覽器載入才會炸——而且
不是炸一個功能，是整個 app 停擺（main.js 匯入失敗，後面所有模組的事件都沒綁上）。
偏偏後端測試這時全綠：test_frontend_modules.py 只做正則靜態比對，
test_speaker_labels_js.py 只實際載入 speakers.js 一支。

實例：inputs.js 的上傳救援路徑改了上百行、加了巢狀 try/catch，開發機沒有 node，
整個 CI 也沒有任何一步會去剖析它——改完到部署上線為止，沒有任何自動化能證明
那份檔案載得起來。

node --check 只剖析不執行，所以碰 document/window 的模組也檢查得動。沒有 node
的環境跳過（GitHub 的 ubuntu runner 內建 node），與 test_speaker_labels_js.py 相同。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 剖析前端模組")


def check(source: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """用 node --check 剖析一段 ES module 原始碼。

    副檔名用 .mjs：node 對 .js 預設走 CommonJS 剖析，import/export 會被判成語法
    錯誤，那樣整個檢查會全部假性失敗。
    """
    target = tmp_path / "probe.mjs"
    target.write_text(source, encoding="utf-8")
    return subprocess.run(
        [NODE, "--check", str(target)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def test_the_checker_itself_distinguishes_valid_from_broken(tmp_path):
    """先證明這個檢查真的有在剖析，否則下面那條測試通過也不代表任何事。

    兩個方向都要驗：合法的 ESM 要過（沒過＝node 把它當 CommonJS 剖析，
    整組檢查失去意義），壞掉的要不過（沒攔下＝根本沒在檢查）。
    """
    valid = 'import { a } from "./x.js";\nconst f = async () => { try { await a(); } catch (e) { } };\nexport { f };\n'
    assert check(valid, tmp_path).returncode == 0, "合法的 ES module 竟然沒通過"

    broken = 'function f() {\n  if (true) {\n}\nexport { f };\n'
    assert check(broken, tmp_path).returncode != 0, "語法錯誤竟然沒被攔下"


@pytest.mark.parametrize(
    "js", sorted(p.relative_to(STATIC).as_posix() for p in STATIC.rglob("*.js")),
)
def test_every_frontend_module_parses(js, tmp_path):
    result = check((STATIC / js).read_text(encoding="utf-8"), tmp_path)
    assert result.returncode == 0, f"{js} 有語法錯誤：\n{result.stderr}"
