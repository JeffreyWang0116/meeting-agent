import { api } from "./api.js";
import { $, PRIORITY_ZH, esc, icon, loadFail, pageNo, paginate, registerPager, registerRefresher, renderPager, showError } from "./core.js";
import { renderHome } from "./home.js";
import { allMeetings, meetingLabel, refreshMeetings } from "./meetings.js";
import { refreshReminders } from "./reminders.js";

let tasksLoaded = false;

/* ==================================================================
   5. 任務庫：依會議分組（可摺疊）、搜尋篩選、列內編輯、手動新增
   ------------------------------------------------------------------
   同一場會議產生的任務收在一格裡，點會議標題旁的三角形展開看全部。分組
   之後每格內的「會議」欄就多餘了（標題已在群組表頭），所以列少一欄。
   ================================================================== */
// ---- 資料庫 ----
const STATUS_ZH = { todo: "待辦", doing: "進行中", done: "完成" };
const MANUAL_KEY = "__manual__";  // meeting_id 為 null 的手動任務歸這一組
let allTasks = [];

let editingTaskId = null;         // 目前列內編輯中的任務
// 使用者手動展開的群組。預設全部收合，只有點開的留著——狀態改動會整個重繪，
// 用原生 <details open> 撐不過重繪，得記在資料層。搜尋時另外強制全開（見 renderTasks）
const openTaskGroups = new Set();

function taskRowHtml(t) {
  if (t.id === editingTaskId) {
    return `<tr>
        <td><input class="cell-input" id="editTask" value="${esc(t.task)}"></td>
        <td><input class="cell-input" id="editOwner" value="${esc(t.owner || "")}" placeholder="未指派"></td>
        <td><input class="cell-input" id="editDue" type="date" value="${esc(t.due_date || "")}"></td>
        <td><span class="pr-dot ${t.priority}"></span>${PRIORITY_ZH[t.priority] || esc(t.priority)}</td>
        <td>${STATUS_ZH[t.status] || esc(t.status)}</td>
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
        <td><div class="row-ops">
          <button class="edit-btn start-edit" data-id="${esc(t.id)}" title="編輯名稱、負責人、期限" aria-label="編輯">
            ${icon("square-pen", "i-sm")}
          </button>
          <button class="del-btn" data-id="${esc(t.id)}" title="刪除此任務" aria-label="刪除">${icon("x", "i-sm")}</button>
        </div></td>
      </tr>`;
}

// 依會議把任務分組並排序：會議照 allMeetings 的新到舊，接著放 allMeetings 裡
// 找不到的會議（剛好還沒載入），手動任務永遠墊底。
function groupTasks(rows) {
  const byKey = new Map();
  for (const t of rows) {
    const key = t.meeting_id || MANUAL_KEY;
    (byKey.get(key) || byKey.set(key, []).get(key)).push(t);
  }
  const order = [];
  for (const m of allMeetings) if (byKey.has(m.id)) order.push(m.id);
  for (const key of byKey.keys())
    if (key !== MANUAL_KEY && !order.includes(key)) order.push(key);
  if (byKey.has(MANUAL_KEY)) order.push(MANUAL_KEY);
  return order.map(key => ({ key, tasks: byKey.get(key) }));
}

function groupHeadHtml(g, open) {
  const label = g.key === MANUAL_KEY ? null : meetingLabel(g.key);
  const title = g.key === MANUAL_KEY
    ? "手動新增"
    : (label ? esc(label.title) : "（找不到的會議）");
  const date = label && label.date ? `<span class="mtg-date mono">${esc(label.date)}</span>` : "";
  const done = g.tasks.filter(t => t.status === "done").length;
  const total = g.tasks.length;
  const allDone = done === total;
  return `<details class="mtg-group" data-key="${esc(g.key)}" ${open ? "open" : ""}>
      <summary class="mtg-summary">
        ${icon("chevron-right", "i-sm mtg-chevron")}
        <span class="mtg-title">${title}</span>
        ${date}
        <span class="mtg-count ${allDone ? "all-done" : ""}">${allDone ? "全部完成" : `${total - done}/${total}`}</span>
      </summary>
      <div class="table-wrap">
        <table class="mtg-table"><tbody>${g.tasks.map(taskRowHtml).join("")}</tbody></table>
      </div>
    </details>`;
}

function renderTasks() {
  const q = $("taskSearch").value.trim().toLowerCase();
  const st = $("taskFilter").value;
  const rows = allTasks.filter(t =>
    (!st || t.status === st) &&
    (!q || `${t.task} ${t.owner || ""} ${t.meeting_id}`.toLowerCase().includes(q))
  );
  const groups = groupTasks(rows);
  const searching = !!(q || st);  // 搜尋/篩選時全部展開，讓命中直接看得到
  const soloGroup = groups.length === 1;

  const p = paginate("tasks", groups);
  $("taskGroups").innerHTML = p.items.length
    ? p.items.map(g => groupHeadHtml(g, searching || soloGroup || openTaskGroups.has(g.key))).join("")
    : `<p class="empty-note tasks-empty">${allTasks.length ? "沒有符合條件的任務" : "尚無任務"}</p>`;
  renderPager("tasks", p);
  renderHome();
}

async function refreshTasks() {
  try {
    allTasks = (await api.listTasks()).tasks;
    tasksLoaded = true;
    renderTasks();
  } catch (e) {
    tasksLoaded = true;  // 骨架不能一直閃：載不到就明講，並留一個重試入口
    $("taskGroups").innerHTML = loadFail("tasks");
    renderHome();
  }
}

const groups = $("taskGroups");

// 記住哪些群組被展開／收合，重繪後才能還原（原生 <details> 的狀態撐不過 innerHTML 重設）。
// toggle 不冒泡，父層要用捕捉階段才收得到
groups.addEventListener("toggle", e => {
  const d = e.target.closest(".mtg-group");
  if (!d) return;
  if (d.open) openTaskGroups.add(d.dataset.key);
  else openTaskGroups.delete(d.dataset.key);
}, true);

groups.addEventListener("change", async e => {
  const sel = e.target.closest(".status-sel");
  if (!sel) return;
  try {
    const updated = await api.updateTask(sel.dataset.id, { status: sel.value });
    const i = allTasks.findIndex(t => t.id === updated.id);
    if (i >= 0) allTasks[i] = updated;
    renderTasks();
    refreshReminders();  // 完成任務可能解除逾期提醒
  } catch (err) { showError("更新任務狀態失敗：" + err.message); refreshTasks(); }
});

groups.addEventListener("click", async e => {
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
      const updated = await api.updateTask(save.dataset.id, fields);
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
    await api.deleteTask(btn.dataset.id);
    allTasks = allTasks.filter(t => t.id !== btn.dataset.id);
    renderTasks();
    refreshReminders();
  } catch (err) { showError("刪除任務失敗：" + err.message); }
});

// 編輯列快捷鍵：Enter 儲存、Esc 取消
groups.addEventListener("keydown", e => {
  if (!e.target.closest(".cell-input")) return;
  if (e.key === "Enter") groups.querySelector(".save-edit")?.click();
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
    await api.createTask({
        task: name,
        owner: $("newTaskOwner").value.trim() || null,
        due_date: $("newTaskDue").value || null,
        priority: $("newTaskPriority").value,
      });
    $("newTaskName").value = ""; $("newTaskOwner").value = ""; $("newTaskDue").value = "";
    $("newTaskPriority").value = "medium";
    $("taskAddRow").style.display = "none";
    openTaskGroups.add(MANUAL_KEY);  // 新增後直接把「手動新增」那格展開，看得到剛加的
    refreshTasks(); refreshReminders();
  } catch (err) { showError("新增任務失敗：" + err.message); }
}
$("btnAddTaskSave").addEventListener("click", submitNewTask);
$("newTaskName").addEventListener("keydown", e => { if (e.key === "Enter") submitNewTask(); });
$("newTaskOwner").addEventListener("keydown", e => { if (e.key === "Enter") submitNewTask(); });

registerRefresher("tasks", refreshTasks);
registerPager("tasks", renderTasks);  // 讓 core 的翻頁按鈕知道要重繪誰

export { STATUS_ZH, allTasks, backToFirstTaskPage, editingTaskId, refreshTasks, renderTasks, submitNewTask, taskRowHtml, tasksLoaded };
