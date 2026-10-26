/* ==================================================================
   跨會議追蹤：未決事項、成員概況、週報月報（首頁最下面三塊）
   ------------------------------------------------------------------
   一場一場翻會議紀錄回答不了「那件事後來怎麼了」「誰手上還有事」
   「這個月決定了什麼」。三份資料都由後端彙整（app/insights.py），
   這裡只負責畫，以及把報告變成一段可以貼進群組的純文字。
   ================================================================== */
import { api } from "./api.js";
import { $, esc, icon, registerRefresher, showError } from "./core.js";
import { copyWithFeedback } from "./result.js";

let reportDays = 7;
let lastReport = null;

// ---- 未決事項 ----

function openItemHtml(item) {
  const where = item.meetings.map(m => esc(m.date)).join("、");
  const answer = item.resolved_by
    ? `<div class="oi-answer">${icon("circle-check-big", "i-sm")}<span>`
      + `${esc(item.resolved_by.date)}「${esc(item.resolved_by.title || "某場會議")}」決議：`
      + `${esc(item.resolved_by.description)}</span></div>`
    : "";
  return `<div class="oi-row ${item.resolved ? "oi-done" : "oi-open"}">
      <div class="oi-main">
        <span class="oi-topic">${esc(item.topic)}</span>
        <span class="oi-meta">提過 ${item.times} 次・${where}</span>
        ${item.reason && !item.resolved ? `<span class="oi-reason">${esc(item.reason)}</span>` : ""}
      </div>
      <span class="oi-tag">${item.resolved ? "可能已解決" : "仍未解決"}</span>
      ${answer}
    </div>`;
}

async function refreshOpenItems() {
  let items = [];
  try {
    items = (await api.openItems()).items || [];
  } catch (e) {
    $("openItemsRows").innerHTML = `<p class="empty-note">讀取失敗</p>`;
    return;
  }
  const unresolved = items.filter(i => !i.resolved).length;
  $("openItemsCount").textContent = unresolved;
  $("openItemsCount").hidden = unresolved === 0;
  $("openItemsRows").innerHTML = items.length
    ? items.map(openItemHtml).join("")
    : `<p class="empty-note">目前沒有待確認事項</p>`;
}

// ---- 成員概況 ----

async function refreshPeople() {
  let people = [];
  try {
    people = (await api.people()).people || [];
  } catch (e) {
    $("peopleRows").innerHTML = `<p class="empty-note">讀取失敗</p>`;
    return;
  }
  $("peopleRows").innerHTML = people.length
    ? people.map(p => `<div class="person-row">
        <span class="person-name">${esc(p.name)}</span>
        <span class="person-stat">出席 <b>${p.meetings}</b></span>
        <span class="person-stat">手上 <b>${p.open}</b></span>
        ${p.overdue ? `<span class="person-stat overdue">逾期 <b>${p.overdue}</b></span>` : ""}
        <span class="person-rate" title="已完成 ${p.done} / ${p.tasks}">
          <span class="rate-bar"><span style="width:${p.rate}%"></span></span>${p.rate}%
        </span>
      </div>`).join("")
    : `<p class="empty-note">還沒有可以統計的人</p>`;
}

// ---- 週報月報 ----

// 報告最常見的用途是貼進群組，所以純文字版要能直接讀，不是 JSON 也不是表格
function reportText(r) {
  const lines = [`${r.start} ~ ${r.end} 會議彙整`, ""];
  lines.push(`會議 ${r.meetings} 場・決議 ${r.decisions} 條・新增代辦 ${r.tasks_created} 件`
    + `（完成 ${r.tasks_done} 件）`);
  if (r.decision_list.length) {
    lines.push("", "決議：");
    for (const d of r.decision_list) lines.push(`- ${d.description}（${d.date}）`);
  }
  if (r.open_items.length) {
    lines.push("", "還沒解決：");
    for (const i of r.open_items) lines.push(`- ${i.topic}（提過 ${i.times} 次）`);
  }
  if (r.top_owners.length) {
    lines.push("", "任務最多：" + r.top_owners.map(o => `${o.name} ${o.tasks} 件`).join("、"));
  }
  return lines.join("\n");
}

async function refreshReport() {
  let r;
  try {
    r = await api.report(reportDays);
  } catch (e) {
    $("reportBody").innerHTML = `<p class="empty-note">讀取失敗</p>`;
    return;
  }
  lastReport = r;
  if (!r.meetings) {
    $("reportBody").innerHTML = `<p class="empty-note">這段期間沒有會議</p>`;
    return;
  }
  const list = (title, rows) => rows.length
    ? `<div class="rep-block"><h4>${title}</h4><ul>${rows.join("")}</ul></div>` : "";
  $("reportBody").innerHTML = `
    <div class="rep-nums">
      <span><b>${r.meetings}</b> 場會議</span>
      <span><b>${r.decisions}</b> 條決議</span>
      <span><b>${r.tasks_created}</b> 件新代辦</span>
      <span><b>${r.tasks_done}</b> 件已完成</span>
    </div>
    ${list("決議", r.decision_list.map(d =>
      `<li>${esc(d.description)}<span class="rep-when">${esc(d.date)}</span></li>`))}
    ${list("還沒解決", r.open_items.map(i =>
      `<li>${esc(i.topic)}<span class="rep-when">提過 ${i.times} 次</span></li>`))}
    ${list("任務最多", r.top_owners.map(o =>
      `<li>${esc(o.name)}<span class="rep-when">${o.tasks} 件</span></li>`))}`;
}

$("reportPanel").addEventListener("click", e => {
  const btn = e.target.closest("[data-days]");
  if (!btn) return;
  reportDays = Number(btn.dataset.days);
  for (const b of document.querySelectorAll("#reportPanel [data-days]")) {
    b.classList.toggle("active", b === btn);
  }
  refreshReport();
});

$("btnCopyReport").addEventListener("click", async e => {
  if (!lastReport) return;
  try {
    await copyWithFeedback(e.target, reportText(lastReport));
  } catch (err) {
    showError(`複製失敗：${err.message}`);
  }
});

// 三塊都平常收著，展開那一刻才是使用者真的要看它
for (const [id, fn] of [["openItemsPanel", refreshOpenItems], ["peoplePanel", refreshPeople],
                        ["reportPanel", refreshReport]]) {
  $(id).addEventListener("toggle", () => { if ($(id).open) fn(); });
}
registerRefresher("openItems", refreshOpenItems);
registerRefresher("people", refreshPeople);
registerRefresher("report", refreshReport);

window.addEventListener("workspacechange", () => {
  lastReport = null;
  if ($("openItemsPanel").open) refreshOpenItems();
  if ($("peoplePanel").open) refreshPeople();
  if ($("reportPanel").open) refreshReport();
});

// 徽章要在使用者展開之前就說「有 3 件還沒解決」，不然沒有人會去點它
refreshOpenItems();

export { refreshOpenItems, refreshPeople, refreshReport, reportText };
