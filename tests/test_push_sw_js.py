"""service worker 收到推播之後做什麼（app/static/sw.js）。

這段程式在手機上、App 關著的時候執行，出錯了既看不到也沒人回報——
所以用假的 self 把它整個跑一遍，確認真的會叫 showNotification。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SW = Path(__file__).resolve().parent.parent / "app" / "static" / "sw.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 執行前端模組")

HARNESS = """
import fs from "node:fs";
const code = fs.readFileSync(%s, "utf8");
const listeners = {};
const shown = [];
const opened = [];
const focused = [];
const self = {
  addEventListener: (type, handler) => { listeners[type] = handler; },
  skipWaiting: () => {},
  location: { origin: "https://meeting.example" },
  clients: {
    claim: () => {},
    matchAll: async () => WINDOWS,
    openWindow: async url => { opened.push(url); },
  },
  registration: {
    showNotification: (title, options) => { shown.push({ title, options }); },
  },
};
const WINDOWS = %s;
new Function("self", code)(self);
%s
process.stdout.write(JSON.stringify({ shown, opened, focused, types: Object.keys(listeners) }));
"""


def run(body: str, windows: str = "[]") -> dict:
    script = HARNESS % (json.dumps(str(SW)), windows, body)
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def push(payload: str) -> dict:
    return run(f"""
await listeners.push({{ data: {payload}, waitUntil: p => p }});
""")


def test_it_listens_for_pushes_and_clicks():
    assert {"push", "notificationclick"} <= set(run("")["types"])


def test_a_push_shows_the_notification_it_was_given():
    out = push('{ json: () => ({ title: "會議助手待辦提醒", body: "3 項逾期", url: "/" }) }')

    assert out["shown"][0]["title"] == "會議助手待辦提醒"
    assert out["shown"][0]["options"]["body"] == "3 項逾期"


def test_broken_payload_still_shows_something():
    """靜靜吞掉只會讓使用者覺得通知時好時壞——而那比沒有通知更難查。"""
    out = push('{ json: () => { throw new Error("not json"); } }')

    assert len(out["shown"]) == 1
    assert out["shown"][0]["title"]


def test_a_push_with_no_payload_at_all_still_shows_something():
    out = push("null")

    assert len(out["shown"]) == 1


def test_notifications_replace_each_other_instead_of_piling_up():
    out = push('{ json: () => ({ title: "x", body: "y" }) }')

    assert out["shown"][0]["options"]["tag"]


def test_clicking_focuses_a_window_that_is_already_open():
    """每點一次就開一個新分頁的話，桌面很快會躺著十個一樣的頁面。"""
    out = run("""
let focusedOne = false;
WINDOWS.push({ url: "https://meeting.example/", focus: () => { focusedOne = true; } });
await listeners.notificationclick({
  notification: { close: () => {}, data: { url: "/" } },
  waitUntil: p => p,
});
focused.push(focusedOne);
""")

    assert out["focused"] == [True]
    assert out["opened"] == []


def test_clicking_opens_the_app_when_nothing_is_open():
    out = run("""
await listeners.notificationclick({
  notification: { close: () => {}, data: { url: "/" } },
  waitUntil: p => p,
});
""")

    assert out["opened"] == ["/"]


def test_a_window_from_another_site_is_not_mistaken_for_ours():
    out = run("""
WINDOWS.push({ url: "https://somewhere.else/", focus: () => {} });
await listeners.notificationclick({
  notification: { close: () => {}, data: { url: "/" } },
  waitUntil: p => p,
});
""")

    assert out["opened"] == ["/"]
