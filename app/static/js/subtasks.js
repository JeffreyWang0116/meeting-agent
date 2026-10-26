/* ==================================================================
   任務的子項目清單與負責人建議
   ------------------------------------------------------------------
   「整理預算表」這種一句話其實常常是五件事。子項目讓它有地方拆，而不是
   逼使用者把一筆任務切成五筆、失去它們本來是一件事的關係。

   負責人建議與「指派給群組成員」是同一份名單（/api/owners）：那本來就是
   同一個問題——我要把這件事交給誰。
   ================================================================== */
import { api } from "./api.js";
import { $, esc, icon, showError } from "./core.js";

const RECURRENCE_ZH = { daily: "每天", weekly: "每週", biweekly: "每兩週", monthly: "每月" };

// 哪幾筆任務的子項目正攤開著。狀態改動會整個重繪，記在 DOM 撐不過去
const openSubtasks = new Set();
let ownerNames = [];

function subtasksOf(task) {
  return task.subtasks || [];
}

function toggleSubtasks(id) {
  if (openSubtasks.has(id)) openSubtasks.delete(id);
  else openSubtasks.add(id);
}

function subtaskRowHtml(task) {
  if (!openSubtasks.has(task.id)) return "";
  const items = subtasksOf(task);
  return `<tr class="sub-row"><td colspan="6">
      <div class="sub-list" data-id="${esc(task.id)}">
        ${items.map((s, i) => `<label class="sub-item">
            <input type="checkbox" class="sub-check" data-i="${i}" ${s.done ? "checked" : ""}>
            <span class="${s.done ? "sub-done" : ""}">${esc(s.text)}</span>
            <button class="del-btn sub-del" data-i="${i}" aria-label="刪除這一項">${icon("x", "i-sm")}</button>
          </label>`).join("")}
        <div class="sub-add">
          <input type="text" class="sub-new" placeholder="加一項…（Enter 新增）">
        </div>
      </div>
    </td></tr>`;
}

// 子項目一改就整份送回去：它們是任務文件裡的一個陣列，沒有單獨的端點可以
// 改一項——而且整份送回去也不會有「改到第 3 項卻存成第 4 項」的機會
async function saveSubtasks(id, items, after) {
  try {
    const updated = await api.updateTask(id, { subtasks: items });
    after(updated);
  } catch (e) {
    showError(`子項目儲存失敗：${e.message}`);
  }
}

function bindSubtasks(root, { taskById, applyUpdate }) {
  root.addEventListener("change", e => {
    const box = e.target.closest(".sub-check");
    if (!box) return;
    const id = box.closest(".sub-list").dataset.id;
    const items = subtasksOf(taskById(id)).map((s, i) =>
      i === Number(box.dataset.i) ? { ...s, done: box.checked } : s);
    saveSubtasks(id, items, applyUpdate);
  });

  root.addEventListener("click", e => {
    const del = e.target.closest(".sub-del");
    if (!del) return;
    e.preventDefault();  // 它在 <label> 裡，不攔的話會順便切換勾選狀態
    const id = del.closest(".sub-list").dataset.id;
    const items = subtasksOf(taskById(id)).filter((_, i) => i !== Number(del.dataset.i));
    saveSubtasks(id, items, applyUpdate);
  });

  root.addEventListener("keydown", e => {
    const input = e.target.closest(".sub-new");
    if (!input || e.key !== "Enter") return;
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    const id = input.closest(".sub-list").dataset.id;
    input.value = "";
    saveSubtasks(id, [...subtasksOf(taskById(id)), { text, done: false }], applyUpdate);
  });
}

// ---- 負責人建議 ----

function ownerOptions() {
  return ownerNames;
}

async function refreshOwners() {
  try {
    const body = await api.owners();
    ownerNames = body.owners || [];
  } catch (e) {
    ownerNames = [];  // 只是建議清單，拿不到就讓使用者自己打
  }
  const list = $("ownerList");
  if (list) list.innerHTML = ownerNames.map(n => `<option value="${esc(n)}">`).join("");
}

window.addEventListener("workspacechange", refreshOwners);

export { RECURRENCE_ZH, bindSubtasks, openSubtasks, ownerOptions, refreshOwners, subtaskRowHtml, subtasksOf, toggleSubtasks };
