/* ==================================================================
   垃圾桶與群組活動紀錄（歷史會議頁最底下的兩塊）
   ------------------------------------------------------------------
   刪除會議以前是不可逆的，群組裡一個人誤刪就影響所有人。現在先進垃圾桶，
   30 天內救得回來；活動紀錄則回答「東西怎麼變成這樣的」。
   ================================================================== */
import { api } from "./api.js";
import { $, esc, icon, registerRefresher, showError, showNotice } from "./core.js";
import { refreshMeetings } from "./meetings.js";
import { refreshTasks } from "./tasks.js";

function trashRowHtml(m) {
  const title = esc((m.meeting && m.meeting.title) || "（未命名會議）");
  const date = esc((m.meeting && m.meeting.date) || "");
  const left = m.days_left === 0 ? "今天就會清掉" : `還剩 ${m.days_left} 天`;
  return `<div class="trash-row" data-id="${esc(m.id)}">
      <div class="trash-main">
        <span class="trash-title">${title}</span>
        <span class="trash-meta">${date}${date ? " · " : ""}${esc(left)}</span>
      </div>
      <button class="ghost act-restore">復原</button>
      <button class="ghost danger act-purge" title="永久刪除，不能復原">永久刪除</button>
    </div>`;
}

async function refreshTrash() {
  let body;
  try {
    body = await api.listTrash();
  } catch (e) {
    $("trashRows").innerHTML = `<p class="empty-note">垃圾桶讀取失敗</p>`;
    return;
  }
  const items = body.trash || [];
  $("trashNote").textContent = `刪除的會議會在這裡保留 ${body.days || 30} 天`;
  $("trashCount").textContent = items.length;
  $("trashCount").hidden = items.length === 0;
  $("trashRows").innerHTML = items.length
    ? items.map(trashRowHtml).join("")
    : `<p class="empty-note">垃圾桶是空的</p>`;
}

$("trashRows").addEventListener("click", async e => {
  const row = e.target.closest(".trash-row");
  if (!row) return;
  const id = row.dataset.id;
  try {
    if (e.target.closest(".act-restore")) {
      await api.restoreMeeting(id);
      showNotice("已復原這場會議");
    } else if (e.target.closest(".act-purge")) {
      // 這一步真的沒有了，問一次
      if (!confirm("永久刪除這場會議？逐字稿、任務與討論都會一起消失，無法復原。")) return;
      await api.purgeMeeting(id);
    } else {
      return;
    }
  } catch (err) {
    showError(`操作失敗：${err.message}`);
    return;
  }
  await Promise.all([refreshTrash(), refreshMeetings(), refreshTasks(), refreshActivity()]);
});

// ---- 群組活動紀錄 ----

// 「2 小時前」比「2026-10-26T07:12:33Z」好讀得多，而活動紀錄看的就是「剛剛誰動了什麼」
function ago(iso) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 1) return "剛剛";
  if (mins < 60) return `${mins} 分鐘前`;
  if (mins < 1440) return `${Math.floor(mins / 60)} 小時前`;
  return `${Math.floor(mins / 1440)} 天前`;
}

async function refreshActivity() {
  const panel = $("activityPanel");
  let entries = [];
  try {
    entries = (await api.listActivity()).activity || [];
  } catch (e) {
    panel.hidden = true;
    return;
  }
  // 個人工作區永遠是空的（後端不記），那時整塊藏起來而不是顯示「目前沒有紀錄」
  panel.hidden = entries.length === 0;
  $("activityRows").innerHTML = entries.map(en =>
    `<div class="act-row">${icon("clock", "i-sm")}<span class="act-text">${esc(en.text)}</span>`
    + `<span class="act-time">${esc(ago(en.at))}</span></div>`).join("");
}

registerRefresher("trash", refreshTrash);
registerRefresher("activity", refreshActivity);

// 展開時重抓一次：面板平常收著，開的那一刻才是使用者真的要看它
$("trashPanel").addEventListener("toggle", () => { if ($("trashPanel").open) refreshTrash(); });
$("activityPanel").addEventListener("toggle", () => { if ($("activityPanel").open) refreshActivity(); });

window.addEventListener("workspacechange", () => {
  refreshTrash();
  refreshActivity();
});

refreshTrash();
refreshActivity();

export { ago, refreshActivity, refreshTrash };
