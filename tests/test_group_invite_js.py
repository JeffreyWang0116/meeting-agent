"""群組邀請信的 Gmail 草稿（app/static/js/groupinvite.js）。

系統不申請 Gmail 寄信權限：開好收件人、主旨、內文的撰寫視窗，由邀請人自己按寄出。
內文最重要的一句是「用這個信箱登入」——用別的 Google 帳號登入就看不到邀請。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

MODULE = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "groupinvite.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 執行前端模組")


def call(fn: str, *args):
    script = f"""
import * as g from {json.dumps(MODULE.as_uri())};
process.stdout.write(JSON.stringify(g.{fn}(...{json.dumps(list(args), ensure_ascii=False)})));
"""
    proc = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_invite_mail_opens_gmail_compose_with_everything_filled():
    url = call("inviteMailUrl", {"to": "bob@gmail.com", "groupName": "專題小組", "role": "viewer",
                                 "inviter": "amy@gmail.com", "siteUrl": "https://meeting-agent.onrender.com"})
    parsed = urlparse(url)
    assert parsed.netloc == "mail.google.com"
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert qs["to"] == "bob@gmail.com"
    assert "專題小組" in qs["su"]
    body = qs["body"]
    assert "https://meeting-agent.onrender.com" in body
    assert "bob@gmail.com" in body and "登入" in body   # 一定要用被邀請的信箱登入
    assert "只能看" in body
    assert "amy@gmail.com" in body


def test_role_labels():
    assert [call("roleLabel", r) for r in ("owner", "editor", "viewer", "x")] == ["建立者", "可編輯", "只能看", "x"]
