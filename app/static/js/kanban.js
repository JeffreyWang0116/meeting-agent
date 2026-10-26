/* ==================================================================
   任務看板：待辦 / 進行中 / 完成三欄，卡片可拖曳
   ------------------------------------------------------------------
   清單檢視回答「有哪些事」，看板回答「卡在哪裡」。兩種都留著，因為手機上
   拖曳很難用——卡片上照樣有狀態下拉，拖曳只是桌面的加速方式，不是唯一的路。
   ================================================================== */
import { $, PRIORITY_ZH, esc, showError } from "./core.js";

const COLUMNS = [
  { status: "todo", label: "待辦" },
  { status: "doing", label: "進行中" },
  { status: "done", label: "完成" },
];

let mode = "list";   // list | board
let source = () => [];
let onMove = async () => {};

function cardHtml(t) {
  const sub = t.subtasks || [];
  const doneCount = sub.filter(s => s.done).length;
  return `<div class="kb-card p-${esc(t.priority)}" draggable="true" data-id="${esc(t.id)}">
      <div class="kb-task">${esc(t.task)}</div>
      <div class="kb-meta">
        <span class="${t.owner ? "" : "unassigned"}">${esc(t.owner || "未指派")}</span>
        ${t.due_date ? `<span class="mono">${esc(t.due_date)}</span>` : ""}
        <span class="pr-dot ${esc(t.priority)}" title="優先級 ${esc(PRIORITY_ZH[t.priority] || "")}"></span>
        ${sub.length ? `<span class="sub-chip">${doneCount}/${sub.length}</span>` : ""}
      </div>
    </div>`;
}

function renderBoard() {
  if (mode !== "board") return;
  const tasks = source();
  $("kanban").innerHTML = COLUMNS.map(col => {
    const rows = tasks.filter(t => (t.status || "todo") === col.status);
    return `<div class="kb-col" data-status="${col.status}">
        <div class="kb-head">${col.label}<span class="kb-count">${rows.length}</span></div>
        <div class="kb-body">${rows.map(cardHtml).join("")
          || `<p class="kb-empty">這一欄是空的</p>`}</div>
      </div>`;
  }).join("");
}

function setMode(next) {
  mode = next === "board" ? "board" : "list";
  $("kanban").hidden = mode !== "board";
  $("taskGroups").hidden = mode === "board";
  // 看板自己分欄，分頁器在這個檢視沒有意義
  const pager = $("tasksPager");
  if (pager) pager.hidden = mode === "board";
  for (const btn of document.querySelectorAll("[data-taskview]")) {
    btn.classList.toggle("active", btn.dataset.taskview === mode);
  }
  renderBoard();
}

document.addEventListener("click", e => {
  const btn = e.target.closest("[data-taskview]");
  if (btn) setMode(btn.dataset.taskview);
});

// ---- 拖曳 ----

let dragging = null;

$("kanban").addEventListener("dragstart", e => {
  const card = e.target.closest(".kb-card");
  if (!card) return;
  dragging = card.dataset.id;
  card.classList.add("kb-dragging");
  // 不設 data 的話 Firefox 根本不會開始拖曳
  e.dataTransfer.setData("text/plain", dragging);
  e.dataTransfer.effectAllowed = "move";
});

$("kanban").addEventListener("dragend", e => {
  e.target.closest(".kb-card")?.classList.remove("kb-dragging");
  for (const col of document.querySelectorAll(".kb-col")) col.classList.remove("kb-over");
  dragging = null;
});

$("kanban").addEventListener("dragover", e => {
  const col = e.target.closest(".kb-col");
  if (!col || !dragging) return;
  e.preventDefault();  // 不擋掉預設行為就不算是可放置的目標
  e.dataTransfer.dropEffect = "move";
  col.classList.add("kb-over");
});

$("kanban").addEventListener("dragleave", e => {
  const col = e.target.closest(".kb-col");
  if (col && !col.contains(e.relatedTarget)) col.classList.remove("kb-over");
});

$("kanban").addEventListener("drop", async e => {
  const col = e.target.closest(".kb-col");
  const id = dragging || e.dataTransfer.getData("text/plain");
  dragging = null;
  if (!col || !id) return;
  e.preventDefault();
  col.classList.remove("kb-over");
  const status = col.dataset.status;
  const task = source().find(t => t.id === id);
  if (!task || task.status === status) return;
  try {
    await onMove(id, status);
  } catch (err) {
    showError(`移動任務失敗：${err.message}`);
  }
});

function bindBoard({ tasks, move }) {
  source = tasks;
  onMove = move;
}

export { bindBoard, cardHtml, renderBoard, setMode };
