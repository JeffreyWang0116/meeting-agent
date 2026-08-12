import { $, PRIORITY_ZH, esc, icon, jsonOrThrow, loadFail, pageNo, paginate, registerPager, registerRefresher, renderPager, showError } from "./core.js";
import { renderHome } from "./home.js";
import { refreshMeetings } from "./meetings.js";
import { refreshReminders } from "./reminders.js";

let tasksLoaded = false;

/* ==================================================================
   5. 任務庫：清單、搜尋篩選、列內編輯、手動新增
   ================================================================== */
// ---- 資料庫 ----
const STATUS_ZH = { todo: "待辦", doing: "進行中", done: "完成" };
let allTasks = [];

let editingTaskId = null;  // 目前列內編輯中的任務

function taskRowHtml(t) {
  if (t.id === editingTaskId) {
    return `<tr>
        <td><input class="cell-input" id="editTask" value="${esc(t.task)}"></td>
        <td><input class="cell-input" id="editOwner" value="${esc(t.owner || "")}" placeholder="未指派"></td>
        <td><input class="cell-input" id="editDue" type="date" value="${esc(t.due_date || "")}"></td>
        <td><span class="pr-dot ${t.priority}"></span>${PRIORITY_ZH[t.priority] || esc(t.priority)}</td>
        <td>${STATUS_ZH[t.status] || esc(t.status)}</td>
        <td class="mono">${esc(t.meeting_id)}</td>
        <td><div class="row-ops">
          <button class="edit-btn save save-edit" data-id="${esc(t.id)}" title="儲存" aria-label="儲存">✓</button>
          <button class="del-btn cancel-edit" title="取消" aria-label="取消">${icon("x", "i-sm")}</button>
        </div></td>
      </tr>`;
  }
  return `<tr class="${t.status === "done" ? "row-done" : ""}">
        <td>${esc(t.task)}</td>
        <td>${t.owner ? esc(t.owner) : `<span class="unassigned">未指派</span>`}</td>
        <td class="mono">${esc(t.due_date || "—")}</td>
        <td><span class="pr-dot ${t.priority}"></span>${PRIORITY_ZH[t.priority] || esc(t.priority)}</td>
        <td><select class="status-sel st-${t.status}" data-id="${esc(t.id)}">
          ${Object.entries(STATUS_ZH).map(([v, zh]) =>
            `<option value="${v}" ${v === t.status ? "selected" : ""}>${zh}</option>`).join("")}
        </select></td>
        <td class="mono">${esc(t.meeting_id)}</td>
        <td><div class="row-ops">
          <button class="edit-btn start-edit" data-id="${esc(t.id)}" title="編輯名稱、負責人、期限" aria-label="編輯">
            ${icon("square-pen", "i-sm")}
          </button>
          <button class="del-btn" data-id="${esc(t.id)}" title="刪除此任務" aria-label="刪除">${icon("x", "i-sm")}</button>
        </div></td>
      </tr>`;
}

function renderTasks() {
  const q = $("taskSearch").value.trim().toLowerCase();
  const st = $("taskFilter").value;
  const rows = allTasks.filter(t =>
    (!st || t.status === st) &&
    (!q || `${t.task} ${t.owner || ""} ${t.meeting_id}`.toLowerCase().includes(q))
  );
  const p = paginate("tasks", rows);
  $("taskRows").innerHTML = p.items.length
    ? p.items.map(taskRowHtml).join("")
    : `<tr><td colspan="7" class="empty-note">${allTasks.length ? "沒有符合條件的任務" : "尚無任務"}</td></tr>`;
  renderPager("tasks", p);
  renderHome();
}

async function refreshTasks() {
  try {
    allTasks = (await jsonOrThrow(await fetch("/api/tasks"))).tasks;
    tasksLoaded = true;
    renderTasks();
  } catch (e) {
    tasksLoaded = true;  // 骨架不能一直閃：載不到就明講，並留一個重試入口
    $("taskRows").innerHTML = `<tr><td colspan="7">${loadFail("tasks")}</td></tr>`;
    renderHome();
  }
}

$("taskRows").addEventListener("change", async e => {
  const sel = e.target.closest(".status-sel");
  if (!sel) return;
  try {
    const updated = await jsonOrThrow(await fetch(`/api/tasks/${sel.dataset.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: sel.value }),
    }));
    const i = allTasks.findIndex(t => t.id === updated.id);
    if (i >= 0) allTasks[i] = updated;
    renderTasks();
    refreshReminders();  // 完成任務可能解除逾期提醒
  } catch (err) { showError("更新任務狀態失敗：" + err.message); refreshTasks(); }
});

$("taskRows").addEventListener("click", async e => {
  const start = e.target.closest(".start-edit");
  if (start) {
    editingTaskId = start.dataset.id;
    renderTasks();
    $("editTask").focus();
    return;
  }
  if (e.target.closest(".cancel-edit")) {
    editingTaskId = null;
    renderTasks();
    return;
  }
  const save = e.target.closest(".save-edit");
  if (save) {
    const fields = {
      task: $("editTask").value.trim(),
      owner: $("editOwner").value.trim() || null,
      due_date: $("editDue").value || null,
    };
    if (!fields.task) { showError("任務名稱不可為空"); return; }
    try {
      const updated = await jsonOrThrow(await fetch(`/api/tasks/${save.dataset.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(fields),
      }));
      const i = allTasks.findIndex(t => t.id === updated.id);
      if (i >= 0) allTasks[i] = updated;
      editingTaskId = null;
      renderTasks();
      refreshReminders();  // 期限改動可能新增/解除逾期提醒
    } catch (err) { showError("更新任務失敗：" + err.message); }
    return;
  }
  const btn = e.target.closest(".del-btn");
  if (!btn) return;
  if (!confirm("確定要刪除這筆任務？")) return;
  try {
    await jsonOrThrow(await fetch(`/api/tasks/${btn.dataset.id}`, { method: "DELETE" }));
    allTasks = allTasks.filter(t => t.id !== btn.dataset.id);
    renderTasks();
    refreshReminders();
  } catch (err) { showError("刪除任務失敗：" + err.message); }
});

// 編輯列快捷鍵：Enter 儲存、Esc 取消
$("taskRows").addEventListener("keydown", e => {
  if (!e.target.closest(".cell-input")) return;
  if (e.key === "Enter") $("taskRows").querySelector(".save-edit")?.click();
  if (e.key === "Escape") { editingTaskId = null; renderTasks(); }
});

// 換了搜尋條件就回到第一頁，否則會停在一個超出新結果筆數的頁碼上
const backToFirstTaskPage = () => { pageNo.tasks = 1; renderTasks(); };
$("taskSearch").addEventListener("input", backToFirstTaskPage);
$("taskFilter").addEventListener("change", backToFirstTaskPage);
$("btnRefreshTasks").addEventListener("click", () => { refreshTasks(); refreshMeetings(); });

// 手動新增任務（會議之外臨時想到的待辦）
$("btnAddTask").addEventListener("click", () => {
  const row = $("taskAddRow");
  const show = row.style.display === "none";
  row.style.display = show ? "flex" : "none";
  if (show) $("newTaskName").focus();
});
$("btnAddTaskCancel").addEventListener("click", () => { $("taskAddRow").style.display = "none"; });
async function submitNewTask() {
  const name = $("newTaskName").value.trim();
  if (!name) { showError("任務名稱不可為空"); return; }
  try {
    await jsonOrThrow(await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task: name,
        owner: $("newTaskOwner").value.trim() || null,
        due_date: $("newTaskDue").value || null,
        priority: $("newTaskPriority").value,
      }),
    }));
    $("newTaskName").value = ""; $("newTaskOwner").value = ""; $("newTaskDue").value = "";
    $("newTaskPriority").value = "medium";
    $("taskAddRow").style.display = "none";
    refreshTasks(); refreshReminders();
  } catch (err) { showError("新增任務失敗：" + err.message); }
}
$("btnAddTaskSave").addEventListener("click", submitNewTask);
$("newTaskName").addEventListener("keydown", e => { if (e.key === "Enter") submitNewTask(); });
$("newTaskOwner").addEventListener("keydown", e => { if (e.key === "Enter") submitNewTask(); });

registerRefresher("tasks", refreshTasks);
registerPager("tasks", renderTasks);  // 讓 core 的翻頁按鈕知道要重繪誰

export { STATUS_ZH, allTasks, backToFirstTaskPage, editingTaskId, refreshTasks, renderTasks, submitNewTask, taskRowHtml, tasksLoaded };
