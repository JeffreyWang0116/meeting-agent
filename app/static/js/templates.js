/* ==================================================================
   自訂會議範本：自己開一種會議種類，列出要哪些欄位
   ------------------------------------------------------------------
   內建的八種是通用的，但「專題 meeting」「幹部會議」各有自己想看的東西。
   這裡只負責管理範本；分析時怎麼用由後端決定（app/templates.py）。

   範本存在工作區底下，所以切到群組看到的是群組的範本——與詞彙表一樣。
   ================================================================== */
import { api } from "./api.js";
import { $, esc, showError, showNotice } from "./core.js";
import { loadKinds } from "./setup.js";

const FEATURES = {
  summary: "會議摘要",
  decisions: "決議事項",
  todos: "代辦事項",
  highlights: "會議重點",
};

let templates = [];
let editingId = null;   // null＝新增，否則是正在編輯的範本 id

function renderList() {
  $("tplList").innerHTML = templates.length
    ? templates.map(t => `
      <div class="tpl-item">
        <div class="tpl-body">
          <b>${esc(t.name)}</b>
          <span class="tpl-meta">${esc(t.hint || "（沒有補充說明）")}</span>
          <span class="tpl-meta">欄位：${(t.features || []).map(f => esc(FEATURES[f] || f)).join("、") || "—"}
            ${(t.sections || []).length ? `｜區塊：${t.sections.map(s => esc(s.label)).join("、")}` : ""}</span>
        </div>
        <button class="ghost" data-edit="${esc(t.id)}">編輯</button>
        <button class="ghost danger" data-del="${esc(t.id)}">刪除</button>
      </div>`).join("")
    : `<p class="empty-note">還沒有自訂範本</p>`;
}

function sectionRow(label = "", desc = "") {
  const row = document.createElement("div");
  row.className = "tpl-section-row";
  row.innerHTML =
    `<input type="text" class="s-label" placeholder="區塊名稱" value="${esc(label)}">` +
    `<input type="text" class="s-desc" placeholder="要 AI 抓什麼（選填）" value="${esc(desc)}">` +
    `<button class="ghost danger" data-drop="1" aria-label="刪除這個區塊">×</button>`;
  return row;
}

function openEditor(tpl) {
  editingId = tpl?.id || null;
  $("tplName").value = tpl?.name || "";
  $("tplHint").value = tpl?.hint || "";
  const on = new Set(tpl?.features || Object.keys(FEATURES));
  $("tplFeatures").innerHTML = Object.entries(FEATURES).map(([k, zh]) =>
    `<label class="chk"><input type="checkbox" value="${k}" ${on.has(k) ? "checked" : ""}> ${zh}</label>`
  ).join("");
  $("tplSections").innerHTML = "";
  (tpl?.sections || []).forEach(s => $("tplSections").append(sectionRow(s.label, s.desc)));
  $("tplEdit").hidden = false;
  $("btnTplNew").hidden = true;
  $("tplName").focus();
}

function closeEditor() {
  editingId = null;
  $("tplEdit").hidden = true;
  $("btnTplNew").hidden = false;
}

function collect() {
  return {
    name: $("tplName").value.trim(),
    hint: $("tplHint").value.trim(),
    features: [...$("tplFeatures").querySelectorAll("input:checked")].map(i => i.value),
    sections: [...$("tplSections").querySelectorAll(".tpl-section-row")]
      .map(row => ({
        label: row.querySelector(".s-label").value.trim(),
        desc: row.querySelector(".s-desc").value.trim(),
      }))
      .filter(s => s.label),   // 空白列當作沒填，不送出去讓後端報錯
  };
}

async function refreshTemplates() {
  try {
    templates = (await api.listTemplates()).templates;
  } catch { templates = []; }
  renderList();
}

async function save() {
  const body = collect();
  if (!body.name) { showError("請先填範本名稱"); return; }
  const btn = $("btnTplSave");
  btn.disabled = true;
  try {
    if (editingId) await api.updateTemplate(editingId, body);
    else await api.createTemplate(body);
    closeEditor();
    await refreshTemplates();
    await loadKinds();   // 新會議的種類下拉要立刻看得到
    showNotice(`範本「${body.name}」已儲存，可在新會議的種類選單選用。`);
  } catch (err) { showError("儲存範本失敗：" + err.message); }
  finally { btn.disabled = false; }
}

function wireTemplates() {
  $("btnTemplates").addEventListener("click", async () => {
    $("templateModal").classList.add("open");
    closeEditor();
    await refreshTemplates();
  });
  $("btnTemplateClose").addEventListener("click", () => $("templateModal").classList.remove("open"));
  $("templateModal").addEventListener("click", e => {
    if (e.target === $("templateModal")) $("templateModal").classList.remove("open");
  });

  $("btnTplNew").addEventListener("click", () => openEditor(null));
  $("btnTplCancel").addEventListener("click", closeEditor);
  $("btnTplSave").addEventListener("click", save);
  $("btnTplAddSection").addEventListener("click", () => $("tplSections").append(sectionRow()));
  $("tplSections").addEventListener("click", e => {
    if (e.target.closest("[data-drop]")) e.target.closest(".tpl-section-row").remove();
  });

  $("tplList").addEventListener("click", async e => {
    const edit = e.target.closest("[data-edit]");
    if (edit) { openEditor(templates.find(t => t.id === edit.dataset.edit)); return; }
    const del = e.target.closest("[data-del]");
    if (!del) return;
    const tpl = templates.find(t => t.id === del.dataset.del);
    // 刪範本不會弄壞用它分析過的會議，但下次分析就沒這個選項了——講清楚
    if (!confirm(`刪除範本「${tpl?.name}」？\n已經用它分析過的會議不受影響，但之後不能再選這個種類。`)) return;
    try {
      await api.deleteTemplate(del.dataset.del);
      await refreshTemplates();
      await loadKinds();
    } catch (err) { showError("刪除範本失敗：" + err.message); }
  });

  // 切換工作區＝換一組範本（群組的範本群組成員共用）
  window.addEventListener("workspacechange", refreshTemplates);
  window.addEventListener("accountchange", refreshTemplates);
}

wireTemplates();

export { refreshTemplates };
