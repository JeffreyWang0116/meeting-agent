"""瀏覽器通知（app/static/js/notify.js 與 sw.js 的點擊處理）。

Android Chrome 不允許 new Notification()（直接丟 TypeError），只能經由
service worker 的 registration.showNotification 發——原本的寫法在手機上
每次都默默失敗，開了「每日提醒通知」也收不到任何東西。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
MODULE = STATIC / "js" / "notify.js"
SW = STATIC / "sw.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 執行前端模組")


def run(body: str) -> dict:
    script = f"""
import {{ showAppNotification }} from {json.dumps(MODULE.as_uri())};
const log = [];
function env({{ permission = "granted", registration = true, ctorThrows = false }} = {{}}) {{
  function Notification(title, options) {{
    if (ctorThrows) throw new TypeError("Illegal constructor");
    log.push(["ctor", title, options.body]);
  }}
  Notification.permission = permission;
  const reg = {{ showNotification: async (title, options) => log.push(["sw", title, options.body]) }};
  return {{
    Notification,
    navigator: {{ serviceWorker: {{ getRegistration: async () => (registration ? reg : undefined) }} }},
  }};
}}
{body}
process.stdout.write(JSON.stringify({{ result, log }}));
"""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_uses_the_service_worker_when_registered():
    out = run('const result = await showAppNotification("標題", { body: "內容" }, env({ ctorThrows: true }));')
    assert out["result"] is True
    assert out["log"] == [["sw", "標題", "內容"]]


def test_falls_back_to_the_constructor_without_a_service_worker():
    out = run('const result = await showAppNotification("標題", { body: "內容" }, env({ registration: false }));')
    assert out["result"] is True
    assert out["log"] == [["ctor", "標題", "內容"]]


def test_nothing_is_shown_without_permission():
    out = run('const result = await showAppNotification("標題", { body: "內容" }, env({ permission: "default" }));')
    assert out["result"] is False
    assert out["log"] == []


def test_reports_failure_instead_of_failing_silently():
    out = run(
        'const result = await showAppNotification("標題", { body: "內容" },'
        ' env({ registration: false, ctorThrows: true }));'
    )
    assert out["result"] is False


def test_unsupported_browser_reports_failure():
    out = run('const result = await showAppNotification("標題", { body: "內容" }, {});')
    assert out["result"] is False


def sw_click(open_windows: list[str]) -> dict:
    """在假的 service worker 環境跑 sw.js，模擬使用者點通知。"""
    script = f"""
const fs = await import("node:fs");
const vm = await import("node:vm");
const handlers = {{}};
const log = [];
const clientsList = {json.dumps(open_windows)}.map(url => ({{
  url, focus: async () => log.push(["focus", url]),
}}));
const self = {{
  addEventListener: (type, fn) => (handlers[type] = fn),
  skipWaiting: () => {{}},
  location: {{ origin: "https://app.example" }},
  clients: {{
    claim: async () => {{}},
    matchAll: async () => clientsList,
    openWindow: async url => log.push(["open", url]),
  }},
}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(SW))}, "utf8"), {{ self, URL }});
let pending;
handlers.notificationclick({{
  notification: {{ close: () => log.push(["close"]), data: {{ url: "/#reminder" }} }},
  waitUntil: p => (pending = p),
}});
await pending;
process.stdout.write(JSON.stringify(log));
"""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_clicking_a_notification_focuses_the_open_app():
    log = sw_click(["https://app.example/#home"])
    assert ["close"] in log
    assert ["focus", "https://app.example/#home"] in log
    assert not any(entry[0] == "open" for entry in log)


def test_clicking_a_notification_opens_the_app_when_closed():
    log = sw_click([])
    assert ["open", "/#reminder"] in log
