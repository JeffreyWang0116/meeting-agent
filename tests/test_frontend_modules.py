"""前端 ES 模組圖的靜態檢查。

app.js 拆成 app/static/js/*.js 之後，最容易壞的不是邏輯而是「接線」：
import 了一個對方沒 export 的名字、或改名時漏改某一支。這種錯只會在瀏覽器
載入時炸出 SyntaxError，而後端測試全綠——等於沒有任何一關擋得住它。

這裡不執行 JS（模組頂層就會碰 document/window），只做靜態比對，
所以不需要 node，CI 直接跑得動。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "js"
IMPORT_RE = re.compile(r'import \{ (.+?) \} from "\./([\w.]+)";')
EXPORT_RE = re.compile(r"^export \{ (.+?) \};", re.M)
BARE_IMPORT_RE = re.compile(r'import "\./([\w.]+)";')


def _names(raw: str) -> set[str]:
    return {n.strip() for n in raw.split(",") if n.strip()}


@pytest.fixture(scope="module")
def modules() -> dict[str, str]:
    files = {p.name: p.read_text(encoding="utf-8") for p in sorted(JS_DIR.glob("*.js"))}
    assert files, f"找不到任何前端模組：{JS_DIR}"
    return files


@pytest.fixture(scope="module")
def exports(modules) -> dict[str, set[str]]:
    out = {}
    for name, src in modules.items():
        m = EXPORT_RE.search(src)
        out[name] = _names(m.group(1)) if m else set()
    return out


def test_every_import_resolves_to_a_real_export(modules, exports):
    missing = []
    for name, src in modules.items():
        for m in IMPORT_RE.finditer(src):
            target = m.group(2)
            if target not in modules:
                missing.append(f"{name} 匯入了不存在的模組 {target}")
                continue
            for want in sorted(_names(m.group(1)) - exports[target]):
                missing.append(f"{name} 從 {target} 匯入了沒有 export 的 {want}")
    assert not missing, "\n".join(missing)


def test_no_unused_imports(modules):
    """未使用的 import 會製造沒必要的模組相依，循環相依就是這樣長出來的。"""
    unused = []
    for name, src in modules.items():
        body = IMPORT_RE.sub("", src)
        for m in IMPORT_RE.finditer(src):
            for want in sorted(_names(m.group(1))):
                if not re.search(rf"(?<![\w$]){re.escape(want)}(?![\w$])", body):
                    unused.append(f"{name} 匯入了 {want}（來自 {m.group(2)}）卻沒用到")
    assert not unused, "\n".join(unused)


def test_main_reaches_every_module(modules):
    """漏接一支模組，它註冊的事件就永遠不會綁上，而且完全沒有錯誤訊息。"""
    main = modules["main.js"]
    imported = set(BARE_IMPORT_RE.findall(main)) | {m.group(2) for m in IMPORT_RE.finditer(main)}
    assert set(modules) - {"main.js"} == imported


def test_index_html_loads_the_module_entry_point():
    html = (JS_DIR.parent / "index.html").read_text(encoding="utf-8")
    assert '<script type="module" src="/static/js/main.js"></script>' in html
    # 舊的單檔進入點不該還被引用
    assert "/static/app.js" not in html


# ---- $("someId") 引用的元素必須真的存在 ----

STATIC_LOOKUP_RE = re.compile(r'\$\("([A-Za-z][\w-]*)"\)')
# id 可能來自 index.html，也可能由 JS 自己以 innerHTML 產生（如 liveCaret）
HTML_ID_RE = re.compile(r'\bid="([A-Za-z][\w-]*)"')


def test_every_static_dom_lookup_has_a_matching_id(modules):
    """$("liveEnrollOn") 這種寫死的查找，對應的元素一定要存在於某處。

    打錯一個字、或改了 index.html 卻漏改 JS，瀏覽器只會在執行到那一行時丟
    「null 沒有 addEventListener」——而且往往是使用者點下去才炸，後端測試全綠。
    模組載入時就會執行的那些綁定更糟：整個 app.js 直接停擺。
    """
    html = (JS_DIR.parent / "index.html").read_text(encoding="utf-8")
    known = set(HTML_ID_RE.findall(html))
    for src in modules.values():  # JS 動態產生的元素也算數
        known |= set(HTML_ID_RE.findall(src))

    missing = sorted(
        f"{name} 找不到元素 #{el}"
        for name, src in modules.items()
        for el in STATIC_LOOKUP_RE.findall(src)
        if el not in known
    )
    assert not missing, "JS 引用了不存在的 DOM id：\n" + "\n".join(missing)
