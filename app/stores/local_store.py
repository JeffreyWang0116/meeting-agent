"""本地 JSON 檔實作的 TaskStore。"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.atomicio import atomic_write_text
from app.models import MeetingAnalysis
from app.stores.base import DEFAULT_USER, TaskStore, owns, scoped_read, scoped_write

# 手動任務可填的欄位（與 AI 產出的任務同型別，缺的補預設）
_MANUAL_TASK_FIELDS = ("task", "owner", "due_date", "priority", "source_quote")


def _new_task_record(task: dict) -> dict:
    """把使用者手動輸入的任務正規化成與 AI 任務一致的完整紀錄。"""
    record = {
        "id": uuid.uuid4().hex[:12],
        "meeting_id": task.get("meeting_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": task.get("status") or "todo",
        "priority": task.get("priority") or "medium",
    }
    for field_name in _MANUAL_TASK_FIELDS:
        record.setdefault(field_name, task.get(field_name))
    return record


class LocalJsonStore(TaskStore):
    backend = "local"

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            data = json.loads(self._path.read_text(encoding="utf-8"))
        else:
            data = {"meetings": [], "tasks": []}
        for task in data["tasks"]:  # 舊版資料沒有 status 欄位，補預設值
            task.setdefault("status", "todo")
        return data

    def _flush(self) -> None:
        atomic_write_text(
            self._path, json.dumps(self._data, ensure_ascii=False, indent=2)
        )

    def save_meeting(
        self,
        analysis: MeetingAnalysis,
        transcript: str | None = None,
        kind: str | None = None,
        terms: list[dict] | None = None,
        user: str = DEFAULT_USER,
        # 會前準備（見 app/agenda.py）：貼的議程、這場屬於哪個系列
        agenda: list[str] | None = None,
        series: str | None = None,
    ) -> str:
        meeting_id = uuid.uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        dumped = analysis.model_dump(mode="json")

        meeting_record = {
            "id": meeting_id,
            "created_at": created_at,
            "user": user,
            "meeting": dumped["meeting"],
            "decisions": dumped["decisions"],
            "pending_items": dumped["pending_items"],
            "highlights": dumped.get("highlights", []),
            "sections": dumped.get("sections", []),
            "transcript": transcript,
            "kind": kind,
            # 本次專用詞彙：存起來「重新分析」才不會把使用者會前打的詞弄丟
            "terms": terms or [],
            "tags": dumped.get("tags", []),
            "agenda": agenda or [],
            "agenda_review": dumped.get("agenda_review", []),
            "off_agenda": dumped.get("off_agenda", []),
            "series": series,
        }
        task_records = [
            {
                "id": uuid.uuid4().hex[:12],
                "meeting_id": meeting_id,
                "created_at": created_at,
                "user": user,
                "status": "todo",
                **todo,
            }
            for todo in dumped["todos"]
        ]

        with self._lock:
            self._data["meetings"].append(meeting_record)
            self._data["tasks"].extend(task_records)
            self._flush()
        return meeting_id

    def get_meeting(self, meeting_id: str, *, user: str = DEFAULT_USER) -> dict | None:
        with self._lock:
            return next(
                (m for m in self._data["meetings"]
                 if m["id"] == meeting_id and owns(m, user) and not m.get("deleted_at")),
                None,
            )

    def list_meetings(
        self, *, user: str = DEFAULT_USER, include_transcript: bool = False
    ) -> list[dict]:
        with self._lock:
            # 逐字稿可能數十 KB，列表回應剔除全文保持輕量（get_meeting 才回傳）
            return [
                {k: v for k, v in m.items() if include_transcript or k != "transcript"}
                for m in reversed(self._data["meetings"])
                if owns(m, user) and not m.get("deleted_at")
            ]

    def update_meeting(self, meeting_id: str, fields: dict, *, user: str = DEFAULT_USER) -> dict | None:
        with self._lock:
            for m in self._data["meetings"]:
                if m["id"] == meeting_id and owns(m, user) and not m.get("deleted_at"):
                    f = dict(fields)
                    nested = f.pop("meeting", None)
                    if nested:
                        m.setdefault("meeting", {}).update(nested)
                    m.update(f)
                    self._flush()
                    return dict(m)
        return None

    # ---- 垃圾桶：刪除先進回收區，30 天內救得回來（見 app/main.py 的 /api/trash）----

    def _mark_deleted(self, meeting_id: str, user: str, at: str | None) -> bool:
        """在會議與它的任務上蓋（或清掉）deleted_at。回傳有沒有動到東西。"""
        with self._lock:
            target = next(
                (m for m in self._data["meetings"]
                 if m["id"] == meeting_id and owns(m, user) and bool(m.get("deleted_at")) != bool(at)),
                None,
            )
            if target is None:
                return False
            for record in [target, *(t for t in self._data["tasks"] if t["meeting_id"] == meeting_id)]:
                if at:
                    record["deleted_at"] = at
                else:
                    record.pop("deleted_at", None)
            self._flush()
            return True

    def trash_meeting(self, meeting_id: str, at: str, *, user: str = DEFAULT_USER) -> bool:
        return self._mark_deleted(meeting_id, user, at)

    def restore_meeting(self, meeting_id: str, *, user: str = DEFAULT_USER) -> bool:
        return self._mark_deleted(meeting_id, user, None)

    def list_trash(self, user: str = DEFAULT_USER) -> list[dict]:
        with self._lock:
            return [
                {k: v for k, v in m.items() if k != "transcript"}
                for m in reversed(self._data["meetings"])
                if owns(m, user) and m.get("deleted_at")
            ]

    def delete_meeting(self, meeting_id: str, *, user: str = DEFAULT_USER) -> bool:
        with self._lock:
            before = len(self._data["meetings"])
            self._data["meetings"] = [
                m for m in self._data["meetings"]
                if not (m["id"] == meeting_id and owns(m, user))
            ]
            if len(self._data["meetings"]) == before:
                return False
            self._data["tasks"] = [t for t in self._data["tasks"] if t["meeting_id"] != meeting_id]
            self._flush()
            return True

    def list_tasks(self, meeting_id: str | None = None, *, user: str = DEFAULT_USER) -> list[dict]:
        with self._lock:
            # 垃圾桶裡的會議的任務也要跟著消失，不然提醒還是會叫你去做已刪會議裡的事
            tasks = [t for t in self._data["tasks"] if owns(t, user) and not t.get("deleted_at")]
            if meeting_id is not None:
                tasks = [t for t in tasks if t["meeting_id"] == meeting_id]
            return list(tasks)

    def add_task(self, task: dict, *, user: str = DEFAULT_USER) -> dict:
        record = _new_task_record(task)
        record["user"] = user
        with self._lock:
            self._data["tasks"].append(record)
            self._flush()
        return dict(record)

    def update_task(self, task_id: str, *, user: str = DEFAULT_USER, **fields) -> dict | None:
        with self._lock:
            for task in self._data["tasks"]:
                if task["id"] == task_id and owns(task, user):
                    task.update(fields)
                    self._flush()
                    return dict(task)
        return None

    def replace_tasks(self, meeting_id: str, todos: list[dict], *, user: str = DEFAULT_USER) -> list[dict]:
        created_at = datetime.now(timezone.utc).isoformat()
        records = [
            {
                "id": uuid.uuid4().hex[:12],
                "meeting_id": meeting_id,
                "created_at": created_at,
                "user": user,
                "status": "todo",
                **todo,
            }
            for todo in todos
        ]
        with self._lock:
            self._data["tasks"] = [
                t for t in self._data["tasks"]
                if not (t["meeting_id"] == meeting_id and owns(t, user))
            ] + records
            self._flush()
        return [dict(r) for r in records]

    def delete_task(self, task_id: str, *, user: str = DEFAULT_USER) -> bool:
        with self._lock:
            before = len(self._data["tasks"])
            self._data["tasks"] = [
                t for t in self._data["tasks"]
                if not (t["id"] == task_id and owns(t, user))
            ]
            if len(self._data["tasks"]) == before:
                return False
            self._flush()
            return True

    # ---- 備份 / 還原 ----

    # ---- 跨會議問答的向量索引（同目錄的 rag_index.json） ----

    def _rag_path(self):
        return self._path.with_name("rag_index.json")

    def get_rag_records(self) -> dict:
        doc = self._read_doc(self._rag_path())
        return {"dim": doc.get("dim"), "records": doc.get("records", [])}

    def save_rag_records(
        self, dim: int | None, records: list[dict], meeting_ids: set[str] | None = None
    ) -> None:
        # 本地是單一檔案，整份寫跟只寫幾場的成本一樣，meeting_ids 用不到
        atomic_write_text(
            self._rag_path(),
            json.dumps({"dim": dim, "records": records}, ensure_ascii=False),
        )

    def export_all(self, *, user: str = DEFAULT_USER) -> dict:
        with self._lock:
            # 垃圾桶裡的不進備份：備份是「我的資料」，不是「我刪掉的東西」
            return {
                "meetings": [dict(m) for m in self._data["meetings"]
                             if owns(m, user) and not m.get("deleted_at")],
                "tasks": [dict(t) for t in self._data["tasks"]
                          if owns(t, user) and not t.get("deleted_at")],
                "glossary": self.get_glossary(user=user),
                }

    def import_all(self, data: dict, *, user: str = DEFAULT_USER) -> None:
        # 還原只能覆蓋自己的資料；別人的原封不動地留下來
        meetings = [{**dict(m), "user": user} for m in data.get("meetings", [])]
        tasks = [{**dict(t), "user": user} for t in data.get("tasks", [])]
        for t in tasks:
            t.setdefault("status", "todo")
        with self._lock:
            self._data = {
                "meetings": [m for m in self._data["meetings"] if not owns(m, user)] + meetings,
                "tasks": [t for t in self._data["tasks"] if not owns(t, user)] + tasks,
            }
            self._flush()
        if "glossary" in data:
            self.save_glossary(data.get("glossary") or [], user=user)

    # ---- 自訂詞彙（沿用同目錄的 glossary.json，與 db.json 並存） ----

    # ---- 群組（同目錄的 groups.json）。不放進 db.json：還原備份會整份重建 db.json 的內容 ----

    def _groups_path(self):
        return self._path.with_name("groups.json")

    def _groups(self) -> dict:
        return self._read_doc(self._groups_path()).get("groups", {})

    def _write_groups(self, groups: dict) -> None:
        atomic_write_text(
            self._groups_path(), json.dumps({"groups": groups}, ensure_ascii=False, indent=2)
        )

    def save_group(self, group: dict) -> None:
        with self._lock:
            groups = self._groups()
            groups[group["id"]] = dict(group)
            self._write_groups(groups)

    def get_group(self, group_id: str) -> dict | None:
        group = self._groups().get(group_id)
        return dict(group) if group else None

    def delete_group(self, group_id: str) -> None:
        with self._lock:
            groups = self._groups()
            if groups.pop(group_id, None) is not None:
                self._write_groups(groups)

    def groups_for(self, uid: str, email: str | None) -> list[dict]:
        return [
            dict(g) for g in self._groups().values()
            if uid in g.get("member_uids", []) or (email and email in g.get("invite_emails", []))
        ]

    # ---- 自訂會議範本（同目錄的 templates.json）。理由同群組：不跟著備份還原 ----

    def _templates_path(self):
        return self._path.with_name("templates.json")

    def _templates(self) -> dict:
        return self._read_doc(self._templates_path()).get("templates", {})

    def _write_templates(self, items: dict) -> None:
        atomic_write_text(
            self._templates_path(), json.dumps({"templates": items}, ensure_ascii=False, indent=2)
        )

    def save_template(self, template: dict) -> None:
        with self._lock:
            items = self._templates()
            items[template["id"]] = dict(template)
            self._write_templates(items)

    def get_template(self, template_id: str, user: str) -> dict | None:
        item = self._templates().get(template_id)
        return dict(item) if item and item.get("user") == user else None

    def list_templates(self, user: str) -> list[dict]:
        return [dict(t) for t in self._templates().values() if t.get("user") == user]

    def delete_template(self, template_id: str, user: str) -> None:
        with self._lock:
            items = self._templates()
            if items.get(template_id, {}).get("user") == user and items.pop(template_id, None):
                self._write_templates(items)

    # ---- AI 錯誤回報（同目錄的 feedback.json）。理由同群組：不跟著備份還原 ----

    def _feedback_path(self):
        return self._path.with_name("feedback.json")

    def _feedback(self) -> dict:
        return self._read_doc(self._feedback_path()).get("feedback", {})

    def _write_feedback(self, items: dict) -> None:
        atomic_write_text(
            self._feedback_path(), json.dumps({"feedback": items}, ensure_ascii=False, indent=2)
        )

    def save_feedback(self, item: dict) -> None:
        with self._lock:
            items = self._feedback()
            items[item["id"]] = dict(item)
            self._write_feedback(items)

    def list_feedback(self, user: str) -> list[dict]:
        return [dict(f) for f in self._feedback().values() if f.get("user") == user]

    def delete_feedback(self, feedback_id: str, user: str) -> None:
        with self._lock:
            items = self._feedback()
            if items.get(feedback_id, {}).get("user") == user and items.pop(feedback_id, None):
                self._write_feedback(items)

    # ---- 推播訂閱（同目錄的 push.json）。理由同群組：不跟著備份還原 ----

    def _push_path(self):
        return self._path.with_name("push.json")

    def _push(self) -> dict:
        return self._read_doc(self._push_path()).get("subscriptions", {})

    def _write_push(self, items: dict) -> None:
        atomic_write_text(
            self._push_path(), json.dumps({"subscriptions": items}, ensure_ascii=False, indent=2)
        )

    def save_push_subscription(self, sub: dict) -> None:
        with self._lock:
            items = self._push()
            items[sub["id"]] = dict(sub)
            self._write_push(items)

    def list_push_subscriptions(self, user: str) -> list[dict]:
        return [dict(s) for s in self._push().values() if s.get("user") == user]

    def all_push_subscriptions(self) -> list[dict]:
        return [dict(s) for s in self._push().values()]

    def delete_push_subscription(self, sub_id: str) -> None:
        with self._lock:
            items = self._push()
            if items.pop(sub_id, None):
                self._write_push(items)

    # ---- 轉錄工作紀錄（同目錄的 jobs.json）。理由同群組：不跟著備份還原 ----

    def _jobs_path(self):
        return self._path.with_name("jobs.json")

    def _jobs(self) -> dict:
        return self._read_doc(self._jobs_path()).get("jobs", {})

    def _write_jobs(self, jobs: dict) -> None:
        atomic_write_text(self._jobs_path(), json.dumps({"jobs": jobs}, ensure_ascii=False))

    def save_job(self, job: dict) -> None:
        with self._lock:
            jobs = self._jobs()
            jobs[job["id"]] = dict(job)
            self._write_jobs(jobs)

    def get_job(self, job_id: str) -> dict | None:
        job = self._jobs().get(job_id)
        return dict(job) if job else None

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            jobs = self._jobs()
            if jobs.pop(job_id, None) is not None:
                self._write_jobs(jobs)

    def list_jobs(self, user: str) -> list[dict]:
        return [dict(j) for j in self._jobs().values() if j.get("user") == user]

    def _glossary_path(self):
        return self._path.with_name("glossary.json")

    def _read_doc(self, path) -> dict:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    # ---- 留言（同目錄的 comments.json）。理由同群組：不跟著備份還原 ----

    def _comments_path(self):
        return self._path.with_name("comments.json")

    def _comments(self) -> dict:
        return self._read_doc(self._comments_path()).get("comments", {})

    def _write_comments(self, items: dict) -> None:
        atomic_write_text(
            self._comments_path(), json.dumps({"comments": items}, ensure_ascii=False, indent=2)
        )

    def save_comment(self, comment: dict) -> None:
        with self._lock:
            items = self._comments()
            items[comment["id"]] = dict(comment)
            self._write_comments(items)

    def list_comments(self, user: str, meeting_id: str | None = None) -> list[dict]:
        items = [dict(c) for c in self._comments().values() if c.get("user") == user]
        if meeting_id is not None:
            items = [c for c in items if c.get("meeting_id") == meeting_id]
        return sorted(items, key=lambda c: c.get("at", ""))

    def delete_comment(self, comment_id: str, user: str) -> None:
        with self._lock:
            items = self._comments()
            if items.get(comment_id, {}).get("user") == user and items.pop(comment_id, None):
                self._write_comments(items)

    # ---- 群組活動紀錄（同目錄的 activity.json）。理由同群組：不跟著備份還原 ----

    def _activity_path(self):
        return self._path.with_name("activity.json")

    def get_activity(self, user: str) -> list[dict]:
        return scoped_read(self._read_doc(self._activity_path()), user, "entries")

    def save_activity(self, user: str, entries: list[dict]) -> None:
        with self._lock:
            path = self._activity_path()
            atomic_write_text(
                path,
                json.dumps(scoped_write(self._read_doc(path), user, "entries", entries),
                           ensure_ascii=False, indent=2),
            )

    def get_glossary(self, *, user: str = DEFAULT_USER) -> list[dict]:
        return scoped_read(self._read_doc(self._glossary_path()), user, "terms")

    def save_glossary(self, terms: list[dict], *, user: str = DEFAULT_USER) -> None:
        path = self._glossary_path()
        atomic_write_text(
            path,
            json.dumps(scoped_write(self._read_doc(path), user, "terms", terms),
                       ensure_ascii=False, indent=2),
        )
