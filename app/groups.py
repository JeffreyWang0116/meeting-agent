"""群組工作區：成員、角色、用信箱邀請。

會議與任務照舊用 `user` 欄位標擁有者，群組的資料蓋 `group:<id>`（scope_of）。
中介層驗過成員身分後把請求的資料範圍切成那個群組，既有端點不必知道群組存在。
這裡只管群組文件本身：誰在裡面、什麼角色、邀請了誰。

角色：owner（建立者，唯一能管人、改名、解散）／editor（可編輯）／viewer（只能看）。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

INVITABLE_ROLES = ("editor", "viewer")
MAX_NAME_LEN = 40
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class GroupError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def scope_of(group_id: str) -> str:
    return f"group:{group_id}"


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def role_of(group: dict | None, uid: str) -> str | None:
    if not group:
        return None
    return next((m["role"] for m in group.get("members", []) if m["uid"] == uid), None)


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise GroupError(400, "群組名稱不可為空")
    if len(name) > MAX_NAME_LEN:
        raise GroupError(400, f"群組名稱最多 {MAX_NAME_LEN} 個字")
    return name


def _save(store, group: dict) -> dict:
    # 兩個陣列欄位是給 Firestore 單欄位 array_contains 查詢用的，每次存檔都從清單重算
    group["member_uids"] = [m["uid"] for m in group["members"]]
    group["invite_emails"] = [i["email"] for i in group["invites"]]
    store.save_group(group)
    return group


def _load(store, group_id: str) -> dict:
    group = store.get_group(group_id)
    if group is None:
        raise GroupError(404, "找不到這個群組")
    return group


def _load_as_owner(store, group_id: str, uid: str) -> dict:
    group = _load(store, group_id)
    if group["owner"] != uid:
        raise GroupError(403, "只有群組建立者能做這件事")
    return group


def create(store, name: str, uid: str, email: str | None) -> dict:
    return _save(store, {
        "id": uuid.uuid4().hex[:12],
        "name": _clean_name(name),
        "owner": uid,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "members": [{"uid": uid, "email": email, "role": "owner"}],
        "invites": [],
    })


def rename(store, group_id: str, uid: str, name: str) -> dict:
    group = _load_as_owner(store, group_id, uid)
    group["name"] = _clean_name(name)
    return _save(store, group)


def invite(store, group_id: str, uid: str, inviter_email: str | None, email: str, role: str) -> dict:
    group = _load_as_owner(store, group_id, uid)
    email = normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise GroupError(400, "請輸入有效的 Google 信箱")
    if role not in INVITABLE_ROLES:
        raise GroupError(400, "角色只能是可編輯（editor）或只能看（viewer）")
    if any(normalize_email(m.get("email")) == email for m in group["members"]):
        raise GroupError(400, "這個信箱已經是成員了")
    # 重複邀請同一個信箱＝更新角色，不另外多一筆
    group["invites"] = [i for i in group["invites"] if i["email"] != email] + [{
        "email": email, "role": role, "invited_by": inviter_email,
        "invited_at": datetime.now(timezone.utc).isoformat(),
    }]
    return _save(store, group)


def revoke(store, group_id: str, uid: str, email: str) -> dict:
    group = _load_as_owner(store, group_id, uid)
    email = normalize_email(email)
    group["invites"] = [i for i in group["invites"] if i["email"] != email]
    return _save(store, group)


def _my_invite(group: dict, email: str | None) -> dict:
    # 信箱由驗簽給（只有 Google 已驗證的才有），請求本身帶不進來，所以冒用不了
    invite_ = next((i for i in group["invites"] if email and i["email"] == email), None)
    if invite_ is None:
        raise GroupError(403, "找不到寄給你這個信箱的邀請（要用被邀請的那個 Google 帳號登入）")
    return invite_


def accept(store, group_id: str, uid: str, email: str | None) -> dict:
    group = _load(store, group_id)
    invite_ = _my_invite(group, email)
    group["invites"] = [i for i in group["invites"] if i is not invite_]
    if role_of(group, uid) is None:
        group["members"].append({"uid": uid, "email": email, "role": invite_["role"]})
    return _save(store, group)


def decline(store, group_id: str, email: str | None) -> dict:
    group = _load(store, group_id)
    invite_ = _my_invite(group, email)
    group["invites"] = [i for i in group["invites"] if i is not invite_]
    return _save(store, group)


def set_role(store, group_id: str, uid: str, target_uid: str, role: str) -> dict:
    group = _load_as_owner(store, group_id, uid)
    if role not in INVITABLE_ROLES:
        raise GroupError(400, "角色只能是可編輯（editor）或只能看（viewer）")
    if target_uid == group["owner"]:
        raise GroupError(400, "建立者的角色不能更改")
    member = next((m for m in group["members"] if m["uid"] == target_uid), None)
    if member is None:
        raise GroupError(404, "這個人不是群組成員")
    member["role"] = role
    return _save(store, group)


def remove_member(store, group_id: str, uid: str, target_uid: str) -> dict:
    """建立者移除成員，或成員自己退出。建立者不能退出——要結束群組請解散。"""
    group = _load(store, group_id)
    if target_uid == group["owner"]:
        raise GroupError(400, "建立者不能退出群組；要結束群組請用「解散群組」")
    if uid != group["owner"] and uid != target_uid:
        raise GroupError(403, "只有群組建立者能移除其他成員")
    if role_of(group, target_uid) is None:
        raise GroupError(404, "這個人不是群組成員")
    group["members"] = [m for m in group["members"] if m["uid"] != target_uid]
    return _save(store, group)


def purge_scope(store, scope: str, on_scope_deleted=None) -> int:
    """刪掉一個資料範圍（群組、訪客）的會議、任務、詞彙表、講者名冊，回傳刪掉的會議數。"""
    meetings = store.list_meetings(user=scope)
    for meeting in meetings:
        store.delete_meeting(meeting["id"], user=scope)
    for task in store.list_tasks(user=scope):  # 手動新增、不屬於任何會議的任務
        store.delete_task(task["id"], user=scope)
    store.save_glossary([], user=scope)
    store.save_speaker_roster([], user=scope)
    if on_scope_deleted:
        on_scope_deleted(scope)
    return len(meetings)


def disband(store, group_id: str, uid: str, on_scope_deleted=None) -> None:
    """解散：群組裡的資料全部刪掉，再刪群組文件。

    資料先刪、文件後刪：中途失敗時群組還在，建立者可以再按一次；反過來的話會留下
    一批沒有群組能進得去、也沒人刪得掉的資料。
    """
    _load_as_owner(store, group_id, uid)
    purge_scope(store, scope_of(group_id), on_scope_deleted)
    store.delete_group(group_id)


def summary_for(store, uid: str, email: str | None) -> dict:
    """側欄要的東西：我在的群組（含成員與待接受邀請）、寄給我的邀請。"""
    groups, invites = [], []
    for g in sorted(store.groups_for(uid, email), key=lambda g: g.get("created_at", "")):
        role = role_of(g, uid)
        if role:
            groups.append({
                "id": g["id"], "name": g["name"], "role": role,
                "members": [{k: m.get(k) for k in ("uid", "email", "role")} for m in g["members"]],
                "invites": [{k: i.get(k) for k in ("email", "role", "invited_by")} for i in g["invites"]],
            })
            continue
        mine = next((i for i in g["invites"] if email and i["email"] == email), None)
        if mine:
            invites.append({"id": g["id"], "name": g["name"], "role": mine["role"],
                            "invited_by": mine.get("invited_by")})
    return {"me": {"uid": uid, "email": email}, "groups": groups, "invites": invites}
